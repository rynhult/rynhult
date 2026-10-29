"""This host's container qualification record: what lets an owner turn containers on for a project.

Containers ship as a Preview (docs/CONTAINERS.md, "Release status"). A project can switch them on only on a host that
has passed the container qualification run. The owner records the passing report once, as root:

    sudo /srv/acc/app/.venv/bin/python -I -m acc.container_host_record record /path/to/report.json

That writes RECORD (root-owned, 0644): the report's sha256, its status, and this machine's /etc/machine-id, so a
record copied from another host doesn't count. accd only reads it (check()): it must be a regular root-owned file that
nobody else can write, in folders nobody else can write, naming a passing report and this machine. An agent account
can't create or change it, and a missing or untrusted record leaves containers off with the reason why.

`python -I -m acc.container_host_record check` prints what accd would conclude. Standard library only: root runs this.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

RECORD = Path("/etc/acc/containers/host-qualified.json")
MACHINE_ID = Path("/etc/machine-id")
PASSING = ("go", "scoped-phase1-pass")  # the qualification driver's statuses that carry go_phase_1: true
MAX_BYTES = 64 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
HOW = ("run the container qualification kit on this host, then record its passing report with "
       "`sudo /srv/acc/app/.venv/bin/python -I -m acc.container_host_record record report.json` (docs/CONTAINERS.md)")


def _machine_id(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else None


def _folders(path: Path, top: Path) -> list[Path]:
    """The folders from the record's up to `top` (/ in production; tests stop at their own temporary folder)."""
    folders = []
    for folder in path.parents:
        folders.append(folder)
        if folder == top:
            break
    return folders


def _trusted(path: Path, root_uid: int, top: Path) -> str | None:
    """Why a path (and every folder above it) isn't one only root can change, or None."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_uid != root_uid or info.st_mode & 0o022:
        return "not owned by root, or writable by someone else"
    for folder in _folders(path, top):
        info = os.lstat(folder)
        if stat.S_ISLNK(info.st_mode) or info.st_uid != root_uid or info.st_mode & 0o022:
            return f"its folder {folder} is not owned by root, or is writable by someone else"
    return None


def check(path: Path = RECORD, machine_id: Path = MACHINE_ID, root_uid: int = 0, top: Path = Path("/")) -> dict:
    """{"passed": bool, "reason": str, and when passed: "report_sha256", "status", "recorded_at"}."""
    try:
        problem = _trusted(path, root_uid, top)
    except OSError:
        problem = "record or parent folder is inaccessible"
    if problem == "missing":
        return {"passed": False, "reason": f"this host hasn't recorded a passed container qualification; {HOW}"}
    if problem:
        return {"passed": False, "reason": f"the qualification record {path} isn't trusted ({problem}); {HOW}"}
    try:
        # lstat above refused a symlink; O_NOFOLLOW closes the window for a swap after it
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
            fd_info = os.fstat(file.fileno())
            if not stat.S_ISREG(fd_info.st_mode) or fd_info.st_size > MAX_BYTES or fd_info.st_uid != root_uid or fd_info.st_mode & 0o022:
                raise ValueError
            value = json.loads(file.read(MAX_BYTES))
    except (OSError, ValueError, RecursionError):
        return {"passed": False, "reason": f"the qualification record {path} can't be read; {HOW}"}
    if (not isinstance(value, dict) or value.get("version") != 1 or value.get("go_phase_1") is not True
            or value.get("status") not in PASSING or not SHA256.match(str(value.get("report_sha256", "")))):
        return {"passed": False, "reason": f"the qualification record {path} doesn't name a passing report; {HOW}"}
    here = _machine_id(machine_id)
    if here is None or value.get("machine_id") != here:
        return {"passed": False, "reason": f"the qualification record {path} is from another machine; {HOW}"}
    return {"passed": True, "reason": "this host passed the container qualification",
            "report_sha256": value["report_sha256"], "status": value["status"],
            "recorded_at": str(value.get("recorded_at", ""))[:40]}


def record(report: Path, path: Path = RECORD, machine_id: Path = MACHINE_ID, root_uid: int = 0,
           top: Path = Path("/")) -> dict:
    """Write the record for a passing report (root only). Refuses a failing report, or a folder others can write."""
    if os.geteuid() != root_uid:
        raise SystemExit("record runs as root: sudo ... -I -m acc.container_host_record record report.json")
    try:
        with os.fdopen(os.open(report, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("not regular")
            data = source.read(16 * 1024 * 1024 + 1)
    except (OSError, ValueError):
        raise SystemExit("the report must be a readable regular file, not a symlink or device") from None
    if len(data) > 16 * 1024 * 1024:
        raise SystemExit("that report is too large to be a qualification report")
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        raise SystemExit("that file isn't a qualification report (not JSON)") from None
    if not isinstance(value, dict) or value.get("go_phase_1") is not True or value.get("status") not in PASSING:
        raise SystemExit(f"that report didn't pass (status {value.get('status') if isinstance(value, dict) else None!r}, "
                         "go_phase_1 must be true): nothing recorded")
    here = _machine_id(machine_id)
    if here is None:
        raise SystemExit(f"can't read this machine's id from {machine_id}")
    for folder in _folders(path, top):
        info = os.lstat(folder)
        if stat.S_ISLNK(info.st_mode) or info.st_uid != root_uid or info.st_mode & 0o022:
            raise SystemExit(f"{folder} must be owned by root and writable by nobody else")
    entry = {"version": 1, "status": value["status"], "go_phase_1": True,
             "report_sha256": hashlib.sha256(data).hexdigest(), "machine_id": here,
             "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, (json.dumps(entry, indent=2) + "\n").encode())
        os.fchmod(fd, 0o644)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m acc.container_host_record",
                                     description="This host's container qualification record (docs/CONTAINERS.md).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="what Rynhult concludes from this host's record")
    write = commands.add_parser("record", help="record a passing qualification report (as root)")
    write.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    if args.command == "record":
        entry = record(args.report)
        print(f"recorded {RECORD}: report {entry['report_sha256']} ({entry['status']}) for this machine")
        return 0
    result = check()
    print(("PASSED: " if result["passed"] else "NOT QUALIFIED: ") + result["reason"])
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
