"""Print the next ADR number across local branch tips and this working tree."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def decision_number(path: str) -> int | None:
    match = re.fullmatch(r"docs/decisions/([0-9]{4})-[^/]*\.md", path)
    return int(match[1]) if match else None


def next_decision_number(root: Path) -> int:
    paths = [path.relative_to(root).as_posix() for path in (root / "docs/decisions").glob("*.md")]
    commits = subprocess.check_output(
        ["git", "for-each-ref", "--format=%(objectname)", "refs/heads/"], cwd=root, text=True,
    ).splitlines()
    for commit in sorted(set(commits)):
        paths.extend(subprocess.check_output(
            ["git", "ls-tree", "-rz", "--name-only", commit, "--", "docs/decisions/"], cwd=root, text=True,
        ).split("\0"))
    numbers = [number for path in paths if (number := decision_number(path)) is not None]
    number = max(numbers, default=0) + 1
    if number > 9999:
        raise ValueError("Four-digit decision numbers are exhausted; review the numbering convention.")
    return number


if __name__ == "__main__":
    print(f"{next_decision_number(Path(__file__).resolve().parents[1]):04d}")
