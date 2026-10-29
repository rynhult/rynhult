"""WSL detection, metadata-only exposure probes and opt-in wsl.conf editing (stdlib only)."""
from __future__ import annotations

import argparse
import configparser
import logging
import os
import platform
import re
import stat
import sys
import uuid
from pathlib import Path

log = logging.getLogger("acc.node")
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
MOUNT_OPTIONS = ("metadata", "uid=0", "gid=0", "umask=077", "fmask=077", "dmask=077")


def wsl_version(release: str | None = None, environ=None,
                interop: Path = Path("/proc/sys/fs/binfmt_misc/WSLInterop")) -> str:
    release = platform.release() if release is None else release
    environ = os.environ if environ is None else environ
    lowered = release.lower()
    signs = ("microsoft" in lowered or "wsl" in lowered or bool(environ.get("WSL_DISTRO_NAME"))
             or bool(environ.get("WSL_INTEROP")) or interop.exists())
    return ("1" if lowered.endswith("-microsoft") else "2") if signs else "0"


def interop_enabled(binfmt: Path = Path("/proc/sys/fs/binfmt_misc")) -> bool | None:
    """Effective interop: any registered WSL handler (WSL with systemd may name it WSLInterop-late) that is enabled.
    No binfmt_misc, or an unreadable handler, is unknown, never "off"."""
    try:
        if not (binfmt / "status").exists():
            return None
        states = [entry.read_text().splitlines()[0] for entry in binfmt.iterdir() if entry.name.startswith("WSLInterop")]
    except (OSError, UnicodeError, IndexError):
        log.warning("Cannot read effective WSL interop state")
        return None
    if any(state not in ("enabled", "disabled") for state in states):
        log.warning("Unrecognized effective WSL interop state")
        return None
    return "enabled" in states


DRIVE = re.compile(r"[A-Za-z]:\\?")


def drive_mount(filesystem: str, source: str, options: str, target: str) -> str | None:
    """"drive" for a mount WSL uses for a Windows drive, "maybe" for one that only looks like it, else None."""
    if (filesystem == "drvfs" or (filesystem == "9p" and (DRIVE.fullmatch(source) or "aname=drvfs" in options))
            or (filesystem == "virtiofs" and (DRIVE.fullmatch(source) or source.lower().startswith("drvfs")))):
        return "drive"
    # Unrecognised but Windows-like: a drive-letter source, drvfs in its options, or the default automount root.
    if DRIVE.fullmatch(source) or "drvfs" in options or re.fullmatch(r"/mnt/[A-Za-z]", target):
        return "maybe"
    return None


def windows_drive_readable(mountinfo: Path = Path("/proc/self/mountinfo")) -> bool | None:
    """Run in the agent's broker: only mount metadata and access checks, never Windows file contents.
    A readable mount that only looks like a Windows drive is unknown, never "not readable"."""
    try:
        mounts = mountinfo.read_text().splitlines()
        unsure = False
        for line in mounts:
            left, right = line.split(" - ", 1)
            filesystem, source, *options = right.split()
            # mountinfo escapes space, tab, newline and backslash as \NNN in both fields: C:\ reads C:\134 (SR7-8).
            unescape = lambda text: re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), text)
            source, target = unescape(source), unescape(left.split()[4])
            kind = drive_mount(filesystem, source, " ".join(options), target)
            if kind is None or not os.access(target, os.R_OK | os.X_OK, effective_ids=True):
                continue
            if kind == "drive":
                return True
            unsure = True
        return None if unsure else False
    except (OSError, UnicodeError, ValueError, IndexError):
        log.warning("Cannot determine Windows drive access for this agent account")
        return None


def isolation_state(interop: bool | None, accounts: dict[str, bool | None]) -> dict:
    readable = True if any(value is True for value in accounts.values()) else (
        False if accounts and all(value is False for value in accounts.values()) else None)
    return {"interop_enabled": interop, "windows_drive_readable": readable, "accounts": accounts}


def clean_isolation(value, accounts: list[str]) -> dict:
    value = value if isinstance(value, dict) else {}
    reported = value.get("accounts")
    reported = reported if isinstance(reported, dict) else {}
    interop = value.get("interop_enabled")
    return isolation_state(interop if type(interop) is bool else None, {
        account: reported.get(account) if type(reported.get(account)) is bool else None
        for account in accounts[:20] if isinstance(account, str) and ACCOUNT.fullmatch(account)})


