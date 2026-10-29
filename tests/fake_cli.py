#!/usr/bin/env python3
"""Fake `claude` / `codex` / `copilot` / `gh` for supervisor tests (provider chosen by argv[0] name).

Mimics what M1 qualified: pre-assigned (Claude) or generated (Codex) ids, transcripts on
disk, resume failing with status 1 when the transcript is missing, hooks via acc-hook.
Copilot follows its documented CLI (1.0.102): --session-id starts or resumes, hooks from ~/.copilot/hooks
(PascalCase events with snake_case fields; `notification` camelCase), `config trustedFolders`, device-code login.
OpenCode follows 2.1.3: `--standalone --session <id>` (ACC's id), its config folder from OPENCODE_CONFIG_DIR, and ACC's
plugin there loaded under Node by tests/fake_opencode_host.mjs (so its guard and events are the real ones); sessions in
a SQLite database like OpenCode's (~/.local/share/opencode/opencode.db: session_v2, session_message).
Control: write a word to $ACC_RUN_DIR/fake.ctl -> exit | crash | permission | permission-request | screen (prints $ACC_RUN_DIR/fake.screen).
Persistent flag: $ACC_RUN_DIR/fake.fail_resume makes every resume exit 1.
"""
import json
import os
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

_name = Path(sys.argv[0]).name
provider = next((p for p in ("codex", "copilot", "antigravity", "cursor", "opencode", "gh") if p in _name), "claude")
args = sys.argv[1:]
if provider == "codex" and args == ["--version"]:
    print("codex-cli 0.163.0")
    sys.exit(0)
run_dir = Path(os.environ.get("ACC_RUN_DIR", "/nonexistent"))
hook_bin = os.environ.get("FAKE_HOOK", "/srv/acc/bin/acc-hook")
home = Path.home()

if (run_dir / "explanation.json").is_file():
    spec = json.loads((run_dir / "explanation.json").read_text())
    changes = spec["changes"]
    refs = [{"path": changes["list"][0]["path"], "side": "new", "start": 2, "end": 2}]
    result = {"steps": [
        {"title": "Update the response", "summary": "The response changes; check the implementation.", "references": refs},
        {"title": "Check the impact", "summary": "Read the surrounding context before accepting this change.",
         "references": refs},
    ]}
    # The fake exercises the same file contract as a real print-mode CLI.
    artifacts = run_dir / "workspace/.acc/artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "walkthrough.json").write_text(json.dumps(result))
    time.sleep(0.5)
    print(json.dumps({"result": "oops"}) if spec["model"] == "fake-invalid-json" else json.dumps(result))
    sys.exit(0)


def hook(event: str, **fields) -> None:
    payload = {"hook_event_name": event, "session_id": native_id, "cwd": os.getcwd(), **fields}
    if provider == "antigravity":  # camelCase payloads; hooks.json passes the event name
        names = {"UserPromptSubmit": "PreInvocation"}
        payload = {"conversationId": native_id, "workspacePaths": [os.getcwd()], "modelName": "fake-model",
                   "transcriptPath": str(transcript(native_id)), **fields}
        # like agy: run what ~/.gemini/config/hooks.json lists, in its shape (flat, or matcher groups for tool events)
        try:
            groups = json.loads((home / ".gemini/config/hooks.json").read_text())
        except (OSError, ValueError):
            groups = {}
        for spec in groups.values():
            for entry in spec.get(names.get(event, event), []):
                handlers = entry.get("hooks", []) if event in ("PreToolUse", "PostToolUse") else [entry]
                for handler in handlers:
                    if "command" in handler:
                        out = subprocess.run(handler["command"], shell=True, input=json.dumps(payload), text=True,
                                             capture_output=True, check=False).stdout
                        json.loads(out)  # agy needs a JSON object back
        return
    if provider == "cursor":  # camelCase events, snake_case fields; the chat id is conversation_id
        names = {"SessionStart": "sessionStart", "SessionEnd": "sessionEnd", "UserPromptSubmit": "beforeSubmitPrompt",
                 "Stop": "stop", "PostToolUse": "postToolUse"}
        name = names.get(event, event)
        payload = {"conversation_id": native_id, "generation_id": "g1", "hook_event_name": name, "cursor_version": "fake",
                   "workspace_roots": [os.getcwd()], **({"status": "completed"} if name == "stop" else {}),
                   **{k: v for k, v in fields.items() if k not in ("fullyIdle", "executionNum", "terminationReason")}}
        if name == "stop" and fields.get("fullyIdle") is False:
            return  # Cursor reports one stop per turn
        subprocess.run([hook_bin, provider, name], input=json.dumps(payload), text=True, check=False)
        return
    if provider == "copilot":  # hooks.json passes the event name; camelCase events carry camelCase fields
        if event == "Notification":
            event, payload = "notification", {"sessionId": native_id, "timestamp": int(time.time() * 1000), "cwd": os.getcwd(), **fields}
        subprocess.run([hook_bin, provider, event], input=json.dumps(payload), text=True, check=False)
        return
    subprocess.run([hook_bin, provider], input=json.dumps(payload), text=True, check=False)


