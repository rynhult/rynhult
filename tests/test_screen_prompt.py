"""\"Needs you\" from live approval dialogs and Codex questions, including prompts without a permission hook."""
from __future__ import annotations

from pathlib import Path

import pytest
from helpers import control, reconcile_passes, wait_for
from test_supervisor import get, launch, state_is


@pytest.mark.parametrize("provider", ["cursor", "antigravity", "copilot"])
async def test_prompt_on_screen_needs_you_until_answered(api, project, env, provider):
    session = await launch(api, project, provider=provider)
    await wait_for(state_is(api, session["id"], "idle"))

    control(env, session["id"], "ask")
    waiting = await wait_for(state_is(api, session["id"], "waiting_input"))
    assert waiting["status_source"] == "screen"
    inbox = (await api.get("/api/attention")).json()
    item = next(i for i in inbox if i["session_id"] == session["id"] and i["kind"] == "needs_input")
    assert item["detail"] == "rm -rf build" and waiting["activity"] == "Asking to run rm -rf build"
    assert item["risk"] is None  # nothing to judge from a prompt on screen: no empty "Review" label (UX review R8)

    control(env, session["id"], "answer")
    answered = await wait_for(state_is(api, session["id"], "active"))
    assert answered["activity"] == "Running rm -rf build"
    inbox = (await api.get("/api/attention")).json()
    assert not any(i["session_id"] == session["id"] and i["kind"] == "needs_input" for i in inbox)


def test_patterns_against_real_screens():
    from acc.providers import get as provider
    cursor = """  Verifying the file contents are exact.
  $ cat -A hello.txt && printf 'bytes: ' && wc -c hello.txt Waiting for approval...
 $  cat -A hello.txt && printf 'bytes: ' && wc -c hello.txt in .
 Run this command?
 Not in allowlist: cat, printf, wc
  → Run (once) (y)
    Skip & tell the agent what to do instead (esc or n)"""
    assert provider("cursor").screen_prompt(cursor) == "cat -A hello.txt && printf 'bytes: ' && wc -c hello.txt"
    agy = """Requesting permission for:
   ls -la /srv/acc/workspaces/local/antigravity-smoke-test-20261021
Run this command?
> 1. Yes, run command
  4. No, cancel"""
    assert provider("antigravity").screen_prompt(agy) == "ls -la /srv/acc/workspaces/local/antigravity-smoke-test-20261021"
    assert provider("antigravity").screen_prompt("● Ran (ls -la)\n> \n? for shortcuts") is None
    assert provider("cursor").screen_prompt("  $ make Waiting for approval...") == ""  # a prompt, command not shown
    assert provider("claude").screen_prompt(agy) is None  # hooks only
    copilot = """ $ Shell Create made-by-copilot.txt or update its timestamp
   touch made-by-copilot.txt

╭─────────────────────────────────────────────────
│ Create made-by-copilot.txt or update its timestamp
│ ────────────────────────────────────────────────
│ ╭───────────────────────────────────────────────
│ │ touch made-by-copilot.txt
│ ╰───────────────────────────────────────────────
│
│ Do you want to run this command?
│
│ ❯ 1. Yes
│   2. Yes, and don't ask again for `touch` in this repo (/srv/acc/.../copilot-smoke-test-20261022)
│   3. No, and tell Copilot what to do differently (Esc to stop)
│
│ ↑/↓ to navigate · enter to select · esc to cancel
╰─────────────────────────────────────────────────"""  # Copilot CLI 1.0.102, captured 2026-10-22
    assert provider("copilot").screen_prompt(copilot) == "touch made-by-copilot.txt"
    working = """ $ Shell List all entries with detailed file information 6 lines…
   ls -la
 ● total 36
❯
 ← open sidebar · Interactive · Manual Approval · / commands · ? help · tab next tab"""
    assert provider("copilot").screen_prompt(working) is None


