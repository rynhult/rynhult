"""Positive native decoder/drain component, inside the offline Claude fixture.

Main's receipt is synthetic here; its acceptance is gated on the real fixture
model receiving this exact native input and native Stop. No production admission
or full-editor release qualification is inferred from this component proof.
"""
import errno
import fcntl
import json
import os
import pty
import struct
import subprocess
import termios
import time
from pathlib import Path
from types import SimpleNamespace

from acc.broker.claude_admission import NativeAdmission
from acc.broker.relay_enclosure import Enclosure
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdapterOperation, InputGrant, TerminalRun
from acc.session_relay.claude_draft import ClaudeDraft


def native_tui(settings, env, observer_file, requests, native):
    text = b'Native TUI isolated fixture turn'
    grant = InputGrant(TerminalRun('fixture', 'run-tui'), 'fixture-person-tab',
        p.Actor(p.ActorKind.PERSON, 'fixture-person', p.Channel.TERMINAL), 'fixture-stream', 2, p.Fence(1, 'b', 'h'))
    boundary = ClaudeDraft(grant, native)
    root = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects'
    paths = list(root.glob('*/' + native + '.jsonl'))
    assert len(paths) == 1
    admission = NativeAdmission(root, paths[0], native)
    enclosed = Enclosure(Path('/evidence/owners'), SimpleNamespace(run_id='run-tui'), '/usr/bin/claude')
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 120, 0, 0))
    args = ['/usr/bin/python3', '-I', '/evidence/pty_exec.py', str(enclosed.cli), '--resume', native,
            '--model', 'claude-sonnet-4-5', '--settings', str(settings)]
    before = len(observer_file.read_text().splitlines()) if observer_file.exists() else 0
    child = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave,
                             env={**env, 'TERM': 'xterm-256color'}, start_new_session=True)
    os.close(slave)
    output, hooks = bytearray(), []
    entered = decoded = completed = refused_partial = refused_decoder_only = False
    pending_enter = None
    drained = None
    transcript_waits = 0
    transcript_snapshots = []
    deadline = time.monotonic() + 20
    def read_hooks():
        if not observer_file.exists():
            return []
        # A hook can be mid-write; never reinterpret a partial JSON line as a
        # positive observation. The newline is its complete fixture record.
        raw = observer_file.read_text()
        complete = raw[:raw.rfind('\n') + 1].splitlines()
        return [json.loads(line) for line in complete[before:]]
    try:
        while child.poll() is None and time.monotonic() < deadline and len(output) < 128 << 10:
            try:
                data = os.read(master, 65536)
                output.extend(data)
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
            except BlockingIOError:
                pass
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            hooks = read_hooks()
            if not entered and any(h.get('hook_event_name') == 'SessionStart' for h in hooks) and time.monotonic() > deadline - 16:
                admission.begin()
                boundary.reserve(grant, 1, text)
                os.write(master, text)
                try:
                    boundary.drain(grant)
                except p.RelayError:
                    refused_partial = True
                pending_enter = time.monotonic() + .7
                entered = True
            if pending_enter and time.monotonic() >= pending_enter:
                boundary.reserve(grant, 2, b'\r')
                os.write(master, b'\r')
                pending_enter = None
            prompt = next((i for i, h in enumerate(hooks) if h.get('hook_event_name') == 'UserPromptSubmit'
                           and h.get('prompt_digest') == p.sha256_digest(text)), None)
            if prompt is not None and not decoded:
                boundary.observe_prompt(hooks[prompt]['session_id'], hooks[prompt]['prompt_digest'])
                params = {'text': text.decode(), 'expectedControlRevision': grant.epoch}
                operation = AdapterOperation('fixture-native-operation', 'fixture', 'run-tui', 'turn.submit',
                    p.operation_digest('turn.submit', params), grant.actor, grant.fence, 9999999999999, params)
                boundary.bind_operation(operation)
                try:
                    boundary.drain(grant)
                except p.RelayError:
                    refused_decoder_only = True
                assert boundary.processed == 0 and boundary.draft == text.decode()
                decoded = True
            stop = prompt is not None and any(h.get('hook_event_name') == 'Stop' for h in hooks[prompt + 1:])
            admitted = any('count_tokens' not in r['path'] and any(text.decode() in m['content'] for m in r['messages']) for r in requests)
            if decoded and stop and admitted:
                try:
                    evidence = admission.evidence(operation)
                except p.RelayError as error:
                    # Stop precedes native transcript flush. Wait only for missing
                    # records under the original cursor and existing deadline;
                    # exact provider evidence is still mandatory before confirm.
                    raw = os.pread(admission.fd, 1 << 20, admission.offset)
                    rows = [json.loads(line) for line in raw[:raw.rfind(b'\n') + 1].splitlines()]
                    users = [row for row in rows if row.get('type') == 'user'
                             and isinstance(row.get('message', {}).get('content'), str)]
                    summary = {'rows': len(rows), 'users': len(users),
                               'exactUsers': sum(row['message']['content'] == text.decode() for row in users),
                               'completedAssistant': sum(row.get('type') == 'assistant'
                                   and row.get('message', {}).get('stop_reason') == 'end_turn' for row in rows)}
                    if (error.code is p.ErrorCode.INPUT_CONFLICT
                            and summary['completedAssistant'] == 0
                            and (summary['users'] == 0 or summary['users'] == summary['exactUsers'] == 1)):
                        transcript_waits += 1
                        if len(transcript_snapshots) < 16:
                            transcript_snapshots.append(summary)
                        time.sleep(.03)
                        continue
                    raise RuntimeError('Strict native draft proof: ' + json.dumps(summary)) from error
                receipt = p.Receipt(operation.operation_id, operation.session_id, operation.run_id, operation.method,
                                    operation.payload_digest, operation.actor)
                receipt = p.advance_receipt(receipt, p.ReceiptState.SUBMITTING)
                receipt = p.advance_receipt(receipt, p.ReceiptState.ACCEPTED, evidence=evidence)
                boundary.confirm(receipt)
                drained = p.to_wire(boundary.drain(grant))
                completed = True
                break
            time.sleep(.03)
    finally:
        if completed and child.poll() is None:
            os.write(master, b'/exit')  # fixture administrative exit, outside claimed text profile
            time.sleep(.7)
            os.write(master, b'\r')
            grace = time.monotonic() + 4
            while child.poll() is None and time.monotonic() < grace:
                try:
                    output.extend(os.read(master, 65536))
                except (BlockingIOError, OSError):
                    pass
                time.sleep(.03)
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=8)
        os.close(master)
    admission.close()
    started = json.loads((enclosed.path / 'started.json').read_text())
    empty = enclosed.proof('exit.json', f"{started['pid']}:{started['started']}")
    enclosed.close()
    assert completed and refused_partial and refused_decoder_only and empty
    return {'transcript_flush_waits': transcript_waits, 'transcript_flush_snapshots': transcript_snapshots, 'provider_evidence_kind': evidence.kind.value, 'provider_ref_contains_native_message': 'msg_fixture_' in evidence.native_ref, 'completed': completed, 'entered': entered, 'exit': child.returncode, 'empty_tree': empty,
            'hooks': hooks, 'output_bytes': len(output), 'partial_draft_refused': refused_partial,
            'decoder_only_refused': refused_decoder_only, 'drained': drained,
            'positive_native_empty': boundary.verified_empty, 'receipt_scope': 'production NativeAdmission evidence from new exact native user/provider parent chain; fixture main ledger',
            'fixture_preferences_seeded': True, 'terminalMediated': False}
