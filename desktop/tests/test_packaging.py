"""Packaging behavior using temporary payloads; no tool installation or live services."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[1]


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def runtime_build(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    for name in ("build-runtime.sh", "sign-runtime-macos.sh"):
        if (DESKTOP / name).exists():
            shutil.copy2(DESKTOP / name, desktop / name)
    shutil.copytree(DESKTOP / "src-tauri", desktop / "src-tauri",
                    ignore=shutil.ignore_patterns("target", "gen"))
    for name in ("requirements-linux.lock", "requirements-macos-arm64.lock", "requirements-build.lock"):
        shutil.copy2(DESKTOP / name, desktop / name)
    tools = tmp_path / "tools"
    tools.mkdir()
    executable(tools / "uname", '#!/bin/sh\ncase "$1" in -s) echo "$TEST_OS";; -m) echo "$TEST_ARCH";; esac\n')
    executable(tools / "uv", """#!/usr/bin/env python3
import json, os, pathlib
import sys
with open(os.environ["TEST_CALLS"], "a") as log:
    log.write(json.dumps(["uv", *sys.argv[1:]]) + "\\n")
if sys.argv[1:3] == ["python", "install"]:
    root = pathlib.Path(os.environ["UV_PYTHON_INSTALL_DIR"]) / sys.argv[-1]
    binary = root / "bin/python3.12"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\\nexit 0\\n")
    binary.chmod(0o755)
    for name in ["libpython3.12.dylib", "libpython3.12.so.1.0", "libtcl9.dylib",
                 "python3.12/lib-dynload/_ssl.so", "python3.12/lib-dynload/_tkinter.so",
                 "python3.12/ensurepip/payload", "python3.12/test/payload"]:
        file = root / "lib" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture")
    (root / "lib/libpython3.12.so").symlink_to("libpython3.12.so.1.0")  # python-build-standalone's layout
""")
    for tool in ("strip", "codesign"):
        executable(tools / tool, f"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["TEST_CALLS"], "a") as log:
    log.write(json.dumps(["{tool}", *sys.argv[1:]]) + "\\n")
if "{tool}" == "codesign" and os.environ.get("TEST_SIGN_FAIL") == "1":
    sys.exit(1)
""")
    calls = tmp_path / "calls.jsonl"
    def build(system, arch, **env):
        previous = desktop / "runtime/python"
        previous.mkdir(parents=True)
        (previous / "previous-payload").write_text("keep until platform validation")
        result = subprocess.run(["sh", str(desktop / "build-runtime.sh")], capture_output=True, text=True,
                                env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
                                     "TEST_OS": system, "TEST_ARCH": arch, "TEST_CALLS": str(calls), **env})
        recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result, desktop / "runtime/python", recorded
    return build


def test_mac_runtime_retains_dynamic_python_and_uses_macho_tools(runtime_build):
    result, runtime, calls = runtime_build("Darwin", "arm64")
    assert result.returncode == 0, result.stderr
    assert (runtime / "lib/libpython3.12.dylib").is_file()
    assert not (runtime / "lib/libtcl9.dylib").exists()
    assert not (runtime / "lib/python3.12/lib-dynload/_tkinter.so").exists()
    assert not (runtime / "lib/python3.12/test").exists()
    install = next(call for call in calls if call[:3] == ["uv", "python", "install"])
    assert install[-1] == "cpython-3.12.15-macos-aarch64-none"
    dependencies = next(call for call in calls if call[:3] == ["uv", "pip", "install"])
    assert "--require-hashes" in dependencies
    assert dependencies[-1].endswith("requirements-macos-arm64.lock")
    assert "--only-binary" in dependencies and "http-ece" in dependencies
    assert "--require-build-hashes" in dependencies and "--build-constraints" in dependencies
    assert all(call[1] == "-x" for call in calls if call[0] == "strip")
    assert any(call[0] == "codesign" for call in calls)


