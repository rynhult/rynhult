"""Read only committed disposable fixtures; never discovers homes or starts CLIs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from acc.providers import transcripts


def inspect() -> None:
    directory = Path(__file__).with_name('native-fixtures')
    manifest = json.loads((directory / 'manifest.json').read_text())
    for provider in ('claude', 'codex'):
        path = directory / f'{provider}.jsonl'
        raw = path.read_bytes()
        details = manifest['providers'][provider]
        assert hashlib.sha256(raw).hexdigest() == details['fixture_sha256']
        records = [json.loads(line) for line in raw.splitlines()]
        assert len(records) == details['projected_records']
        messages = getattr(transcripts, provider)(path)
        assert [(item['role'], item['text']) for item in messages] == [
            ('user', 'Fixture question é 🌲'), ('assistant', 'Fixture reply é 🌲'),
            ('user', 'Fixture question é 🌲'), ('assistant', 'Fixture reply é 🌲'),
        ]
        if provider == 'claude':
            assert len({r['sessionId'] for r in records}) == 1
            assert len({r['uuid'] for r in records}) == 4
            assert all(r['version'] == '2.1.308' for r in records)
        else:
            meta = [r['payload'] for r in records if r['type'] == 'session_meta']
            assert len(meta) == 1 and meta[0]['cli_version'] == '0.163.0'
            turns = [r['payload']['turn_id'] for r in records if r['type'] == 'turn_context']
            assert len(turns) == 2 and turns[0] != turns[1]
            visible = [r for r in records if r['type'] == 'response_item'
                       and r['payload'].get('role') in ('user', 'assistant')
                       and 'Fixture ' in str(r['payload']['content'])]
            keys = [(r['payload']['internal_chat_message_metadata_passthrough']['turn_id'],
                     r['payload']['id']) for r in visible]
            assert len(keys) == len(set(keys)) == 4
        print(f'{provider}: PASS native format, resume identity and repeated-message preservation')


if __name__ == '__main__':
    inspect()
