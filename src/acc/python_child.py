"""Launch ACC children without Python's implicit caller-controlled import paths.

The installed package/source root is trusted code, as is the interpreter's system
site. Neither the caller's working directory nor its Python environment selects
that code. An explicit root also supports bundled runtimes and source checkouts
without falling back to an unrelated editable install in the shared interpreter.
"""
import sys
from pathlib import Path

# -I leaves no cwd or environment entry, so sys.path is the standard library, then site-packages. The ACC root goes
# between them: the standard library can't be shadowed by anything in it, and it still wins over a different editable
# install in a shared interpreter's site-packages.
_BOOTSTRAP = ('import runpy,site,sys; r=sys.argv.pop(1); s=set(site.getsitepackages()); '
              'sys.path.insert(next((i for i,p in enumerate(sys.path) if p in s),len(sys.path)),r); '
              'runpy.run_module(sys.argv.pop(1),run_name="__main__",alter_sys=True)')


def module_command(module: str, *args: str) -> list[str]:
    """Internal fixed module names only; never take the module or code root from a request."""
    return [sys.executable, '-I', '-c', _BOOTSTRAP,
            str(Path(__file__).resolve().parent.parent), module, *args]
