"""Disposable account-prefix fixtures, never a real package install."""
import os
import time
from pathlib import Path

import pytest
from test_install_artifact import PACKAGE, archive

from acc import install_artifact as artifact
from acc.broker import account_install as adapter
from acc.broker.account_install import non_root_uid as production_uid_policy
from acc.broker.config import BrokerConfig
from acc.broker.server import Broker


@pytest.fixture
def extractor(tmp_path, monkeypatch, install_test_uid_policy):
    home, run = tmp_path / 'home', tmp_path / 'run'
    home.mkdir()
    run.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: home)
    config = BrokerConfig(run_dir=run)
    lock = adapter.AccountLock(run)
    instance = adapter.Extractor(config, lock, 'a' * 32, home=home)
    inspected, _ = artifact.inspect(archive(), PACKAGE)
    envelope = {'protocol': adapter.PROTOCOL, 'operation_id': 'b' * 32, 'nonce': 'c' * 32,
        'uid': os.geteuid(), 'runtime_id': 'a' * 32, 'host': 'local', 'recipe_digest': 'd' * 64,
        'lease_deadline': int(time.time()) + 160, 'artifact_sha512': inspected['artifact_sha512'],
        'manifest_digest': inspected['manifest_digest'], 'metadata': PACKAGE,
        'provenance': {key: None for key in adapter.PROVENANCE_FIELDS}, 'limits': dict(artifact.LIMITS)}
    envelope['provenance']['qualification'] = adapter.qualification_fingerprint(envelope['recipe_digest'], instance.runtime_id)
    return instance, envelope, home


def qualify(monkeypatch, instance, envelope):
    fingerprint = adapter.qualification_fingerprint(envelope['recipe_digest'], instance.runtime_id)
    monkeypatch.setattr(adapter, 'QUALIFIED_TOOL_EXECUTORS', frozenset({fingerprint}))


def test_default_off_and_manager_name_cannot_qualify(extractor, monkeypatch):
    instance, envelope, _ = extractor
    for qualified in (frozenset(), frozenset({'npm'})):
        monkeypatch.setattr(adapter, 'QUALIFIED_TOOL_EXECUTORS', qualified)
        with pytest.raises(artifact.InvalidArtifact, match='runtime_unqualified'):
            instance.stage(envelope, archive())


@pytest.mark.parametrize('change', [{'uid': 0}, {'uid': 999998}, {'host': 'remote'},
    {'runtime_id': 'e'*32}, {'protocol': 'legacy'}, {'command': 'arbitrary'}])
def test_scope_fails_before_writes(extractor, monkeypatch, change):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    if change.get('uid') == 0:
        monkeypatch.setattr(adapter, 'non_root_uid', production_uid_policy)
    with pytest.raises(artifact.InvalidArtifact):
        instance.stage(envelope | change, archive())
    assert not list(home.iterdir())


def test_staging_no_prefix_writes_and_step_by_step_publish(extractor, monkeypatch):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    staged = instance.stage(envelope, archive())
    assert not (home / '.local').exists()
    # An unrelated hidden lockfile is never copied, rewritten or removed.
    (home / '.local/lib/node_modules').mkdir(parents=True)
    hidden = home / '.local/lib/node_modules/.package-lock.json'
    hidden.write_bytes(b'person-owned metadata')
    for index, entry in enumerate(staged['plan']):
        result = instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
        assert result['path'] == entry['path']
    result = instance.finish(envelope['operation_id'])
    assert result['state'] == 'installed'
    assert hidden.read_bytes() == b'person-owned metadata'
    assert (home / '.local/bin/sample').readlink() == Path('../lib/node_modules/@reviewed/sample/cli.js')
    assert not instance.operations


