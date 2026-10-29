"""Shared rename compatibility for process environments and parsed env files.

Presence, including an empty value, selects RYNHULT_. Iteration exports both
spellings for older subprocesses. No values are logged or persisted here.
This module uses only the standard library so installers can copy it alongside
standalone hooks and privileged helpers as ``acc_env.py``.
"""
from __future__ import annotations

import os
from collections.abc import MutableMapping


def names(key: str) -> tuple[str, ...]:
    if key.startswith('ACC_'):
        return ('RYNHULT_' + key[4:], key)
    if key.startswith('RYNHULT_'):
        return (key, 'ACC_' + key[8:])
    return (key,)


PROTECTED_SESSION_VARIABLES = frozenset(
    key for suffix in ('RUN_DIR', 'SESSION_ID', 'GUARD_FILES') for key in names('ACC_' + suffix)
)


# Project credentials must not redirect interpreter imports, the dynamic loader or shell startup.
# Shared by the vault and broker: validate again at the execution boundary.
INTERPRETER_ENV_VARIABLES = frozenset({
    'PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONSTARTUP', 'PYTHONINSPECT',
    'NODE_PATH', 'NODE_OPTIONS', 'NODE_REPL_EXTERNAL_MODULE', 'BASH_ENV', 'ENV', 'ZDOTDIR',
    'PERL5OPT', 'PERL5LIB', 'PERLLIB', 'RUBYOPT', 'RUBYLIB', 'JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS',
    'GCONV_PATH',
    # git runs these or reads its configuration (core.fsmonitor, core.sshCommand, … run commands) from them. Rynhult's
    # own git calls set some in trusted code; a project key must not. EDITOR, VISUAL and PAGER are git's documented
    # fallbacks, LESSOPEN/LESSCLOSE run under git's default pager, and ext:: transport runs a command once
    # GIT_ALLOW_PROTOCOL allows it. GIT_AUTHOR_* and GIT_COMMITTER_* stay allowed.
    'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_PROXY_COMMAND', 'GIT_ASKPASS', 'SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE',
    'GIT_EDITOR', 'GIT_SEQUENCE_EDITOR', 'EDITOR', 'VISUAL', 'GIT_PAGER', 'PAGER', 'LESSOPEN', 'LESSCLOSE',
    'GIT_EXTERNAL_DIFF', 'GIT_EXEC_PATH', 'GIT_TEMPLATE_DIR', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR',
    'GIT_ALLOW_PROTOCOL', 'GIT_PROTOCOL_FROM_USER',
    # Where the agent CLIs (and git, through XDG_CONFIG_HOME/git/config) read their configuration: Rynhult writes the
    # guard hooks into the default folders, so a key that moves one starts the CLI without them (security review 8).
    'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'COPILOT_HOME', 'CURSOR_CONFIG_DIR', 'GNUPGHOME',
})
# Whole families: Python reads many more (PYTHONBREAKPOINT imports a module, PYTHONPYCACHEPREFIX picks where bytecode
# is trusted from); LD_ is glibc's loader (LD_AUDIT, not only LD_PRELOAD); DYLD_ is macOS's (DYLD_INSERT_LIBRARIES).
# NODE_ stays enumerated: NODE_AUTH_TOKEN and NODE_ENV are ordinary project settings. GIT_CONFIG covers GIT_CONFIG,
# _GLOBAL, _SYSTEM, _NOSYSTEM, _PARAMETERS, _COUNT and every GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n.
# XDG_ moves every XDG tool's configuration and data; NPM_CONFIG_ sets npm's options (node-options among them).
INTERPRETER_ENV_PREFIXES = ('PYTHON', 'LD_', 'DYLD_', 'GIT_CONFIG', 'XDG_', 'NPM_CONFIG_')


def interpreter_variable(name: str) -> bool:
    return name in INTERPRETER_ENV_VARIABLES or name.startswith(INTERPRETER_ENV_PREFIXES)


class Environment(MutableMapping):
    def __init__(self, values=None):
        self._values = values

    @property
    def values_map(self):
        return os.environ if self._values is None else self._values

    def __getitem__(self, key):
        for candidate in names(key):
            if candidate in self.values_map:
                return self.values_map[candidate]
        raise KeyError(key)

    def __setitem__(self, key, value):
        for candidate in names(key):
            self.values_map[candidate] = value

    def __delitem__(self, key):
        if key not in self:
            raise KeyError(key)
        for candidate in names(key):
            self.values_map.pop(candidate, None)

    def __iter__(self):
        return iter(dict.fromkeys(candidate for key in self.values_map for candidate in names(key)))

    def __len__(self):
        return sum(1 for _ in self)

    def copy(self):
        return dict(self)


environment = Environment()
