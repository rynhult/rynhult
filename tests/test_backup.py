"""deploy/backup/acc-backup: backs up acc's data without following links planted in folders other users control."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/backup/acc-backup"


def load(monkeypatch, tmp_path):
    for key, value in {"ACC_DATA": tmp_path / "data", "ACC_BACKUP_DIR": tmp_path / "backups",
                       "ACC_CATALOG_LOCAL": tmp_path / "catalog-local", "ACC_GIT": tmp_path / "git"}.items():
        monkeypatch.setenv(key, str(value))
    loader = importlib.machinery.SourceFileLoader("acc_backup", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("acc_backup", loader))
    loader.exec_module(module)
    return module


@pytest.fixture
def data(tmp_path):
    secrets = tmp_path / "data/secrets"
    secrets.mkdir(parents=True)
    db = sqlite3.connect(tmp_path / "data/acc.db")
    db.execute("create table t (x)")
    db.commit()
    db.close()
    (secrets / "api_token").write_text("token")
    (tmp_path / "outside.txt").write_text("not acc's")
    (secrets / "planted").symlink_to(tmp_path / "outside.txt")
    (secrets / "nested").mkdir()
    (secrets / "nested/key").write_text("k")
    return tmp_path


def test_secrets_copied_without_following_links(monkeypatch, data):
    backup = load(monkeypatch, data)
    print(backup.backup_acc("2026-10-21"))
    out = data / "backups/2026-10-21"
    assert (out / "secrets/api_token").read_text() == "token"
    assert (out / "secrets/nested/key").read_text() == "k"
    assert not (out / "secrets/planted").exists() and not (out / "secrets/planted").is_symlink()
    assert (out / "secrets/api_token").stat().st_mode & 0o777 == 0o600


def test_symlinked_database_refused(monkeypatch, data):
    (data / "data/acc.db").rename(data / "real.db")
    (data / "data/acc.db").symlink_to(data / "real.db")
    backup = load(monkeypatch, data)
    with pytest.raises(SystemExit, match="symlink"):
        backup.backup_acc("2026-10-21")


@pytest.mark.skipif(os.geteuid() != 0 or shutil.which("runuser") is None, reason="needs root and runuser")
def test_mirror_runs_as_its_user_without_chown(monkeypatch, data):
    source, dest = data / "project-src", data / "home/backups/project"
    monkeypatch.setenv("ACC_BACKUP_MIRRORS", f"root:{source}:{dest}")
    backup = load(monkeypatch, data)
    assert backup.MIRRORS == [("root", str(source), str(dest))]
    source.mkdir()
    (source / "dump.sql").write_text("x")
    calls = []
    real_run = backup.subprocess.run
    def run_as_test_user(args, **kwargs):
        calls.append(args)
        # Exercise the filesystem commands in our temporary tree without root/runuser.
        assert args[:4] == ["runuser", "-u", "root", "--"]
        return real_run(args[4:], **kwargs)

    monkeypatch.setattr(backup.subprocess, "run", run_as_test_user)
    assert "mirrored" in backup.mirror(*backup.MIRRORS[0])
    assert (dest / "dump.sql").read_text() == "x"
    assert all(args[:3] == ["runuser", "-u", "root"] for args in calls)  # every step as the mirror's user
    assert not any("chown" in args for args in calls)
    assert os.stat(dest.parent).st_mode & 0o777 == 0o700


def test_git_backups_copied_and_linked_day_to_day(monkeypatch, data):
    """/srv/acc/git goes with the day's backup; unchanged files (git objects) are hard links to yesterday's copy."""
    backup = load(monkeypatch, data)
    assert "nothing yet" in backup.backup_git("2026-10-20")
    repo = data / "git/3.git"
    (repo / "objects/pack").mkdir(parents=True)
    (repo / "objects/pack/a.pack").write_bytes(b"pack")
    (repo / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "planted").symlink_to(data / "outside.txt")
    (data / "backups").mkdir()
    print(backup.backup_git("2026-10-21"))
    first = data / "backups/2026-10-21/git/3.git"
    assert (first / "objects/pack/a.pack").read_bytes() == b"pack" and not (first / "planted").is_symlink()
    (repo / "HEAD").write_text("ref: refs/heads/other\n")
    print(backup.backup_git("2026-10-22"))
    second = data / "backups/2026-10-22/git/3.git"
    assert os.path.samefile(first / "objects/pack/a.pack", second / "objects/pack/a.pack")
    assert (second / "HEAD").read_text() == "ref: refs/heads/other\n" and (first / "HEAD").read_text().endswith("main\n")


def test_mirrors_never_copy_a_sessions_keys(monkeypatch, data):
    """D21: session.env holds a session's key values; a mirror of a folder holding a run folder leaves it out."""
    source, dest = data / "project-src", data / "home/backups/project"
    monkeypatch.setenv("ACC_BACKUP_MIRRORS", f"root:{source}:{dest}")
    backup = load(monkeypatch, data)
    (source / "run/0123456789ab").mkdir(parents=True)
    (source / "run/0123456789ab/session.env").write_text("export OPENAI_API_KEY=sk-x\n")
    (source / "dump.sql").write_text("x")
    real_run = backup.subprocess.run
    as_is = lambda args, **kw: real_run(args[4:] if args[:2] == ["runuser", "-u"] else args, **kw)  # noqa: E731
    monkeypatch.setattr(backup.subprocess, "run", as_is)  # no runuser here: run each step directly
    assert "mirrored" in backup.mirror(*backup.MIRRORS[0])
    assert (dest / "dump.sql").exists() and (dest / "run/0123456789ab").is_dir()
    assert not (dest / "run/0123456789ab/session.env").exists()


