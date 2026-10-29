"""Guard the migration chain and allocation across parallel local branches."""
import re
import runpy
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util.exc import CommandError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/next_migration_number.py"
VERSIONS = Path("src/acc/server/migrations/versions")
# Intentionally skipped numbers need a nonempty explanation here.
# The entries below are permanently skipped (coordinator
# registry, 2026-10-26: 0067 mobile, 0068 remote-access backup forwarder, 0069-0072 orchestrator v2, next free 0073);
# they were reserved once and their work took other numbers or none, so they stay unused for good.
DOCUMENTED_GAPS: dict[int, str] = {
    55: "skipped: SCIM and suspension's old reservation (parked; takes a new number when replayed)",
    56: "skipped: brokered SAML's old reservation (parked; takes a new number when replayed)",
    57: "skipped: public git backup consent moved to 0068 (remote-access backup forwarder)",
    58: "skipped: orchestrator v2 slice 1 moved to 0069",
    59: "skipped: dependency automation needed no migration (its decision is on main, #333)",
    60: "skipped: orchestrator v2 slice 3 moved to 0070",
    61: "skipped: orchestrator v2 slice 4 moved to 0071",
    62: "skipped: orchestrator v2 slice 5 moved to 0072",
    65: "skipped: organization roles' old reservation (parked; takes a new number when replayed)",
}
declared_revision = runpy.run_path(str(SCRIPT))["declared_revision"]


def check_migration_graph(repo, documented_gaps=None):
    errors = []
    file_numbers, revisions = defaultdict(list), defaultdict(list)
    for path in sorted((repo / VERSIONS).glob("*.py")):
        if path.name == "__init__.py":
            continue
        match = re.fullmatch(r"([0-9]{4,})_.+\.py", path.name)
        if match:
            file_numbers[int(match[1])].append(path.name)
        else:
            errors.append(f"{path.name}: expected a zero-padded numbered migration filename")
        try:
            revision = declared_revision(path.read_text(encoding="utf-8"), str(path))
        except (SyntaxError, ValueError) as error:
            errors.append(f"{path.name}: {error}")
            continue
        number = int(revision)
        revisions[number].append(path.name)
        if number < 1 or (match and revision != match[1]):
            errors.append(f"{path.name}: revision {revision} must match its positive filename number")
    for label, groups in (("number", file_numbers), ("revision", revisions)):
        for number, paths in sorted(groups.items()):
            if len(paths) > 1:
                errors.append(f"Duplicate {label} {number:04d}: {', '.join(paths)}")
    missing = set(range(1, max(revisions, default=0) + 1)) - revisions.keys()
    explained = {number for number, reason in (documented_gaps or {}).items() if reason.strip()}
    if missing - explained:
        errors.append("Undocumented migration number gaps: " + ", ".join(f"{n:04d}" for n in sorted(missing - explained)))
    if explained - missing:
        errors.append("Documented gaps are not missing: " + ", ".join(f"{n:04d}" for n in sorted(explained - missing)))
    if not errors:
        scripts = ScriptDirectory(str(repo / VERSIONS.parent))
        try:
            heads = scripts.get_heads()
            list(scripts.walk_revisions())
            if len(heads) != 1:
                files = [f"{head} ({Path(scripts.get_revision(head).path).name})" for head in heads]
                errors.append(f"Expected exactly one head; found {len(heads)}: {', '.join(files) or '(none)'}")
        except (CommandError, RevisionError, KeyError) as error:
            errors.append(f"Invalid Alembic migration graph: {error}")
    if errors:
        suggestion = run_number(repo)
        if suggestion.returncode == 0:
            errors.append("Next free migration number across local branches and worktree: " + suggestion.stdout.strip())
        else:
            errors.append("Next free migration number unavailable: " + suggestion.stderr.strip())
        raise AssertionError("\n".join(errors))


def test_migration_graph_has_one_head_and_unique_gapless_or_documented_numbers():
    check_migration_graph(ROOT, DOCUMENTED_GAPS)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def write_revision(repo, number, parent=None, *, name="change", revision=None):
    path = repo / VERSIONS / f"{number}_{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"revision = {revision or number!r}\ndown_revision = {parent!r}\n")
    return path


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Migration Tests")
    git(tmp_path, "config", "user.email", "migration-tests@example.com")
    write_revision(tmp_path, "0001")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial migration")
    return tmp_path


def run_number(repo):
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, capture_output=True, text=True)


