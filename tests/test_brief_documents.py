"""Document intake and bounded broker extraction; all content is artificial."""
import base64
import io
import zipfile

import pytest

from acc.server.orchestrator import project_briefs as brief

pytest_plugins = ['project_brief_migrations']


@pytest.fixture(autouse=True)
def v2(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')


def archive(name, content):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, content)
    return stream.getvalue()


@pytest.mark.parametrize('extension,data,expected', [
    ('md', b'# Product\nRoom bookings', 'Room bookings'),
    ('txt', b'Room bookings', 'Room bookings'),
    ('html', b'<h1>Rooms</h1><script>evil()</script><p>Book a room</p>', 'Book a room'),
    ('rtf', br'{\rtf1\ansi Room \b bookings\b0\par No macros}', 'bookings'),
    ('docx', archive('word/document.xml', '<w:document xmlns:w="urn:w"><w:p><w:t>Rooms</w:t></w:p></w:document>'), 'Rooms'),
    ('odt', archive('content.xml', '<doc><p>Rooms</p></doc>'), 'Rooms'),
], ids=['markdown', 'plain-text', 'html', 'rtf', 'docx', 'odt'])
def test_extract_formats(extension, data, expected):
    from acc.broker.document_extract import extract
    result = extract(data, extension)
    assert expected in result['sections'][0]['text']
    assert result['sections'][0]['location']
    assert result['untrusted_evidence'] is True
    assert 'evil()' not in result['sections'][0]['text']


@pytest.mark.parametrize('extension,data', [
    ('docx', b'broken'), ('pdf', b'%PDF-broken'),
    ('odt', archive('content.xml', '<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><p>&x;</p>')),
    ('docx', archive('word/document.xml', 'a' * (21 << 20))),
    ('txt', b'\xff\xfe\x00'), ('rtf', b'not rtf'),
], ids=['broken-docx', 'broken-pdf', 'xml-entity', 'zip-bomb', 'invalid-utf8', 'broken-rtf'])
def test_hostile_or_malformed_is_bounded_failure(extension, data):
    from acc.broker.document_extract import extract
    with pytest.raises(ValueError):
        extract(data, extension)


def test_text_output_bound():
    from acc.broker.document_extract import extract
    result = extract(b'a' * 300000, 'txt')
    assert sum(len(s['text']) for s in result['sections']) <= 200000
    assert result['truncated']


async def test_upload_keeps_extraction_failure_private_and_proposes_cited_fields(api, project, env):
    from test_project_briefs import person
    api = await person(api, env)
    url = f'/api/projects/{project["id"]}/brief'
    response = await api.post(url + '/documents', json={'files': [
        {'name': 'rooms.md', 'data': base64.b64encode(b'# Scope\nA room booking board').decode()},
        {'name': 'broken.docx', 'data': base64.b64encode(b'broken').decode()},
    ]})
    assert response.status_code == 200, response.text
    documents = response.json()['documents']
    assert documents[0]['document']['status'] == 'ready'
    assert documents[1]['document']['status'] == 'error'
    assert (await api.get(url)).json()['current_version'] == 0
    with api.app.state.db() as db:
        value = brief.propose_documents(db, project['id'], 'session:lead', 0,
            {'product': 'Room booking board', 'design': 'Calm layout'},
            {'product': [{'reference': documents[0]['reference_id'], 'location': 'section 1'}],
             'design': [{'reference': documents[0]['reference_id'], 'location': 'section 1'}]})
        out = brief.version_out(db, value)
        db.commit()
    assert out['source'] == 'documents' and out['state'] == 'proposed'
    assert out['citations']['product'][0]['reference'] == documents[0]['reference_id']
    assert 'rooms.md' not in out['markdown']
    accepted = await api.post(url + f'/versions/{out["version"]}/accept', json={'version': 0, 'fields': ['product']})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['submitted'] == {'product': 'Room booking board'}
    assert 'design' not in accepted.json()['submitted']
    download = await api.get(url + '/' + 'references/' + documents[1]['reference_id'])
    assert download.content == b'broken' and download.headers['cache-control'] == 'no-store'


