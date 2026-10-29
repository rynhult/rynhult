"""Lane-E fake-provider server; missing parallel predecessors exist only in tmp."""
import runpy
import sys
import tempfile
from pathlib import Path

from project_brief_migrations import missing_predecessors, tree

from acc.server import db


def main():
    original = db.MIGRATIONS
    with tempfile.TemporaryDirectory(prefix='brief-e2e-chain-') as temporary:
        if missing_predecessors(original):
            db.MIGRATIONS = tree(original, Path(temporary) / 'migrations')
        try:
            server = Path(__file__).with_name('e2e_server.py')
            sys.argv[0] = str(server)
            runpy.run_path(str(server), run_name='__main__')
        finally:
            db.MIGRATIONS = original


if __name__ == '__main__':
    main()
