"""Pinned local image supply-chain contract."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_image_dependencies_are_exact_and_integrity_locked():
    import json
    import re

    folder = ROOT / 'deploy/containers/session'
    manifest = json.loads((folder / 'package.json').read_text())
    lock = json.loads((folder / 'package-lock.json').read_text())
    for version in manifest['dependencies'].values():
        assert re.fullmatch(r'\d+\.\d+\.\d+', version)
    for path, package in lock['packages'].items():
        if path:
            assert package['integrity'].startswith('sha512-')
            assert package['resolved'].startswith('https://registry.npmjs.org/')
    containerfile = (ROOT / 'deploy/containers/Containerfile').read_text()
    assert re.search(r'FROM \S+@sha256:[0-9a-f]{64}', containerfile)
    assert 'npm ci --ignore-scripts' in containerfile
    assert 'sha256sum -c' in containerfile



def test_image_supplies_integrity_pinned_provider_registry_dependency():
    import re

    requirements = (ROOT / 'deploy/containers/session/requirements.txt').read_text()
    assert re.fullmatch(r'PyYAML==6\.0\.3 --hash=sha256:[0-9a-f]{64}\n', requirements)
    recipe = (ROOT / 'deploy/containers/Containerfile').read_text()
    assert '--require-hashes' in recipe and '--only-binary=:all:' in recipe
    assert 'session/requirements.txt' in recipe


def final_stage(recipe: str) -> list[str]:
    """The final stage's instructions, continuation lines joined, comments dropped."""
    joined = re.sub(r'\\\n', ' ', recipe)
    lines = [line.strip() for line in joined.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    start = max(i for i, line in enumerate(lines) if line.upper().startswith('FROM '))
    return lines[start:]


def test_the_final_stage_never_runs_anything():
    """Qualification attempt 10: a RUN in the final stage committed run/.wh..containerenv (Podman mounts its own
    /run/.containerenv, /etc/hosts, /etc/hostname and /etc/resolv.conf during RUN), deleting a read-only mount target."""
    recipe = (ROOT / 'deploy/containers/Containerfile').read_text()
    stage = final_stage(recipe)
    assert stage[0] == 'FROM scratch'
    keywords = [line.split()[0].upper() for line in stage]
    assert set(keywords) <= {'FROM', 'COPY', 'ENV', 'USER', 'WORKDIR', 'ENTRYPOINT', 'CMD'}, keywords
    assert keywords.count('FROM') == 1 and 'RUN' not in keywords
    assert stage[1] == 'COPY --from=assembled /runtime/ /'
    # Besides the runtime tree, only the unchanged file that makes the build run the checked stage.
    assert stage[2] == 'COPY --from=checked /opt/acc/rootfs-size.json /opt/acc/rootfs-size.json'
    assert [line for line in stage if line.upper().startswith('COPY')] == stage[1:3]
    # The checks that ran there run in the assembled stage, against /runtime, after it is assembled.
    assembled = recipe[:recipe.index('FROM scratch')]
    assert assembled.index('slim_rootfs.py /runtime') < assembled.index('check_rootfs.py /runtime')


def test_the_image_is_checked_as_it_will_run_in_a_stage_the_build_cannot_skip():
    """Review of 73cc94e7 (M2): check_rootfs.py runs the runtime's files with the build stage's loader, libraries,
    interpreters and absolute paths (the Codex wrapper runs the build stage's Python, policy and binary), so a runtime
    missing a library or file they load would pass. The old final-stage checks ran in the image itself; they still do,
    in a stage of their own whose one unchanged file the final stage copies (Podman skips stages nothing uses)."""
    recipe = (ROOT / 'deploy/containers/Containerfile').read_text()
    joined = re.sub(r'\\\n', ' ', recipe)
    lines = [line.strip() for line in joined.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    start = lines.index('FROM scratch AS checked')
    end = next(i for i in range(start + 1, len(lines)) if lines[i].upper().startswith('FROM '))
    stage = lines[start:end]
    assert stage[1] == 'COPY --from=assembled /runtime/ /'
    assert [line for line in stage if line.startswith('ENV')] == [line for line in final_stage(recipe) if line.startswith('ENV')]
    run = ' '.join(line for line in stage if line.startswith('RUN '))
    for command in ('/usr/local/bin/claude --version', '/usr/local/bin/codex --version', '/usr/local/bin/copilot --version',
                    '/usr/bin/git --version', 'from acc.providers import get', 'state-directories.json'):
        assert command in run, command
    assert 'COPY --from=checked ' in ' '.join(final_stage(recipe))


def test_image_contains_fixed_integration_runtime_and_request_policy(tmp_path):
    recipe = (ROOT / 'deploy/containers/Containerfile').read_text()
    slim = (ROOT / 'deploy/containers/slim_rootfs.py').read_text()
    assert 'COPY src/acc/tooling.py /usr/local/bin/acc-tool-policy.py' in recipe
    assert "'acc-tool-policy.py'" in slim
    assert 'deploy/containers/session_runtime.py' in recipe and 'deploy/containers/egress_bridge.py' in recipe
    assert 'COPY src/acc/container_egress.py /opt/acc/runtime/egress.py' in recipe
    # The adapter mount targets are read-only placeholders in every image, and archive-verified mount targets.
    spec = importlib.util.spec_from_file_location('slim_rootfs', ROOT / 'deploy/containers/slim_rootfs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.mount_targets(tmp_path)
    for name in ('run/acc-credentials/auth.json', 'run/acc-egress/socket'):
        assert name in module.MOUNT_FILES and (tmp_path / name).read_text() == '{}\n'
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o444
        assert (tmp_path / name).parent.stat().st_mode & 0o777 == 0o555
