#!/usr/bin/python3 -I
"""Installer data preflight and descriptor-relative bundle installation.

The acc account can rename data children during an update. Root must never open
a writable destination by a string path beneath data, even after preflight.
"""
import os
import pwd
import stat
import sys
import uuid

DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def open_dir(path):
    fd = os.open("/", DIR)
    try:
        for part in os.path.abspath(path).split("/")[1:]:
            child = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def check(fd, uid, bundle=False, top=True):
    info = os.fstat(fd)
    expected = 0 if bundle else uid
    if info.st_uid != expected or info.st_mode & 0o022:
        raise ValueError("wrong owner or writable directory")
    for item in os.scandir(fd):
        info = item.stat(follow_symlinks=False)
        child_bundle = bundle or (top and item.name == "bundle")
        expected = 0 if child_bundle else uid
        if stat.S_ISLNK(info.st_mode) or info.st_uid != expected:
            raise ValueError("symlink or wrong owner: " + item.name)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(item.name, DIR, dir_fd=fd)
            try:
                check(child, uid, child_bundle, top=False)
            finally:
                os.close(child)


def install_bundle(fd, uid, source, name):
    if "/" in name or not name.startswith("acc-") or not name.endswith(".tar.gz"):
        raise ValueError("invalid bundle filename")
    try:
        os.mkdir("bundle", 0o750, dir_fd=fd)
        # Open before chown/chmod: all metadata operations use the descriptor.
        made = True
    except FileExistsError:
        made = False
    bundle = os.open("bundle", DIR, dir_fd=fd)
    try:
        if made:
            os.fchown(bundle, 0, pwd.getpwuid(uid).pw_gid)
            os.fchmod(bundle, 0o750)
        check(bundle, uid, bundle=True)
        temp = ".install-" + uuid.uuid4().hex
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=bundle)
        try:
            os.fchown(out, 0, pwd.getpwuid(uid).pw_gid)
            with open(source, "rb") as src, os.fdopen(out, "wb", closefd=False) as dest:
                while chunk := src.read(1024 * 1024):
                    dest.write(chunk)
            os.fsync(out)
            os.rename(temp, name, src_dir_fd=bundle, dst_dir_fd=bundle)
        finally:
            os.close(out)
            try:
                os.unlink(temp, dir_fd=bundle)
            except FileNotFoundError:
                pass
        for old in os.listdir(bundle):
            if old.startswith("acc-") and old.endswith(".tar.gz") and old != name:
                os.unlink(old, dir_fd=bundle)
    finally:
        os.close(bundle)


if __name__ == "__main__":
    try:
        command, path, uid, *args = sys.argv[1:]
        uid = int(uid)
        fd = open_dir(path)
        try:
            check(fd, uid)
            if command == "bundle":
                install_bundle(fd, uid, *args)
            elif command != "check":
                raise ValueError("unknown command")
        finally:
            os.close(fd)
    except (OSError, ValueError) as error:
        sys.exit("unsafe Rynhult data path: " + str(error))
