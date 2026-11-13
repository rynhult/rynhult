"""No receipt from old history, echo, ambiguous input or a replaced transcript."""
import json
from dataclasses import replace

import pytest
from test_session_relay_claude_draft import fixture, operation

from acc.broker.claude_admission import NativeAdmission
from acc.session_relay import protocol as p


def create(tmp_path):
    root = tmp_path / 'projects'
    project = root / 'workspace'
    project.mkdir(parents=True, mode=0o700)
    path = project / 'native-fixture.jsonl'
    path.write_text('')
    path.chmod(0o600)
    reader = NativeAdmission(root, path, 'native-fixture')
    return path, reader


def records(text='exact fixture prompt'):
    return [{'type': 'user', 'sessionId': 'native-fixture', 'isSidechain': False,
             'uuid': 'user-1', 'parentUuid': 'old', 'message': {'role': 'user', 'content': text}},
            {'type': 'attachment', 'sessionId': 'native-fixture', 'isSidechain': False,
             'uuid': 'context-1', 'parentUuid': 'user-1'},
            {'type': 'assistant', 'sessionId': 'native-fixture', 'isSidechain': False,
             'uuid': 'assistant-1', 'parentUuid': 'context-1',
             'message': {'role': 'assistant', 'id': 'provider-message', 'stop_reason': 'end_turn'}}]


def append(path, rows):
    with path.open('a') as output:
        for row in rows:
            output.write(json.dumps(row) + '\n')


def test_exact_new_parent_chain_proves_provider_admission_only_once(tmp_path):
    path, reader = create(tmp_path)
    grant, _ = fixture()
    try:
        reader.begin()
        append(path, records())
        evidence = reader.evidence(operation(grant))
        assert evidence.kind is p.EvidenceKind.PROVIDER_ADMISSION
        assert evidence.native_ref == 'native-fixture:user-1:provider-message'
        with pytest.raises(p.RelayError):
            reader.evidence(operation(grant))
    finally:
        reader.close()


@pytest.mark.parametrize('fault', ['history', 'echo', 'wrong_text', 'wrong_session', 'sidechain', 'parent',
                                  'ambiguous', 'rewrite', 'replace', 'symlink', 'unknown', 'unfinished'])
def test_unknown_or_unbound_native_result_never_becomes_admission(tmp_path, fault):
    path, reader = create(tmp_path)
    grant, _ = fixture()
    try:
        if fault == 'history':
            append(path, records())
        reader.begin()
        rows = records()
        if fault == 'echo':
            rows = rows[:1]
        elif fault == 'wrong_text':
            rows[0]['message']['content'] = 'different'
        elif fault == 'wrong_session':
            rows[2]['sessionId'] = 'other'
        elif fault == 'sidechain':
            rows[2]['isSidechain'] = True
        elif fault == 'parent':
            rows[2]['parentUuid'] = 'missing'
        elif fault == 'ambiguous':
            rows.append({**rows[0], 'uuid': 'user-2'})
        elif fault == 'unknown':
            rows[1]['type'] = 'unknown-vendor-record'
        elif fault == 'unfinished':
            rows[2]['message']['stop_reason'] = None
        if fault != 'history':
            append(path, rows)
        if fault == 'replace':
            path.unlink()
            append(path, rows)
        if fault == 'symlink':
            other = path.with_suffix('.other')
            path.rename(other)
            path.symlink_to(other)
        if fault == 'rewrite':
            # Prefix mutation is checked in addition to path/inode stability.
            reader.begin()
            path.write_text(path.read_text().replace('user-1', 'user-X'))
        with pytest.raises((p.RelayError, OSError)):
            reader.evidence(operation(grant))
    finally:
        reader.close()


def test_full_operation_digest_is_required_even_for_matching_native_prompt(tmp_path):
    path, reader = create(tmp_path)
    grant, _ = fixture()
    try:
        reader.begin()
        append(path, records())
        with pytest.raises(p.RelayError):
            reader.evidence(replace(operation(grant), payload_digest=p.sha256_digest(b'other')))
    finally:
        reader.close()


@pytest.mark.parametrize('fault', ['truncate', 'rewrite-accepted-tail'])
def test_rebegin_cannot_launder_prior_accepted_history_changes(tmp_path, fault):
    path, reader = create(tmp_path)
    grant, _ = fixture()
    try:
        reader.begin()
        append(path, records())
        reader.evidence(operation(grant))
        if fault == 'truncate':
            path.write_text('')
        else:
            path.write_text(path.read_text().replace('provider-message', 'changed-provider'))
        with pytest.raises(p.RelayError) as error:
            reader.begin()
        assert error.value.code is p.ErrorCode.INTEGRITY_FAILURE
    finally:
        reader.close()
