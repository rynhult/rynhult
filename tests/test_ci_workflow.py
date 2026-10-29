"""The contributor checks keep the browser cases that need production helpers absent (security review 7, SR7-9)."""
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/checks.yml"


def test_ci_runs_the_remote_access_cases_with_both_test_helpers_absent():
    text = WORKFLOW.read_text()
    step = text[text.index("- name: Remote access with both test helpers absent"):]
    step = step[:step.find("\n      - name:", 1)] if "\n      - name:" in step[1:] else step
    assert "ACC_E2E_DEVICE_CA: '0'" in step and "ACC_E2E_REMOTE_EDGE: '0'" in step
    assert "e2e/63-device-pairing.spec.ts" in step
    for case in ("public status exposes review", "remote access stays off"):
        assert case in step
    spec = (WORKFLOW.parents[2] / "web/e2e/63-device-pairing.spec.ts").read_text()
    assert "test(\"remote access stays off" in spec or "test('remote access stays off" in spec
    assert "public status exposes review" in spec