def transcript(sid: str) -> Path:
    if provider == "claude":
        return home / ".claude/projects/-fake" / f"{sid}.jsonl"
    if provider == "copilot":
        return home / ".copilot/session-state" / sid / "events.jsonl"
    if provider == "antigravity":
        return home / ".gemini/antigravity-cli/brain" / sid / "transcript.jsonl"
    if provider == "cursor":
        return home / ".cursor/chats/fake-workspace" / sid / "store.db"
    return home / ".codex/sessions/2026/01/01" / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"


if provider == "gh":  # like real gh: status, and a device login that waits for Enter, then completes on its own
    if args[:2] == ["auth", "status"]:
        print("github.com\n  ✓ Logged in to github.com account fake-agent (keyring)\n  - Token scopes: 'gist', 'repo', 'workflow'")
        sys.exit(0)
    if args[:2] == ["auth", "login"]:
        print("\n! First copy your one-time code: ABCD-1234", flush=True)
        sys.stdout.write("Press Enter to open github.com in your browser... ")
        sys.stdout.flush()
        sys.stdin.readline()
        time.sleep(1)
        print("✓ Authentication complete.", flush=True)
        sys.exit(0)
    if args[:1] == ["api"]:  # like `gh api --include`: a status line, headers, then the body (here: what it was asked)
        stdin = sys.stdin.read() if "--input" in args else None
        print("HTTP/2.0 201 Created\r\nContent-Type: application/json\r\n\r\n" + json.dumps({"argv": args, "stdin": stdin}))
        sys.exit(0)
    sys.exit(0)

if provider == "copilot":
    config_path = home / ".copilot/config.json"  # starts with // comment lines, like the real one
    config = json.loads("\n".join(l for l in config_path.read_text().splitlines() if not l.startswith("//"))) \
        if config_path.exists() else {}
    if args[:1] in (["--version"], ["-v"]):
        print("GitHub Copilot CLI 1.0.102-fake")
        sys.exit(0)
    if args[:1] == ["--acp"]:  # Agent Client Protocol: the models this login may use come with a new session
        for line in sys.stdin:
            message = json.loads(line)
            method, mid = message.get("method"), message.get("id")
            if method == "initialize":
                print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}}), flush=True)
            elif method == "session/new":
                sid = str(uuid.uuid4())
                (home / ".copilot/session-state" / sid).mkdir(parents=True, exist_ok=True)
                print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid, "models": {
                    "currentModelId": "gpt-test-sol", "availableModels": [
                        {"modelId": "auto", "name": "Auto", "description": "Let Copilot pick"},
                        {"modelId": "gpt-test-sol", "name": "GPT Test Sol"}, {"modelId": "auto", "name": "Auto"}]}}}), flush=True)
            elif method == "session/close":
                print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {}}), flush=True)
        sys.exit(0)
    if args[:2] == ["config", "trustedFolders"]:  # like the real CLI: not a settings key
        print('Error: Unknown settings key segment "trustedFolders" at "<root>".', file=sys.stderr)
        sys.exit(1)
    if args[:1] == ["login"]:  # device code, completes on its own
        print("To authenticate, visit https://github.com/login/device and enter code ABCD-1234.", flush=True)
        time.sleep(1)
        config["last_logged_in_user"] = {"host": "https://github.com", "login": "fake-agent"}
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("// This file is managed automatically.\n" + json.dumps(config))
        print("Signed in successfully as fake-agent.", flush=True)
        sys.exit(0)

