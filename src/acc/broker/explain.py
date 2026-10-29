"""One bounded native CLI turn, in a private workspace with no tools or inherited customizations."""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from acc.env import environment

from .. import providers

DEADLINE = 180
MAX_OUTPUT = 256 * 1024


def isolated_home(original: Path, home: Path, provider: str, hook_path: str, exe: str | None = None, *, bootstrap: bool = True) -> None:
    home.mkdir(mode=0o700, exist_ok=True)
    credentials = {
        "claude": (".claude/.credentials.json",),
        "codex": (".codex/auth.json",),
        "copilot": (),
        "antigravity": (".gemini/antigravity-cli/antigravity-oauth-token",),
        "cursor": (".config/cursor/auth.json", ".cursor/auth.json"),
    }
    for name in credentials[provider]:
        source, target = original / name, home / name
        if source.is_file():
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.symlink_to(source)
    if provider == "claude":
        (home / ".claude.json").write_text('{"hasCompletedOnboarding":true}\n')
    if provider == "copilot":
        source = original / ".copilot/config.json"
        data = json.loads("\n".join(line for line in source.read_text().splitlines()
                                    if not line.lstrip().startswith("//"))) if source.exists() else {}
        auth = {k: v for k, v in data.items() if k in (
            "authTokens", "lastLoggedInUser", "last_logged_in_user", "logged_in_users", "loggedInUsers")}
        (home / ".copilot").mkdir(mode=0o700, exist_ok=True)
        target = home / ".copilot/config.json"
        target.write_text(json.dumps(auth))
        target.chmod(0o600)
    if bootstrap:
        providers.get(provider).bootstrap(home, hook_path, exe=exe)


def arguments(provider: str, model: str, run_dir: Path, native_id: str | None) -> list[str]:
    model_args = ["--model", model]
    session_args = ["--session-id", native_id] if native_id else []
    if provider in ("claude", "copilot") and not session_args:
        raise ValueError("this explanation CLI needs a pre-assigned session id")
    if provider == "claude":
        return ["--print", "--output-format", "json", *session_args,
                "--permission-mode", "plan", "--tools", "", "--max-turns", "1",
                "--disable-slash-commands", "--setting-sources", "",
                "--settings", str(run_dir / "settings.json"),
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', *model_args]
    if provider == "codex":
        return ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ignore-user-config",
                "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                "-c", "features.shell_tool=false", "-c", "features.multi_agent=false",
                "--dangerously-bypass-hook-trust", *model_args, "-"]
    if provider == "copilot":
        return [*session_args, "--no-auto-update", "--disable-client-telemetry",
                "--no-custom-instructions", "--disable-builtin-mcps", "--available-tools", "",
                "--deny-tool", "*", "--no-ask-user", "--silent", "--stream", "off", *model_args]
    if provider == "antigravity":
        return ["--print", "--output-format", "json", "--mode", "plan",
                "--disable-slash-commands", "--print-timeout", "170s", *model_args]
    if provider == "cursor":
        return ["--print", "--output-format", "json", "--mode", "ask", "--trust",
                "--sandbox", "enabled", *model_args]
    raise ValueError("this agent CLI can't write explanations in Rynhult's read-only mode; choose another CLI")


def run(run_dir: Path, executable: str, hook_path: str) -> int:
    spec = json.loads((run_dir / "explanation.json").read_text())
    artifacts = run_dir / "workspace/.acc/artifacts"
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = {"status": "error", "error": "the explanation helper did not finish"}
    process = None
    def interrupted(signum, _frame):
        raise ValueError(f"explanation helper stopped (signal {signum})")
    handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
    try:
        home = run_dir / "home"
        isolated_home(Path.home(), home, spec["provider"], hook_path, exe=executable)
        env = {k: v for k, v in environment.items() if k in (
            "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "ACC_RUN_DIR", "ACC_SESSION_ID", "ACC_GUARD_FILES",
            "RYNHULT_RUN_DIR", "RYNHULT_SESSION_ID", "RYNHULT_GUARD_FILES")}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), CODEX_HOME=str(home / ".codex"),
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", NO_OPEN_BROWSER="1")
        argv = [executable, *arguments(spec["provider"], spec["model"], run_dir, spec.get("native_id"))]
        with (run_dir / "prompt.txt").open("rb") as stdin:
            process = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       cwd=run_dir / "workspace", env=env, start_new_session=True)
            streams = selectors.DefaultSelector()
            for pipe in (process.stdout, process.stderr):
                streams.register(pipe, selectors.EVENT_READ)
            output, errors = bytearray(), bytearray()
            deadline = time.monotonic() + DEADLINE
            try:
                while streams.get_map():
                    if time.monotonic() >= deadline:
                        raise ValueError("explanation exceeded its 180-second time limit")
                    for key, _ in streams.select(min(0.2, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            streams.unregister(key.fileobj)
                            continue
                        (output if key.fileobj is process.stdout else errors).extend(chunk)
                        if len(output) + len(errors) > MAX_OUTPUT:
                            raise ValueError("explanation exceeded its output limit")
                code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            finally:
                streams.close()
            if code:
                raise ValueError(f"the agent CLI stopped with status {code}; open the helper session for details")
            raw = output.decode("utf-8")
            # Print-mode CLIs wrap the final answer; the walkthrough itself is still validated by accd.
            envelope = json.loads(raw)
            if isinstance(envelope, dict) and "steps" not in envelope:
                raw = envelope.get("result", envelope.get("response", ""))
            if not isinstance(raw, str) or len(raw.encode()) > 64 * 1024:
                raise ValueError("the walkthrough is larger than 64 KiB or isn't text")
            (artifacts / "walkthrough.json").write_text(raw)
            status = {"status": "done"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        status = {"status": "error", "error": str(error)[:300]}
    finally:
        if process is not None:
            # Kill descendants too: even a successful CLI must leave no tools or background connections behind.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        # Sign-ins were only lent for this run: drop the copied Copilot tokens and the credential links.
        home = run_dir / "home"
        for name in (".copilot/config.json", ".claude/.credentials.json", ".codex/auth.json",
                     ".gemini/antigravity-cli/antigravity-oauth-token", ".config/cursor/auth.json", ".cursor/auth.json"):
            try:
                (home / name).unlink()
            except FileNotFoundError:
                pass
        pending = run_dir / "explanation-status.tmp"
        pending.write_text(json.dumps(status))
        pending.replace(run_dir / "explanation-status.json")
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        print(json.dumps(status), flush=True)
    return 0 if status["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
