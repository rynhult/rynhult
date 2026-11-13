"""Bounded ancestry from a live, incarnation-pinned native enclosure."""
from pathlib import Path


def descends(pid,root,start):
    def fields(value):return Path(f'/proc/{value}/stat').read_text().rsplit(')',1)[1].split()
    try:
        if fields(root)[19]!=start:return False
        seen=set()
        for _ in range(64):
            if pid==root:return fields(root)[19]==start
            if pid<=1 or pid in seen:return False
            seen.add(pid);pid=int(fields(pid)[1])
    except (OSError,ValueError,IndexError):pass
    return False