async def test_codex_hook_review_needs_person_with_plain_message(api, project, env):
    session = await launch(api, project, provider='codex')
    await wait_for(state_is(api, session['id'], 'idle'))
    control(env, session['id'], 'hooks-review')
    waiting = await wait_for(state_is(api, session['id'], 'waiting_input'))
    message = 'Codex is asking to trust hooks; open the session to review'
    assert waiting['activity'] == message
    inbox = (await api.get('/api/attention')).json()
    item = next(i for i in inbox if i['session_id'] == session['id'] and i['kind'] == 'needs_input')
    assert item['detail'] == message
    assert waiting['status_source'] == 'screen'
    control(env, session['id'], 'answer')
    answered = await wait_for(state_is(api, session['id'], 'active'))
    assert not answered['activity'] or 'trust hooks' not in answered['activity']
    inbox = (await api.get('/api/attention')).json()
    assert not any(i['session_id'] == session['id'] and i['kind'] == 'needs_input' for i in inbox)


def test_codex_hook_review_screen():
    from acc.providers import get
    screen = '  Hooks need review\n  1 hook is new or changed.\n\n  Review hooks\n  Trust all and continue\n  Continue without trusting\n'
    assert get('codex').screen_prompt(screen) == 'Codex is asking to trust hooks; open the session to review'
    assert get('codex').screen_prompt('● Ran make\n› ') is None


def test_codex_hook_review_heading_above_long_dialog():
    from acc.providers import get
    screen = 'Hooks need review\n8 hooks are new or changed.\n' + 'Review details\n' * 20 + 'Trust all and continue\nContinue without trusting\n'
    assert get('codex').screen_prompt(screen) is not None


# Security review 2026-10-23: the real dialog (tui/src/startup_hooks_review.rs, rust-v0.163.0) is a heading line of its
# own, a count line, and three choices; text a session prints that merely starts with the heading is not it.
REAL_REVIEW = (" Hooks need review\n 8 hooks are new or changed.\n Hooks can run outside the sandbox after you trust them.\n\n"
               "› 1. Review hooks\n  2. Trust all and continue\n  3. Continue without trusting (hooks won't run)\n\n"
               " Press enter to confirm or esc to skip\n")


def test_codex_real_hook_review_dialog():
    from acc.providers import get
    assert get('codex').screen_prompt(REAL_REVIEW) == 'Codex is asking to trust hooks; open the session to review'


@pytest.mark.parametrize('output', [
    '• Ran cat docs/research/codex-hook-trust.md\n  Hooks need review is the Codex startup heading.\n\n› ',
    '  Hooks need review: 8 hooks are new or changed\n' + '  log line\n' * 30 + '› ',
    'Hooks need review\n' + '  some file\n' * 20 + '› Write tests for Trust all and continue',
])
def test_codex_session_output_is_not_the_hook_review(output):
    from acc.providers import get
    assert get('codex').screen_prompt(output) is None


FIXTURES = Path(__file__).with_name("fixtures")


@pytest.mark.parametrize(("fixture", "expected"), [
    ("pending", "Codex has a question; open the session and press Shift+Left to answer"),
    ("pending-footer", "Codex has a question; open the session and press Shift+Left to answer"),  # live screen, 2026-10-24
    ("open", "Which environment should receive the demo deployment?"),
    ("answered", None),
])
def test_codex_inline_question_screens(fixture, expected):
    from acc.providers import get
    screen = (FIXTURES / f"codex-question-{fixture}.txt").read_text()
    assert get("codex").screen_prompt(screen + "\n" * 30) == expected


@pytest.mark.parametrize("count", [2, 12])
@pytest.mark.parametrize("main_prompt", [True, False])
def test_codex_multiple_pending_questions(count, main_prompt):
    from acc.providers import get
    screen = (FIXTURES / "codex-question-pending.txt").read_text().replace("1 question", f"{count} questions")
    if not main_prompt:
        screen = screen.replace("› Ask Codex to do anything", "")
    assert get("codex").screen_prompt(screen) is not None


def test_codex_question_placeholder_on_input_row():
    from acc.providers import get
    screen = (FIXTURES / "codex-question-open.txt").read_text().replace("Type your answer\n›", "› Type your answer")
    assert get("codex").screen_prompt(screen) == "Which environment should receive the demo deployment?"


