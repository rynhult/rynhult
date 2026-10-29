"""Check local Markdown destinations against the source bundle's public path policy."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from acc.bundle import private_content

ROOT = Path(__file__).resolve().parents[1]


def problems(root: Path) -> list[str]:
    errors = []
    for page in sorted(root.rglob("*.md")):
        relative = page.relative_to(root).as_posix()
        if private_content(relative):
            continue
        content = re.sub(r"^\s*(```|~~~).*?^\s*\1[^\n]*$", "", page.read_text(), flags=re.M | re.S)
        content = re.sub(r"(`+).*?\1", "", content, flags=re.S)
        destinations = re.findall(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)", content)
        destinations += re.findall(r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>]+>|\S+)", content, re.M)
        destinations += re.findall(r'''(?:href|src)=["']([^"']+)["']''', content)
        for destination in destinations:
            link = destination.strip("<>")
            url = urlsplit(link)
            if url.scheme or url.netloc or not url.path:
                continue
            path = unquote(url.path)
            wiki = root / "docs/wiki"
            if path.startswith("/") and page.is_relative_to(wiki):
                target = wiki / path.lstrip("/")
                if not target.suffix:
                    target = target.with_suffix(".md")
            else:
                target = root / path.lstrip("/") if path.startswith("/") else page.parent / path
            target = target.resolve()
            if not target.is_relative_to(root):
                reason = "outside source tree"
            elif private_content(target.relative_to(root).as_posix()):
                reason = "excluded"
            elif not target.exists():
                reason = "missing"
            else:
                continue
            errors.append(f"{relative}: {link} ({reason})")
    return errors


if __name__ == "__main__":
    found = problems(ROOT)
    print("\n".join(found) if found else "Public Markdown links resolve.")
    sys.exit(bool(found))