def merged_config(text: str, disable_automount: bool) -> str:
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",),
                                       inline_comment_prefixes=("#", ";"), empty_lines_in_values=False)
    try:
        if any(line.lstrip().startswith("[") and not re.fullmatch(r"\s*\[[^]\r\n]+\]\s*(?:[#;].*)?", line)
               for line in text.splitlines()):
            raise ValueError
        parser.read_string(text)
        sections = {section.casefold(): section for section in parser.sections()}
        if (parser.defaults() or len(sections) != len(parser.sections())
                or any(section != section.strip() for section in parser.sections())
                or any("\n" in value for section in parser.sections() for _, value in parser.items(section))):
            raise ValueError
    except (configparser.Error, ValueError):
        raise ValueError("unparsable wsl.conf; repair it manually before opting into WSL isolation") from None
    desired = {"interop": {"enabled": "false", "appendWindowsPath": "false"}, "automount": {}}
    if disable_automount:
        desired["automount"]["enabled"] = "false"
    else:
        original = parser.get(sections["automount"], "options", fallback="") if "automount" in sections else ""
        if original.startswith(('"', "'")):
            if len(original) < 2 or original[-1] != original[0]:
                raise ValueError("unparsable wsl.conf automount options; repair them before opting in")
            original = original[1:-1]
        replaced = {option.split("=", 1)[0] for option in MOUNT_OPTIONS}
        extra = [option.strip() for option in original.split(",")
                 if option.strip() and option.strip().split("=", 1)[0].strip().casefold() not in replaced]
        desired["automount"]["options"] = '"' + ",".join((*MOUNT_OPTIONS, *extra)) + '"'
    newline = "\r\n" if "\r\n" in text else "\n"
    output, block = [], []
    current = None

    def finish():
        wanted = dict(desired.get(current, {}))
        for line in block:
            assignment = re.match(r"^(\s*([^=]+?)\s*=\s*)(.*?)(\r?\n)?$", line)
            key = assignment[2].strip().casefold() if assignment else ""
            selected = next((name for name in wanted if name.casefold() == key), None)
            if selected and not line.lstrip().startswith(("#", ";")):
                comment = re.search(r"\s+[;#].*$", assignment[3])
                line = assignment[1] + wanted.pop(selected) + (comment[0] if comment else "") + (assignment[4] or "")
            output.append(line)
        if wanted:
            if output and not output[-1].endswith("\n"):
                output[-1] += newline
            output.extend(f"{key}={value}{newline}" for key, value in wanted.items())

    for line in text.splitlines(keepends=True):
        section = re.match(r"^\s*\[([^]]+)\]", line)
        if section:
            finish()
            block = [line]
            current = section[1].casefold()
        else:
            block.append(line)
    finish()
    for section, values in desired.items():
        if section not in sections:
            if output and not output[-1].endswith("\n"):
                output[-1] += newline
            output.append(f"{newline if output else ''}[{section}]{newline}")
            output.extend(f"{key}={value}{newline}" for key, value in values.items())
    return "".join(output)


def configure(path: Path, *, disable_automount: bool = False, dry_run: bool = False) -> Path | None:
    folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        parent = os.fstat(folder)
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise ValueError("wsl.conf needs a protected directory owned by its administrator")
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        except FileNotFoundError:
            original, before = None, b""
        else:
            with os.fdopen(fd, "rb") as stream:
                original = os.fstat(stream.fileno())
                if (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1
                        or original.st_uid != os.geteuid() or original.st_mode & 0o022 or original.st_size > 65536):
                    raise ValueError("wsl.conf must be a small, unlinked, administrator-owned file, not group/world writable")
                before = stream.read(65537)
        try:
            after = merged_config(before.decode("utf-8"), disable_automount).encode("utf-8")
        except UnicodeError:
            raise ValueError("unparsable wsl.conf; use UTF-8 text before opting into WSL isolation") from None
        if after == before or dry_run:
            return None
        mode = stat.S_IMODE(original.st_mode) if original else 0o644
        backup = None
        if original:
            current = os.stat(path.name, dir_fd=folder, follow_symlinks=False)
            if (current.st_ino, current.st_mtime_ns, current.st_size) != (
                    original.st_ino, original.st_mtime_ns, original.st_size):
                raise ValueError("wsl.conf changed while reading it; retry after the other edit finishes")
            backup = path.with_name(f"{path.name}.rynhult-backup-{uuid.uuid4().hex}")
            backup_fd = os.open(backup.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=folder)
            with os.fdopen(backup_fd, "wb") as stream:
                stream.write(before)
                stream.flush()
                os.fsync(stream.fileno())
        temporary = f".{path.name}.{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=folder)
        with os.fdopen(fd, "wb") as stream:
            stream.write(after)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=folder, dst_dir_fd=folder)
        temporary = None
        os.fsync(folder)
        return backup
    finally:
        if temporary:
            os.unlink(temporary, dir_fd=folder)
        os.close(folder)


def main() -> int:
    parser = argparse.ArgumentParser(description="Opt-in WSL node isolation; never called by updates")
    parser.add_argument("--detect", action="store_true")
    parser.add_argument("--disable-automount", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--path", type=Path, default=Path("/etc/wsl.conf"))
    args = parser.parse_args()
    if args.detect:
        print(wsl_version())
        return 0
    try:
        backup = configure(args.path, disable_automount=args.disable_automount, dry_run=args.dry_run)
    except (OSError, ValueError) as error:
        print(f"WSL isolation refused: {error}", file=sys.stderr)
        return 1
    print(f"{'Would configure' if args.dry_run else 'Configured'} WSL isolation; "
          f"automount {'disabled' if args.disable_automount else 'restricted to root'}")
    if backup:
        print(f"Previous settings backed up at {backup}")
    print("From Windows, run wsl --shutdown to apply this (all running WSL distributions stop).")
    print("Interop off also stops running Windows programs from this distro.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
