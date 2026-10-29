"""Protect every pytest tree, including desktop/tests, before collecting application tests."""
import sys
from pathlib import Path

# tests is not a package; use its dedicated plugin without an installed tests-package collision.
sys.path.insert(0, str(Path(__file__).parent / 'tests'))
# The live-root gate runs on Linux; leave native Mac/Windows desktop test runners unchanged.
pytest_plugins = ['host_isolation'] if sys.platform.startswith('linux') else []
