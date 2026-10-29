"""How to install an agent CLI by hand where Rynhult can't install it (Providers → Agent software, and the Guide's
"Connect an agent" page).

Every command and link below is copied from the provider's own installation documentation, as checked on 2026-10-23;
change them only from those pages:

- Claude Code: https://code.claude.com/docs/en/setup ("Native Install", "Homebrew")
- Codex: https://github.com/openai/codex#readme (README "Installing and running Codex CLI"; the docs page
  https://developers.openai.com/codex/cli shows the same standalone installer)
- GitHub Copilot CLI: https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli
- Google Antigravity CLI: https://antigravity.google/docs/getting-started?tab=cli (no Homebrew documented)
- Cursor CLI: https://cursor.com/docs/cli/installation (no Homebrew documented)
- OpenCode 2 (npm @opencode/cli, the line Rynhult runs): https://opencode.ai/v2/docs
- Ollama: https://ollama.com/download/mac, https://docs.ollama.com/macos ("mount the ollama.dmg and drag-and-drop"),
  https://ollama.com/download/linux

A Windows machine runs its sessions inside WSL (Ubuntu), so the CLIs go there, with the Linux commands; the
providers' native Windows installers would put them where sessions can't see them.

The commands install for the user who runs them, so they are run as the agent account sessions use (on a Mac, the
signed-in user Rynhult runs as).
"""
from __future__ import annotations

import re

BACKUP_TOOLS = ("restic",)  # in the acc-packages catalog, but not agent software: Providers lists it apart

# name -> label, docs per system, commands per system ([how, command], the recommended one first), and what to do
# after installing (most CLIs sign in from their card on Providers).
GUIDES: dict[str, dict] = {
    "claude": {
        "label": "Claude Code",
        "docs": {"macos": "https://code.claude.com/docs/en/setup", "linux": "https://code.claude.com/docs/en/setup"},
        "macos": [["Homebrew", "brew install --cask claude-code"],
                  ["Anthropic's installer", "curl -fsSL https://claude.ai/install.sh | bash"]],
        "linux": [["Anthropic's installer", "curl -fsSL https://claude.ai/install.sh | bash"]],
        "after": "signin",
    },
    "codex": {
        "label": "Codex",
        "docs": {"macos": "https://developers.openai.com/codex/cli", "linux": "https://developers.openai.com/codex/cli"},
        "macos": [["Homebrew", "brew install --cask codex"],
                  ["OpenAI's installer", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"]],
        "linux": [["OpenAI's installer", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"]],
        "after": "signin",
    },
    "copilot": {
        "label": "GitHub Copilot CLI",
        "docs": {"macos": "https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli",
                 "linux": "https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli"},
        "macos": [["Homebrew", "brew install --cask copilot-cli"],
                  ["GitHub's installer", "curl -fsSL https://gh.io/copilot-install | bash"]],
        "linux": [["GitHub's installer", "curl -fsSL https://gh.io/copilot-install | bash"]],
        "after": "signin",
    },
    "antigravity": {
        "label": "Google Antigravity CLI",
        "docs": {"macos": "https://antigravity.google/docs/getting-started?tab=cli",
                 "linux": "https://antigravity.google/docs/getting-started?tab=cli"},
        "macos": [["Google's installer", "curl -fsSL https://antigravity.google/cli/install.sh | bash"]],
        "linux": [["Google's installer", "curl -fsSL https://antigravity.google/cli/install.sh | bash"]],
        "after": "signin",
    },
    "cursor": {
        "label": "Cursor CLI",
        "docs": {"macos": "https://cursor.com/docs/cli/installation", "linux": "https://cursor.com/docs/cli/installation"},
        "macos": [["Cursor's installer", "curl https://cursor.com/install -fsS | bash"]],
        "linux": [["Cursor's installer", "curl https://cursor.com/install -fsS | bash"]],
        "after": "signin",
    },
    "opencode": {
        "label": "OpenCode",
        "docs": {"macos": "https://opencode.ai/v2/docs", "linux": "https://opencode.ai/v2/docs"},
        "macos": [["Homebrew", "brew install anomalyco/tap/opencode-v2"],
                  ["OpenCode's installer", "curl -fsSL https://opencode.ai/v2/install | bash"]],
        "linux": [["OpenCode's installer", "curl -fsSL https://opencode.ai/v2/install | bash"]],
        "after": "ready",
    },
    "ollama": {
        "label": "Ollama",
        "docs": {"macos": "https://ollama.com/download/mac", "linux": "https://ollama.com/download/linux"},
        # Ollama's macOS docs prefer the app: download Ollama.dmg, drag Ollama to Applications, open it once (it offers
        # to link the ollama command into /usr/local/bin). Its download page also lists the script for macOS.
        "macos": [["Ollama's app", "https://ollama.com/download/Ollama.dmg"],
                  ["Ollama's installer", "curl -fsSL https://ollama.com/install.sh | sh"]],
        "linux": [["Ollama's installer", "curl -fsSL https://ollama.com/install.sh | sh"]],
        "after": "endpoint",
    },
}

SYSTEMS = ("macos", "linux", "wsl")


def system_of(info: dict | None, kind: str | None) -> str:
    """macos, wsl or linux, from what a machine reported (its os, and wsl when its kernel is WSL's)."""
    info = info or {}
    if kind == "mac" or info.get("os") == "darwin":
        return "macos"
    return "wsl" if str(info.get("wsl", "")).strip().lower() not in ("", "0", "false") else "linux"


def valid_agent_account(account: object) -> bool:
    return (isinstance(account, str) and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", account) is not None
            and account not in {"root", "nobody", "acc", "acc-node", "acc-build", "acc-svc"})


def guide(name: str, system: str, account: str | None = None, machine: str | None = None) -> dict | None:
    """The steps to install `name` by hand on a machine of `system`, for the dashboard: a first step that opens the
    right shell, the commands (recommended first), the provider's page, and what to do afterwards."""
    entry = GUIDES.get(name)
    if entry is None or system not in SYSTEMS:
        return None
    commands = entry["macos" if system == "macos" else "linux"]
    switch = f"sudo -iu {account}" if valid_agent_account(account) else None
    where = machine or ("the Mac" if system == "macos" else "the Windows PC" if system == "wsl" else "the machine")
    if system == "macos":
        shell = f"On {where}, open Terminal, signed in as the user Rynhult runs as (sessions run as that user)."
    elif system == "wsl":
        shell = (f"On {where}, open Ubuntu (WSL) and switch to the agent account sessions run as:"
                 if switch else f"On {where}, open Ubuntu (WSL) as the agent account sessions run as.")
    else:
        shell = (f"On {where}, open a terminal and switch to the agent account sessions run as:"
                 if switch else f"On {where}, open a terminal as the agent account sessions run as.")
    return {"label": entry["label"], "system": system, "shell": shell,
            "shell_command": switch if system != "macos" else None,
            "commands": [{"how": how, "command": command, "link": command.startswith("https://")}
                         for how, command in commands],
            "docs": entry["docs"]["macos" if system == "macos" else "linux"], "after": entry["after"]}