def test_restart_never_replays(extractor, monkeypatch):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    instance.stage(envelope, archive())
    restarted = adapter.Extractor(instance.config, instance.lock, instance.runtime_id, home=home)
    with pytest.raises(artifact.InvalidArtifact, match='transport_lost'):
        restarted.publish_step(envelope['operation_id'], envelope['manifest_digest'], 0)
    assert not list(home.iterdir())
    instance.abort(envelope['operation_id'])


def test_one_lock_covers_person_and_delegated_stage(extractor, monkeypatch):
    instance, envelope, _ = extractor
    qualify(monkeypatch, instance, envelope)
    broker = Broker(instance.config)
    with broker._account_tools_lock.held():
        with pytest.raises((BlockingIOError, artifact.InvalidArtifact)):
            instance.stage(envelope, archive())
    instance.stage(envelope, archive())
    monkeypatch.setattr('acc.broker.server.tooling.execute', lambda **kw: pytest.fail('person executed while staging'))
    # Exercise lock contention as a modeled unprivileged legacy caller. The
    # independent legacy root refusal is tested below, without this override.
    with monkeypatch.context() as caller:
        caller.setattr('acc.broker.server.os.geteuid', lambda: 1001)
        with pytest.raises((BlockingIOError, artifact.InvalidArtifact)):
            broker.tool_change({'manager': 'npm', 'name': 'sample', 'version': '1.2.3', 'operation': 'install'})
    instance.abort(envelope['operation_id'])


def test_expiry_and_staging_tamper(extractor, monkeypatch):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    staged = instance.stage(envelope, archive())
    scratch = instance.operations[envelope['operation_id']]['scratch']
    (scratch / 'cli.js').write_bytes(b'tampered')
    for index, entry in enumerate(staged['plan']):
        if entry.get('source') == 'cli.js':
            with pytest.raises(artifact.InvalidArtifact, match='footprint_mismatch'):
                instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
            assert not (home / entry['path']).exists()
            break
        instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
    instance.abort(envelope['operation_id'])


def test_symlink_parent_no_follow(extractor, monkeypatch, tmp_path):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    instance.stage(envelope, archive())
    external = tmp_path / 'external'
    external.mkdir()
    (home / '.local').symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], 0)
    assert not list(external.iterdir())
    instance.abort(envelope['operation_id'])


def test_noop_rehash_detects_drift(extractor, monkeypatch):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    inspected, _ = artifact.inspect(archive(), PACKAGE)
    assert instance.footprint(envelope, inspected['manifest'])['state'] == 'absent'
    staged = instance.stage(envelope, archive())
    for index in range(len(staged['plan'])):
        instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
    instance.finish(envelope['operation_id'])
    assert instance.footprint(envelope, inspected['manifest'])['state'] == 'already_installed'
    (home / '.local/lib/node_modules/@reviewed/sample/cli.js').write_bytes(b'changed')
    with pytest.raises(artifact.InvalidArtifact, match='collision'):
        instance.footprint(envelope, inspected['manifest'])


@pytest.mark.parametrize('tamper', [False, True])
def test_compensation_removes_only_matching_journaled_paths(extractor, monkeypatch, tamper):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    staged = instance.stage(envelope, archive())
    observations = []
    for index, entry in enumerate(staged['plan']):
        observations.append(instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index))
        if entry.get('source') == 'cli.js':
            break
    if tamper:
        (home / observations[-1]['path']).write_bytes(b'another writer')
        assert instance.compensate(envelope['operation_id'], len(observations)-1, observations[-1]) == {'state':'changed'}
        assert (home / observations[-1]['path']).read_bytes() == b'another writer'
    else:
        for index in reversed(range(len(observations))):
            assert instance.compensate(envelope['operation_id'], index, observations[index]) == {'state':'removed'}
        assert not list(home.iterdir())
    instance.abort(envelope['operation_id'])


