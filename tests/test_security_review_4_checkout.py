"""A damaged checkout must not silently become a publish-all installed tree."""
# ruff: noqa: F811
import shutil

import pytest
from test_public_snapshot import source  # noqa: F401, F811


@pytest.mark.parametrize('metadata', ['invalid', 'dangling-link'])
def test_damaged_checkout_never_publishes_untracked_files(source, metadata):
    from acc.bundle import build
    repo, _, output = source
    shutil.rmtree(repo / '.git')
    if metadata == 'invalid':
        (repo / '.git').write_text('not a git checkout')
    else:
        (repo / '.git').symlink_to('missing-git-metadata')
    (repo / 'private-scratch.txt').write_text('synthetic unpublished local note')
    with pytest.raises(SystemExit, match='git checkout'):
        build(repo, output.parent / 'bundle', version='test')
    assert not list((output.parent / 'bundle').glob('acc-*.tar.gz'))
