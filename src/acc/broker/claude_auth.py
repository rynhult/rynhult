"""Probe the same cwd, settings and environment used to launch a session.

Contexts are broker-private memory. After a broker restart, existing Claude terminals are
unknown and automation refuses until relaunched. Never recover authority from an
agent-writable file or from the caller's claimed provider.
"""
import json
import subprocess
import time

from acc.env import environment
from acc.providers.claude_auth import AUTH_ENV, classify


def probe(exe, cwd, env, args, *, inherit=True):
    settings = []
    for index, value in enumerate(args[:-1]):
        if value in ('--settings', '--setting-sources'):
            settings += [value, args[index + 1]]
    try:
        result = subprocess.run([exe, *settings, 'auth', 'status'], cwd=cwd,
                                env={**({k: v for k, v in environment.items() if k not in AUTH_ENV} if inherit else {}), **env}, capture_output=True, text=True, timeout=5)
        return classify(json.loads(result.stdout)) if result.returncode == 0 else 'unknown'
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 'unknown'


def refresh(config, name, *, cached=False):
    context = config.auth_contexts.get(name)
    if context is None:
        return {'provider': None, 'auth_mode': 'unknown'}
    if context['provider'] != 'claude':
        return {'provider': context['provider'], 'auth_mode': 'unknown'}
    if not cached or time.monotonic() - context.get('checked', 0) > 30:
        from .containers import auth_mode
        reported = (auth_mode(config, context['acc_id'], context['container']) if context['container'] else
                    probe(context['exe'], context['cwd'], context['env'], context['args'],
                          inherit=context.get('inherit', True)))
        previous = context.get('mode')
        if previous is None:
            supplied = supplied_mode(context['env'])
            context['mode'] = reported if reported not in ('api_key', 'cloud') or reported == supplied else 'unknown'
        elif reported != previous:
            context['mode'] = reported if reported in ('subscription', 'unknown') else 'unknown'
        context['checked'] = time.monotonic()
    return {'provider': 'claude', 'auth_mode': context['mode']}


def account(config):
    from pathlib import Path
    return {'auth_mode': probe(config.providers.get('claude', 'claude'), str(Path.home()), {}, [])}


def supplied_mode(env):
    """Only environment supplied by Rynhult at launch grants automation authority."""
    if env.get('ANTHROPIC_API_KEY') or env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_FOUNDRY_API_KEY'):
        return 'api_key'
    if any(env.get(key) == '1' for key in (
            'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')):
        return 'cloud'
    return 'unknown'