async def test_document_count_and_size_limits(api, project):
    url = f'/api/projects/{project["id"]}/brief/documents'
    file = {'name': 'room.txt', 'data': base64.b64encode(b'Room').decode()}
    assert (await api.post(url, json={'files': [file] * 11})).status_code == 422
    assert (await api.post(url, json={'files': [{'name': 'room.exe', 'data': file['data']}]})).status_code == 422
    oversized = {'name': 'room.txt', 'data': base64.b64encode(b'x' * ((10 << 20) + 1)).decode()}
    assert (await api.post(url, json={'files': [oversized]})).status_code == 422


def pdf(pages=1):
    # Tiny valid PDF with explicit page boundaries, no optional PDF library.
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               f'<< /Type /Pages /Count {pages} /Kids [ '.encode() + b' '.join(f'{3+n*2} 0 R'.encode() for n in range(pages)) + b' ] >>']
    for n in range(pages):
        objects += [f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents {4+n*2} 0 R >>'.encode(),
                    b'<< /Length 45 >>\nstream\nBT /F1 12 Tf 20 200 Td (Room bookings) Tj ET\nendstream']
    result, offsets = b'%PDF-1.4\n', [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(result)); result += f'{n} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(result)
    result += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    result += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return result + f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


def test_broker_worker_pdf_and_network_isolation(tmp_path):
    from acc.broker.document_extract import run
    result = run(None, 'pdf', base64.b64encode(pdf(2)).decode())
    assert result['status'] == 'ready', result
    assert [s['location'] for s in result['sections']] == ['page 1', 'page 2']
    assert all('Room bookings' in s['text'] for s in result['sections'])


def test_network_filter_is_inherited_by_children():
    import subprocess
    import sys
    code = ('from acc.broker.document_extract import deny_network; deny_network(); '
            'import subprocess,sys; '
            'raise SystemExit(subprocess.call([sys.executable,"-c",'
            '"import socket; socket.socket()\"]))')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True)
    assert result.returncode != 0
    assert b'Operation not permitted' in result.stderr


async def test_kickoff_and_spool_document_proposal_without_an_accepted_brief(api, project, env):
    from test_team import lead, request
    result = await api.post(f'/api/projects/{project["id"]}/brief/documents', json={'files': [
        {'name': 'scope.txt', 'data': base64.b64encode(b'Room bookings').decode()}]})
    assert result.status_code == 200, result.text
    x = await lead(api, project, env)
    assert 'brief_propose_documents' in x['launch_spec']['inputs']['initial_prompt']
    assert 'Room bookings' not in x['launch_spec']['inputs']['initial_prompt']
    evidence = await request(api, env, x, 'brief_documents')
    assert evidence['ok'], evidence
    proposed = await request(api, env, x, 'brief_propose_documents', base_version=0,
        fields={'product': 'Room booking board'},
        citations={'product': [{'reference': 'REF-1', 'location': 'section 1'}]})
    assert proposed['ok'], proposed
    view = (await api.get(f'/api/projects/{project["id"]}/brief')).json()
    assert view['current_version'] == 0
    assert view['history'][0]['source'] == 'documents'
    bad = await request(api, env, x, 'brief_propose_documents', base_version=0,
        fields={'product': 'Invented'}, citations={'product': [{'reference': 'REF-1', 'location': 'page 999'}]})
    assert not bad['ok']


async def test_total_bytes_and_accumulated_count_are_project_bounded(api, project, env, monkeypatch):
    # Run validation on compact metadata; do not send 40 MiB through every test layer.
    from types import SimpleNamespace
    monkeypatch.setattr(brief, 'document_rows', lambda db, pid: [SimpleNamespace(size=39 << 20)] * 1)
    with pytest.raises(brief.Refused, match='40 MiB'):
        brief.validate_documents(None, project['id'], [{'name': 'scope.txt', 'data': base64.b64encode(b'x' * (2 << 20)).decode()}])
    monkeypatch.setattr(brief, 'document_rows', lambda db, pid: [SimpleNamespace(size=4)] * 10)
    with pytest.raises(brief.Refused, match='10 source'):
        brief.validate_documents(None, project['id'], [{'name': 'scope.txt', 'data': 'eA=='}])


