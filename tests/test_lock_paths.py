"""Every lock accd takes must sit where its systemd unit lets it write (ProtectSystem=strict + ReadWritePaths).

#176 put the git backup lock in /srv/acc, the git folder's parent, which accd can't write: every git backup
then failed on a deployment with "Read-only file system" until this was fixed.
"""
from pathlib import Path

from acc.maintenance import git_lock
from acc.server.config import Settings

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "accd.service"


def writable_paths() -> list[Path]:
    for line in UNIT.read_text().splitlines():
        if line.startswith("ReadWritePaths="):
            return [Path(p.lstrip("-")) for p in line.split("=", 1)[1].split()]
    raise AssertionError("accd.service has no ReadWritePaths")


def under_writable(path: Path) -> bool:
    return any(path == root or root in path.parents for root in writable_paths())


def test_git_backup_lock_is_inside_the_git_folder():
    assert git_lock(Path("/srv/acc/git")) == Path("/srv/acc/git/.git-backup.lock")


def test_every_default_lock_path_is_writable_by_accd():
    settings = Settings()
    locks = [git_lock(settings.git_dir), settings.data_dir / ".maintenance.lock",
             settings.data_dir / ".capture-update.lock"]
    for lock in locks:
        assert under_writable(lock), f"{lock} is outside accd's ReadWritePaths {writable_paths()}"