@pytest.mark.parametrize("fixture", ["pending", "open"])
@pytest.mark.parametrize("following", [
    "• Continuing work.\n› Ask Codex to do anything\n",
    "• Still working.\n" * 20 + "› Ask Codex to do anything\n",
])
def test_codex_old_question_in_transcript_is_not_pending(fixture, following):
    from acc.providers import get
    screen = (FIXTURES / f"codex-question-{fixture}.txt").read_text()
    assert get("codex").screen_prompt(screen + following) is None


@pytest.mark.parametrize("screen", [
    "The footer says ? 1 question and shift+← to answer.\n› Ask Codex to do anything",
    "? 0 questions\nshift+← to answer\n› Ask Codex to do anything",
    "? 1 question\n› Ask Codex to do anything",
    "Which environment?\nType your answer\n› Ask Codex to do anything",
    "enter submit   ^] skip   shift+→ main prompt",
])
def test_codex_question_words_without_live_controls_are_not_pending(screen):
    from acc.providers import get
    assert get("codex").screen_prompt(screen) is None


async def test_codex_question_detail_refreshes_and_answer_clears_attention(api, project, env):
    from test_supervisor import get

    session = await launch(api, project, provider="codex")
    sid = session["id"]
    await wait_for(state_is(api, sid, "idle"))
    control(env, sid, "busy")
    await wait_for(state_is(api, sid, "active"))
    run = env["run_dir"] / sid

    for fixture, detail in [
        ("pending", "Codex has a question; open the session and press Shift+Left to answer"),
        ("open", "Which environment should receive the demo deployment?"),
    ]:
        screen = (FIXTURES / f"codex-question-{fixture}.txt").read_text()
        (run / "fake.screen").write_text("\x1b[2J\x1b[H" + screen)
        control(env, sid, "screen")

        async def question_shown():
            current = await get(api, sid)
            return current if current["observed_state"] == "waiting_input" and current["activity"] == detail else None

        waiting = await wait_for(question_shown)
        assert waiting["status_source"] == "screen"
        inbox = (await api.get("/api/attention")).json()
        items = [i for i in inbox if i["session_id"] == sid and i["kind"] == "needs_input"]
        assert len(items) == 1 and items[0]["detail"] == detail
        assert items[0]["risk"] is None

    # No submission hook: the disappearing footer alone must clear screen-sourced attention.
    screen = (FIXTURES / "codex-question-answered.txt").read_text()
    (run / "fake.screen").write_text("\x1b[2J\x1b[H" + screen)
    control(env, sid, "screen")
    answered = await wait_for(state_is(api, sid, "active"))
    assert answered["activity"] is None
    inbox = (await api.get("/api/attention")).json()
    assert not any(i["session_id"] == sid and i["kind"] == "needs_input" for i in inbox)


@pytest.mark.parametrize(("fixture", "expected"), [
    ("idle", "Selected model is at capacity. Please try a different model."),
    ("idle-footer", "Selected model is at capacity. Please try a different model."),  # live screen, captured 2026-10-24
    ("working", None),
])
def test_codex_turn_error_footer(fixture, expected):
    from acc.providers import get
    screen = (FIXTURES / f"codex-error-{fixture}.txt").read_text()
    assert get("codex").screen_idle(screen + "\n" * 30) == expected
    assert get("codex").screen_prompt(screen) is None


@pytest.mark.parametrize("following", [
    "• Continuing work.\n› Ask Codex to do anything\n",
    "• Still working.\n" * 20 + "› Ask Codex to do anything\n",
])
def test_codex_old_turn_error_is_not_idle(following):
    from acc.providers import get
    screen = (FIXTURES / "codex-error-idle.txt").read_text()
    assert get("codex").screen_idle(screen + following) is None


async def test_codex_turn_error_becomes_idle_with_error_activity(api, project, env):
    session = await launch(api, project, provider="codex")
    sid = session["id"]
    await wait_for(state_is(api, sid, "idle"))
    control(env, sid, "busy")
    await wait_for(state_is(api, sid, "active"))

    screen = (FIXTURES / "codex-error-idle.txt").read_text()
    (env["run_dir"] / sid / "fake.screen").write_text("\x1b[2J\x1b[H" + screen)
    control(env, sid, "screen")
    idle = await wait_for(state_is(api, sid, "idle"))
    assert idle["status_source"] == "screen"
    assert idle["activity"] == "Selected model is at capacity. Please try a different model."
    inbox = (await api.get("/api/attention")).json()
    assert not any(i["session_id"] == sid and i["kind"] == "needs_input" for i in inbox)


