"""Public documents must remain navigable in a sanitized source export."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_public_links.py"
spec = importlib.util.spec_from_file_location("check_public_links", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_public_markdown_links_resolve():
    assert not (errors := checker.problems(checker.ROOT)), "\n".join(errors)


def test_checker_handles_local_reference_and_wiki_links(tmp_path):
    (tmp_path / "docs/wiki").mkdir(parents=True)
    (tmp_path / "docs/PLAN.md").write_text("Private")
    (tmp_path / "docs/wiki/Home.md").write_text("[guide](/Guide)\n![image](/.attachments/missing.png)")
    (tmp_path / "docs/wiki/Guide.md").write_text("Public")
    (tmp_path / "README.md").write_text("[private][plan]\n[plan]: docs/PLAN.md\n[missing](gone.md#title)\n")
    assert checker.problems(tmp_path) == [
        "README.md: gone.md#title (missing)",
        "README.md: docs/PLAN.md (excluded)",
        "docs/wiki/Home.md: /.attachments/missing.png (missing)",
    ]


def test_checker_ignores_code_examples_and_external_links(tmp_path):
    (tmp_path / "README.md").write_text(
        '```markdown\n[example](missing.md)\n```\n'
        '`[example](also-missing.md)`\n'
        '[web](https://example.com/page) [section](#section)\n'
    )
    assert checker.problems(tmp_path) == []