if provider == "antigravity" and args[:1] in (["--version"], ["models"], ["-p"]):
    auth = home / ".gemini/antigravity-cli/fake-auth"
    if args[0] == "--version":
        print("1.4.4-fake")
        sys.exit(0)
    if args[0] == "models":  # like agy 1.4.4: it never starts a sign-in itself
        if not auth.exists():
            print("Fetching available models...\nError: Please sign in to view available models. Launch the CLI without arguments to sign in.")
            sys.exit(1)
        print("Fetching available models...\ngemini-3-pro\ngemini-3-flash", flush=True)
        sys.exit(0)
    if not auth.exists() and not stat.S_ISCHR(os.fstat(0).st_mode):  # it offers a sign-in only on a terminal
        print("Error: authentication required. Run 'agy' to log in, then retry.\nerror: authentication failed or timed out")
        sys.exit(1)
    if not auth.exists():  # a prompt needs an account: a sign-in URL, then the pasted code (60 s)
        print("Authentication required. Please visit the URL to log in:\n  https://accounts.google.com/o/oauth2/auth?client_id=fake", flush=True)
        print("\nWaiting for authentication (timeout 60s)...\nOr, paste the authorization code here and press Enter:", flush=True)
        code = sys.stdin.readline().strip()
        if code != "good-code":
            print("error: authentication failed or timed out", flush=True)
            sys.exit(1)
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text("ok")
    print("OK", flush=True)
    sys.exit(0)

if provider == "cursor" and args[:1] in (["--version"], ["status"], ["login"], ["models"]):
    auth = home / ".cursor/fake-auth"
    if args[0] == "--version":
        print("2026.10.19-fake")
    elif args[0] == "status":
        print("\x1b[32mLogged in as fake@example.com\x1b[0m" if auth.exists() else "\x1b[31mNot logged in\x1b[39m")
    elif args[0] == "login":  # like cursor-agent with NO_OPEN_BROWSER: a link, then it finishes once approved
        print("Starting login process...\nOpen a browser and navigate to this link: https://cursor.com/loginDeepControl?challenge=fake", flush=True)
        time.sleep(1)
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text("ok")
        print("Login successful!", flush=True)
    elif auth.exists():
        print("Available models\n\ngpt-test - GPT Test\nsonnet-test - Sonnet Test")
    else:
        print("Not logged in")
    sys.exit(0)