@pytest.mark.parametrize('fixture,expected,activity', [
    ('question-open', 'waiting_input', 'Which environment should receive the demo deployment?'),
    ('error-idle', 'idle', 'Selected model is at capacity. Please try a different model.'),
])
async def test_codex_footer_survives_soft_wrapping_in_a_phone_pane(api, project, env, fixture, expected, activity):
    from acc.broker import ops

    session = await launch(api, project, provider='codex')
    sid = session['id']
    await wait_for(state_is(api, sid, 'idle'))
    control(env, sid, 'busy')
    await wait_for(state_is(api, sid, 'active'))
    ops.tmux(env['broker'], 'resize-window', '-t', f'={session["tmux_name"]}:', '-x', '39', '-y', '30', check=True)
    screen = (FIXTURES / f'codex-{fixture}.txt').read_text()
    (env['run_dir'] / sid / 'fake.screen').write_text('\x1b[2J\x1b[H' + screen)
    control(env, sid, 'screen')

    async def footer_detected():
        current = await get(api, sid)
        return current if current['observed_state'] == expected and current['activity'] == activity else None

    await wait_for(footer_detected)


@pytest.mark.parametrize("fixture,working", [
    ("claude-background-shell-screen.txt", True),
    ("claude-background-tasks-screen.txt", True),
    ("claude-background-agent-screen.txt", True),
    ("claude-idle-screen.txt", False),
])
def test_claude_background_work_screens(fixture, working):
    from acc.providers import get
    screen = Path(__file__).with_name("fixtures").joinpath(fixture).read_text()
    assert get("claude").screen_working(screen) is working


@pytest.mark.parametrize("screen", [
    "⏵⏵ bypass permissions on · 1 monitor\n",
    "⏵⏵ bypass permissions on · 1 shell\n",
])
def test_claude_single_background_task(screen):
    from acc.providers import get
    assert get("claude").screen_working(screen)


@pytest.mark.parametrize("screen", [
    "⏵⏵ bypass permissions on · 0 shells, 0 monitors\n",
    "● general-purpose  Finished checking inputs\n",
    "The log says 1 shell still running\n❯\n",
    "◯ general-purpose  Reading inputs\n" + "Old output\n" * 6 + "❯\n",
])
def test_claude_non_working_output(screen):
    from acc.providers import get
    assert not get("claude").screen_working(screen)


@pytest.mark.parametrize("fixture", [
    "claude-background-shell-screen.txt",
    "claude-background-tasks-screen.txt",
    "claude-background-agent-screen.txt",
])
async def test_claude_background_work_ends_without_overriding_hooks(api, project, env, fixture):
    fixtures = Path(__file__).with_name("fixtures")
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    screen_file = env["run_dir"] / session["id"] / "fake.screen"

    screen_file.write_text("\x1b[2J\x1b[H" + (fixtures / fixture).read_text())
    control(env, session["id"], "screen")
    active = await wait_for(state_is(api, session["id"], "active"))
    assert active["status_source"] == "screen"
    assert active["activity"] == "Working in background"
    await reconcile_passes(api)
    assert (await get(api, session["id"]))["observed_state"] == "active"

    control(env, session["id"], "permission")
    waiting = await wait_for(state_is(api, session["id"], "waiting_input"))
    assert waiting["status_source"] == "claude_hook"
    await reconcile_passes(api)
    assert (await get(api, session["id"]))["observed_state"] == "waiting_input"

    control(env, session["id"], "tool-done")
    await wait_for(state_is(api, session["id"], "active"))
    screen_file.write_text("\x1b[2J\x1b[H" + (fixtures / "claude-idle-screen.txt").read_text())
    control(env, session["id"], "screen")
    idle = await wait_for(state_is(api, session["id"], "idle"))
    assert idle["activity"] is None

    control(env, session["id"], "busy")
    foreground = await wait_for(state_is(api, session["id"], "active"))
    assert foreground["status_source"] == "claude_hook"
    await reconcile_passes(api)
    assert (await get(api, session["id"]))["observed_state"] == "active"
