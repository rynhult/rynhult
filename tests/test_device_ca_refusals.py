"""Device authority: refusal paths the rollover tests don't reach. Temporary authorities only."""
import os
import shutil
import time

import pytest
from test_device_ca_rollover import pem, staged


def tree(state):
    return sorted(str(p.relative_to(state)) for p in state.rglob('*'))


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'folder'])
def test_promote_refuses_an_edge_trust_file_that_is_not_a_plain_file(tmp_path, kind):
    # A linked trust file is refused as the edge helper refuses it, never read as absent or followed.
    ca, state, helper_dir, _, old_ca = staged(tmp_path)
    current = helper_dir / 'device-ca.pem'
    if kind == 'symlink':
        current.rename(tmp_path / 'elsewhere.pem')
        current.symlink_to(tmp_path / 'elsewhere.pem')
    elif kind == 'hardlink':
        os.link(current, tmp_path / 'second-name.pem')
    else:
        current.unlink()
        current.mkdir()
    before = tree(state)
    with pytest.raises(ValueError, match='edge trust file device-ca.pem is unsafe'):
        ca.rollover_promote(state, helper_dir)
    assert tree(state) == before and pem(state) == old_ca


def test_a_key_and_certificate_of_different_authorities_never_sign(tmp_path):
    ca, state, _, _, _ = staged(tmp_path)
    shutil.copy2(state / 'next/ca.pem', state / 'ca.pem')  # a promote seen half-way: new name, old key
    with pytest.raises(ValueError, match='authority is changing'):
        ca.issue(state, {'operation': 'issue', 'device_id': 'b' * 32, 'days': 90})


def test_an_unusable_rollover_record_refuses_each_step_before_any_change(tmp_path):
    ca, state, helper_dir, _, old_ca = staged(tmp_path)
    (state / 'rollover.log').chmod(0o644)
    before = tree(state)
    with pytest.raises(ValueError, match='rollover record must be private'):
        ca.rollover_promote(state, helper_dir)
    assert tree(state) == before and pem(state) == old_ca
    (state / 'rollover.log').chmod(0o600)
    ca.rollover_promote(state, helper_dir)
    (helper_dir / 'device-ca-previous.pem').unlink()
    (state / 'rollover.log').chmod(0o640)
    before = tree(state)
    with pytest.raises(ValueError, match='rollover record must be private'):
        ca.rollover_finish(state, helper_dir)
    assert tree(state) == before
    (state / 'rollover.log').chmod(0o600)
    ca.rollover_finish(state, helper_dir)
    (state / 'rollover.log').chmod(0o604)
    with pytest.raises(ValueError, match='rollover record must be private'):
        ca.rollover_stage(state)
    assert not (state / 'next').exists()  # a retry after fixing the record stages, and records, normally
    (state / 'rollover.log').chmod(0o600)
    time.sleep(1.1)  # authority names carry the second they were made in
    ca.rollover_stage(state)
    assert (state / 'rollover.log').read_text().splitlines()[-1].count('"stage"') == 1


def test_promote_refuses_when_nothing_is_staged_or_the_state_is_mixed(tmp_path):
    ca, state, helper_dir, _, _ = staged(tmp_path)
    (state / 'previous').mkdir(mode=0o700)  # staged and previous both present while the top is still in place
    with pytest.raises(ValueError, match='unexpected authority state'):
        ca.rollover_promote(state, helper_dir)
    os.rmdir(state / 'previous')
    shutil.rmtree(state / 'next')
    with pytest.raises(ValueError, match='nothing staged'):
        ca.rollover_promote(state, helper_dir)
