#!/usr/bin/python3 -I
"""Installer-only Caddyfile migration and fail-closed adapted-JSON validation."""
import json
import re
import sys
from pathlib import Path

LISTEN = "unix//run/caddy/admin.sock|0660"


def configure(path: Path) -> None:
    text = path.read_text()
    # Only a leading global block is valid Caddyfile syntax. Keep sites and other
    # global options; unsupported multiline admin options must fail adaptation.
    prefix = re.match(r"(?:\s|\#[^\n]*(?:\n|$))*", text).end()
    if text[prefix:prefix + 1] == "{":
        end = text.find("}", prefix)
        if end < 0:
            raise ValueError("unterminated global block")
        block = text[prefix + 1:end]
        block = re.sub(r"(?m)^\s*admin\b[^\n]*", "", block)
        text = text[:prefix + 1] + "\n\tadmin " + LISTEN + "\n" + block + text[end:]
    else:
        text = "{\n\tadmin " + LISTEN + "\n}\n\n" + text
    if "import acc-hardening.options" not in text:
        text = text.replace("admin " + LISTEN, "admin " + LISTEN + "\n\timport acc-hardening.options", 1)
    path.write_text(text)


if __name__ == "__main__":
    if sys.argv[1] == "configure":
        configure(Path(sys.argv[2]))
    elif sys.argv[1] == "check":
        config = json.load(sys.stdin)
        admin = config.get("admin", {})
        if admin.get("listen") != LISTEN or admin.get("disabled"):
            sys.exit("Caddy admin socket missing or unsafe; refusing to continue")
    else:
        sys.exit("expected configure or check")
