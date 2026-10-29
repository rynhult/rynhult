"""Security review of group exchange phase 2 (docs/reviews/2026-10-23-security-review-groups2.md).

Each test is a finding's proof: it failed before the matching fix.
"""
# ruff: noqa: F811
import asyncio
import base64
import hashlib
import sqlite3
import subprocess

import pytest
from helpers import write_db
from test_group_exchange import exchange_case, grant, transfer  # noqa: F401
from test_groups import grouped, two  # noqa: F401
from test_users import csrf

from acc import git_exchange
from acc.maintenance import git_lock, lease
from acc.server.models import AuditLog, Group, GroupMembership


async def published(case):
    minted = await grant(case)
    pack = base64.b64encode(git_exchange.export(case['source'] / '.git', case['sha'])).decode()
    accepted = await transfer(case, minted.json()['token'], pack)
    assert accepted.status_code == 200, accepted.text
    return accepted.json()['ref']


def writer_free(settings) -> bool:
    connection = sqlite3.connect(settings.data_dir / 'acc.db', timeout=0.2)
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


async def test_waiting_for_the_git_lease_never_holds_the_database_writer(exchange_case):
    """M1: a viewer's read (or a maintainer's write) queued behind a long Git capture held
    SQLite's writer lock for up to 60 s, so every other ACC write failed meanwhile."""
    case = exchange_case
    await published(case)
    settings = case['owner'].app.state.ctx.settings
    with write_db(case['owner'].app.state.db) as s:
        s.get(GroupMembership, (case['group']['id'], 2)).role = 'viewer'
        s.get(Group, case['group']['id']).revision += 1
        s.commit()
    with lease(git_lock(settings.git_dir), timeout=5):
        reading = asyncio.create_task(case['member'].get(case['base'] + '/published/tree', params={'commit': case['sha']}))
        await asyncio.sleep(0.5)
        assert writer_free(settings)
    assert (await reading).status_code == 200
    with write_db(case['owner'].app.state.db) as s:
        s.get(GroupMembership, (case['group']['id'], 2)).role = 'maintainer'
        s.get(Group, case['group']['id']).revision += 1
        s.commit()
    with lease(git_lock(settings.git_dir), timeout=5):
        tagging = asyncio.create_task(case['member'].post(case['base'] + '/exchange/tags',
            json={'name': 'v1', 'commit': case['sha']}, headers=csrf(case['member'])))
        await asyncio.sleep(0.5)
        assert writer_free(settings)
    assert (await tagging).status_code == 201


def test_publication_stores_only_objects_the_exchange_lacks(tmp_path):
    """M2: each fast-forward, tag, integration or restore copied the entire history again,
    so ordinary use filled the 128 MiB store and then refused every publication."""
    import os

    from test_gitbackup import commit, make_repo
    source = make_repo(tmp_path / 'source')
    (source / 'large.bin').write_bytes(os.urandom(1 << 20))
    old = git_exchange.ZERO
    target = git_exchange.initialize(tmp_path / 'exchange.git', 'exchange')
    for step in range(4):
        new = commit(source, f'step{step}.txt', str(step))
        git_exchange.publish(target, git_exchange.export(source / '.git', new), 'refs/heads/people/a/s', old, new)
        old = new
    git_exchange.publish(target, git_exchange.export(target, old), 'refs/tags/v1', git_exchange.ZERO, old)
    used = sum(p.stat().st_size for p in (target / 'objects').rglob('*') if p.is_file())
    assert used < 3 << 19, used
    assert git_exchange.read_file(target, old, 'step3.txt') == b'3'
    subprocess.run(['git', '--git-dir', str(target), 'fsck', '--strict', '--no-dangling'], check=True, capture_output=True)


async def test_one_writer_cannot_consume_the_maintainer_ref_reserve(exchange_case):
    """M3: session ids are caller-chosen, so one writer could take all 128 refs; refs can't be
    deleted, so integration, tags and restore then failed until an operator edited the store."""
    case = exchange_case
    ref = await published(case)
    settings = case['owner'].app.state.ctx.settings
    store = settings.git_dir / 'exchange' / f"{case['group_project']['id']}.git"
    stable = hashlib.sha256(b'alex').hexdigest()[:32]
    assert ref.startswith(f'refs/heads/people/{stable}/')
    for n in range(126):
        subprocess.run(['git', '--git-dir', str(store), 'update-ref', f'refs/heads/people/{stable}/{n:012x}', case['sha']], check=True)
    extra = await grant(case, session_id='f' * 32)
    assert (await transfer(case, extra.json()['token'],
        base64.b64encode(git_exchange.export(case['source'] / '.git', case['sha'])).decode())).status_code == 409
    tagged = await case['member'].post(case['base'] + '/exchange/tags', json={'name': 'v1', 'commit': case['sha']}, headers=csrf(case['member']))
    assert tagged.status_code == 201, tagged.text


