"""Print the next unused migration number from all local branch tips and this worktree.

Run inside the repository: python scripts/next_migration_number.py
Read-only; this suggests a number, it does not reserve one or fetch remote refs.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

VERSIONS = "src/acc/server/migrations/versions"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def declared_revision(source: str, path: str) -> str:
    values = []
    for node in ast.parse(source, filename=path).body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == "revision" for target in targets):
            values.append(node.value)
    if len(values) != 1:
        raise ValueError(f"{path}: expected exactly one revision assignment")
    try:
        value = ast.literal_eval(values[0])
    except ValueError as error:
        raise ValueError(f"{path}: revision must be a numeric string literal") from error
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{path}: revision must be a numeric string literal")
    return value


def next_migration_number(repo: Path) -> str:
    root = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    tips = set(git(root, "for-each-ref", "--format=%(objectname)", "refs/heads").splitlines())
    numbers, seen = set(), set()
    for tip in sorted(tips):
        for entry in git(root, "ls-tree", "-r", "-z", "--full-tree", tip, "--", VERSIONS).split("\0"):
            if not entry:
                continue
            metadata, path = entry.split("\t", 1)
            _, kind, oid = metadata.split()
            if kind != "blob" or not path.endswith(".py") or Path(path).name == "__init__.py":
                continue
            if match := re.match(r"([0-9]+)_", Path(path).name):
                numbers.add(int(match[1]))
            if oid not in seen:
                numbers.add(int(declared_revision(git(root, "cat-file", "blob", oid), path)))
                seen.add(oid)
    for path in sorted((root / VERSIONS).glob("*.py")):
        if path.name != "__init__.py":
            numbers.add(int(declared_revision(path.read_text(encoding="utf-8"), str(path))))
            if match := re.match(r"([0-9]+)_", path.name):
                numbers.add(int(match[1]))
    return f"{max(numbers, default=0) + 1:04d}"


def main() -> int:
    try:
        print(next_migration_number(Path.cwd()))
    except subprocess.CalledProcessError as error:
        print(error.stderr.strip(), file=sys.stderr)
        return 1
    except (OSError, SyntaxError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
