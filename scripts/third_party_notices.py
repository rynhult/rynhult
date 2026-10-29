"""Writes THIRD_PARTY_NOTICES.md: the open-source software Rynhult uses, with each one's license.

- Web packages (npm) bundled into the dashboard: name, version, license and the license text itself, since their code
  ships inside Rynhult's built files (MIT and similar licenses ask for their notice to travel with the code).
- Python packages Rynhult installs at setup (from uv.lock, runtime only): name, version and license.

Run after changing dependencies (uv lock / npm install) and commit the result:
    .venv/bin/python scripts/third_party_notices.py
`--check` exits 1 when the file is out of date (tests use it).
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "THIRD_PARTY_NOTICES.md"
LICENSE_FILES = re.compile(r"^(licen[cs]e|copying|notice)(\.(md|txt|markdown|mit|bsd|apache))?$", re.I)


def npm_packages() -> list[dict]:
    """The packages bundled into the web build: package-lock entries that aren't dev-only."""
    lock = json.loads((ROOT / "web" / "package-lock.json").read_text())
    found = []
    for path, info in sorted(lock["packages"].items()):
        if not path or info.get("dev") or not path.startswith("node_modules/"):
            continue
        name = path.rsplit("node_modules/", 1)[1]
        folder = ROOT / "web" / path
        try:
            installed = json.loads((folder / "package.json").read_text())
        except (OSError, ValueError) as error:
            raise ValueError(f"{name}: missing installed metadata; run npm ci --prefix web") from error
        if installed.get("version") != info["version"]:
            raise ValueError(f"{name}: installed version differs from web/package-lock.json")
        text = ""
        for file in sorted(folder.iterdir()):
            if LICENSE_FILES.match(file.name) and file.is_file():
                text = file.read_text(errors="replace").strip()
                break
        if not text:
            raise ValueError(f"{name}: installed license text is missing")
        found.append({"name": name, "version": info.get("version", ""), "license": info.get("license") or "see text",
                      "text": text})
    return found


def runtime_python() -> dict[str, str]:
    """Runtime dependencies of the project in uv.lock, followed transitively (dev groups excluded)."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["name"]
    seen, todo = set(), [d["name"] for d in packages[project].get("dependencies", [])]
    while todo:
        name = todo.pop()
        if name in seen or name not in packages:
            continue
        seen.add(name)
        package = packages[name]
        todo += [d["name"] for d in package.get("dependencies", [])]
        for extra in package.get("optional-dependencies", {}).values():  # e.g. uvicorn[standard]
            todo += [d["name"] for d in extra]
    return {name: packages[name]["version"] for name in sorted(seen)}


def python_license(name: str, expected: str) -> tuple[str, str]:
    try:
        meta = metadata.metadata(name)
    except metadata.PackageNotFoundError as error:
        raise ValueError(f"{name}: missing installed metadata; run uv sync --frozen") from error
    if meta["Version"] != expected:
        raise ValueError(f"{name}: installed version differs from uv.lock")
    expression = meta.get("License-Expression")
    if expression:
        return meta["Version"], expression
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    short = (meta.get("License") or "").strip().splitlines()[0][:80] if meta.get("License") else ""
    license_name = ", ".join(classifiers) or short
    if not license_name:
        raise ValueError(f"{name}: installed license metadata is missing")
    return expected, license_name


def supplemental_notices() -> list[str]:
    lines = ["## Vendored catalog", "",
             "Catalog text retains its upstream license and attribution. Source revisions and item licenses follow; "
             "keep the adjacent LICENSE files when redistributing. Rynhult's own items use the MIT License.", "",
             "| Item | License | Source |", "|---|---|---|"]
    manifest = tomllib.loads((ROOT / "catalog/manifest.toml").read_text())
    for kind in ("skill", "agent"):
        for item in manifest.get(kind, []):
            lines.append(f"| {item['id']} | {item['license']} | {item['source']} |")
    lines += ["", "License and notice files shipped with the catalog:", ""]
    for path in sorted((ROOT / "catalog").rglob("*")):
        if path.is_file() and (LICENSE_FILES.match(path.name) or path.name.startswith("LICENSE-")):
            relative = path.relative_to(ROOT).as_posix()
            lines.append(f"- [{relative}]({relative})")
    lines += ["", "## MCP packages", "",
              "These separately installed npm servers and their dependencies are locked in "
              "`deploy/mcp/package-lock.json`. Their installed packages retain upstream license texts and notices; "
              "the table records the lockfile license declarations, not a legal compatibility audit.", "",
              "| Package | Version | License |", "|---|---|---|"]
    lock = json.loads((ROOT / "deploy/mcp/package-lock.json").read_text())
    for path, info in sorted(lock["packages"].items()):
        if path.startswith("node_modules/"):
            name = path.rsplit("node_modules/", 1)[1]
            lines.append(f"| {name} | {info.get('version', '')} | {info.get('license', 'see upstream package')} |")
    lines += ["", "## Website (rynhult.dev)", "",
              "The public site in `site/` ships one font file; provenance is in `site/assets/fonts/README.md`.", "",
              "### Noto Sans (Latin subset, from @fontsource-variable/noto-sans 5.3.0)", "", "License: OFL-1.1", "",
              "```", (ROOT / "site/assets/fonts/OFL.txt").read_text().strip().replace("```", "'''"), "```"]
    return lines + [""]


def render() -> str:
    lines = ["# Third-party notices", "",
             "Rynhult is licensed under the MIT License (see `LICENSE`). It is built on the open-source software below; each keeps its own "
             "license. Generated by `scripts/third_party_notices.py`; don't edit by hand.", "",
             "Agent CLIs (Claude Code, Codex, GitHub Copilot CLI, Antigravity, Cursor, OpenCode) and services such as Caddy, tmux "
             "and git are installed separately under their own terms; Rynhult doesn't include them.", "",
             "Web builds apply a local composition-boundary fix to @xterm/xterm 6.0.0 (MIT); "
             "see `web/scripts/xterm-composition.mjs`. Its original license notice is retained below.", "",
             "## Python packages (installed by Rynhult's setup)", "", "| Package | Version | License |", "|---|---|---|"]
    for name, expected in runtime_python().items():
        version, license_name = python_license(name, expected)
        lines.append(f"| {name} | {version} | {license_name} |")
    lines += ["", "## Web packages (bundled into the dashboard)", ""]
    for package in npm_packages():
        lines += [f"### {package['name']} {package['version']}", "", f"License: {package['license']}", ""]
        if package["text"]:
            lines += ["```", package["text"].replace("```", "'''"), "```", ""]
    lines += supplemental_notices()
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    try:
        text = render()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("THIRD_PARTY_NOTICES.md is out of date: run scripts/third_party_notices.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
