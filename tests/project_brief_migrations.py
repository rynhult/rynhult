"""Parallel-lane predecessors exist only in temporary test setup, never in source versions/."""
import ast
import shutil
from pathlib import Path


def missing_predecessors(source):
    revisions, predecessors = set(), set()
    for path in (source / 'versions').glob('*.py'):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ('revision', 'down_revision') for t in node.targets):
                value = ast.literal_eval(node.value)
                if isinstance(value, str):
                    (revisions if any(getattr(t, 'id', '') == 'revision' for t in node.targets) else predecessors).add(value)
    return predecessors - revisions


def tree(source, destination):
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__'))
    while missing := missing_predecessors(Path(destination)):
        for revision in missing:
            number = int(revision)
            if not 74 <= number <= 76:
                raise ValueError('only missing parallel-lane predecessors may be stubbed')
            (destination / 'versions' / f'{number:04d}_test_predecessor.py').write_text(
                f"revision = '{number:04d}'\ndown_revision = '{number - 1:04d}'\n"
                'branch_labels = depends_on = None\ndef upgrade(): pass\ndef downgrade(): pass\n')
    return Path(destination)


import pytest


@pytest.fixture(scope='session', autouse=True)
def parallel_brief_migration_chain(tmp_path_factory):
    from acc.server import db
    original = db.MIGRATIONS
    missing = missing_predecessors(original)
    if missing:
        db.MIGRATIONS = tree(original, tmp_path_factory.mktemp('brief-chain') / 'migrations')
    try:
        yield
    finally:
        db.MIGRATIONS = original