def test_secrets_can_be_encrypted_with_a_passphrase(monkeypatch, data, tmp_path):
    """D24: a day's folder holds acc.db and the key that opens its vault side by side; with a passphrase file the
    secrets are only in the backup encrypted."""
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("correct horse battery staple\n")
    passphrase.chmod(0o644)
    monkeypatch.setenv("ACC_BACKUP_PASSPHRASE_FILE", str(passphrase))
    backup = load(monkeypatch, data)
    with pytest.raises(SystemExit, match="mode 0600"):  # readable by others: refused
        backup.backup_acc("2026-10-21")
    passphrase.chmod(0o600)
    assert "secrets (encrypted)" in backup.backup_acc("2026-10-21")
    out = data / "backups/2026-10-21"
    sealed = out / "secrets.tar.enc"
    assert not (out / "secrets").exists() and sealed.stat().st_mode & 0o777 == 0o600
    assert b"token" not in sealed.read_bytes()
    restored = tmp_path / "restored"
    restored.mkdir()
    plain = subprocess.run([*backup.OPENSSL, "-d", "-pass", f"file:{passphrase}", "-in", str(sealed)],
                           capture_output=True, check=True).stdout  # the restore command in the Operations page
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
        tar.extractall(restored, filter="data")
    assert (restored / "secrets/api_token").read_text() == "token"
    assert (restored / "secrets/nested/key").read_text() == "k"
    assert not (restored / "secrets/planted").exists()  # a link is never followed or kept
    wrong = tmp_path / "wrong"
    wrong.write_text("not it\n")
    rejected = subprocess.run([*backup.OPENSSL, "-d", "-pass", f"file:{wrong}", "-in", str(sealed)],
                              capture_output=True)
    # CBC isn't authenticated: a wrong key can randomly produce valid padding (roughly 1/256).
    # What matters is that it cannot restore the secrets, even if openssl reports success.
    assert rejected.stdout != plain
    with pytest.raises(tarfile.ReadError):
        tarfile.open(fileobj=io.BytesIO(rejected.stdout))
