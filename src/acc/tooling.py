"""Registry-only tool coordinates and fixed argv execution. Standard library; also deployed beside root helpers."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

SYSTEM = frozenset(("apt", "snap", "flatpak"))
NAMES = {
    "apt": r"[a-z0-9][a-z0-9+.-]+(?::[a-z0-9][a-z0-9-]*)?",
    "snap": r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",
    "flatpak": r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_-]*){2,}",
    "npm": r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*",
    "pip": r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?",
    "cargo": r"[A-Za-z0-9][A-Za-z0-9_-]*",
    "go": r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}/[A-Za-z0-9_./~-]+",
    "gem": r"[A-Za-z0-9][A-Za-z0-9_.-]*",
    "brew": r"[a-z0-9][a-z0-9+_.-]*",
}
NAMES.update(pnpm=NAMES["npm"], yarn=NAMES["npm"], pipx=NAMES["pip"], uv=NAMES["pip"])
VERSIONS = {
    "apt": r"(?:[0-9]+:)?[0-9][A-Za-z0-9.+:~_-]*",
    "snap": r"[1-9][0-9]*",
    "flatpak": r"[a-f0-9]{64}",
    "npm": r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?",
    "pip": r"[0-9]+(?:![0-9]+)?(?:\.[0-9]+)*(?:(?:a|b|rc|\.post|\.dev)[0-9]+)*(?:\+[a-z0-9.]+)?",
    "cargo": r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?",
    "go": r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?(?:\+incompatible)?",
    "gem": r"[0-9]+(?:\.[A-Za-z0-9]+)*",
    "brew": r"[0-9]+(?:\.[0-9]+)*",
}
VERSIONS.update(pnpm=VERSIONS["npm"], yarn=VERSIONS["npm"], pipx=VERSIONS["pip"], uv=VERSIONS["pip"])
OPERATIONS = ("install", "remove")
HELPER_FIELDS = {"id", "action", "manager", "name", "version", "operation"}
REQUEST_ID = re.compile(r"tool-[0-9a-f]{12}")


def validate(manager: str, name: str, version: str | None = None) -> tuple[str, str, str | None]:
    if not isinstance(manager, str) or manager not in NAMES:
        raise ValueError("unknown tool manager")
    if not isinstance(name, str) or len(name) > 200 or not re.fullmatch(NAMES[manager], name) or ".." in name:
        raise ValueError(f"invalid {manager} package name: use a registry name, never a URL, path or command")
    if version is not None and (not isinstance(version, str) or len(version) > 100 or
                                not re.fullmatch(VERSIONS[manager], version)):
        raise ValueError(f"invalid {manager} version: use an exact version, never options, ranges or shell text")
    return manager, name, version


def helper_request(body: object) -> dict:
    if not isinstance(body, dict) or set(body) != HELPER_FIELDS or body.get("action") != "tool":
        raise ValueError("bad tool request fields")
    if not isinstance(body["id"], str) or not REQUEST_ID.fullmatch(body["id"]):
        raise ValueError("bad tool request id")
    validate(body["manager"], body["name"], body["version"])
    if body["manager"] not in SYSTEM or body["operation"] not in OPERATIONS:
        raise ValueError("the root helper only accepts system managers and install/remove")
    return body


def destination(manager: str, account: str) -> str:
    return "system-wide" if manager in SYSTEM else f"{account}'s agent account"


def _default_run(argv: list[str], *, env: dict, timeout: int):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=timeout, cwd="/")


def execute(manager: str, name: str, version: str | None, operation: str, *, home: Path,
            system: bool, run=None) -> dict:
    validate(manager, name, version)
    if operation not in OPERATIONS or system != (manager in SYSTEM):
        raise ValueError("invalid tool operation or execution scope")
    run = run or _default_run
    prefix = home / ".local"
    env = {"HOME": str(home), "PATH": f"{prefix}/bin:{home}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
           "LANG": "C.UTF-8", "DEBIAN_FRONTEND": "noninteractive", "PIP_NO_INPUT": "1",
           "PIPX_HOME": str(prefix / "share/pipx"), "PIPX_BIN_DIR": str(prefix / "bin"),
           "UV_TOOL_DIR": str(prefix / "share/uv/tools"), "UV_TOOL_BIN_DIR": str(prefix / "bin"),
           "CARGO_HOME": str(home / ".cargo"), "GOBIN": str(home / "go/bin"),
           "GEM_HOME": str(prefix / "share/gems"), "GEM_PATH": str(prefix / "share/gems"),
           "npm_config_userconfig": "/dev/null", "npm_config_globalconfig": "/dev/null",
           "PIP_CONFIG_FILE": "/dev/null", "UV_NO_CONFIG": "1"}
    if system:
        env["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    install = operation == "install"
    spec = name + (f"@{version}" if version else "")
    if manager == "apt":
        argv = ["apt-get", "install" if install else "remove", "-y", "--no-install-recommends",
                name + (f"={version}" if install and version else "")]
    elif manager == "snap":
        argv = ["snap", "install" if install else "remove", name]
        if install and version:
            argv += ["--revision", version]
    elif manager == "flatpak":
        argv = (["flatpak", "install", "--or-update", "--system", "--noninteractive", "flathub", name] if install else
                ["flatpak", "uninstall", "--system", "--noninteractive", name])
    elif manager == "npm":
        argv = ["npm", "install" if install else "uninstall", "--global", "--prefix", str(prefix),
                "--no-audit", "--no-fund", spec if install else name]
    elif manager == "pnpm":
        argv = ["pnpm", "add" if install else "remove", "--global", "--global-dir", str(prefix / "share/pnpm/global"),
                "--global-bin-dir", str(prefix / "bin"), spec if install else name]
    elif manager == "yarn":
        argv = ["yarn", "global", "add" if install else "remove", "--prefix", str(prefix),
                "--non-interactive", spec if install else name]
    elif manager == "pip":
        argv = ["python3", "-P", "-m", "pip", "--isolated", "install" if install else "uninstall"]
        argv += (["--user", "--upgrade", name + (f"=={version}" if version else "")] if install else ["-y", name])
        # -P removes the implicit cwd while retaining the account's user-installed pip.
        # --isolated ignores user config, but also PYTHONUSERBASE; HOME defines Python's ordinary ~/.local base.
    elif manager == "pipx":
        argv = ["pipx", "install", "--force", name + (f"=={version}" if version else "")] if install else [
            "pipx", "uninstall", name]
    elif manager == "uv":
        argv = ["uv", "tool", "install", "--force", "--upgrade", name + (f"=={version}" if version else "")] if install else [
            "uv", "tool", "uninstall", name]
    elif manager == "cargo":
        argv = ["cargo", "install", "--locked", "--force", name] if install else ["cargo", "uninstall", name]
        if install and version:
            argv += ["--version", f"={version}"]
    elif manager == "go":
        argv = ["go", "install", name + "@" + (version or "latest")] if install else []
    elif manager == "gem":
        argv = ["gem", "install", "--no-document", "--bindir", str(prefix / "bin"), name] if install else [
            "gem", "uninstall", "--all", "--executables", "--ignore-dependencies", name]
        if install and version:
            argv += ["--version", version]
    else:
        argv = ["brew", "install" if install else "uninstall", spec if install else name]

    def call(args: list[str], *, missing_ok: bool = False) -> str:
        try:
            result = run(args, env=env, timeout=900)
        except OSError as error:
            raise ValueError(f"{manager} cannot run on this machine/account: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"{manager} timed out; inspect the account before retrying") from error
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            absent = {"apt": "no packages found matching", "snap": "no matching snaps installed",
                      "flatpak": "is not installed"}
            if missing_ok and result.returncode == 1 and absent.get(manager, "\0") in detail:
                return ""
            raise ValueError(f"{manager} failed: {detail[-500:]}")
        return result.stdout

    if manager == "go" and not install:
        (home / "go/bin" / name.rsplit("/", 1)[-1]).unlink(missing_ok=True)
    else:
        if manager == "snap" and install and installed_version(manager, name, home, call):
            argv[1] = "refresh"
        call(argv)
    if manager == "flatpak" and install and version:
        call(["flatpak", "update", "--system", "--noninteractive", "--commit", version, name])
    observed = installed_version(manager, name, home, call)
    if install and (not observed or version is not None and version != observed):
        raise ValueError(f"installed version could not be verified (requested {version or 'latest'}, observed {observed})")
    if not install and observed:
        raise ValueError(f"removal could not be verified: {name} {observed} is still installed")
    return {"ok": True, "version": observed, "detail": f"{name} {observed or ''} {'installed' if install else 'removed'}"}


def installed_version(manager: str, name: str, home: Path, call) -> str | None:
    try:
        version = _installed_version(manager, name, home, call)
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError(f"{manager} returned invalid installed-package metadata") from error
    if version is not None and (not isinstance(version, str) or not version.strip() or len(version) > 100):
        raise ValueError(f"{manager} returned invalid installed-version metadata")
    return version


def _installed_version(manager: str, name: str, home: Path, call) -> str | None:
    prefix = home / ".local"
    if manager == "apt":
        out = call(["dpkg-query", "-W", "-f", "${Status} ${Version}", name], missing_ok=True)
        return out.split()[-1] if "install ok installed " in out else None
    if manager == "snap":
        out = call(["snap", "list", name], missing_ok=True)
        rows = [r.split() for r in out.splitlines() if r.split() and r.split()[0] == name]
        return rows[0][2] if rows and len(rows[0]) > 2 else None  # immutable revision, not a channel
    if manager == "flatpak":
        return call(["flatpak", "info", "--system", "--show-commit", name], missing_ok=True).strip() or None
    if manager in ("npm", "pnpm"):
        argv = (["npm", "ls", "--global", "--prefix", str(prefix), "--json", "--depth=0"] if manager == "npm" else
                ["pnpm", "list", "--global", "--global-dir", str(prefix / "share/pnpm/global"), "--json", "--depth=0"])
        out = call(argv)
        data = json.loads(out)
        if isinstance(data, list):
            data = data[0] if data else {}
        entry = data.get("dependencies", {}).get(name, {})
        return entry.get("version")
    if manager == "yarn":
        out = call(["yarn", "global", "list", "--prefix", str(prefix), "--json"])
        for line in out.splitlines():
            data = json.loads(line)
            for tree in data.get("data", {}).get("trees", []) if isinstance(data.get("data"), dict) else []:
                value = tree.get("name", "")
                if value.startswith(name + "@"):
                    return value[len(name) + 1:]
        return None
    if manager == "pip":
        out = call(["python3", "-P", "-m", "pip", "--isolated", "list", "--user", "--format=json"])
        return next((p["version"] for p in json.loads(out) if normalized(p["name"]) == normalized(name)), None)
    if manager == "pipx":
        data = json.loads(call(["pipx", "list", "--json"]))
        entry = next((v for k, v in data.get("venvs", {}).items() if normalized(k) == normalized(name)), {})
        return entry.get("metadata", {}).get("main_package", {}).get("package_version")
    if manager == "uv":
        out = call(["uv", "tool", "list"])
        match = next((r.split()[1].lstrip("v") for r in out.splitlines()
                      if len(r.split()) >= 2 and normalized(r.split()[0]) == normalized(name)), None)
        return match
    if manager == "cargo":
        out = call(["cargo", "install", "--list"])
        match = re.search(rf"^{re.escape(name)} v([^ :]+).*:", out, re.M)
        return match.group(1) if match else None
    if manager == "go":
        binary = home / "go/bin" / name.rsplit("/", 1)[-1]
        if not binary.exists():
            return None
        out = call(["go", "version", "-m", str(binary)])
        match = re.search(r"^\s*mod\s+\S+\s+(v\S+)", out, re.M)
        return match.group(1) if match else None
    if manager == "gem":
        out = call(["gem", "list", "--local", "--exact", name])
        match = re.search(rf"^{re.escape(name)} \(([^ ,)]+)", out, re.M)
        return match.group(1) if match else None
    data = json.loads(call(["brew", "info", "--json=v2", name]))
    formula = data.get("formulae", [])
    installed = formula[0].get("installed", []) if formula else []
    return installed[0].get("version") if installed else None


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()