def test_exchange_grants_are_capped_per_person():
    """L1: 4,096 grants was one global pool; one person could exhaust it for everybody."""
    from fastapi import HTTPException

    from acc.server.api.group_exchange import Tokens
    tokens = Tokens()
    with pytest.raises(HTTPException) as refused:
        for _ in range(4096):
            tokens.mint({'person': 'mallory'})
    assert refused.value.status_code == 429
    assert tokens.mint({'person': 'alex'})


async def test_unauthenticated_group_requests_keep_the_default_body_limit(grouped):
    """L2: every /api/groups route accepted 12 MiB bodies before authentication."""
    from test_users import browser
    async with browser(grouped['owner']) as anonymous:
        body = b'{"name": "' + b'x' * (5 << 20) + b'"}'
        refused = await anonymous.post(f"/api/groups/{grouped['group']['id']}/projects", content=body, headers={'Content-Type': 'application/json'})
        assert refused.status_code == 413
        base = f"/api/groups/{grouped['group']['id']}/projects/{grouped['group_project']['id']}"
        transfer = await anonymous.post(base + '/exchange/transfer', content=body,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer nonsense'})
        assert transfer.status_code == 401


async def test_one_grant_serves_one_git_request_at_a_time(exchange_case):
    """L3: a reusable read grant let one caller buffer many 8 MiB bodies at once."""
    case = exchange_case
    await published(case)
    fetched = await grant(case, 'fetch')
    headers = {'Authorization': 'Bearer ' + fetched.json()['token'], 'Content-Type': 'application/x-git-upload-pack-request'}
    release = asyncio.Event()

    async def slow():
        yield b'0000'
        await release.wait()

    first = asyncio.create_task(case['member'].post(case['base'] + '/exchange/git/git-upload-pack', content=slow(), headers=headers))
    await asyncio.sleep(0.3)
    second = await case['member'].post(case['base'] + '/exchange/git/git-upload-pack', content=b'0000', headers=headers)
    release.set()
    await first
    assert second.status_code == 429


async def test_backup_downloads_and_private_recovery_are_audited(exchange_case):
    """L4: group backup downloads and private recovery creation/download left no receipt."""
    from pathlib import Path

    from sqlalchemy import select
    from test_gitbackup import commit, git
    case = exchange_case
    await published(case)
    backed = await case['member'].post(case['base'] + '/exchange/backup', headers=csrf(case['member']))
    assert (await case['member'].get(case['base'] + '/exchange/backup/' + backed.json()['snapshot'])).status_code == 200
    assert (await case['owner'].post(case['base'] + f"/placements/{case['placement']}/provision")).status_code == 200
    checked = await case['member'].post(case['base'] + '/exchange/checkout', json={
        'placement_id': case['placement'], 'session_id': 'b' * 12, 'commit': case['sha']}, headers=csrf(case['member']))
    path = Path(checked.json()['path'])
    git(path, 'config', 'user.name', 'Alex Example')
    git(path, 'config', 'user.email', 'alex@example.com')
    private = commit(path, 'private.txt', 'private')
    endpoint = case['base'] + f"/placements/{case['placement']}/private-backups"
    saved = await case['member'].post(endpoint, json={'session_id': 'b' * 12, 'commit': private}, headers=csrf(case['member']))
    assert saved.status_code == 201
    assert (await case['member'].get(endpoint + '/' + saved.json()['snapshot'])).status_code == 200
    with case['owner'].app.state.db() as s:
        actions = set(s.scalars(select(AuditLog.action).where(AuditLog.actor == 'alex')))
    assert {'group.backup_download', 'group.private_backup', 'group.private_download'} <= actions
    # Personal receipts stay out of the group-visible audit projection.
    shown = {a['action'] for a in (await case['member'].get(f"/api/groups/{case['group']['id']}/audit")).json()}
    assert 'group.backup_download' in shown
    assert not shown & {'group.private_backup', 'group.private_download'}


def bomb(expanded: int) -> tuple[bytes, str]:
    """A tiny pack whose one blob inflates to `expanded` zero bytes, plus its commit id."""
    import struct
    import zlib

    def header(kind, size):
        first, size, out = (kind << 4) | (size & 15), size >> 4, bytearray()
        while size:
            out.append(first | 0x80)
            first, size = size & 127, size >> 7
        return bytes(out + bytes([first]))

    def oid(kind, body):
        return hashlib.sha1(kind + b' ' + str(len(body)).encode() + b'\0' + body).digest()
    squeeze, digest, chunk = zlib.compressobj(9), hashlib.sha1(b'blob %d\0' % expanded), b'\0' * (1 << 20)
    compressed = bytearray()
    for _ in range(expanded >> 20):
        compressed += squeeze.compress(chunk)
        digest.update(chunk)
    compressed += squeeze.flush()
    tree = b'100644 big\0' + digest.digest()
    commit = b'tree %s\nauthor A <a@example.com> 0 +0000\ncommitter A <a@example.com> 0 +0000\n\nx\n' % oid(b'tree', tree).hex().encode()
    body = (b'PACK' + struct.pack('>II', 2, 3) + header(3, expanded) + bytes(compressed) + header(2, len(tree))
            + zlib.compress(tree) + header(1, len(commit)) + zlib.compress(commit))
    return body + hashlib.sha1(body).digest(), oid(b'commit', commit).hex()


def test_decompression_bomb_packs_are_refused_before_git_inflates_them(tmp_path, monkeypatch):
    """M4: a 1 MiB pack expanding to 1 GiB took ~9 s of Git work (8 MiB: ~minutes) while the
    publication held the Git lease and SQLite's writer, then stored the blob for every fetcher."""
    pack, commit = bomb(256 << 20)
    target = git_exchange.initialize(tmp_path / 'exchange.git', 'exchange')
    real = git_exchange.run

    def no_index(repo, *args, **kwargs):
        assert 'index-pack' not in args, 'Git indexed an oversized pack'
        return real(repo, *args, **kwargs)
    monkeypatch.setattr(git_exchange, 'run', no_index)
    with pytest.raises(ValueError, match='expand'):
        git_exchange.publish(target, pack, 'refs/heads/people/a/s', git_exchange.ZERO, commit)


async def test_git_http_push_of_a_bomb_is_refused_before_staging(exchange_case, monkeypatch):
    from acc.server import group_git_http
    case = exchange_case
    pack, commit = bomb(256 << 20)
    minted = await grant(case, new_sha=commit)
    ref = minted.json()['ref']
    line = f'{git_exchange.ZERO} {commit} {ref}\0report-status\n'.encode()
    body = b'%04x' % (len(line) + 4) + line + b'0000' + pack

    def staged(*args, **kwargs):
        raise AssertionError('receive-pack ran on an oversized pack')
    monkeypatch.setattr(group_git_http, 'serve', staged)
    pushed = await case['member'].post(case['base'] + '/exchange/git/git-receive-pack', content=body,
        headers={'Authorization': 'Bearer ' + minted.json()['token'], 'Content-Type': 'application/x-git-receive-pack-request'})
    assert pushed.status_code == 409
    assert 'expand' in pushed.text


def test_pack_scan_counts_delta_results_and_refuses_lying_sizes():
    import struct
    import zlib

    def pack(*entries):
        body = b'PACK' + struct.pack('>II', 2, len(entries)) + b''.join(entries)
        return body + hashlib.sha1(body).digest()
    # A 6-byte ref delta claiming a 1 GiB result: two copy instructions could build it.
    delta = bytes([1]) + bytes([0x80, 0x80, 0x80, 0x80, 0x04]) + b'\x91\x00\xff'
    with pytest.raises(ValueError, match='expand'):
        git_exchange.inspect_pack(pack(bytes([0x70 | len(delta)]) + b'\x11' * 20 + zlib.compress(delta)))
    # A blob declaring 10 bytes that inflates to far more.
    with pytest.raises(ValueError, match='declared'):
        git_exchange.inspect_pack(pack(bytes([0x3a]) + zlib.compress(b'\0' * (1 << 20))))
    with pytest.raises(ValueError):
        git_exchange.inspect_pack(pack(bytes([0x3a]) + b'not zlib at all'))
