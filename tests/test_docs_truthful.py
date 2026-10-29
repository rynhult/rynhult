"""The README and the Guide's front pages match what ships (UX review 2, beta item 4)."""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DECISION_SCRIPT = ROOT / "scripts/next_decision_number.py"
README = (ROOT / "README.md").read_text()
HOME = (ROOT / "docs/wiki/Home.md").read_text()
GUIDE = ROOT / "docs/wiki/User-Guide"


def test_decision_record_numbers_are_unique():
    helper = decision_helper()
    records = defaultdict(list)
    for path in sorted((ROOT / "docs/decisions").glob("*.md")):
        relative = path.relative_to(ROOT).as_posix()
        if (number := helper.decision_number(relative)) is not None:
            records[number].append(relative)
    duplicates = [f"{number:04d}: {', '.join(paths)}" for number, paths in sorted(records.items()) if len(paths) > 1]
    if duplicates:
        number = helper.next_decision_number(ROOT)
        pytest.fail(
            "Duplicate decision record numbers:\n" + "\n".join(duplicates)
            + f"\nNext free number: {number:04d}. Run: python scripts/next_decision_number.py"
        )


def decision_helper():
    assert DECISION_SCRIPT.is_file(), "The branch-aware decision numbering helper is missing"
    spec = importlib.util.spec_from_file_location("next_decision_number", DECISION_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decision_git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "user.name=Alex Example", "-c", "user.email=alex@example.com", *args],
        text=True,
    ).strip()


@pytest.fixture
def decision_repo(tmp_path):
    decision_git(tmp_path, "init", "-q", "-b", "main")
    records = tmp_path / "docs/decisions"
    records.mkdir(parents=True)
    (records / "0001-original.md").write_text("# Original\n")
    decision_git(tmp_path, "add", ".")
    decision_git(tmp_path, "commit", "-qm", "First decision")
    return tmp_path


def test_next_decision_number_reads_other_branches_without_changing_checkout(decision_repo):
    root = decision_repo
    decision_git(root, "switch", "-qc", "parallel")
    (root / "docs/decisions/0031-parallel.md").write_text("# Parallel\n")
    decision_git(root, "add", ".")
    decision_git(root, "commit", "-qm", "Parallel decision")
    decision_git(root, "switch", "-q", "main")
    decision_git(root, "switch", "-qc", "another")
    (root / "docs/decisions/0042-another decision.md").write_text("# Another\n")
    (root / "docs/decisions/drafts").mkdir()
    (root / "docs/decisions/drafts/9999-not-a-record.md").write_text("# Draft\n")
    decision_git(root, "add", ".")
    decision_git(root, "commit", "-qm", "Another decision")
    decision_git(root, "switch", "-q", "main")
    before_refs = decision_git(root, "for-each-ref", "--format=%(refname) %(objectname)")
    before_index = (root / ".git/index").read_bytes()
    helper = decision_helper()
    assert helper.next_decision_number(root) == 43
    (root / "docs/decisions/0044-uncommitted.md").write_text("# Pending\n")
    assert helper.next_decision_number(root) == 45
    assert decision_git(root, "branch", "--show-current") == "main"
    assert decision_git(root, "for-each-ref", "--format=%(refname) %(objectname)") == before_refs
    assert (root / ".git/index").read_bytes() == before_index


