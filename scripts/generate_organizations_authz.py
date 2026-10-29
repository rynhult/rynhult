"""Render the reviewed inventory. Never auto-classify newly discovered routes.

Run with: python scripts/generate_organizations_authz.py [--check]
The registered-route/MATRIX gate lives in tests/test_ownership.py.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render(records: list[dict]) -> str:
    lines = [
        '# Organizations authorization inventory', '',
        'Generated from `tests/ownership_inventory.json` by `scripts/generate_organizations_authz.py`. '
        'Review that explicit catalogue, then regenerate; new routes are never classified automatically.', '',
        'This describes current personal/group behavior, not proposed org permissions. Nothing is enabled. '
        'The pytest gate extends `tests/test_ownership.py` MATRIX: every registered `/api/` method and WebSocket '
        'must have exactly one classification, and MATRIX refusal statuses must agree. Fixed routes are included. '
        'Static UI fallback and framework OpenAPI/docs routes are outside this API inventory. '
        'Recovery/startup-hold catchalls deny traffic and are not additional execution authority. '
        'HA-only routes are checked against the HA app in tests/test_ha.py; desktop-only routes are checked '
        'against the desktop composition in tests/test_desktop_sessions.py. Desktop additionally requires '
        'the native launch capability on every HTTP/WebSocket request.', '',
        'Owner means the trusted installation owner. The installation bearer API token has owner authority '
        'except where a human identity is required. Browser mutations also require CSRF/Origin validation. '
        'Group maintainer/member/viewer are group-scoped roles, not org roles. Exchange requires actual '
        'membership even for installation owners; its bootstrap token is not a collaboration identity.', '',
        'Audit cells name current durable actions (including delegated service actions), or explicitly record '
        'a gap/absence. They do not claim all reads, failures or denied operations are audited. '
        'Phase 2 must add scoped actor IDs, denial/access coverage, redaction and transactional outbox delivery. '
        'Existing raw detail fields are not a safe org audit projection. The gate enforces inventory completeness, '
        'not semantic authorization correctness; the existing cross-person tests and group tests exercise enforcement.', '',
        f'**{len(records)} registered method/path pairs**, including both WebSockets.', '',
        '| Method and route | Required principal | Resource ownership rule | Current audit action | Evidence |',
        '|---|---|---|---|---|',
    ]
    for r in sorted(records, key=lambda r: (r['path'], r['method'])):
        values = [f"`{r['method']} {r['path']}`", r['principal'], r['ownership'], r['audit'],
                  f"[{r['handler']}]({'../' + r['source']})"]
        lines.append('| ' + ' | '.join(v.replace('|', '\\|').replace('\n', ' ') for v in values) + ' |')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    result = render(json.loads((ROOT / 'tests/ownership_inventory.json').read_text()))
    target = ROOT / 'docs/ORGANIZATIONS-AUTHZ.md'
    if args.check:
        return 0 if target.read_text() == result else 1
    target.write_text(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
