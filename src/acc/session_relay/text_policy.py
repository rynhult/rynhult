"""Authoritative native command-prefix policy; a parity test pins the browser copy."""
import json
from importlib.resources import files

REFUSED_PREFIXES = tuple(json.loads(files(__package__).joinpath("text_prefixes.json").read_text()))