def test_linux_runtime_keeps_elf_pruning_and_stripping(runtime_build):
    result, runtime, calls = runtime_build("Linux", "x86_64")
    assert result.returncode == 0, result.stderr
    # Neither the library nor its link: a dangling link stops Tauri from bundling the resources.
    assert not any(os.path.lexists(runtime / "lib" / name) for name in ("libpython3.12.so", "libpython3.12.so.1.0"))
    assert (runtime / "bin/python3").is_file()
    assert all(call[1] == "--strip-unneeded" for call in calls if call[0] == "strip")
    assert not any(call[0] == "codesign" for call in calls)


def test_mac_signature_failures_stop_runtime_assembly(runtime_build):
    result, _, _ = runtime_build("Darwin", "arm64", TEST_SIGN_FAIL="1")
    assert result.returncode != 0


def test_unsupported_runtime_does_not_destroy_the_previous_payload(runtime_build):
    result, runtime, calls = runtime_build("Darwin", "x86_64")
    assert result.returncode != 0
    assert "arm64" in result.stderr
    assert (runtime / "previous-payload").read_text() == "keep until platform validation"
    assert not calls


@pytest.fixture
def package_run(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    shutil.copy2(DESKTOP / "package.sh", desktop / "package.sh")
    shutil.copy2(DESKTOP / "sign-runtime-macos.sh", desktop / "sign-runtime-macos.sh")
    shutil.copytree(DESKTOP / "src-tauri", desktop / "src-tauri",
                    ignore=shutil.ignore_patterns("target", "gen"))
    binary = desktop / "runtime/python/bin/python3"
    binary.parent.mkdir(parents=True)
    executable(binary, "#!/bin/sh\nexit 0\n")
    library = desktop / "runtime/python/lib/example.so"
    library.parent.mkdir()
    library.write_text("fixture")
    for folder in ("desktop/web-dist", "catalog", "docs/wiki", "bin", "deploy/lib"):
        (tmp_path / folder).mkdir(parents=True)
    (desktop / "web-dist/index.html").write_text("fixture desktop UI")
    (tmp_path / "LICENSE").write_text("fixture license")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "rynhult"\nversion = "1.2.3"\n')
    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text("fixture notices")
    for name in ("main.py", "execution.py", "broker.py", "setup.py", "mac_setup.py", "mac_broker.py",
                 "mac_gate.py", "mac_keychain.py", "mac_launchd.py", "mac_tmux.py"):
        (desktop / name).write_text("# fixture")
    (tmp_path / "bin/acc-hook").write_text("# fixture")
    (tmp_path / "deploy/lib/acc-env.sh").write_text("# fixture")
    (tmp_path / "src/acc").mkdir(parents=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    executable(tools / "uname", '#!/bin/sh\ncase "$1" in -s) echo "${TEST_OS:-Darwin}";; -m) echo "${TEST_ARCH:-arm64}";; esac\n')
    executable(tools / "codesign", """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["TEST_SIGN_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
""")
    executable(tools / "cargo", """#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path.cwd()
stage = root.parent / "package/stage"
assert (stage / "desktop/runtime/python/lib/example.so").is_file()
assert (stage / "docs/wiki").is_dir()
if os.environ.get("TEST_REQUIRE_NOTICES") == "1":
    assert (stage / "docs/LICENSE").read_text() == "fixture license"
    assert (stage / "docs/THIRD_PARTY_NOTICES.md").read_text() == "fixture notices"
assert (stage / "desktop/web-dist/index.html").read_text() == "fixture desktop UI"
assert (stage / "bin/acc-hook").is_file()
for name in ("execution.py", "broker.py", "setup.py", "mac_setup.py", "mac_broker.py",
             "mac_gate.py", "mac_keychain.py", "mac_launchd.py", "mac_tmux.py"):
    assert (stage / "desktop" / name).is_file()
(root.parent / "cargo-call.json").write_text(json.dumps({
    "args": sys.argv[1:], "apple_variables": sorted(k for k in os.environ if k.startswith("APPLE_"))}))
if os.environ.get("TEST_CARGO_FAIL") == "1":
    sys.exit(1)
if os.environ.get("TEST_OS") == "Linux":
    # A real (tiny) .deb with the version Tauri was given, as Tauri names it.
    import subprocess
    version = next(json.loads(a)["version"] for a in sys.argv if a.startswith("{"))
    package = root / "target/fixture-deb"
    (package / "DEBIAN").mkdir(parents=True)
    (package / "DEBIAN/control").write_text(
        f"Package: rynhult-linux-spike\\nVersion: {version}\\nArchitecture: amd64\\nMaintainer: fixture\\nDescription: fixture\\n")
    (package / "usr/bin").mkdir(parents=True)
    (package / "usr/bin/rynhult-desktop-spike").write_text("fixture")
    output = root / f"target/release/bundle/deb/rynhult-linux-spike_{version}_amd64.deb"
    output.parent.mkdir(parents=True)
    subprocess.run(["dpkg-deb", "--root-owner-group", "-b", str(package), str(output)], check=True, capture_output=True)
else:
    (root / "target/release/bundle/macos/Rynhult Mac spike.app").mkdir(parents=True)
""")
    def package(*args, **env):
        if "TEST_PYPROJECT_VERSION" in env:
            (tmp_path / "pyproject.toml").write_text(
                f'[project]\nname = "rynhult"\nversion = "{env["TEST_PYPROJECT_VERSION"]}"\n')
        result = subprocess.run(["sh", str(desktop / "package.sh"), *args], capture_output=True, text=True,
                                env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
                                     "APPLE_SIGNING_IDENTITY": "EXAMPLE", "APPLE_PASSWORD": "EXAMPLE",
                                     "APPLE_API_KEY": "EXAMPLE",
                                     "TEST_SIGN_LOG": str(desktop / "sign-calls.jsonl"), **env})
        call = desktop / "cargo-call.json"
        return result, desktop, json.loads(call.read_text()) if call.exists() else None
    return package


def test_unsigned_mac_package_does_not_use_inherited_signing_credentials_and_cleans_stage(package_run):
    result, desktop, call = package_run("--unsigned", "--app-only")
    assert result.returncode == 0, result.stderr
    assert "--no-sign" in call["args"]
    assert "tauri.macos.conf.json" in call["args"]
    assert call["apple_variables"] == []
    assert not (desktop / "package/stage").exists()


def test_failed_mac_packaging_cleans_copied_wiki_without_deleting_output(package_run):
    result, desktop, _ = package_run("--unsigned", "--app-only", TEST_CARGO_FAIL="1")
    assert result.returncode != 0
    assert not (desktop / "package/stage").exists()


def test_signed_mac_package_refuses_missing_notarization_configuration(package_run):
    result, desktop, call = package_run("--signed")
    assert result.returncode != 0
    assert "notarization" in result.stderr
    assert call is None
    assert not (desktop / "package/stage").exists()


def test_signed_package_signs_nested_staged_code_before_outer_bundling(package_run, tmp_path):
    key = tmp_path / "example-notary-key.p8"
    key.write_text("EXAMPLE, not a real key")
    result, desktop, call = package_run("--signed", "--app-only", APPLE_API_ISSUER="EXAMPLE",
                                        APPLE_API_KEY_PATH=str(key))
    assert result.returncode == 0, result.stderr
    assert "--no-sign" not in call["args"]
    assert "APPLE_PASSWORD" not in call["apple_variables"]
    calls = [json.loads(line) for line in (desktop / "sign-calls.jsonl").read_text().splitlines()]
    signatures = [args for args in calls if "--sign" in args]
    assert len(signatures) == 2  # fixture extension and interpreter, before cargo bundles
    assert all(args[args.index("--sign") + 1] == "EXAMPLE" and "--timestamp" in args for args in signatures)
    assert all(args[-1].startswith(str(desktop / "package/stage/desktop/runtime")) for args in signatures)
    python = next(args for args in signatures if args[-1].endswith("/bin/python3"))
    assert "--options" in python and "runtime" in python and "--entitlements" in python
    assert (desktop / "runtime/python/lib/example.so").read_text() == "fixture"


def locked_requirements(path):
    packages = {}
    for line in path.read_text().replace("\\\n", "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([\w.-]+)==([^\s]+)((?:\s+--hash=sha256:[a-f0-9]{64})+)\s*", line)
        assert match, f"Unpinned/unhashed dependency in {path.name}: {line[:100]}"
        name, version, hashes = match.groups()
        assert name not in packages, f"Duplicate dependency: {name}"
        packages[name] = (version, set(re.findall(r"sha256:([a-f0-9]{64})", hashes)))
    assert packages, "Empty runtime lock"
    return packages


def test_macos_dependencies_are_hash_locked_to_tested_linux_versions():
    linux = locked_requirements(DESKTOP / "requirements-linux.lock")
    mac = locked_requirements(DESKTOP / "requirements-macos-arm64.lock")
    assert {name: value[0] for name, value in mac.items()} == {name: linux[name][0] for name in mac}
    locked_requirements(DESKTOP / "requirements-build.lock")


@pytest.mark.skipif(os.environ.get("DESKTOP_MAC_LOCK_CHECK") != "1",
                    reason="Opt-in public artifact download/hash check; no installation")
def test_macos_lock_has_verified_arm64_wheels_and_only_the_documented_sdist():
    import hashlib
    import itertools
    import urllib.request

    from packaging.tags import compatible_tags, cpython_tags, mac_platforms
    from packaging.utils import parse_wheel_filename

    platforms = list(mac_platforms((13, 0), "arm64"))
    supported = list(itertools.chain(cpython_tags((3, 12), platforms=platforms),
                                    compatible_tags((3, 12), interpreter="cp312", platforms=platforms)))
    rank = {tag: index for index, tag in enumerate(supported)}
    evidence = []
    requirements = list(locked_requirements(DESKTOP / "requirements-macos-arm64.lock").items())
    requirements += list(locked_requirements(DESKTOP / "requirements-build.lock").items())
    for name, (version, hashes) in requirements:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
            files = json.load(response)["urls"]
        candidates = []
        for file in files:
            if file["packagetype"] != "bdist_wheel":
                continue
            tags = parse_wheel_filename(file["filename"])[3]
            matches = tags & rank.keys()
            if matches:
                candidates.append((min(rank[tag] for tag in matches), file))
        if candidates:
            selected = min(candidates, key=lambda item: item[0])[1]
        else:
            assert name == "http-ece", f"No macOS 13 arm64 wheel for {name}=={version}"
            selected = next(file for file in files if file["packagetype"] == "sdist")
        assert selected["digests"]["sha256"] in hashes, selected["filename"]
        digest = hashlib.sha256()
        with urllib.request.urlopen(selected["url"], timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
        assert digest.hexdigest() in hashes, f"Artifact hash mismatch: {selected['filename']}"
        evidence.append({"name": name, "version": version, "file": selected["filename"],
                         "kind": selected["packagetype"], "sha256": digest.hexdigest()})
    output = DESKTOP.parent / ".acc/artifacts"
    output.mkdir(parents=True, exist_ok=True)
    (output / "desktop-macos-lock-artifacts.json").write_text(json.dumps(evidence, indent=2) + "\n")


@pytest.mark.parametrize('system,arch', [('Linux', 'x86_64'), ('Darwin', 'arm64')])
def test_packages_stage_license_and_notices_before_bundling(package_run, system, arch):
    args = ('--app-only',) if system == 'Darwin' else ()
    result, _, call = package_run(*args, TEST_OS=system, TEST_ARCH=arch, TEST_REQUIRE_NOTICES='1')
    assert result.returncode == 0, result.stderr
    assert call is not None


def test_linux_packages_have_a_menu_category():
    """Without one, Tauri writes an empty Categories= and menus file Rynhult under Other (rc package check)."""
    config = json.loads((DESKTOP / "src-tauri/tauri.package.linux.conf.json").read_text())
    assert config["bundle"]["category"] == "DeveloperTool"  # Categories=Development; in the .desktop file
    assert "{{categories}}" in (DESKTOP / "src-tauri/linux/rynhult.desktop").read_text()


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="needs dpkg-deb")
@pytest.mark.parametrize("project,deb", [("1.2.3", "1.2.3"), ("0.15.0-rc.1", "0.15.0~rc.1")])
def test_linux_package_takes_the_project_version_and_sorts_candidates_before_the_release(package_run, project, deb):
    """Not tauri.conf.json's 0.0.0. Debian reads 1.0.0-rc.1 as revision rc.1 of 1.0.0, i.e. newer than 1.0.0."""
    result, desktop, call = package_run(TEST_OS="Linux", TEST_ARCH="x86_64", TEST_PYPROJECT_VERSION=project)
    assert result.returncode == 0, result.stderr
    assert {"version": project} in [json.loads(a) for a in call["args"] if a.startswith("{")]
    built = list((desktop / "src-tauri/target/release/bundle/deb").glob("*.deb"))
    assert [p.name for p in built] == [f"rynhult-linux-spike_{deb}_amd64.deb"]
    fields = subprocess.run(["dpkg-deb", "-f", str(built[0]), "Version"], capture_output=True, text=True, check=True)
    assert fields.stdout.strip() == deb
    listing = subprocess.run(["dpkg-deb", "-c", str(built[0])], capture_output=True, text=True, check=True).stdout
    assert " root/root " in listing and "usr/bin/rynhult-desktop-spike" in listing
    if deb != project:
        older = subprocess.run(["dpkg", "--compare-versions", deb, "lt", "0.15.0"])
        assert older.returncode == 0
        assert subprocess.run(["dpkg", "--compare-versions", "0.14.0", "lt", deb]).returncode == 0


def test_linux_package_refuses_a_version_that_is_not_semver(package_run):
    result, _, call = package_run(TEST_OS="Linux", TEST_ARCH="x86_64", TEST_PYPROJECT_VERSION="1.0.0rc1")
    assert result.returncode != 0 and "not major.minor.patch" in result.stderr and call is None


def test_linux_copyright_file_is_the_projects_mit_license():
    copyright_text = (DESKTOP / "src-tauri/linux/copyright").read_text()
    license_lines = (DESKTOP.parent / "LICENSE").read_text().splitlines()
    assert copyright_text.startswith("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/")
    assert f"Copyright: {license_lines[2].removeprefix('Copyright (c) ')}" in copyright_text
    body = [line[1:] if line != " ." else "" for line in copyright_text.split("License: Expat\n", 2)[2].splitlines()]
    assert "\n".join(body).strip() == "\n".join(license_lines[4:]).strip()
    linux = json.loads((DESKTOP / "src-tauri/tauri.package.linux.conf.json").read_text())
    assert linux["bundle"]["linux"]["deb"]["files"] == {"/usr/share/doc/rynhult-linux-spike/copyright": "linux/copyright"}


def test_maintainer_is_a_marked_placeholder_and_identifier_is_the_project_domain():
    linux = json.loads((DESKTOP / "src-tauri/tauri.package.linux.conf.json").read_text())
    assert linux["bundle"]["publisher"].startswith("MAINTAINER PLACEHOLDER") and "example.invalid" in linux["bundle"]["publisher"]
    assert "MAINTAINER PLACEHOLDER" in (DESKTOP / "arch/PKGBUILD").read_text()
    assert "MAINTAINER PLACEHOLDER" in (DESKTOP / "rpm/rynhult-desktop.spec").read_text()
    assert json.loads((DESKTOP / "src-tauri/tauri.conf.json").read_text())["identifier"] == "dev.rynhult.desktop"
    assert "libayatana-appindicator" not in (DESKTOP / "arch/PKGBUILD").read_text()  # no tray icon