if provider == "opencode":
    import sqlite3
    import threading

    database = home / ".local/share/opencode/opencode.db"

    def db() -> sqlite3.Connection:
        database.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(database)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS session_v2 (id text PRIMARY KEY, project_id text NOT NULL, "
                     "parent_id text, slug text NOT NULL, directory text NOT NULL, title text, version text NOT NULL, "
                     "time_created integer NOT NULL, time_updated integer NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS session_message (id text PRIMARY KEY, session_id text NOT NULL, "
                     "type text NOT NULL, seq integer NOT NULL, time_created integer NOT NULL, "
                     "time_updated integer NOT NULL, data text NOT NULL)")
        return conn

    if args[:1] in (["--version"], ["-v"]):
        print("opencode v2.1.3-fake")
        sys.exit(0)
    if args[:2] == ["auth", "list"]:  # like 2.1.3 --format json: integrations with their connections
        rows = []
        if (home / ".local/share/opencode/fake-copilot").exists():
            rows.append({"id": "github-copilot", "name": "GitHub Copilot",
                         "connections": [{"type": "oauth", "label": "fake-agent"}]})
        if os.environ.get("OPENROUTER_API_KEY"):
            rows.append({"id": "openrouter", "name": "OpenRouter", "connections": [{"type": "env", "name": "OPENROUTER_API_KEY"}]})
        print(json.dumps(rows, indent=2))
        sys.exit(0)
    if args[:1] == ["models"]:
        print("opencode/big-pickle\nopencode/fake-free")
        sys.exit(0)
    native_id = args[args.index("--session") + 1]
    config_dir = os.environ.get("OPENCODE_CONFIG_DIR", "")
    plugin = Path(config_dir, "plugins/acc/index.js")
    with db() as conn:
        resume = conn.execute("SELECT 1 FROM session_v2 WHERE id = ?", (native_id,)).fetchone() is not None
    if resume and (run_dir / "fake.fail_resume").exists():
        print(f"Session not found: {native_id}", file=sys.stderr)
        time.sleep(0.3)
        sys.exit(1)
    print(f"fake-opencode ready · session {native_id} · {'resumed' if resume else 'new'} · "
          f"{'--standalone' if '--standalone' in args else 'SHARED SERVER'} · plugin {'found' if plugin.is_file() else 'missing'}",
          flush=True)
    host = subprocess.Popen(["node", str(Path(__file__).resolve().with_name("fake_opencode_host.mjs")), str(plugin), native_id],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def relay():
        for line in host.stdout:
            print(f"plugin: {line.rstrip()}", flush=True)
    threading.Thread(target=relay, daemon=True).start()

    def send(**message) -> None:
        host.stdin.write(json.dumps(message) + "\n")
        host.stdin.flush()

    def event(kind: str, **data) -> None:
        send(event={"id": f"evt_{uuid.uuid4().hex[:12]}", "type": kind, "data": data})

    def remember(prompt: str, reply: str | None = None) -> None:
        now_ms = int(time.time() * 1000)
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO session_v2 VALUES (?, 'global', NULL, 'fake', ?, NULL, '2.1.3', ?, ?)",
                         (native_id, os.getcwd(), now_ms, now_ms))
            seq = conn.execute("SELECT count(*) FROM session_message WHERE session_id = ?", (native_id,)).fetchone()[0]
            conn.execute("INSERT INTO session_message VALUES (?, ?, 'user', ?, ?, ?, ?)",
                         (f"msg_{uuid.uuid4().hex[:12]}", native_id, seq, now_ms, now_ms,
                          json.dumps({"time": {"created": now_ms}, "text": prompt, "files": []})))
            if reply:
                conn.execute("INSERT INTO session_message VALUES (?, ?, 'assistant', ?, ?, ?, ?)",
                             (f"msg_{uuid.uuid4().hex[:12]}", native_id, seq + 1, now_ms, now_ms,
                              json.dumps({"time": {"created": now_ms}, "agent": "build",
                                          "content": [{"type": "text", "text": reply}]})))

    if "--prompt" in args:
        remember(args[args.index("--prompt") + 1])
        event("session.execution.started", sessionID=native_id)
        event("session.execution.succeeded", sessionID=native_id)

    def stop(signum, _frame):
        send(exit=True)
        host.wait(timeout=5)
        sys.exit(128 + signum)
    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGTERM, stop)
    ctl = run_dir / "fake.ctl"
    while True:
        if host.poll() is not None:
            print(f"plugin host exited ({host.returncode})", flush=True)
            time.sleep(0.5)
            sys.exit(5)
        if ctl.exists():
            command = ctl.read_text().strip()
            ctl.unlink()
            if command == "exit":
                send(exit=True)
                host.wait(timeout=5)
                sys.exit(0)
            if command == "crash":
                host.kill()
                sys.exit(3)
            if command == "screen":
                sys.stdout.write((run_dir / "fake.screen").read_text())
                sys.stdout.flush()
            if command == "busy":
                remember("do something")
                event("session.execution.started", sessionID=native_id)
            if command == "turn":
                remember("do something", "done")
                event("session.execution.started", sessionID=native_id)
                time.sleep(0.5)
                event("session.execution.succeeded", sessionID=native_id)
            if command.startswith("say "):
                remember(command[4:], f"Done: {command[4:]}")
                event("session.execution.started", sessionID=native_id)
                event("session.execution.succeeded", sessionID=native_id)
            if command == "subagent-done":  # a subagent's own session ends: the turn goes on
                event("session.execution.succeeded", sessionID="ses_childfake0000000001")
            if command == "fail":
                event("session.execution.failed", sessionID=native_id, error={"type": "api", "message": "rate limited"})
            if command == "permission":
                event("permission.asked", id="per_fake1", sessionID=native_id, action="shell", resources=["npm test"],
                      save=["npm *"], source={"type": "tool", "messageID": "msg_1", "id": "call_1"})
            if command == "permission-reply":
                event("permission.replied", sessionID=native_id, requestID="per_fake1", reply="once")
            if command == "question":
                event("form.created", form={"id": "frm_1", "sessionID": native_id, "title": "Questions",
                                            "fields": [{"key": "q0", "title": "Which database?"}]})
            if command == "tool":
                remember("run the tests")
                event("session.execution.started", sessionID=native_id)
                send(tool="shell", input={"command": "npm test", "description": "Run tests"})
            if command.startswith("guarded "):  # a shell command through ACC's plugin (acc-guard), as OpenCode runs it
                send(tool="shell", input={"command": command[8:], "description": "fake"})
            if command == "ask":  # OpenCode's approval prompt on screen
                sys.stdout.write("\x1b[2J\x1b[H  ┃  △ Permission required\n  ┃\n  ┃  $ rm -rf build\n  ┃\n"
                                 "  ┃   Allow once   Always allow   Reject\n")
                sys.stdout.flush()
            if command == "answer":
                sys.stdout.write("\x1b[2J\x1b[H  $ rm -rf build\n")
                sys.stdout.flush()
        time.sleep(0.1)