def test_next_decision_cli_handles_an_unborn_repository_from_a_subdirectory(tmp_path):
    helper = decision_helper()
    decision_git(tmp_path, "init", "-q", "-b", "main")
    assert helper.next_decision_number(tmp_path) == 1
    script = tmp_path / "scripts/next_decision_number.py"
    script.parent.mkdir()
    shutil.copyfile(DECISION_SCRIPT, script)
    result = subprocess.run([sys.executable, str(script)], cwd=script.parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0001\n"
    (tmp_path / "docs/decisions").mkdir(parents=True)
    (tmp_path / "docs/decisions/9999-last.md").write_text("# Last\n")
    with pytest.raises(ValueError, match="exhausted"):
        helper.next_decision_number(tmp_path)


def test_next_decision_number_fails_instead_of_guessing_outside_git(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        decision_helper().next_decision_number(tmp_path)


def test_duplicate_decision_diagnostic_names_files_and_free_number(decision_repo, monkeypatch):
    root = decision_repo
    decision_git(root, "switch", "-qc", "parallel")
    (root / "docs/decisions/0031-parallel.md").write_text("# Parallel\n")
    decision_git(root, "add", ".")
    decision_git(root, "commit", "-qm", "Parallel decision")
    decision_git(root, "switch", "-q", "main")
    for name in ("0001-collision.md", "0002-first.md", "0002-second.md"):
        (root / "docs/decisions" / name).write_text("# Decision\n")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)
    with pytest.raises(pytest.fail.Exception) as error:
        test_decision_record_numbers_are_unique()
    for name in ("0001-original.md", "0001-collision.md", "0002-first.md", "0002-second.md"):
        assert f"docs/decisions/{name}" in str(error.value)
    assert "0032" in str(error.value)
    assert "scripts/next_decision_number.py" in str(error.value)


def test_release_framing_is_an_unpublished_release_candidate():
    assert "**Release candidate.**" in README and "not a stable release" in README
    assert "publication" in README and "owner" in README
    assert "release candidate" in HOME and "not a stable release" in HOME


def test_no_claims_the_product_has_outgrown():
    for text in (README, HOME):
        for stale in ("Supervision, not orchestration", "no autonomous orchestration", "on one machine",
                      "Needs you, Active and Idle", "Backups (daily, 14 days)", "single host",
                      "designed but not released yet", "gated until phase 3", "iOS, then Android"):
            assert stale not in text, stale
    # Group sessions shipped (phase 4, #325); native-app order is not chosen yet (decision 0073).
    assert "Orchestrator" in README and "**Groups**" in README and "group sessions" in README
    assert all("order is not chosen yet" in " ".join(text.split()) for text in (README, HOME))
    assert "48 hourly, 30 daily and 12 monthly" in README  # the Host backups card's retention
    assert "Several agents" in README and "Schedules" in README


def test_guide_order_follows_a_new_owners_path():
    order = (GUIDE / ".order").read_text().split()
    path = ["Install", "Getting-Started", "Glossary", "Projects", "New-Session", "Connect-Your-Phone", "Backups", "Nodes",
            "Workspaces", "Settings", "Remote-Access"]  # install, first project and session, phone, backups, machines, everyday, admin, advanced
    assert [order.index(name) for name in path] == sorted(order.index(name) for name in path)
    assert order.index("Desktop-Apps") > order.index("Remote-Access")  # experimental platforms are advanced, not first steps
    assert sorted(order) == sorted(p.stem for p in GUIDE.glob("*.md"))
    index = (ROOT / "docs/wiki/User-Guide.md").read_text()
    assert all(f"(/User-Guide/{name})" in index for name in order)


def test_glossary_defines_the_first_run_words_and_every_link_to_it_lands():
    glossary = (GUIDE / "Glossary.md").read_text()
    # The Guide's slugs (Wiki.tsx): lower case, punctuation dropped, spaces to dashes.
    anchors = {re.sub(r"[^\w -]", "", line[3:].strip().lower()).replace(" ", "-")
               for line in glossary.splitlines() if line.startswith("## ")}
    assert {"session", "project", "main", "machine-node", "profile", "provider-account", "agent-account", "worktree", "preview",
            "inbox", "receiver", "passkey", "remote-access", "orchestrator-lead-and-helpers"} <= anchors
    links = [(page.stem, anchor) for page in GUIDE.glob("*.md")
             for anchor in re.findall(r"\(/User-Guide/Glossary#([^)]+)\)", page.read_text())]
    assert len({page for page, _ in links}) > 40  # first uses are linked across the Guide
    assert [link for link in links if link[1] not in anchors] == []
    assert "](/User-Guide/Glossary#agent-account)" in (GUIDE / "Getting-Started.md").read_text()


def test_guard_guide_does_not_claim_checker_errors_allow_the_command():
    guardrails = (GUIDE / 'Guardrails.md').read_text()
    assert 'one the checker fails on is allowed' not in guardrails
    assert guardrails.count('They read the command\'s text, so they can\'t see') == 1
