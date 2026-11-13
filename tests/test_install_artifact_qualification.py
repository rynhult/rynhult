"""Mandatory-oracle and interpreter-selection regressions; no UID switching."""
import json
import sys
from types import SimpleNamespace

import install_artifact_qualification as qualification
import pytest

from acc.broker.account_install import non_root_uid


def config(**values):
    return SimpleNamespace(getoption=lambda key: values.get(key, False if key == 'install_qualification' else None))


@pytest.mark.parametrize('mode', ['optional', 'flag', 'env'])
@pytest.mark.parametrize('missing', ['node', 'module'])
def test_missing_oracle_skips_only_outside_qualification(monkeypatch, mode, missing):
    monkeypatch.delenv('ACC_INSTALL_QUALIFICATION', raising=False)
    if mode == 'env':
        monkeypatch.setenv('ACC_INSTALL_QUALIFICATION', '1')
    monkeypatch.setattr(qualification.shutil, 'which', lambda *args, **kwargs: None if missing == 'node' else '/usr/bin/node')
    if missing == 'module':
        monkeypatch.setattr(qualification.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout='null'))
    exception = pytest.skip.Exception if mode == 'optional' else pytest.fail.Exception
    with pytest.raises(exception, match='absent|refusing to skip'):
        qualification.host_node_tar(config(install_qualification=mode == 'flag'))


def test_qualification_requires_both_interpreters_and_record(monkeypatch):
    for name in ('ACCD_PYTHON', 'BROKER_PYTHON', 'QUALIFICATION_RECORD'):
        monkeypatch.delenv('ACC_INSTALL_' + name, raising=False)
    with pytest.raises(pytest.fail.Exception, match='explicit accd/broker'):
        qualification.runtime_config(config(install_qualification=True, install_accd_python=sys.executable), 'accd')


def test_selected_interpreter_never_falls_back(monkeypatch):
    monkeypatch.delenv('ACC_INSTALL_QUALIFICATION', raising=False)
    with pytest.raises(pytest.fail.Exception, match='no fallback'):
        qualification.runtime_config(config(install_broker_python='/not-an-installed-python'), 'broker')


def test_interpreter_and_record_parameters_are_preserved(tmp_path):
    chosen = config(install_qualification=True, install_accd_python=sys.executable,
        install_broker_python=sys.executable, install_qualification_record=str(tmp_path / 'corpus.json'))
    for role in ('accd', 'broker'):
        executable, record = qualification.runtime_config(chosen, role)
        assert executable == sys.executable
        evidence = {'node_tar': {'version': '7.5.22', 'module': '/pinned/tar/index.js'}, 'role': role}
        output = qualification.write_record(record, role, evidence)
        assert json.loads(open(output).read()) == evidence
        with pytest.raises(FileExistsError):
            qualification.write_record(record, role, evidence)


@pytest.mark.parametrize('uid', [0, -1, True, None])
def test_production_uid_policy_cannot_be_enabled_by_qualification_env(monkeypatch, uid):
    monkeypatch.setenv('ACC_INSTALL_QUALIFICATION', '1')
    assert not non_root_uid(uid)
    assert non_root_uid(1001)


def test_root_ci_fixture_only_replaces_the_policy_predicate(monkeypatch):
    import os

    import conftest
    from acc.broker import account_install
    from acc.server import account_installs
    real_uid = os.geteuid()
    with monkeypatch.context() as root_context:
        root_context.setattr(conftest, 'os', SimpleNamespace(geteuid=lambda: 0))
        conftest.install_test_uid_policy.__wrapped__(monkeypatch)
    assert os.geteuid() == real_uid
    assert account_install.non_root_uid(0)
    assert account_installs.non_root_uid(0)
    assert not non_root_uid(0)  # The imported production predicate is unchanged.


def test_qualification_env_survives_scrub_and_xdist_workers(monkeypatch):
    def fresh():
        option = SimpleNamespace(**{name: False if name == 'install_qualification' else None for name in qualification.OPTIONS})
        return SimpleNamespace(option=option, getoption=lambda key: getattr(option, key))
    controller = fresh()
    env = {'ACC_INSTALL_QUALIFICATION': '1', 'ACC_INSTALL_ACCD_PYTHON': sys.executable,
        'ACC_INSTALL_BROKER_PYTHON': sys.executable, 'ACC_INSTALL_QUALIFICATION_RECORD': '/tmp/qualification.json'}
    qualification.capture_environment(controller, env)
    worker = fresh()
    qualification.capture_environment(worker, {}, {name: controller.getoption(name) for name in qualification.OPTIONS})
    assert qualification.qualification_required(worker)
    assert qualification.runtime_config(worker, 'accd') == (sys.executable, '/tmp/qualification.json')
    monkeypatch.setattr(qualification.shutil, 'which', lambda *args, **kwargs: None)
    with pytest.raises(pytest.fail.Exception, match='refusing to skip'):
        qualification.host_node_tar(worker)