if args[:1] == ["app-server"]:  # minimal JSON-RPC: initialize + model/list
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "initialize":
            print(json.dumps({"id": message["id"], "result": {}}), flush=True)
        elif message.get("method") == "account/rateLimits/read":
            print(json.dumps({"id": message["id"], "result": {"rateLimits": {"planType": "prolite",
                "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1792900000},
                "secondary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": 1793400000},
                "credits": {"balance": "0"}}}}), flush=True)
        elif message.get("method") == "model/list":
            print(json.dumps({"id": message["id"], "result": {"data": [
                {"model": "gpt-test", "displayName": "GPT Test", "description": "fake", "hidden": False,
                 "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}],
                 "defaultReasoningEffort": "low"},
                {"model": "hidden-one", "hidden": True}]}}), flush=True)
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "test@example.com", "subscriptionType": "max"}))
    sys.exit(0)
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
    sys.exit(0)
if args[:2] == ["login", "--device-auth"]:  # like real Codex: coloured output, completes on its own
    print("1. Open this link in your browser and sign in to your account\n   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m", flush=True)
    print("2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n   \x1b[94m37HC-WVMZN\x1b[0m", flush=True)
    time.sleep(1)
    print("Successfully logged in", flush=True)
    sys.exit(0)
if args[:2] == ["auth", "login"]:  # prints a URL, waits for the pasted code like the real CLI
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=test", flush=True)
    print("Paste code here if prompted > ", flush=True)
    code = sys.stdin.readline().strip()
    print("Login successful." if code == "good-code" else "Invalid code", flush=True)
    sys.exit(0 if code == "good-code" else 1)

resume = False
if provider == "copilot":  # one flag for both: an existing session is resumed, a new id starts one
    native_id = args[args.index("--session-id") + 1]
    resume = transcript(native_id).exists()
    if resume and (run_dir / "fake.fail_resume").exists():
        print(f"Failed to resume session {native_id}", file=sys.stderr)
        time.sleep(0.3)
        sys.exit(1)
    extra = os.environ.get("COPILOT_CUSTOM_INSTRUCTIONS_DIRS")
    if extra:
        print(f"instructions: {extra}/AGENTS.md ({'found' if Path(extra, 'AGENTS.md').exists() else 'missing'})", flush=True)
elif provider == "cursor":
    if "--resume" in args:
        native_id, resume = args[args.index("--resume") + 1], True
    else:
        native_id = str(uuid.uuid4())
elif provider == "antigravity":
    if "--conversation" in args:
        native_id, resume = args[args.index("--conversation") + 1], True
    else:
        native_id = str(uuid.uuid4())
elif provider == "claude":
    if "--resume" in args:
        native_id, resume = args[args.index("--resume") + 1], True
    else:
        native_id = args[args.index("--session-id") + 1]
else:
    if args and args[0] == "resume":
        native_id, resume = args[1], True
    else:
        native_id = str(uuid.uuid4())

if resume and ((run_dir / "fake.fail_resume").exists() or not transcript(native_id).exists()):
    print(f"No conversation found with session ID: {native_id}", file=sys.stderr)
    time.sleep(0.3)  # a real CLI never exits within milliseconds; tmux can miss the exit status of one that does
    sys.exit(1)

path = transcript(native_id)
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a") as handle:
    handle.write(json.dumps({"type": "start", "resume": resume, "sessionId": native_id}) + "\n")

# Fixture-only evidence of the actual provider invocation, including resume.
with (run_dir / "fake.argv.jsonl").open("a") as handle:
    handle.write(json.dumps(args) + "\n")

print(f"fake-{provider} ready · session {native_id} · {'resumed' if resume else 'new'}", flush=True)
hook("SessionStart", source="resume" if resume else "startup")


def on_signal(signum, _frame):
    hook("SessionEnd", reason="other")
    sys.exit(128 + signum)


