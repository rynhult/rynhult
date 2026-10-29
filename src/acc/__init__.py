"""Rynhult coding-agent control center."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version() -> str:
    """Identify the source actually imported; another editable install may own the metadata."""
    import tomllib

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        return tomllib.loads(pyproject.read_text())["project"]["version"]
    try:
        return version("rynhult")  # wheels have package metadata, without a sibling source pyproject
    except PackageNotFoundError:
        return "0.0.0"



__version__ = _version()
