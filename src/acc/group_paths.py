"""Authority-free validation of group-visible relative paths."""

PROTECTED = {'.git', '.env', '.ssh', '.aws', '.acc', '.claude', '.codex', '.config', '.cache', '.local',
             '.npmrc', '.netrc', '.pypirc', 'session.env', 'vault.key', 'id_rsa', 'id_ed25519',
             'secrets', 'keys', 'credentials', 'transcripts', 'artifacts', 'node_modules'}


def safe_path(value: str) -> str:
    if not value or len(value) > 300 or '\\' in value or any(ord(c) < 32 for c in value):
        raise ValueError('choose a relative source path')
    parts = value.rstrip('/').split('/')
    if any(not p or p in {'.', '..'} or p.casefold() in PROTECTED or p.casefold().startswith('.env')
           or p.casefold().endswith(('.pem', '.key', '.p12', '.pfx')) for p in parts):
        raise ValueError('protected or invalid source path')
    return value


def overlap(a: str, b: str) -> bool:
    a, b = a.casefold().rstrip('/'), b.casefold().rstrip('/')
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')