def test_scratch_expiry_does_not_follow_links(tmp_path):
    import shutil
    if not shutil.rmtree.avoids_symlink_attacks:
        pytest.skip('stdlib does not provide descriptor-safe rmtree')
    scratch = tmp_path / 'account-install-abcdefgh'
    scratch.mkdir()
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'keep').write_text('not scratch')
    (scratch / 'link').symlink_to(external, target_is_directory=True)
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(scratch, (old, old))
    adapter.expire_scratch(tmp_path)
    assert not scratch.exists()
    assert (external / 'keep').read_text() == 'not scratch'


def test_already_installed_refuses_new_external_command_collision(extractor, monkeypatch):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    staged = instance.stage(envelope, archive())
    for index in range(len(staged['plan'])):
        instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
    instance.finish(envelope['operation_id'])
    (home / '.cargo/bin').mkdir(parents=True)
    (home / '.cargo/bin/sample').write_bytes(b'another command')
    with pytest.raises(artifact.InvalidArtifact, match='collision'):
        instance.footprint(envelope, staged['manifest'])


def test_shared_writable_prefix_is_refused(extractor, monkeypatch):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    (home / '.local').mkdir(mode=0o777)
    (home / '.local').chmod(0o777)
    inspected, _ = artifact.inspect(archive(), PACKAGE)
    with pytest.raises(artifact.InvalidArtifact, match='collision'):
        instance.footprint(envelope, inspected['manifest'])


def test_failed_temp_create_preserves_another_writer(extractor, monkeypatch):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    staged = instance.stage(envelope, archive())
    for index, entry in enumerate(staged['plan']):
        if entry['type'] == 'file':
            temporary = home / adapter.temporary_path(envelope['operation_id'], index, entry)
            temporary.write_bytes(b'another writer')
            with pytest.raises(FileExistsError):
                instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
            assert temporary.read_bytes() == b'another writer'
            break
        instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
    instance.abort(envelope['operation_id'])


def test_unproved_bin_temp_prevents_absent_reconciliation(extractor, monkeypatch):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    inspected, _ = artifact.inspect(archive(), PACKAGE)
    plan = adapter.publication_plan(inspected)
    index = len(plan)-1
    temporary = home / adapter.temporary_path(envelope['operation_id'], index, plan[index])
    temporary.parent.mkdir(parents=True)
    temporary.write_bytes(b'unknown publication effects')
    with pytest.raises(artifact.InvalidArtifact, match='collision'):
        instance.footprint(envelope, inspected['manifest'])
    assert temporary.exists()


@pytest.mark.parametrize('kind', ['file', 'directory', 'symlink'])
def test_noop_refuses_extra_unmanaged_package_paths(extractor, monkeypatch, kind):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    inspected, _ = artifact.inspect(archive(), PACKAGE)
    staged = instance.stage(envelope, archive())
    for index in range(len(staged['plan'])):
        instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
    instance.finish(envelope['operation_id'])
    extra = home / '.local/lib/node_modules/@reviewed/sample/extra'
    if kind == 'file':
        extra.write_bytes(b'unmanaged')
    elif kind == 'directory':
        extra.mkdir()
    else:
        extra.symlink_to('/unmanaged')
    with pytest.raises(artifact.InvalidArtifact, match='collision'):
        instance.footprint(envelope, inspected['manifest'])


def test_interpreter_digest_cached_across_recipes(monkeypatch):
    import io
    from unittest.mock import Mock
    adapter._interpreter_digest.cache_clear()
    opener = Mock(side_effect=lambda *args: io.BytesIO(b'pinned interpreter'))
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(adapter, 'open', opener, raising=False)
            first = adapter.qualification_fingerprint('a' * 64, 'b' * 32)
            assert adapter.qualification_fingerprint('c' * 64, 'b' * 32) != first
            assert adapter.qualification_fingerprint('a' * 64, 'b' * 32) == first
            assert opener.call_count == 1
    finally:
        adapter._interpreter_digest.cache_clear()


