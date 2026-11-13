"""Bounded hook facts and deterministic advice; never an authorization decision."""
from __future__ import annotations

import json
import re
import unicodedata

INPUT_LIMIT = 8192
LABELS = frozenset(('Rewrites history', 'Deletes files', 'Touches secrets paths', 'Spends money',
                    'Restarts or stops services', 'Installs packages', 'Network / external message',
                    'No specific hint'))


def validated_card(value) -> dict | None:
    """Validate stored derived facts without rebuilding or classifying them."""
    if not isinstance(value, dict) or not isinstance(value.get('label'), str) or value['label'] not in LABELS:
        return None
    required = ('command', 'cwd', 'diff')
    optional = ('tool_name', 'file_path')
    for key in required + optional:
        if key in required and key not in value:
            return None
        field = value.get(key)
        if field is not None and (not isinstance(field, str) or len(field.encode('utf-8')) > INPUT_LIMIT):
            return None
    return {key: value.get(key) for key in required + optional + ('label',)}


def classify(command: str, tool: str = '', paths: str = '') -> str:
    """First matching category wins. Shell text is evidence, not executable input.

    Deliberately conservative substring rules also match nested shell commands.
    This is a hint, not a shell parser or policy check.
    """
    text = (command[:4096] + '\n' + command[-4096:] if len(command) > 4096 else command).lower()
    tool = tool[:200].lower()
    segments = [set(re.findall(r'[\w.-]+', part)) for part in re.split(r'[\n;|&]', text) if part.strip()]

    def together(names, actions):
        return any(words.intersection(names) and words.intersection(actions) for words in segments)

    if any({'git', 'push'} <= words and words.intersection({'--force', '--force-with-lease', '-f'})
           or {'git', 'reset', '--hard'} <= words for words in segments):
        return 'Rewrites history'
    if re.search(r'\b(?:rm|rmdir|unlink)\b', text) or together({'find'}, {'-delete'}) or re.search(r'delete|remove', tool):
        return 'Deletes files'
    if re.search(r'(?:^|[/\s])(?:\.env(?:\b|\.)|\.ssh/|\.aws/|\.config/gcloud/|credentials\b|id_rsa\b)', text + ' ' + paths[:4096]):
        return 'Touches secrets paths'
    if re.search(r'\b(?:aws|gcloud|az|terraform|pulumi)\b', text):
        return 'Spends money'
    if together({'systemctl', 'service', 'docker', 'podman', 'kubectl'}, {'restart', 'stop', 'kill', 'down'}) or re.search(r'\b(?:kill|pkill|killall|shutdown|reboot)\b', text):
        return 'Restarts or stops services'
    if together({'npm', 'pnpm', 'yarn', 'pip', 'pip3', 'uv', 'apt', 'apt-get', 'brew', 'cargo'}, {'install', 'add', 'ci', 'sync'}):
        return 'Installs packages'
    if re.search(r'\b(?:curl|wget|ssh|scp|rsync|sendmail|mail)\b|https?://', text) or together({'git'}, {'push', 'pull', 'fetch', 'clone'}) or re.search(r'web|fetch|http|slack|message', tool):
        return 'Network / external message'
    return 'No specific hint'


def card(payload: dict) -> dict:
    rendered = payload.get('acc_complete_input')
    args = {}
    if isinstance(rendered, str) and len(rendered.encode('utf-8')) <= INPUT_LIMIT:
        try:
            parsed = json.loads(rendered)
            args = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            pass
    command = args.get('command')
    command = command if isinstance(command, str) else None
    # Never synthesize a diff from old/new strings or file content.
    diff = args.get('diff')
    diff = diff if isinstance(diff, str) else None
    cwd = payload.get('cwd')
    cwd = cwd if isinstance(cwd, str) and len(cwd) < 2000 and not cwd.endswith('…') else None
    path_args = args or (payload.get('tool_input') if isinstance(payload.get('tool_input'), dict) else {})
    known_paths = [path_args[k] for k in ('file_path', 'path') if isinstance(path_args.get(k), str)
                   and len(path_args[k]) < 2000 and not path_args[k].endswith('…')]
    paths = ' '.join(known_paths)
    return {'command': command, 'cwd': cwd, 'diff': diff,
            'tool_name': str(payload.get('tool_name', ''))[:200],
            'file_path': next(iter(known_paths), None),
            'label': classify(command or '', str(payload.get('tool_name', ''))[:200], paths)}


# Match the browser helper. Preserve newlines and tabs, expose directional and
# invisible controls rather than letting the renderer interpret them.
HIDDEN = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]')


def visible_text(text: str) -> str:
    return ''.join(f'⟨U+{ord(ch):04X}⟩' if HIDDEN.fullmatch(ch) or unicodedata.category(ch) == 'Cf' else ch
                   for ch in text)


def push_body(facts: dict, fallback: str = '') -> str:
    raw = facts.get('command')
    text = raw if raw is not None else fallback
    lines = text.split('\n')
    first_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    remaining = sum(bool(line.strip()) for line in lines[first_index + 1:]) if first_index is not None else 0
    first = visible_text(lines[first_index]) if first_index is not None else 'No command text; review tool input in Inbox'
    warning = ' · Hidden characters shown' if visible_text(text) != text else ''
    suffix = (f' (+{remaining} more lines)' if remaining else '') + f" · {facts['label']}" + warning
    budget = min(180, 240 - len(suffix))
    return (first[:budget - 1] + '…' if len(first) > budget else first) + suffix
