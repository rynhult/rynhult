"""Section G: catalog policies survive loading and provider compilation, without live agents."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from acc.broker import ops
from acc.server.api.schemas import SessionIn
from acc.server.catalog import load
from acc.server.compiler import CompileError, LaunchInputs, compile_files

ROOT = Path(__file__).resolve().parents[1] / "catalog"
CATALOG = load(ROOT)
HOOK, STATUS = "/test/acc-hook", "/test/acc-statusline"
PROVIDERS = ("claude", "codex", "copilot", "antigravity", "cursor", "opencode")
NEW_SKILLS = ("product-discovery", "visual-references", "evidence-research", "migration-recovery")


def skill_body(skill: str, provider: str = "claude") -> str:
    files = compile_files(LaunchInputs(provider=provider, skills=(skill,)), HOOK, STATUS, catalog=CATALOG).files
    if provider == "codex":
        overrides = json.loads(files["codex-overrides.json"])
        hidden = next(value for value in overrides if value.startswith("skills.config="))
        assert f"/acc-{skill}/SKILL.md" not in hidden
        assert "/acc-frontend-design/SKILL.md" in hidden
        return (ROOT / "skills" / skill / "SKILL.md").read_text()
    return next(content for name, content in files.items() if name.endswith(f"/{skill}/SKILL.md"))


def agent_body(agent: str) -> str:
    files = compile_files(LaunchInputs(provider="claude", agents=(agent,)), HOOK, STATUS, catalog=CATALOG).files
    return files[f"plugin/agents/{agent}.md"]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("skill", NEW_SKILLS)
def test_new_procedures_are_delivered_without_extra_tools(provider, skill):
    body = skill_body(skill, provider)
    assert skill in body and len(body.splitlines()) > 10
    assert CATALOG.skills[skill].meta["license"] == "MIT"
    assert set(CATALOG.mcps) == {"github", "playwright", "mslearn"}


def test_codex_links_new_procedures_and_removes_retired_council(env):
    home = Path(env["home"])
    links = home / ".codex/skills"
    links.mkdir(parents=True)
    (links / "acc-council").symlink_to(ROOT / "skills/council")
    config = dataclasses.replace(env["broker"], catalog_dir=ROOT)
    wanted = [skill for skill, item in CATALOG.skills.items() if "codex" in item.providers]
    result = ops.sync_codex_skills(config, wanted)
    assert "acc-council" in result["removed"]
    assert not (links / "acc-council").is_symlink()
    for skill in (*NEW_SKILLS, "acc-workflow"):
        delivered = links / f"acc-{skill}/SKILL.md"
        assert delivered.resolve() == ROOT / "skills" / skill / "SKILL.md"
        assert skill in delivered.read_text()


def test_tdd_worker_contract_does_not_require_an_unmanaged_full_suite():
    body = skill_body("test-driven-development")
    assert "Standalone sessions" in body
    assert "Team workers" in body and "related checks" in body
    assert "gate owns the full suite" in body
    assert "full suite not run" in body
    assert "A scope statement in your task bounds the" not in body


def test_writing_plans_uses_versioned_rynhult_tools_not_missing_skills():
    body = skill_body("writing-plans")
    for contract in ("plan_update", "R-n", "C-n", "blocks", "prepare", "build", "report"):
        assert contract in body
    assert "superpowers:" not in body and "docs/superpowers" not in body
    assert "obra/superpowers" in body and (ROOT / "skills/writing-plans/LICENSE").is_file()


def test_e2e_runner_uses_installed_playwright_and_reports_flakes():
    body = agent_body("e2e-runner")
    assert "PLAYWRIGHT_BROWSERS_PATH" in body
    assert "Report flakes" in body and "original failure" in body
    for forbidden in ("agent-browser", "npm install -g", "test.fixme(", "test.skip("):
        assert forbidden not in body
    assert "LICENSE-ECC" in body


def test_security_reviewer_reports_without_remediation_or_installation():
    body = agent_body("security-reviewer")
    for contract in ("report-only", "findings_md", "reviewed head", "gitleaks", "osv-scanner", "when installed"):
        assert contract in body
    assert "Do not fix" in body and "separate assignment" in body
    assert "Rotate secrets" not in body and "npx eslint" not in body
    assert "LICENSE-ECC" in body


@pytest.mark.parametrize("provider", PROVIDERS)
def test_legacy_council_cannot_be_selected_but_its_license_is_kept(provider):
    assert "council" not in CATALOG.skills
    assert all("council" not in profile["skills"] for profile in CATALOG.profiles.values())
    with pytest.raises(CompileError, match="unknown skill"):
        compile_files(LaunchInputs(provider=provider, skills=("council",)), HOOK, STATUS, catalog=CATALOG)
    retired = next(item for item in CATALOG.deferred if item["id"] == "council")
    assert "76aaa77df6adc5b889e689007bf99b9a4f74af34" in retired["source"]
    for license_path in ("skills/council/LICENSE", "agents/LICENSE-ECC"):
        assert "Copyright (c) 2026 Affaan Mustafa" in (ROOT / license_path).read_text()


def test_database_review_is_conditional_on_the_actual_engine():
    body = agent_body("database-reviewer")
    for contract in ("SQLite", "PostgreSQL", "Supabase", "only when", "restore", "read-only"):
        assert contract in body
    assert "Always, no exceptions" not in body
    assert "Supabase team" in body and "LICENSE-ECC" in body


@pytest.mark.parametrize("provider", PROVIDERS)
def test_optional_workflow_preserves_provider_delivery_without_role_git_rules(provider):
    body = skill_body("acc-workflow", provider)
    assert "copilot" in CATALOG.skills["acc-workflow"].meta["providers"]
    assert "project conventions" in body and "existing documentation layout" in body
    assert "gh pr create" not in body and "create the skeleton" not in body
    assert "One file per session" not in body


@pytest.mark.parametrize("provider", ("claude", "codex", "copilot"))
def test_code_reviewer_compiles_with_read_only_defaults_and_report_contract(provider):
    inputs = SessionIn(provider=provider, name="Review", profile="code-reviewer").inputs(CATALOG)
    assert CATALOG.profiles["code-reviewer"]["agents"] == ["code-reviewer"]
    if provider == "claude":
        assert inputs.permission_mode == "plan" and inputs.agents == ("code-reviewer",)
    elif provider == "codex":
        assert inputs.sandbox == "read-only"
    else:
        assert inputs.options == {"mode": "plan", "permissions": "ask"}
    files = compile_files(inputs, HOOK, STATUS, catalog=CATALOG).files
    assert any("report" in text and "reviewed head" in text and "Do not fix" in text for text in files.values())


def test_coordinator_guidance_cannot_supply_grant_authority_or_tools():
    profile = CATALOG.profiles["coordinator"]
    assert {"writing-plans", "product-discovery", "visual-references"} <= set(profile["skills"])
    inputs = SessionIn(provider="claude", name="Coordinate", profile="coordinator", permission_mode="plan").inputs(CATALOG)
    assert inputs.permission_mode == "plan" and not inputs.mcps
    body = compile_files(inputs, HOOK, STATUS, catalog=CATALOG).files["append-prompt.md"]
    for contract in ("grant", "assign", "select", "stopped descendants", "continuation pack", "temporary", "need.role",
                     "previous attempt's head", "requirements and constraints", "full earlier evidence"):
        assert contract in body


def test_researcher_and_read_only_advisers_use_reports_instead_of_forced_file_writes():
    assert "evidence-research" in CATALOG.profiles["researcher"]["skills"]
    assert "writing-plans" not in CATALOG.profiles["researcher"]["skills"]
    for profile in ("researcher", "architect", "council"):
        text = CATALOG.profiles[profile]["instructions"]
        assert "report" in text and "read-only" in text
    assert "findings_md" in skill_body("evidence-research")


def test_council_requires_managed_capacity_or_honest_sequential_perspectives():
    body = skill_body("acc-council")
    for contract in ("managed helpers", "concurrency", "single-session perspectives", "not independent",
                     "cannot cap", "findings_md"):
        assert contract in body
    assert "launch all seven in parallel" not in body
    assert "open a PR" not in body


@pytest.mark.parametrize("skill, contracts", [
    ("product-discovery", ("audience", "journeys", "success", "blocks", "already supplied", "R-n", "C-n")),
    ("visual-references", ("REF-n", "annotations", "observation", "requirement", "unreadable", "private")),
    ("evidence-research", ("primary sources", "date", "confidence", "counterevidence")),
    ("migration-recovery", ("engine", "backup", "restore", "rollback", "forward fix")),
])
def test_new_procedures_keep_the_spec_safety_boundaries(skill, contracts):
    body = skill_body(skill)
    for contract in contracts:
        assert contract in body


def test_a_local_profile_listing_the_retired_council_still_loads_after_upgrade(tmp_path, caplog):
    import json
    from pathlib import Path

    from acc.server.catalog import load
    root = Path(__file__).resolve().parents[1] / 'catalog'
    (tmp_path / 'local.json').write_text(json.dumps({'profiles': {
        'my-council': {'id': 'my-council', 'name': 'Mine', 'description': 'An owner profile from 0.14',
                       'skills': ['council', 'acc-council']},
        'council': {'id': 'council', 'add': {'skills': ['council']}}}}))
    catalog = load(root, tmp_path)
    assert catalog.profiles['my-council']['skills'] == ['acc-council']
    assert 'council' not in catalog.profiles['council']['skills']
    assert 'retired skills' in caplog.text