def test_scratch_scans_are_linear_in_manifest_not_per_step(extractor, monkeypatch):
    instance, envelope, _ = extractor
    blob = archive([(f'package/extra-{index}.js', b'x', {}) for index in range(100)])
    inspected, _ = artifact.inspect(blob, PACKAGE)
    envelope = envelope | {key: inspected[key] for key in ('artifact_sha512', 'manifest_digest')}
    qualify(monkeypatch, instance, envelope)
    scans = []
    original = adapter.monitor_scratch
    def monitor(root, ceiling):
        scans.append(root)
        return original(root, ceiling)
    monkeypatch.setattr(adapter, 'monitor_scratch', monitor)
    staged = instance.stage(envelope, blob)
    for index in range(len(staged['plan'])):
        instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
    instance.finish(envelope['operation_id'])
    assert len(scans) == 2


def test_scratch_budget_checks_before_writing_and_after_stage(extractor, monkeypatch):
    instance, envelope, _ = extractor
    qualify(monkeypatch, instance, envelope)
    with pytest.raises(artifact.InvalidArtifact, match='resource_limit'):
        instance.stage(envelope | {'limits': artifact.LIMITS | {'scratch_bytes': 1}}, archive())


def test_finish_rechecks_scratch_ceiling(extractor, monkeypatch):
    instance, envelope, _ = extractor
    qualify(monkeypatch, instance, envelope)
    staged = instance.stage(envelope, archive())
    for index in range(len(staged['plan'])):
        instance.publish_step(envelope['operation_id'], envelope['manifest_digest'], index)
    scratch = instance.operations[envelope['operation_id']]['scratch']
    (scratch / 'unexpected').write_bytes(b'xx')
    instance.operations[envelope['operation_id']]['envelope']['limits']['scratch_bytes'] = 1
    with pytest.raises(artifact.InvalidArtifact, match='resource_limit'):
        instance.finish(envelope['operation_id'])
    instance.abort(envelope['operation_id'])


def test_root_cannot_execute_even_with_qualified_digest(extractor, monkeypatch):
    instance, envelope, _ = extractor
    qualify(monkeypatch, instance, envelope)
    monkeypatch.setattr(adapter, 'non_root_uid', production_uid_policy)
    monkeypatch.setattr(adapter.os, 'geteuid', lambda: 0)
    with pytest.raises(artifact.InvalidArtifact, match='runtime_unqualified'):
        instance.stage(envelope, archive())


def test_identity_rpc_keeps_production_root_fence(extractor, monkeypatch):
    instance, envelope, home = extractor
    qualify(monkeypatch, instance, envelope)
    monkeypatch.setattr(adapter, 'non_root_uid', production_uid_policy)
    monkeypatch.setattr(adapter.os, 'geteuid', lambda: 0)
    protocol = adapter.Protocol(instance.config, instance.lock, instance.runtime_id, home=home)
    assert protocol.call({'phase': 'identity', 'recipe_digest': envelope['recipe_digest']}) == {'reason': 'runtime_unqualified'}


def test_legacy_person_rpc_keeps_production_root_fence(extractor, monkeypatch):
    from acc.broker.server import RequestError
    instance, _, _ = extractor
    broker = Broker(instance.config)
    monkeypatch.setattr('acc.broker.server.os.geteuid', lambda: 0)
    with pytest.raises(RequestError, match='never root'):
        broker.tool_change({'manager': 'npm', 'name': 'sample', 'version': '1.2.3', 'operation': 'install'})


def test_services_broker_starts_without_install_identity(tmp_path, monkeypatch):
    """A services broker has no private run dir; it must start without creating the install identity."""
    from acc.broker import image_probe, server
    from acc.broker.config import BrokerConfig
    called = []
    monkeypatch.setattr(image_probe, 'runtime_id', lambda config: called.append(config) or 'x' * 32)
    monkeypatch.setenv('ACC_BROKER_ROLE', 'services')
    monkeypatch.setenv('ACC_RUN_DIR', str(tmp_path / 'run'))
    server.Broker(BrokerConfig.from_env())
    assert called == []
