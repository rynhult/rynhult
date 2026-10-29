#!/usr/bin/python3 -I
"""Read broker account settings as data, through protected no-follow descriptors.

The caller is the root node installer. --owner and --dry-run are supplied only by its dry-run path.
No file is sourced and nothing in this module provisions an account.
"""
import os as _compat_os
import runpy as _compat_runpy
import stat as _compat_stat
from pathlib import Path as _CompatPath


def _compat_reader(relative):
    # Root runs most of these helpers: load only a reader owned like this script that no one else can change,
    # and never fall back past an installed reader that fails the check. Exit 2 also makes acc-guard block.
    here = _CompatPath(__file__).resolve()
    mine = here.stat()
    for candidate in (here.with_name("acc_env.py"), here.parent / relative):
        try:
            info = candidate.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            break
        if _compat_stat.S_ISREG(info.st_mode) and info.st_uid == mine.st_uid and not info.st_mode & 0o002 and (
                not info.st_mode & 0o020 or info.st_gid == mine.st_gid):
            if _compat_os.geteuid() == 0:
                for path in (candidate, *candidate.parents):
                    try:
                        checked = path.stat(follow_symlinks=False)
                    except OSError:
                        break
                    sticky = _compat_stat.S_ISDIR(checked.st_mode) and checked.st_mode & _compat_stat.S_ISVTX
                    if checked.st_uid != 0 or (checked.st_mode & 0o022 and not sticky):
                        break
                else:
                    return _compat_runpy.run_path(str(candidate))
                break
            return _compat_runpy.run_path(str(candidate))
        break
    print(f"{here.name}: refusing an unsafe or missing settings reader beside it", file=__import__("sys").stderr)
    raise SystemExit(2)


_compat = _compat_reader("../../src/acc/env.py")
Environment = _compat["Environment"]

import argparse
import os
import pwd
import re
import stat
import sys
from pathlib import PurePosixPath

ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
RESERVED = {"root", "nobody", "acc", "acc-node", "acc-build", "acc-svc"}


def roots_ok(value):
    paths = value.split(":")
    return bool(value) and all(
        re.fullmatch(r"/[A-Za-z0-9_./ -]+", path) and
        not any(part in ("", ".", "..") for part in path.split("/")[1:])
        for path in paths)


def protected(st, owner, directory=False):
    if st.st_uid not in {0, owner}:
        raise ValueError("broker settings have an untrusted owner")
    if st.st_mode & 0o022 and not (directory and st.st_uid == 0 and st.st_mode & stat.S_ISVTX):
        raise ValueError("broker settings are writable by another account")
    if not directory and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 65536):
        raise ValueError("broker settings must be a small, regular, unlinked file")


def read_accounts(folder, owner, check_users):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in PurePosixPath(os.path.abspath(folder)).parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            protected(os.fstat(fd), owner, directory=True)
        out = []
        for filename in sorted(os.listdir(fd)):
            if not filename.endswith(".env"):
                continue
            file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd) as stream:
                protected(os.fstat(stream.fileno()), owner)
                values = {}
                for line in stream:
                    key, sep, value = line.rstrip("\n").partition("=")
                    if sep and key in ("ACC_BROKER_ROLE", "ACC_ALLOWED_ROOTS", "RYNHULT_BROKER_ROLE", "RYNHULT_ALLOWED_ROOTS"):
                        if key in values:
                            raise ValueError("duplicate broker setting")
                        values[key] = value
            values = Environment(values)
            if values.get("ACC_BROKER_ROLE") != "agents":
                continue
            account = filename[:-4]
            if not ACCOUNT.fullmatch(account) or account in RESERVED:
                raise ValueError("unsafe agent account")
            if check_users:
                user = pwd.getpwnam(account)
                if user.pw_uid < 1000 or user.pw_uid == 65534:
                    raise ValueError("a system account cannot become an agent")
            roots = values.get("ACC_ALLOWED_ROOTS", "")
            if not roots_ok(roots):
                raise ValueError("unsafe workspace roots")
            out.append(f"{account} {roots}")
        return out
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--owner", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        accounts = read_accounts(args.folder, args.owner, not args.dry_run)
    except FileNotFoundError:
        # A missing directory is a fresh install; a missing account referenced by a file is corruption.
        if os.path.exists(args.folder):
            print("error: configured agent account is missing", file=sys.stderr)
            return 1
        accounts = []
    except (OSError, ValueError, KeyError, UnicodeError):
        print("error: unsafe broker settings or agent account; inspect ownership, permissions and workspace roots", file=sys.stderr)
        return 1
    print("\n".join(accounts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
