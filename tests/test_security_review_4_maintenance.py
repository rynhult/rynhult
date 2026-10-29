"""Readable flock files must not grant agents global maintenance authority."""
import stat

import pytest
from isolated_settings import isolated_settings


def test_catalog_coordination_file_is_private_even_with_permissive_umask(tmp_path):
    import os

    from acc.server.catalog_store import catalog_lock
    root = tmp_path / 'catalog'
    root.mkdir(mode=0o755)
    previous = os.umask(0o022)
    try:
        with catalog_lock(root):
            mode = (root / '.restore.lock').stat().st_mode
            assert stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600
    finally:
        os.umask(previous)


def test_existing_catalog_coordination_file_loses_other_account_access(tmp_path):
    from acc.server.catalog_store import catalog_lock
    path = tmp_path / '.restore.lock'
    path.touch(mode=0o644)
    with catalog_lock(tmp_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_contended_catalog_lock_times_out_and_refuses_symlinks(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from acc.server.catalog_store import catalog_lock
    with ThreadPoolExecutor() as pool:
        with catalog_lock(tmp_path):
            def contended():
                with catalog_lock(tmp_path, timeout=.05):
                    pytest.fail('must not pass the held coordination barrier')
            with pytest.raises(TimeoutError):
                pool.submit(contended).result(timeout=2)
    lock = tmp_path / '.restore.lock'
    lock.unlink()
    target = tmp_path / 'unrelated'
    target.write_text('preserve')
    target.chmod(0o644)
    lock.symlink_to(target)
    with pytest.raises(OSError):
        with catalog_lock(tmp_path):
            pytest.fail('must not lock or chmod a link target')
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.read_text() == 'preserve'


def test_capture_releases_maintenance_after_a_preexisting_reader_holds_catalog(tmp_path, monkeypatch):
    import fcntl

    from acc import backup_server
    from acc.maintenance import lease
    from acc.server import catalog_store

    settings = isolated_settings(tmp_path, catalog_local_dir=tmp_path / 'catalog')
    settings.data_dir.mkdir()
    settings.catalog_local_dir.mkdir()
    lock = settings.catalog_local_dir / '.restore.lock'
    lock.touch(mode=0o644)
    original = catalog_store.catalog_lock
    monkeypatch.setattr(catalog_store, 'catalog_lock', lambda root: original(root, timeout=.05))
    # Model an agent's already-open readable descriptor from before the permission repair.
    with lock.open('r') as reader:
        fcntl.flock(reader, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            backup_server.Service(settings).capture()
        with lease(settings.data_dir / '.maintenance.lock', timeout=.1):
            pass  # the failed capture cannot retain global exclusion
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert not list((settings.data_dir / 'backup-service/staging').iterdir())
