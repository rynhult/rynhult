"""Root's custody of the builder's archive never follows a link (kit review BA-L2): fakes only, no root needed."""
import importlib.util
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('take_archive', ROOT / 'deploy/containers/take_archive.py')
take_archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(take_archive)
ME = os.getuid()


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / 'rynhult-build'
    (home / 'build-1').mkdir(parents=True)
    archive = home / 'build-1/acc-session.oci.tar'
    archive.write_bytes(b'archive bytes')
    custody = tmp_path / 'review'
    custody.mkdir(mode=0o700)
    return home, archive, custody


def test_a_regular_archive_of_the_builder_is_copied_privately(layout):
    home, archive, custody = layout
    take_archive.take(archive, custody / 'reviewed.oci.tar', home, ME)
    copy = custody / 'reviewed.oci.tar'
    assert copy.read_bytes() == b'archive bytes' and stat.S_IMODE(copy.stat().st_mode) == 0o600


def test_a_linked_archive_is_refused(layout, tmp_path):
    home, archive, custody = layout
    secret = tmp_path / 'secret'
    secret.write_bytes(b'root-only')
    archive.unlink()
    archive.symlink_to(secret)
    with pytest.raises(take_archive.Refused, match='is a link'):
        take_archive.take(archive, custody / 'reviewed.oci.tar', home, ME)
    assert not (custody / 'reviewed.oci.tar').exists()


def test_a_linked_build_folder_is_refused(layout, tmp_path):
    home, _, custody = layout
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'acc-session.oci.tar').write_bytes(b'x')
    (home / 'build-2').symlink_to(elsewhere)
    with pytest.raises(take_archive.Refused, match='build-2 in the archive path is a link'):
        take_archive.take(home / 'build-2/acc-session.oci.tar', custody / 'reviewed.oci.tar', home, ME)


def test_another_owner_a_fifo_or_a_path_outside_the_home_is_refused(layout, tmp_path):
    home, archive, custody = layout
    with pytest.raises(take_archive.Refused, match='owned by rynhult-build'):
        take_archive.take(archive, custody / 'a.tar', home, ME + 1)
    fifo = home / 'build-1/fifo.tar'
    os.mkfifo(fifo)  # never opened for a blocking read
    with pytest.raises(take_archive.Refused, match='not a regular file'):
        take_archive.take(fifo, custody / 'b.tar', home, ME)
    for path in (tmp_path / 'other.tar', home / 'build-1/../build-1/acc-session.oci.tar'):
        with pytest.raises(take_archive.Refused, match='plain path inside'):
            take_archive.take(path, custody / 'c.tar', home, ME)


def test_the_destination_is_new_and_never_followed(layout, tmp_path):
    home, archive, custody = layout
    target = tmp_path / 'target'
    (custody / 'linked.tar').symlink_to(target)
    with pytest.raises(FileExistsError):
        take_archive.take(archive, custody / 'linked.tar', home, ME)
    assert not target.exists()
    open_dir = tmp_path / 'open'
    open_dir.mkdir()
    open_dir.chmod(0o755)  # not mkdir's mode alone: the umask filters it (another test's 077 made it 0700 under xdist)
    with pytest.raises(take_archive.Refused, match='mode 0700'):
        take_archive.take(archive, open_dir / 'x.tar', home, ME)


def test_the_checked_destination_folder_is_the_one_written_to(layout, tmp_path, monkeypatch):
    """The custody folder is checked and then written through the same descriptor: swapping it for a link between the
    check and the create (whoever can write the folder above it) can't redirect root's new file."""
    home, archive, custody = layout
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir(mode=0o700)
    real_open = take_archive.os.open

    def swapping_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:  # just before the create: the checked folder is replaced by a link
            custody.rename(tmp_path / 'checked')
            custody.symlink_to(elsewhere)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(take_archive.os, 'open', swapping_open)
    take_archive.take(archive, custody / 'reviewed.oci.tar', home, ME)
    assert not (elsewhere / 'reviewed.oci.tar').exists()
    assert (tmp_path / 'checked/reviewed.oci.tar').read_bytes() == b'archive bytes'


def test_a_linked_destination_folder_is_refused(layout, tmp_path):
    home, archive, custody = layout
    (tmp_path / 'link').symlink_to(custody)
    with pytest.raises(take_archive.Refused, match='mode 0700'):
        take_archive.take(archive, tmp_path / 'link/reviewed.oci.tar', home, ME)


def test_short_writes_copy_every_byte(layout, monkeypatch):
    home, archive, custody = layout
    original = take_archive.os.write
    monkeypatch.setattr(take_archive.os, 'write', lambda fd, data: original(fd, data[:3]))
    take_archive.take(archive, custody / 'reviewed.oci.tar', home, ME)
    assert (custody / 'reviewed.oci.tar').read_bytes() == archive.read_bytes()


@pytest.mark.parametrize('caller', [0, 10001], ids=['root-metadata', 'nonroot-metadata'])
def test_custody_checks_the_callers_ownership_on_the_open_directory(layout, monkeypatch, caller):
    from types import SimpleNamespace
    home, archive, custody = layout
    original = take_archive.os.fstat
    directory_owner = caller
    def metadata(fd):
        result = original(fd)
        return SimpleNamespace(st_uid=directory_owner, st_mode=result.st_mode) if stat.S_ISDIR(result.st_mode) else result
    monkeypatch.setattr(take_archive.os, 'geteuid', lambda: caller)
    monkeypatch.setattr(take_archive.os, 'fstat', metadata)
    take_archive.take(archive, custody / 'good.tar', home, ME)
    assert (custody / 'good.tar').read_bytes() == archive.read_bytes()
    directory_owner = caller + 1
    with pytest.raises(take_archive.Refused):
        take_archive.take(archive, custody / 'bad.tar', home, ME)
    assert not (custody / 'bad.tar').exists()


def test_zero_progress_write_refuses_instead_of_hanging(layout, monkeypatch):
    home, archive, custody = layout
    monkeypatch.setattr(take_archive.os, 'write', lambda *_: 0)
    with pytest.raises(OSError, match='no write progress'):
        take_archive.take(archive, custody / 'reviewed.oci.tar', home, ME)