async def test_helpers_only_receive_assigned_document_text(api, project, env):
    import json

    from helpers import pause_supervisor
    from test_team import lead

    from acc.server.models import Session
    url = f'/api/projects/{project["id"]}/brief'
    response = await api.post(url + '/documents', json={'files': [
        {'name': 'assigned.txt', 'data': base64.b64encode(b'ASSIGNED EVIDENCE').decode()},
        {'name': 'private.txt', 'data': base64.b64encode(b'UNASSIGNED SECRET EVIDENCE').decode()}]})
    assert response.status_code == 200
    assert (await api.post(url, json={'version': 0, 'fields': {'product': 'Rooms'}, 'references': ['REF-1', 'REF-2']})).status_code == 200
    x = await lead(api, project, env)
    await pause_supervisor(api)
    with api.app.state.db() as db:
        parent = db.get(Session, x['id'])
        helper = Session(id='b' * 32, project_id=project['id'], launch_spec_id=parent.launch_spec_id,
            name='Document helper', tmux_name='document-helper-test', provider=parent.provider, run_as=parent.run_as,
            owner=parent.owner, team_id=x['id'], parent_session_id=x['id'])
        db.add(helper)
        db.commit()
    supervisor = api.app.state.supervisor
    # Share through the real lead handler: sharing changes assignments, it does
    # not copy every project document or give helpers any proposal authority.
    shared = await brief._tool(supervisor, x['id'], {'action': 'brief_share',
        'helper': helper.id, 'references': ['REF-1']})
    assert shared == {'helper': helper.id, 'references': ['REF-1']}
    from acc.server.supervisor_common import LaunchRefused
    with pytest.raises(LaunchRefused, match='only the lead'):
        await brief._tool(supervisor, helper.id, {'action': 'brief_share', 'helper': helper.id, 'references': ['REF-2']})
    with pytest.raises(LaunchRefused, match='not assigned'):
        await brief._tool(supervisor, helper.id, {'action': 'brief_reference', 'reference': 'REF-2'})
    with pytest.raises(LaunchRefused, match='only the lead'):
        await brief._tool(supervisor, helper.id, {'action': 'brief_propose_documents'})
    visible = await brief._tool(supervisor, helper.id, {'action': 'brief_documents'})
    assert visible['untrusted_evidence']
    assert 'ASSIGNED EVIDENCE' in json.dumps(visible)
    assert 'UNASSIGNED SECRET EVIDENCE' not in json.dumps(visible)
    assert 'private.txt' not in json.dumps(visible)


def test_large_pdf_worker_stays_bounded():
    from acc.broker.document_extract import run
    result = run(None, 'pdf', base64.b64encode(pdf(250)).decode())
    assert result['status'] in ('ready', 'error')
    if result['status'] == 'ready':
        assert len(result['sections']) == 200
        assert result['truncated']


async def test_invalid_citations_never_save_or_widen_mode(api, project):
    url = f'/api/projects/{project["id"]}/brief'
    assert (await api.post(url + '/documents', json={'files': [{'name': 'scope.txt', 'data': 'Um9vbXM='}]})).status_code == 200
    with api.app.state.db() as db:
        for fields, citations in [({'product': 'Rooms'}, {}),
            ({'product': 'Rooms', 'run_mode': 'build'}, {'product': [{'reference': 'REF-1', 'location': 'section 1'}]}),
            ({'product': 'Rooms'}, {'product': [{'reference': 'REF-999', 'location': 'section 1'}]})]:
            with pytest.raises(brief.Refused):
                brief.propose_documents(db, project['id'], 'session:lead', 0, fields, citations)
        assert brief.view(db, project['id'])['current_version'] == 0
        assert not brief.view(db, project['id'])['history']


async def test_lead_can_receive_original_before_any_pin(api, project, env):
    from test_team import lead, request
    response = await api.post(f'/api/projects/{project["id"]}/brief/documents', json={'files': [
        {'name': 'scope.txt', 'data': base64.b64encode(b'Rooms').decode()}]})
    assert response.status_code == 200
    x = await lead(api, project, env)
    result = await request(api, env, x, 'brief_reference', reference='REF-1')
    assert result['ok'], result


async def test_malformed_pdf_is_retained_and_text_is_not_in_metadata(api, project):
    url = f'/api/projects/{project["id"]}/brief'
    result = await api.post(url + '/documents', json={'files': [
        {'name': 'broken.pdf', 'data': base64.b64encode(b'not a PDF').decode()},
        {'name': 'scope.txt', 'data': base64.b64encode(b'PRIVATE DOCUMENT BODY').decode()}]})
    assert result.status_code == 200, result.text
    assert result.json()['documents'][0]['document']['status'] == 'error'
    metadata = await api.get(url)
    assert 'PRIVATE DOCUMENT BODY' not in metadata.text
    assert 'sections' not in metadata.text