def git_state(repo):
    return (git(repo, "symbolic-ref", "HEAD"), git(repo, "status", "--porcelain"), git(repo, "show-ref"))


def test_next_number_includes_main_other_local_branches_and_uncommitted_files(repository):
    repo = repository
    git(repo, "switch", "-c", "parallel")
    write_revision(repo, "0004", "0001")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Parallel migration")
    git(repo, "switch", "main")
    git(repo, "switch", "-c", "work")
    write_revision(repo, "0002", "0001")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Work migration")
    result = run_number(repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0005\n"
    git(repo, "switch", "main")
    write_revision(repo, "0006", "0001")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Main advanced")
    git(repo, "switch", "work")
    before = git_state(repo)
    result = run_number(repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0007\n"
    assert git_state(repo) == before
    write_revision(repo, "0008", "0002")
    before = git_state(repo)
    result = run_number(repo / "src")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0009\n"
    assert git_state(repo) == before


def test_next_number_reserves_declared_revision_even_when_filename_is_wrong(repository):
    repo = repository
    git(repo, "switch", "-c", "misnumbered")
    path = write_revision(repo, "0002", "0001", revision="0012")
    path.write_text(path.read_text().replace("revision =", "revision: str =", 1)
                    + "raise RuntimeError('Migration source must not be executed')\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Mismatched revision awaiting correction")
    git(repo, "switch", "main")
    result = run_number(repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0013\n"


def test_next_number_reports_git_failure_without_a_success_number(tmp_path):
    result = run_number(tmp_path)
    assert result.returncode != 0
    assert not result.stdout
    assert "not a git repository" in result.stderr.lower()


@pytest.mark.parametrize("metadata", [
    "revision = '0002'\nrevision = '0012'\n",
    "revision = '00' + '02'\n",
])
def test_next_number_refuses_ambiguous_metadata_instead_of_guessing(repository, metadata):
    path = write_revision(repository, "0002", "0001")
    path.write_text(metadata + "down_revision = '0001'\n")
    result = run_number(repository)
    assert result.returncode != 0
    assert not result.stdout
    assert path.name in result.stderr


@pytest.mark.parametrize("problem,expected", [
    ("duplicate_revision", ["revision 0002", "0002_first.py", "0003_second.py"]),
    ("duplicate_filename_number", ["number 0002", "0002_first.py", "0002_second.py"]),
    ("multiple_heads", ["exactly one head", "0002_first.py", "0003_second.py"]),
    ("undocumented_gap", ["Undocumented", "0002"]),
    ("empty_gap_reason", ["Undocumented", "0002"]),
    ("empty_chain", ["exactly one head", "found 0"]),
])
def test_graph_guard_explains_conflicts_and_suggests_next_free_number(repository, problem, expected):
    repo = repository
    gaps = {}
    if problem == "empty_chain":
        (repo / VERSIONS / "0001_change.py").unlink()
    elif problem in {"undocumented_gap", "empty_gap_reason"}:
        write_revision(repo, "0003", "0001")
        if problem == "empty_gap_reason":
            gaps[2] = " "
    else:
        write_revision(repo, "0002", "0001", name="first")
        write_revision(repo, "0002" if problem == "duplicate_filename_number" else "0003",
                       "0001" if problem != "duplicate_filename_number" else "0002", name="second",
                       revision="0002" if problem == "duplicate_revision" else "0003")
    with pytest.raises(AssertionError) as failure:
        check_migration_graph(repo, gaps)
    message = str(failure.value)
    for detail in expected:
        assert detail in message
    next_free = "0002" if problem == "empty_chain" else "0004"
    assert f"Next free migration number across local branches and worktree: {next_free}" in message


def test_graph_guard_accepts_an_explicitly_documented_gap(repository):
    write_revision(repository, "0003", "0001")
    check_migration_graph(repository, {2: "Reserved by an abandoned parallel migration; never deployed."})


def test_integrated_orchestrator_lanes_have_one_ordered_head():
    from acc.server.db import alembic_config
    script = ScriptDirectory.from_config(alembic_config('sqlite://'))
    assert script.get_heads() == ['0079']
    assert [(script.get_revision(revision).revision, script.get_revision(revision).down_revision)
            for revision in ('0074', '0075', '0076', '0077', '0078')] == [
                ('0074', '0073'), ('0075', '0074'), ('0076', '0075'), ('0077', '0076'), ('0078', '0077')]
