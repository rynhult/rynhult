"""Catalog frontmatter is parsed as YAML and rebuilt from an allowlist (security review 2026-10-22, #5).
Nothing here runs a CLI: it checks what the review flags say and what sessions would be given."""
from __future__ import annotations

import pytest
import yaml

from acc.server.catalog_import import ALLOWED_FRONTMATTER, parse_frontmatter, review
from acc.server.compiler import safe_markdown


def bad_messages(text: str) -> list[str]:
    return [f["message"] for f in review({"SKILL.md": text}, []) if f["level"] == "bad"]


def compiled_meta(text: str) -> dict:
    """The frontmatter a session gets, read back with a real YAML parser."""
    out = safe_markdown(text)
    assert out.startswith("---\n")
    block, _, _ = out[4:].partition("---\n")
    return yaml.safe_load(block) or {}


@pytest.mark.parametrize("key", ['"allowed-tools"', "'hooks'", '"permissionMode"', "'mcpServers'"])
def test_quoted_risky_keys_are_flagged_and_dropped(key):
    text = f"---\nname: q\ndescription: d\n{key}: Bash(*)\n---\nbody\n"
    assert any("frontmatter" in m or "hooks" in m for m in bad_messages(text))
    assert compiled_meta(text) == {"name": "q", "description": "d"}


def test_flow_mapping_and_unknown_keys_never_pass():
    text = "---\n{name: f, description: d, allowed-tools: 'Bash(*)', settings: {x: 1}}\n---\nbody\n"
    assert parse_frontmatter(text).meta["allowed-tools"] == "Bash(*)"
    assert any("Pre-approves" in m for m in bad_messages(text))
    assert compiled_meta(text) == {"name": "f", "description": "d"}
    info = [f["message"] for f in review({"SKILL.md": text}, []) if f["level"] == "info"]
    assert any("settings" in m for m in info)


def test_yaml_merge_key_is_rejected():
    text = "---\nname: m\ndescription: d\n<<: {allowed-tools: 'Bash(*)'}\n---\nbody\n"
    assert parse_frontmatter(text).error == "YAML merge keys (<<) are not supported"
    assert any("can't read safely" in m for m in bad_messages(text))
    assert compiled_meta(text) == {}


def test_alias_and_anchor_are_rejected():
    text = "---\nname: &n a\ndescription: *n\n---\nbody\n"
    assert "anchors and aliases" in parse_frontmatter(text).error
    assert compiled_meta(text) == {}
    anchored = "---\nname: x\nmetadata: &m {a: 1}\n---\n"
    assert parse_frontmatter(anchored).error


def test_duplicate_keys_are_rejected():
    text = "---\nname: a\ndescription: safe\n'description': other\n---\nbody\n"
    assert "duplicate key" in parse_frontmatter(text).error
    assert any("can't read safely" in m for m in bad_messages(text))
    assert compiled_meta(text) == {}
    nested = "---\nname: a\nmetadata:\n  x: 1\n  x: 2\n---\n"
    assert "duplicate key" in parse_frontmatter(nested).error


def test_crlf_line_endings_are_parsed_and_stripped():
    text = "---\r\nname: c\r\ndescription: d\r\nhooks:\r\n  Stop: [{command: curl x}]\r\n---\r\nBody line\r\n"
    parsed = parse_frontmatter(text)
    assert parsed.error is None and set(parsed.meta) == {"name", "description", "hooks"}
    assert any("hooks" in m for m in bad_messages(text))
    out = safe_markdown(text)
    assert out == "---\nname: c\ndescription: d\n---\nBody line\r\n" and "curl" not in out


def test_missing_closing_delimiter():
    text = "---\nname: x\nallowed-tools: Bash(*)\nno end here\n"
    assert parse_frontmatter(text).error == "no closing --- line"
    assert any("can't read safely" in m for m in bad_messages(text))
    out = safe_markdown(text)
    assert out.startswith("---\n---\n")  # an empty block first: nothing after it can be read as settings
    assert compiled_meta(text) == {}


@pytest.mark.parametrize("text", [
    "--- {hooks: x}\n---\n",  # YAML content on the opening line
    "---\nname: x\n---hooks: x\n",  # a closing line with more on it
    "---\n- a\n- b\n---\n",  # not a mapping
    "---\njust text\n---\n",
    "---\nname: !!python/object:os.system x\n---\n",
    "---\nname: x\n...\nhooks: y\n---\n",  # a second YAML document
    "---\n1: x\n---\n",
])
def test_unsupported_forms_are_errors_and_compile_to_an_empty_block(text):
    assert parse_frontmatter(text).error
    assert compiled_meta(text) == {}


def test_frontmatter_size_limit():
    text = "---\nname: x\ndescription: " + "y" * (64 * 1024) + "\n---\n"
    assert parse_frontmatter(text).error == "frontmatter too large"


def test_allowed_keys_survive_with_their_values():
    text = ("---\nname: ok\ndescription: >-\n  Folded\n  text.\nmodel: sonnet\ntools: Read, Grep\nversion: 2\n"
            "metadata:\n  origin: ECC\nlicense: MIT\n---\n# Body\n")
    assert compiled_meta(text) == {"name": "ok", "description": "Folded text.", "model": "sonnet",
                                   "tools": "Read, Grep", "version": 2, "metadata": {"origin": "ECC"}, "license": "MIT"}
    assert safe_markdown(text).endswith("---\n# Body\n")
    assert set(compiled_meta(text)) <= set(ALLOWED_FRONTMATTER)


def test_no_frontmatter_is_left_alone():
    assert safe_markdown("# Title\n\n---\n\nhooks: not settings\n") == "# Title\n\n---\n\nhooks: not settings\n"
