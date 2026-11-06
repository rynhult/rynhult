"""Summarize disposable terminal probes; never reads installed instance state."""
import hashlib
import json
import statistics
import sys
import zipfile
from pathlib import Path


def report(base, results):
    events = [json.loads(line) for line in (base / 'terminal-times.jsonl').read_text().splitlines()]
    browsers = {}
    for path in results.glob('terminal-input-ordinary*/trace.zip'):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith('.trace'):
                    continue
                for line in archive.read(name).decode().splitlines():
                    for item in json.loads(line).get('attachments', []):
                        if item.get('name') == 'terminal-timeline':
                            data = json.loads(archive.read(item['file']))
                            browsers[data['session']] = data['timeline']
    # Successful traces are normally discarded: keep timing attachments outside
    # traces too when the browser test requests an output file.
    for path in results.glob('terminal-input-ordinary*/terminal-timeline.json'):
        data = json.loads(path.read_text())
        browsers[data['session']] = data['timeline']
    state = json.loads((base / 'info.json').read_text())
    rows = []
    for session, timeline in sorted(browsers.items()):
        selected = [item for item in events if item['session'] == session]
        stages = {name: [item for item in selected if item['stage'] == name]
                  for name in {item['stage'] for item in selected}}
        sends = [item for item in timeline if item['stage'] == 'browser.send' and item['frame']['t'] == 'i']
        user = [index for index, item in enumerate(sends) if not item['frame']['d'].startswith('\x1b')]
        pairs = {}
        for begin, end, label in [('server.on_input_begin', 'server.on_input_end', 'on_input'),
                                  ('supervisor.lock_wait', 'supervisor.lock_acquired', 'lock_wait'),
                                  ('broker.pty_write_begin', 'broker.pty_write_end', 'pty_write')]:
            pairs[label] = [(b['ns'] - a['ns']) / 1e6 for a, b in zip(stages.get(begin, []), stages.get(end, []))]
        ingress = stages.get('server.protocol_input', [])
        received = stages.get('server.receive', [])
        forwarded = stages.get('server.broker_write', [])
        broker = stages.get('broker.receive', [])
        pty = stages.get('broker.pty_write_begin', [])
        count = min(len(sends), len(ingress), len(received), len(forwarded), len(broker), len(pty))
        assert all(hashlib.sha256(sends[i]['frame']['d'].encode()).hexdigest()[:16]
                   == ingress[i]['hash'] == received[i]['hash'] == forwarded[i]['hash']
                   == broker[i]['hash'] == pty[i]['hash'] for i in range(count)), session
        def maximum(values):
            return round(max(values, default=0), 3)
        def total(values):
            return round(sum(values), 3)
        row = {'session': session, 'sent': len(sends), 'matched_through_pty': count,
               'on_input_total_ms': total(pairs['on_input']), 'on_input_max_ms': maximum(pairs['on_input']),
               'lock_wait_total_ms': total(pairs['lock_wait']), 'lock_wait_max_ms': maximum(pairs['lock_wait']),
               'pty_write_max_ms': maximum(pairs['pty_write'])}
        keys = [item for item in timeline if item['stage'] == 'browser.keydown']
        if len(keys) == len(user):
            deltas = [sends[i]['at'] - key['at'] for i, key in zip(user, keys)]
            row['keydown_to_send_max_ms'] = maximum(deltas)
            row['keydown_to_send_min_ms'] = round(min(deltas, default=0), 3)
        row['browser_to_protocol_max_ms'] = maximum(ingress[i]['ns'] / 1e6 - sends[i]['at'] for i in range(count))
        row['protocol_to_bridge_max_ms'] = maximum((received[i]['ns'] - ingress[i]['ns']) / 1e6 for i in range(count))
        row['broker_hop_max_ms'] = maximum((broker[i]['ns'] - forwarded[i]['ns']) / 1e6 for i in range(count))
        end = user[-1] if user and user[-1] < count and sends[user[-1]]['frame']['d'] == '\r' else None
        if end is not None:
            row['enter_send_to_pty_ms'] = round(pty[end]['ns'] / 1e6 - sends[end]['at'], 3)
            row['first_key_send_to_enter_pty_ms'] = round(pty[end]['ns'] / 1e6 - sends[user[0]]['at'], 3)
        chunks = Path(state['run_dir']) / session / 'fake.input-times.jsonl'
        if chunks.exists():
            chunks = [json.loads(line) for line in chunks.read_text().splitlines()]
            row['provider_chunks'] = len(chunks)
            row['provider_bytes'] = sum(item['bytes'] for item in chunks)
            if end is not None:
                row['enter_pty_to_last_provider_read_ms'] = round((chunks[-1]['ns'] - pty[end]['ns']) / 1e6, 3)
        rows.append(row)
    lag = [item['ms'] for item in events if item['stage'] == 'event_loop.lag']
    return {'sessions': rows, 'event_loop_lag_max_ms': round(max(lag, default=0), 3),
            'event_loop_lag_median_ms': round(statistics.median(lag), 3) if lag else 0}


if __name__ == '__main__':
    print(json.dumps(report(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