signal.signal(signal.SIGHUP, on_signal)
signal.signal(signal.SIGTERM, on_signal)
ctl = run_dir / "fake.ctl"
while True:
    # A real submitted terminal line: echo alone is not evidence of submission.
    import select
    if select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        if line:
            with (run_dir / "fake.received").open("a") as received:
                received.write(line)
            hook("UserPromptSubmit", prompt=line.strip())
    if ctl.exists():
        command = ctl.read_text().strip()
        ctl.unlink()
        if command == "exit":
            hook("SessionEnd", reason="prompt_input_exit")
            sys.exit(0)
        if command == "crash":
            sys.exit(3)
        if command == "screen":  # the demo's terminal: what a real conversation looks like
            sys.stdout.write((run_dir / "fake.screen").read_text())
            sys.stdout.flush()
        if command == "busy":  # a turn starts and then goes quiet (e.g. a failed request with no closing hook)
            hook("UserPromptSubmit", prompt="do something")
        if command == "turn":  # a whole turn: starts, one execution ends mid-turn, then the turn ends
            hook("UserPromptSubmit", prompt="do something")
            time.sleep(0.5)
            hook("Stop", fullyIdle=False, executionNum=1)
            time.sleep(0.5)
            hook("Stop", fullyIdle=True, executionNum=2, terminationReason="completed")
        if command == "ask":  # an approval prompt on screen, no hook (what Cursor and Antigravity show)
            prompts = {"cursor": " $  rm -rf build in .\n Run this command?\n  → Run (once) (y)\n",
                       "antigravity": "Requesting permission for:\n   rm -rf build\nRun this command?\n> 1. Yes, run command\n",
                       "copilot": "╭─────\n│ Delete build\n│ ╭────\n│ │ rm -rf build\n│ ╰────\n│\n│ Do you want to run this command?\n│\n│ ❯ 1. Yes\n"}
            sys.stdout.write("\x1b[2J\x1b[H" + prompts.get(provider, "Run this command?\n"))
            sys.stdout.flush()
        if command == "hooks-review":
            sys.stdout.write("\x1b[2J\x1b[H  Hooks need review\n  8 hooks are new or changed.\n\n› 1. Review hooks\n  2. Trust all and continue\n  3. Continue without trusting (hooks won't run)\n")
            sys.stdout.flush()
        if command == "answer":
            sys.stdout.write("\x1b[2J\x1b[H● Ran rm -rf build\n")
            sys.stdout.flush()
        if command.startswith("guarded "):  # a shell command through the pre-tool hook (acc-guard), as Claude runs it
            payload = {"hook_event_name": "PreToolUse", "session_id": native_id, "cwd": os.getcwd(),
                       "tool_name": "Bash", "tool_input": {"command": command[8:]}}
            guard = str(Path(hook_bin).with_name("acc-guard"))
            result = subprocess.run([guard, provider, "PreToolUse"], input=json.dumps(payload), text=True,
                                    capture_output=True, check=False)
            print(f"guard exit {result.returncode}: {result.stderr.strip()}", flush=True)
        if command.startswith("say "):  # one exchange in the conversation file (Claude's format), then the turn ends
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with transcript(native_id).open("a") as handle:
                handle.write(json.dumps({"type": "user", "timestamp": stamp,
                                         "message": {"role": "user", "content": command[4:]}}) + "\n")
                handle.write(json.dumps({"type": "assistant", "timestamp": stamp, "message": {
                    "role": "assistant", "content": [{"type": "text", "text": f"Done: {command[4:]}"}]}}) + "\n")
            hook("UserPromptSubmit", prompt=command[4:])
            hook("Stop", fullyIdle=True, executionNum=1, terminationReason="completed")
        if command == "tool":  # a turn that runs one shell command (pre- and post-tool hooks), then waits
            hook("UserPromptSubmit", prompt="run the tests")
            hook("PreToolUse", tool_name="Bash", tool_input={"command": "npm test", "description": "Run tests"})
        if command == "tool-done":
            hook("PostToolUse", tool_name="Bash", tool_input={"command": "npm test"}, tool_response={"stdout": "ok"})
            hook("Stop", fullyIdle=True, executionNum=1, terminationReason="completed")
        if command == "permission":
            hook("Notification", notification_type="permission_prompt", message="Claude needs your permission")
        if command == "permission-request":  # Copilot's: sent before every tool, even ones it approves itself
            hook("PermissionRequest", tool_name="bash", tool_input={"command": "npm test"})
    time.sleep(0.1)
