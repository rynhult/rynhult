"""Root transaction runner for acc-update. All paths and commands are injectable for isolated tests."""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from acc.env import environment

from . import bundle
from .updates import (
    BUILD,
    GIT_MESSAGE,
    check,
    download_release,
    fetch,
    repository,
    selected_channel,
    validate_request,
    version,
)


def read_json(path: Path, limit: int = 65536) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = file.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def write_json(path: Path, data: dict, group: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o640)
            json.dump(data, file)
            file.flush()
            os.fsync(file.fileno())
        if group:
            shutil.chown(name, group=group)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Transaction:
    def __init__(self, root: Path, source: Path, data: Path, target: str, config: dict, run=None):
        self.root, self.source, self.data, self.target, self.config = root, source, data, target, config
        self.run = run or subprocess.run
        self.id = uuid.uuid4().hex
        self.backup_dir = root / "backups" / self.id
        self.previous = source.with_name(f"{source.name}.previous-{self.id}")
        self.stage = source.parent / f".acc-update-{self.id}"
        self.new_source = None
        self.activated = False
        self.install_started = False
        self.state = {
            "id": self.id,
            "version": target,
            "from_version": bundle.version_of(source) if (source / "pyproject.toml").is_file() else "unknown",
            "steps": [],
            "running": True,
            "at": time.time(),
        }

    def save(self):
        write_json(self.root / "state.json", self.state, self.config.get("group", ""))

    def step(self, name, work):
        entry = {"name": name, "running": True}
        self.state["steps"].append(entry)
        self.save()
        try:
            value = work()
            entry.update(running=False, ok=True)
            return value
        except Exception:
            entry.update(running=False, ok=False)
            raise
        finally:
            self.save()

    def command(self, argv, env=None):
        result = self.run(
            [str(a) for a in argv],
            env={**environment, **(env or {})},
            capture_output=True,
            timeout=60 if str(argv[0]) == "systemctl" else 2700,
        )
        if result.returncode:
            # Install output can include machine configuration; expose the fixed step name only.
            raise ValueError(f"{Path(str(argv[0])).name} failed (exit {result.returncode})")

    def backup(self):
        url = environment.get("ACC_DATABASE_URL", "")
        if url and url != f"sqlite:///{self.data / 'acc.db'}":
            raise ValueError("Automatic updates require the default SQLite database; PostgreSQL needs an "
                             "owner-managed backup, update and recovery procedure")
        self.backup_dir.mkdir(parents=True, mode=0o700)
        self.command(
            [self.config.get("backup_command", "/usr/local/sbin/acc-backup")],
            {"ACC_BACKUP_DIR": str(self.backup_dir), "ACC_DATA": str(self.data)},
        )
        saved = self.backup_dir / datetime.date.today().isoformat()
        if not (saved / "acc.db").is_file() or not (
            (saved / "secrets").is_dir() or (saved / "secrets.tar.enc").is_file()
        ):
            raise ValueError("full database and secrets backup missing")
        self.saved = saved

    def download(self):
        self.stage.mkdir(mode=0o700)
        token = Path(self.config["token_file"]) if self.config.get("token_file") else None
        repo = self.config.get("repository", "")
        current = bundle.version_of(self.source)
        latest = check(repo, current, token, bundle.installed_identity(self.source),
                       include_prereleases=selected_channel(self.config, current) == "rc")
        if latest["latest"] != self.target or not latest["available"]:
            raise ValueError("requested version is no longer the newer release")
        tree = download_release(repo, latest, self.stage, token, get=fetch,
                                allow_unsigned=self.config.get('allow_unsigned', False), verification=self.state,
                                include_prereleases=selected_channel(self.config, current) == "rc")
        self.expected_build = bundle.identity(tree)
        return tree

    def activate(self):
        static = Path("src/acc/server/static")
        current = self.source / static
        if current.is_symlink():
            raise ValueError("installed web build is a symlink")
        if current.is_dir():
            # Bundles omit generated assets. Keep serving the old build during installation;
            # the web installer then retains its current chunks for one more deployment.
            shutil.copytree(current, self.new_source / static, symlinks=True)
        self.source.rename(self.previous)
        try:
            self.new_source.rename(self.source)
        except BaseException:
            self.previous.rename(self.source)
            raise
        self.activated = True

    def install(self, source):
        self.command(["bash", source / "deploy/setup.sh", "--yes"])

    def health(self):
        deadline = time.monotonic() + self.config.get("health_timeout", 120)
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection("localhost", timeout=3)
            try:
                connection.sock = socket.socket(socket.AF_UNIX)
                connection.sock.settimeout(3)
                connection.sock.connect(self.config.get("health_socket", "/run/accd.sock"))
                connection.request("GET", "/api/health")
                response = connection.getresponse()
                value = json.loads(response.read(4096))
                if (
                    response.status == 200
                    and value.get("version") == self.target
                    and value.get("build") == self.expected_build
                ):
                    return
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(1)
        raise ValueError("new version and build did not become healthy within the time limit")

    def restore(self):
        if self.install_started:
            self.command(["systemctl", "stop", "accd.service", "accd.socket"])
        if self.activated:
            failed = self.stage / "failed"
            self.source.rename(failed)
            self.previous.rename(self.source)
        if self.install_started:
            # Work through descriptors: acc owns data and can rename its children. Root must not
            # follow a swapped database/WAL path or write an inode owned by another account.
            data_fd = os.open(self.data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open("acc.db", os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=data_fd)
                with os.fdopen(fd, "wb") as output:
                    info = os.fstat(output.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.fstat(data_fd).st_uid:
                        raise ValueError("unsafe database owner or file type during rollback")
                    for suffix in ("-wal", "-shm"):
                        try:
                            os.unlink("acc.db" + suffix, dir_fd=data_fd)
                        except FileNotFoundError:
                            pass
                    output.truncate(0)
                    with (self.saved / "acc.db").open("rb") as before:
                        shutil.copyfileobj(before, output)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.close(data_fd)
            self.install(self.source)

    def after_success(self):
        """Optional cleanup only after installation and health have succeeded."""

    def apply(self):
        from .maintenance import update_hold
        with update_hold(self.data):
            return self._apply()

    def _apply(self):
        try:
            if (self.source / ".git").exists():
                raise ValueError(GIT_MESSAGE)
            version(self.target)
            self.step("backup", self.backup)
            self.new_source = self.step("download", self.download)
            self.step("activate", self.activate)
            self.install_started = True
            self.step("install", lambda: self.install(self.source))
            self.step("health", self.health)
            self.state.update(ok=True, detail="Updated; previous source and full backup retained")
            self.after_success()
        except Exception as error:
            self.state.update(ok=False, detail=str(error)[:300])
            if self.install_started or self.activated:
                try:
                    self.step("rollback", self.restore)
                    self.state["rolled_back"] = True
                except Exception:
                    self.state.update(
                        rolled_back=False, rollback_error="Rollback failed; restore the retained backup and source"
                    )
        finally:
            self.state.update(running=False, finished_at=time.time())
            self.save()
        return self.state


class NodeTransaction(Transaction):
    """Nodes never consult a release server: only their enrolled primary and TLS trust."""

    def backup(self):
        # No dashboard database on a node. The old source remains available for rollback.
        pass

    def download(self):
        import ssl

        from .node.config import NodeConfig

        config = NodeConfig.load(Path(self.config.get("node_config", "/etc/acc-node.env")))
        if config.trust == "ca":
            fd = os.open(config.ca_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise ValueError("node CA must be a regular file")
                pem = file.read(65537)
            if len(pem) > 65536:
                raise ValueError("node CA is too large")
            # Only the first enrolled certificate is trusted, never an appended extra root.
            first = pem.split("-----END CERTIFICATE-----", 1)[0] + "-----END CERTIFICATE-----\n"
            der = ssl.PEM_cert_to_DER_cert(first)
            if not config.ca_fingerprint or hashlib.sha256(der).hexdigest() != config.ca_fingerprint:
                raise ValueError("node CA fingerprint differs from enrollment; refusing the update")
            context = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(der))
        else:
            context = ssl.create_default_context()
        if version(self.target) < version(bundle.version_of(self.source)):
            raise ValueError("refusing a node downgrade")
        self.stage.mkdir(mode=0o700)
        from .node import machine
        # The public edge admits this machine's download; trust is unchanged. The node account owns its state folder,
        # so the checked identity is loaded from root's own private copy, never re-opened there by name.
        machine.present(context, Path(self.config.get("node_state", "/var/lib/acc-node")), self.stage)
        path = self.stage / "release.tar.gz"
        path.write_bytes(fetch(config.primary + "/api/nodes/bundle", limit=256 << 20, context=context))
        tree = bundle.unpack(path, self.stage / "unpacked", self.target, self.config["sha256"])
        self.expected_build = self.config["build"]
        # unpack already authenticated the entire archive against the checksum
        # supplied by the enrolled primary. Publication exclusions can change:
        # never recompute the NEW tree using this OLD helper's bundle rules.
        # Require the actual stamp; installed_identity's fallback would silently
        # accept a missing/malformed stamp using those same obsolete rules.
        try:
            with (tree / ".acc-build").open("rb") as stamp:
                raw_stamp = stamp.read(66)
            stamped_build = raw_stamp.decode("ascii").strip() if len(raw_stamp) <= 65 else ""
        except (OSError, UnicodeError):
            stamped_build = ""
        if bundle.version_of(tree) != self.target or stamped_build != self.expected_build:
            raise ValueError("node bundle version or build mismatch")
        return tree

    def after_success(self):
        try:
            self.step("retention", self.prune_previous)
            self.state["detail"] = "Updated; kept the last two previous installations"
        except OSError:
            # Cleanup failure must not roll back a healthy installation or lose
            # its recovery source. Retry old-directory cleanup on the next update.
            self.state["detail"] = "Updated; previous-install cleanup failed. Check disk space and permissions before the next update."

    def prune_previous(self):
        """Keep two helper-created source generations; never follow links or remove manual backups."""
        pattern = re.compile(re.escape(self.source.name) + r"\.previous-[0-9a-f]{32}")
        # Mark when this source became a previous generation; archive directory
        # mtimes may otherwise be reproducibly zero or inherited from the source.
        os.utime(self.previous, None, follow_symlinks=False)
        candidates = []
        for path in self.source.parent.iterdir():
            if pattern.fullmatch(path.name):
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    candidates.append((path == self.previous, info.st_mtime_ns, path.name, path))
        for _, _, _, path in sorted(candidates, reverse=True)[2:]:
            try:
                shutil.rmtree(path)  # fd-based removal; nested links are not followed
            except FileNotFoundError:
                pass

    def install(self, source):
        # --update: keep the node's configured agent accounts; never create a default one (no SUDO_USER here)
        self.command(["bash", source / "deploy/node/install.sh", "--update"])

    def health(self):
        deadline = time.monotonic() + self.config.get("health_timeout", 120)
        status = Path(self.config.get("node_status", "/var/lib/acc-node/status.json"))
        while time.monotonic() < deadline:
            try:
                value = read_json(status)
                if (
                    value.get("state") == "connected"
                    and value.get("version") == self.target
                    and value.get("build") == self.expected_build
                ):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise ValueError("updated node did not reconnect with expected version and build")

    def restore(self):
        self.command(["systemctl", "stop", "acc-node.service"])
        if self.activated:
            self.source.rename(self.stage / "failed")
            self.previous.rename(self.source)
        self.install(self.source)


def validate_node_request(data):
    if not isinstance(data, dict) or set(data) != {"action", "version", "build", "sha256"}:
        raise ValueError("invalid node update request")
    validate_request({k: data[k] for k in ("action", "version")}, include_prereleases=True)
    if not all(isinstance(data[k], str) and BUILD.fullmatch(data[k]) for k in ("build", "sha256")):
        raise ValueError("invalid node build or checksum")
    return data["version"]


def main():
    root = Path(environment.get("ACC_UPDATE_ROOT", "/srv/acc/updates"))
    source = Path(environment.get("ACC_UPDATE_SOURCE", "/srv/acc/app"))
    data = Path(environment.get("ACC_DATA", "/srv/acc/data"))
    group = environment.get("ACC_UPDATE_GROUP", "acc")
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        node_mode = environment.get("ACC_UPDATE_MODE") == "node"
        from .backup_targets import BINDING
        if node_mode and (BINDING.exists() or BINDING.is_symlink()):
            write_json(root / 'state.json', {'ok': False, 'running': False,
                       'detail': 'Backup target updates require independent owner review'}, group)
            return 0
        authorized_repo = environment.get(
            "ACC_UPDATE_REPOSITORY", repository(environment.get("ACC_SOURCE_URL", "https://github.com/rynhult/rynhult"))
        )
        config = {
            "repository": authorized_repo,
            "token_file": str(data / "secrets/update_token"),
            "group": group,
            "include_prereleases": version(bundle.version_of(source))[3] == 0 if (source / "pyproject.toml").is_file() else False,
        }
        try:
            if not node_mode:
                config.update(read_json(data / "updates-config.json"))
        except FileNotFoundError:
            pass
        # Configuration cannot override helper paths, commands or privileges.
        config = {k: config[k] for k in ("repository", "token_file", "group", "include_prereleases", "release_channel") if k in config}
        config["token_file"], config["group"] = str(data / "secrets/update_token"), group
        # Root-controlled service environment only; the acc-owned configuration cannot set these paths.
        config["health_socket"] = environment.get("ACC_UPDATE_HEALTH_SOCKET", "/run/accd.sock")
        config["node_config"] = environment.get("ACC_NODE_CONFIG", "/etc/acc-node.env")
        config["node_status"] = str(Path(environment.get("ACC_NODE_STATE_DIR", "/var/lib/acc-node")) / "status.json")
        config["backup_command"] = environment.get("ACC_UPDATE_BACKUP_COMMAND", "/usr/local/sbin/acc-backup")
        for path in sorted((root / "requests").glob("*.json")):
            try:
                if len(path.stem) != 32 or any(c not in "0123456789abcdef" for c in path.stem):
                    raise ValueError("invalid request filename")
                request = read_json(path, 1024)
                target = validate_node_request(request) if node_mode else validate_request(request, include_prereleases=selected_channel(config, bundle.version_of(source)) == "rc")
                if not node_mode and (not authorized_repo or config["repository"] != authorized_repo):
                    raise ValueError("repository is not authorized by the root-owned service configuration")
                if node_mode:
                    options = {**config, "build": request["build"], "sha256": request["sha256"]}
                    result = NodeTransaction(root, source, data, target, options).apply()
                else:
                    result = Transaction(root, source, data, target,
                                         {**config, 'allow_unsigned': request.get('allow_unsigned', False)}).apply()
                write_json(root / "results" / f"{path.stem}.json", result, group)
            except (ValueError, OSError):
                write_json(
                    root / "state.json", {"ok": False, "running": False, "detail": "Invalid update request"}, group
                )
            finally:
                path.unlink(missing_ok=True)
    return 0
