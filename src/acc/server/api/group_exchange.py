"""Phase 2: explicit, bounded publication; no live checkout or group launches."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update

from ... import git_exchange as git
from ...maintenance import git_lock, lease
from .. import auth, gitbackup, group_git_http, group_recovery
from ..broker_client import BrokerError
from ..db import WRITE_INTENT
from ..models import (
    AuditLog,
    Group,
    GroupCheckout,
    GroupMembership,
    GroupMergeApproval,
    GroupProjectOwner,
    Placement,
    Project,
    User,
)
from .context import Context

PERSON_GRANTS = 64  # outstanding per person; the 4,096 pool is shared by everyone
ACTIVE_GIT = 8  # buffered Git HTTP requests at once, across all grants
PERSON_REFS = 32  # published branches per person
RESERVED_REFS = 16  # integration branches, tags and restores only
RESERVED_STORE = 16 << 20


class GrantIn(BaseModel):
    operation: Literal['publish', 'fetch']
    placement_id: int | None = None
    session_id: str = Field(pattern=r'^[a-f0-9]{12,32}$')
    old_sha: str = Field(pattern=r'^[a-f0-9]{40}$')
    new_sha: str = Field(pattern=r'^[a-f0-9]{40}$')


class PackIn(BaseModel):
    data: str | None = Field(default=None, max_length=4 * -(-git.LIMIT // 3))


class CheckoutIn(BaseModel):
    placement_id: int
    session_id: str = Field(pattern=r'^[a-f0-9]{12,32}$')
    commit: str = Field(pattern=r'^[a-f0-9]{40}$')


class PrivateBackupIn(BaseModel):
    session_id: str = Field(pattern=r'^[a-f0-9]{12,32}$')
    commit: str = Field(pattern=r'^[a-f0-9]{40}$')


class PublishIn(BaseModel):
    placement_id: int
    session_id: str = Field(pattern=r'^[a-f0-9]{12,32}$')
    old_sha: str = Field(pattern=r'^[a-f0-9]{40}$')
    new_sha: str = Field(pattern=r'^[a-f0-9]{40}$')


class TagIn(BaseModel):
    name: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    commit: str = Field(pattern=r'^[a-f0-9]{40}$')


class RestoreIn(BaseModel):
    snapshot: str = Field(pattern=r'^[0-9a-f]{32}$')
    commit: str = Field(pattern=r'^[0-9a-f]{40}$')


class IntegrateIn(BaseModel):
    approval_id: int


class Tokens:
    """One operation, 60 seconds, hashed in memory. Restart and membership churn revoke."""
    def __init__(self):
        self.entries = {}
        self.busy = set()
        self.lock = threading.Lock()

    def mint(self, binding):
        with self.lock:
            now = time.monotonic()
            self.entries = {k: v for k, v in self.entries.items() if v[0] > now}
            mine = sum(1 for _, held in self.entries.values() if held.get('person') == binding.get('person'))
            if len(self.entries) >= 4096 or mine >= PERSON_GRANTS:
                raise HTTPException(429, 'Too many publish or checkout requests are in progress. Wait a minute, then try again.')
            token = secrets.token_urlsafe(32)
            self.entries[auth.digest(token)] = (now + 60, binding)
            return token

    def peek(self, token):
        with self.lock:
            found = self.entries.get(auth.digest(token))
        if found is None or found[0] <= time.monotonic():
            raise HTTPException(401, 'expired or consumed exchange grant')
        return found[1]

    def take(self, token):
        with self.lock:
            found = self.entries.pop(auth.digest(token), None)
        if found is None or found[0] <= time.monotonic():
            raise HTTPException(401, 'expired or consumed exchange grant')
        return found[1]

    @contextmanager
    def active(self, token):
        """Git clients make one request at a time; a grant never buffers several bodies at once."""
        key = auth.digest(token)
        with self.lock:
            if key in self.busy:
                raise HTTPException(429, 'this exchange grant already has a Git request in progress')
            if len(self.busy) >= ACTIVE_GIT:
                raise HTTPException(503, 'too many Git exchange requests; try again shortly')
            self.busy.add(key)
        try:
            yield
        finally:
            with self.lock:
                self.busy.discard(key)


def router(ctx: Context) -> APIRouter:
    api = APIRouter(prefix='/api/groups/{group_id}/projects/{project_id}')
    tokens = Tokens()
    root = ctx.settings.git_dir / 'exchange'

    def access(s, group_id, project_id, username, write=False):
        # Bootstrap instance authority is deliberately not a collaboration identity.
        user = s.scalar(select(User).where(User.username == username))
        member = s.get(GroupMembership, (group_id, user.id)) if user else None
        project, group = s.get(Project, project_id), s.get(Group, group_id)
        if (group is None or group.archived or project is None or project.archived or project.group_id != group_id
                or member is None or (write and member.role == 'viewer')):
            raise HTTPException(404, 'Group project not found.')
        return group, project, user, member

    def fence(s, group_id, revision=None):
        query = update(Group).where(Group.id == group_id, Group.archived.is_(False))
        if revision is not None:
            query = query.where(Group.revision == revision)
        if s.execute(query.values(revision=Group.revision).execution_options(synchronize_session=False)).rowcount != 1:
            raise HTTPException(404, 'The group changed or was archived. Reload the page and try again.')

    def current(s, group_id, revision=None):
        # Reads recheck authority without SQLite's writer: publications are permanent,
        # so a read racing a removal shows nothing the person could not see a moment earlier.
        group = s.get(Group, group_id)
        if group is None or group.archived or (revision is not None and group.revision != revision):
            raise HTTPException(404, 'The group changed or was archived. Reload the page and try again.')

    @contextmanager
    def serialized(write):
        """The Git lease first, then the database: waiting behind a capture never holds the writer."""
        gate = lease(git_lock(ctx.settings.git_dir), timeout=60)
        try:
            gate.__enter__()
        except TimeoutError:
            raise HTTPException(503, 'Rynhult is busy with another publish or checkout. Try again in a moment.') from None
        intent = WRITE_INTENT.set(write)
        try:
            with ctx.db() as s:
                yield s
        finally:
            WRITE_INTENT.reset(intent)
            gate.__exit__(None, None, None)

    def receipt(person, group_id, action, project_id, **detail):
        intent = WRITE_INTENT.set(True)
        try:
            with ctx.db() as s:
                audit(s, person, group_id, action, project_id, **detail)
                s.commit()
        finally:
            WRITE_INTENT.reset(intent)

    def writer_budget(target, ref):
        """Session ids are caller-chosen and refs are never deleted: cap each person, keep headroom."""
        mine = ref.rsplit('/', 1)[0] + '/'
        current = git.refs(target)
        if ref not in current and sum(name.startswith(mine) for name in current) >= PERSON_REFS:
            raise ValueError(f'You can publish at most {PERSON_REFS} branches in this project. Continue one of your existing branches instead.')
        return {'max_refs': git.MAX_REFS - RESERVED_REFS, 'max_store': git.MAX_STORE - RESERVED_STORE}

    def repo(project):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        git.private_directory(root)
        return git.initialize(root / f'{project.id}.git', project.group_id + ':' + gitbackup.identity(project))

    def placement(s, project_id, person, placement_id):
        row = s.get(Placement, placement_id) if placement_id else None
        if row is None or row.project_id != project_id or row.person != person:
            raise HTTPException(404, "Private checkout not found, or its machine isn't connected.")
        if row.host != 'local':
            from .. import group_remote
            if not ctx.settings.groups_remote_enabled or not group_remote.connected(ctx.broker, s, row):
                raise HTTPException(404, "Private checkout not found, or its machine isn't connected.")
        return row

    def audit(s, person, group_id, action, project_id, **detail):
        s.add(AuditLog(actor=person, action='group.' + action, target=group_id,
                       detail={'project_id': project_id, **detail}))

    @api.post('/exchange/grants', status_code=201)
    def grant(group_id: str, project_id: int, body: GrantIn, who=Depends(ctx.principal)):
        with ctx.db() as s:
            group, project, user, _ = access(s, group_id, project_id, who.username, body.operation == 'publish')
            if body.operation == 'publish' or body.placement_id is not None:
                placement(s, project_id, user.username, body.placement_id)
            # Usernames are never reused; numeric user ids can be recycled after retirement.
            stable = hashlib.sha256(user.username.encode()).hexdigest()[:32]
            ref = f'refs/heads/people/{stable}/{body.session_id}'
            binding = {'group_id': group_id, 'project_id': project_id, 'person': user.username,
                       'revision': group.revision, 'identity': gitbackup.identity(project), 'ref': ref, **body.model_dump()}
            return {'token': tokens.mint(binding), 'expires_in': 60, 'ref': ref}

    @api.post('/exchange/transfer')
    async def transfer(group_id: str, project_id: int, request: Request):
        scheme, _, token = request.headers.get('authorization', '').partition(' ')
        if scheme.lower() != 'bearer':
            raise HTTPException(401, 'exchange grant required')
        binding = tokens.take(token)
        if binding['group_id'] != group_id or binding['project_id'] != project_id:
            raise HTTPException(404, 'no such exchange grant')
        # Only a valid grant's holder gets the larger pack body read and parsed.
        try:
            body = PackIn.model_validate_json(await request.body())
        except ValidationError:
            raise HTTPException(422, 'invalid exchange transfer') from None
        return await asyncio.to_thread(execute, group_id, project_id, body, binding)

    def execute(group_id, project_id, body, binding):
        writing = binding['operation'] == 'publish'
        try:
            if writing:
                try:
                    data = base64.b64decode(body.data or '', validate=True)
                except ValueError:
                    raise HTTPException(422, 'invalid pack encoding') from None
                if data:
                    git.inspect_pack(data)  # before the Git lease and database writer
            with serialized(writing) as s:
                # Writers hold the database fence through the Git operation: membership
                # mutations cannot commit between this check and the completed ref update.
                (fence if writing else current)(s, group_id, binding['revision'])
                group, project, user, _ = access(s, group_id, project_id, binding['person'], writing)
                if gitbackup.identity(project) != binding['identity']:
                    raise HTTPException(404, 'The project changed while this was running. Try again.')
                if writing or binding['placement_id'] is not None:
                    placement(s, project_id, user.username, binding['placement_id'])
                target = repo(project)
                if not writing:
                    if not git.published(target, binding['new_sha']):
                        raise HTTPException(404, 'That commit has not been published to the group.')
                    result = {'commit': binding['new_sha'], 'data': base64.b64encode(git.export(target, binding['new_sha'])).decode()}
                else:
                    git.publish(target, data, binding['ref'], binding['old_sha'], binding['new_sha'],
                                **writer_budget(target, binding['ref']))
                    audit(s, user.username, group_id, 'publish', project_id, ref=binding['ref'], sha=binding['new_sha'], placement_id=binding['placement_id'])
                    result = {'ref': binding['ref'], 'commit': binding['new_sha']}
                s.commit()
            if not writing:
                receipt(user.username, group_id, 'sync', project_id, sha=binding['new_sha'], placement_id=binding['placement_id'])
            return result
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    async def smart_http(group_id, project_id, request, service):
        scheme, _, token = request.headers.get('authorization', '').partition(' ')
        if scheme.lower() != 'bearer':
            raise HTTPException(401, 'exchange grant required')
        # GET advertisements and intermediate stateless read negotiation may repeat;
        # writes consume at POST, reads consume before returning their first pack.
        binding = tokens.peek(token)
        writing = binding['operation'] == 'publish'
        spent = request.method == 'POST' and writing
        if spent:
            tokens.take(token)
        if binding['group_id'] != group_id or binding['project_id'] != project_id:
            raise HTTPException(404, 'no such exchange grant')
        expected = 'git-receive-pack' if writing else 'git-upload-pack'
        if service != expected:
            raise HTTPException(403, 'exchange grant does not allow this Git service')
        if spent and request.headers.get('content-encoding'):
            raise HTTPException(415, 'send Git exchange pushes uncompressed')
        with tokens.active(token):
            contents = bytearray()
            try:
                async with asyncio.timeout(git.TIMEOUT):
                    async for chunk in request.stream():
                        contents.extend(chunk)
                        if len(contents) > git.LIMIT + 65536:
                            raise HTTPException(413, 'Git exchange request exceeds limit')
            except TimeoutError:
                raise HTTPException(408, 'Git exchange request body took too long') from None
            if spent:
                # Outside the Git lease and database: a bomb costs only this request.
                try:
                    await asyncio.to_thread(group_git_http.inspect, bytes(contents))
                except ValueError as error:
                    raise HTTPException(409, str(error)) from None
            return await asyncio.to_thread(perform, group_id, project_id, request, service, token, binding, spent, bytes(contents))

    def perform(group_id, project_id, request, service, token, binding, spent, contents):
        writing = binding['operation'] == 'publish'
        try:
            with serialized(writing) as s:
                (fence if writing else current)(s, group_id, binding['revision'])
                group, project, user, _ = access(s, group_id, project_id, binding['person'], writing)
                # Recheck under the Git lease: another negotiation may have
                # already consumed this capability while this request waited.
                if not spent:
                    tokens.peek(token)
                if gitbackup.identity(project) != binding['identity']:
                    raise HTTPException(404, 'The project changed while this was running. Try again.')
                if writing or binding['placement_id'] is not None:
                    placement(s, project_id, user.username, binding['placement_id'])
                target = repo(project)
                budget = writer_budget(target, binding['ref']) if writing else {}
                status, headers, data, changed = group_git_http.serve(target, binding, request.method, service,
                    dict(request.headers), contents, budget)
                if changed:
                    audit(s, user.username, group_id, 'publish', project_id, ref=binding['ref'],
                          sha=binding['new_sha'], placement_id=binding['placement_id'])
                consumed = request.method == 'POST' and not writing and (b'PACK' in data or status >= 400 or b'ERR ' in data)
                if consumed:
                    tokens.take(token)
                s.commit()
            if consumed:
                receipt(user.username, group_id, 'sync', project_id, sha=binding['new_sha'], placement_id=binding['placement_id'])
            return Response(data, status_code=status, headers={**dict(headers), 'Cache-Control': 'no-store'})
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.get('/exchange/git/info/refs')
    async def git_advertisement(group_id: str, project_id: int, request: Request, service: str = ''):
        return await smart_http(group_id, project_id, request, service)

    @api.post('/exchange/git/git-receive-pack')
    async def receive_pack(group_id: str, project_id: int, request: Request):
        return await smart_http(group_id, project_id, request, 'git-receive-pack')

    @api.post('/exchange/git/git-upload-pack')
    async def upload_pack(group_id: str, project_id: int, request: Request):
        return await smart_http(group_id, project_id, request, 'git-upload-pack')

    @api.get('/exchange/refs')
    def refs(group_id: str, project_id: int, who=Depends(ctx.principal)):
        with serialized(False) as s:
            current(s, group_id)
            _, project, _, _ = access(s, group_id, project_id, who.username)
            return git.refs(repo(project))

    @api.get('/published/diff')
    def published_diff(group_id: str, project_id: int, base: str = '', commit: str = '', who=Depends(ctx.principal)):
        try:
            with serialized(False) as s:
                current(s, group_id)
                _, project, _, _ = access(s, group_id, project_id, who.username)
                target = repo(project)
                if not git.published(target, base) or not git.published(target, commit):
                    raise ValueError('select published commits')
                data = git.run(target, 'diff', '--no-ext-diff', '--no-textconv', '--no-color', base, commit, limit=1 << 20)
                return {'base': base, 'commit': commit, 'diff': data.decode('utf-8', errors='replace')}
        except ValueError:
            raise HTTPException(404, "Rynhult can't show that change. Choose published commits, or a smaller change.") from None

    @api.get('/published/file')
    def file(group_id: str, project_id: int, commit: str = '', path: str = '', who=Depends(ctx.principal)):
        try:
            with serialized(False) as s:
                current(s, group_id)
                _, project, _, _ = access(s, group_id, project_id, who.username)
                data = git.read_file(repo(project), commit, path)
                return {'commit': commit, 'path': path, 'content': data.decode('utf-8')}
        except (ValueError, UnicodeError):
            raise HTTPException(404, "Rynhult can't show that file. It isn't in the published commit, or it isn't a text file.") from None

    @api.post('/exchange/integrate')
    def integrate(group_id: str, project_id: int, body: IntegrateIn, who=Depends(ctx.principal)):
        try:
            with serialized(True) as s:
                fence(s, group_id)
                group, project, user, member = access(s, group_id, project_id, who.username, True)
                approval = s.get(GroupMergeApproval, body.approval_id)
                if approval is None or approval.project_id != project_id:
                    raise HTTPException(409, 'This merge has no current approval. A named project owner needs to approve it first.')
                approver = s.scalar(select(User).where(User.username == approval.approved_by))
                approver_member = s.get(GroupMembership, (group_id, approver.id)) if approver else None
                if (member.role != 'maintainer' or approval.revision != group.revision or
                        approver_member is None or approver_member.role != 'maintainer' or
                        s.get(GroupProjectOwner, (project_id, approver.id)) is None):
                    raise HTTPException(409, 'Only a maintainer can merge, and the merge needs a current approval from a named project owner. Ask them to approve it again.')
                target = repo(project)
                if not git.published(target, approval.new_sha):
                    raise HTTPException(409, 'Publish the merge result for review first, then merge it.')
                # Exact-old-ref CAS is the durable replay fence even after a crash
                # between Git and the DB commit. No retries ever overwrite a ref.
                ref = 'refs/heads/' + approval.branch
                if git.refs(target).get(ref, git.ZERO) != approval.old_sha or approval.new_sha == approval.old_sha:
                    raise HTTPException(409, 'The branch changed since the merge was approved. Review it and approve the merge again.')
                git.publish(target, git.export(target, approval.new_sha), ref, approval.old_sha, approval.new_sha)
                audit(s, user.username, group_id, 'integrate', project_id, approval_id=approval.id,
                      branch=approval.branch, old_sha=approval.old_sha, new_sha=approval.new_sha)
                # Retain receipts so SQLite cannot recycle their numeric ids.
                # The successful expected-old transition permanently spends
                # this receipt: refs can never be forced/deleted back to old.
                s.commit()
                return {'ref': ref, 'commit': approval.new_sha}
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/exchange/tags', status_code=201)
    def tag(group_id: str, project_id: int, body: TagIn, who=Depends(ctx.principal)):
        try:
            with serialized(True) as s:
                fence(s, group_id)
                _, project, _, member = access(s, group_id, project_id, who.username, True)
                if member.role != 'maintainer':
                    raise HTTPException(404, 'Only a group maintainer can do this.')
                target = repo(project)
                if not git.published(target, body.commit):
                    raise HTTPException(404, 'Choose a published commit.')
                ref = 'refs/tags/' + body.name
                git.publish(target, git.export(target, body.commit), ref, git.ZERO, body.commit)
                audit(s, who.username, group_id, 'tag', project_id, ref=ref, sha=body.commit)
                s.commit()
                return {'ref': ref, 'commit': body.commit}
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/placements/{placement_id}/provision')
    async def provision(group_id: str, project_id: int, placement_id: int, who=Depends(ctx.owner)):
        with ctx.db() as s:
            project = s.get(Project, project_id)
            group = s.get(Group, group_id)
            row = s.get(Placement, placement_id)
            if (project is None or project.group_id != group_id or project.archived or group is None or group.archived or
                    row is None or row.project_id != project_id or row.host != 'local'):
                raise HTTPException(404, 'Private checkout not found.')
            access(s, group_id, project_id, row.person, True)
            person, account, path, revision = row.person, row.account, row.path, group.revision
        try:
            result = await ctx.broker.call('group_provision', account=account, path=path)
        except BrokerError as error:
            raise HTTPException(409, str(error)) from None
        with ctx.db() as s:
            fence(s, group_id, revision)
            access(s, group_id, project_id, person, True)
            row = placement(s, project_id, person, placement_id)
            row.trust, row.sync_status = 'approved', 'ready'
            audit(s, who.username, group_id, 'placement_provision', project_id, placement_id=placement_id)
            s.commit()
        return result

    def own_ready(group_id, project_id, placement_id, who):
        with ctx.db() as s:
            access(s, group_id, project_id, who.username, True)
            row = placement(s, project_id, who.username, placement_id)
            if row.trust != 'approved' or row.sync_status != 'ready':
                raise HTTPException(409, "Your private checkout isn't set up yet. Ask an instance owner to approve and set it up.")
            return row.account, row.path, row.host

    @api.post('/exchange/publish')
    async def publish(group_id: str, project_id: int, body: PublishIn, who=Depends(ctx.principal)):
        account, path, host = own_ready(group_id, project_id, body.placement_id, who)
        capability = await asyncio.to_thread(grant, group_id, project_id, GrantIn(operation='publish', **body.model_dump()), who)
        binding = tokens.take(capability['token'])
        try:
            exported = await ctx.broker.call('group_export', account=account, host=host, path=path, session_id=body.session_id, commit=body.new_sha)
        except BrokerError as error:
            raise HTTPException(409, str(error)) from None
        return await asyncio.to_thread(execute, group_id, project_id, PackIn(data=exported['data']), binding)

    @api.post('/exchange/checkout')
    async def checkout(group_id: str, project_id: int, body: CheckoutIn, who=Depends(ctx.principal)):
        account, path, host = own_ready(group_id, project_id, body.placement_id, who)
        with ctx.db() as s:
            prior = s.get(GroupCheckout, body.session_id)
            if prior:
                if prior.placement_id != body.placement_id or prior.base_sha != body.commit:
                    raise HTTPException(409, 'This checkout is already in use. Start the launch again.')
                return {'path': path + '/sessions/' + body.session_id, 'commit': prior.base_sha,
                        'source_review_required': True, 'checkout_id': prior.id}
        capability = await asyncio.to_thread(grant, group_id, project_id, GrantIn(operation='fetch', placement_id=body.placement_id,
            session_id=body.session_id, old_sha=git.ZERO, new_sha=body.commit), who)
        exported = await asyncio.to_thread(execute, group_id, project_id, PackIn(), tokens.take(capability['token']))
        try:
            result = await ctx.broker.call('group_receive', account=account, host=host, path=path, session_id=body.session_id,
                                           commit=body.commit, data=exported['data'])
            with ctx.db() as s:
                access(s, group_id, project_id, who.username, True)
                placement(s, project_id, who.username, body.placement_id)
                if s.get(GroupCheckout, body.session_id) is not None:
                    raise HTTPException(409, 'This checkout is already in use. Start the launch again.')
                s.add(GroupCheckout(id=body.session_id, placement_id=body.placement_id, base_sha=body.commit))
                s.commit()
            return {**result, 'checkout_id': body.session_id}
        except BrokerError as error:
            raise HTTPException(409, str(error)) from None

    @api.get('/published/tree')
    def tree(group_id: str, project_id: int, commit: str = '', who=Depends(ctx.principal)):
        try:
            with serialized(False) as s:
                current(s, group_id)
                _, project, _, _ = access(s, group_id, project_id, who.username)
                return git.files(repo(project), commit)
        except (ValueError, UnicodeError):
            raise HTTPException(404, "Rynhult can't show the files of that commit. Choose a published commit.") from None

    @api.get('/published/wiki')
    def wiki(group_id: str, project_id: int, commit: str = '', path: str = '', who=Depends(ctx.principal)):
        if not path.endswith('.md'):
            raise HTTPException(404, 'Wiki page not found in the published commit.')
        return file(group_id, project_id, commit, 'docs/wiki/' + path, who)

    def backups(project):
        return ctx.settings.git_dir / 'group-backups' / str(project.id)

    def backup_repo(project, snapshot):
        if not re.fullmatch('[0-9a-f]{32}', snapshot):
            raise HTTPException(404, 'Group backup not found.')
        target = backups(project) / snapshot
        if not (target / 'metadata.json').is_file():
            raise HTTPException(404, 'Group backup not found.')
        git.private_directory(target)
        return git.initialize(target / 'repo.git', project.group_id + ':' + gitbackup.identity(project))

    @api.get('/exchange/backups')
    def backup_list(group_id: str, project_id: int, who=Depends(ctx.principal)):
        with serialized(False) as s:
            current(s, group_id)
            _, project, _, _ = access(s, group_id, project_id, who.username)
            parent = backups(project)
            if not parent.exists():
                return []
            result = []
            for entry in sorted(parent.iterdir()):
                if re.fullmatch('[0-9a-f]{32}', entry.name) and (entry / 'metadata.json').is_file():
                    try:
                        saved = backup_repo(project, entry.name)
                    except ValueError:
                        continue
                    result.append({'snapshot': entry.name, 'refs': len(git.refs(saved)),
                                   'created_at': (entry / 'metadata.json').stat().st_mtime,
                                   'offsite_status': 'See host backup status'})
            return result

    @api.post('/exchange/backup', status_code=201)
    def backup(group_id: str, project_id: int, who=Depends(ctx.principal)):
        try:
            with serialized(True) as s:
                fence(s, group_id)
                group, project, _, member = access(s, group_id, project_id, who.username, True)
                if member.role != 'maintainer':
                    raise HTTPException(404, 'Only a group maintainer can do this.')
                people = s.execute(select(User.username, GroupMembership.role).join(GroupMembership,
                    GroupMembership.user_id == User.id).where(GroupMembership.group_id == group_id)).all()
                metadata = {'group_id': group_id, 'group_name': group.name, 'revision': group.revision,
                            'project_id': project_id, 'project_name': project.name, 'project_slug': project.slug,
                            'people': [dict(person=p, role=r) for p, r in people],
                            'owners': list(s.scalars(select(GroupProjectOwner.user_id).where(GroupProjectOwner.project_id == project_id)))}
                name = group_recovery.snapshot(repo(project), backups(project), metadata)
                audit(s, who.username, group_id, 'backup', project_id, snapshot=name)
                s.commit()
                return {'snapshot': name}
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.get('/exchange/backup/{snapshot}')
    def backup_download(group_id: str, project_id: int, snapshot: str, who=Depends(ctx.principal)):
        try:
            with serialized(False) as s:
                current(s, group_id)
                _, project, _, _ = access(s, group_id, project_id, who.username, True)
                saved = backup_repo(project, snapshot)
                # Bounded packs, plus safe group metadata; no private placement refs.
                packs = {sha: base64.b64encode(git.export(saved, sha)).decode() for sha in set(git.refs(saved).values())}
                if sum(map(len, packs.values())) > 90 << 20:
                    raise ValueError('This backup is too large to download here.')
                metadata = json.loads((saved.parent / 'metadata.json').read_text())
            receipt(who.username, group_id, 'backup_download', project_id, snapshot=snapshot)
            return {'metadata': metadata, 'packs': packs}
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/exchange/restore')
    def restore(group_id: str, project_id: int, body: RestoreIn, who=Depends(ctx.principal)):
        try:
            with serialized(True) as s:
                fence(s, group_id)
                _, project, _, member = access(s, group_id, project_id, who.username, True)
                if member.role != 'maintainer':
                    raise HTTPException(404, 'Only a group maintainer can do this.')
                saved = backup_repo(project, body.snapshot)
                if not git.published(saved, body.commit):
                    raise HTTPException(404, 'That commit is not in the group backup.')
                ref = 'refs/heads/restore/' + secrets.token_hex(16)
                git.publish(repo(project), git.export(saved, body.commit), ref, git.ZERO, body.commit)
                audit(s, who.username, group_id, 'restore', project_id, snapshot=body.snapshot, ref=ref)
                s.commit()
                return {'ref': ref, 'commit': body.commit}
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    def personal(s, group_id, project_id, placement_id, who):
        row = s.get(Placement, placement_id)
        project = s.get(Project, project_id)
        if (row is None or project is None or project.group_id != group_id or row.project_id != project_id or
                row.host != 'local' or (row.person != who.username and not who.is_owner)):
            raise HTTPException(404, 'Your private backups for this checkout were not found.')
        # Group membership never grants this authority; leaving a group does not
        # surrender a person's private recovery history to its maintainers.
        identity = group_id + ':' + gitbackup.identity(project) + ':' + auth.digest(row.person)[:32]
        destination = ctx.settings.git_dir / 'private-groups' / str(project_id) / auth.digest(row.person)[:32]
        return row, identity, destination

    @api.post('/placements/{placement_id}/private-backups', status_code=201)
    async def private_backup(group_id: str, project_id: int, placement_id: int, body: PrivateBackupIn, who=Depends(ctx.principal)):
        with ctx.db() as s:
            row, identity, destination = personal(s, group_id, project_id, placement_id, who)
            account, path, host = row.account, row.path, row.host
        try:
            exported = await ctx.broker.call('group_export', account=account, host=host, path=path, session_id=body.session_id, commit=body.commit)
            def save():
                with lease(git_lock(ctx.settings.git_dir), timeout=60):
                    with tempfile.TemporaryDirectory(prefix='acc-private-backup-') as scratch:
                        source = git.initialize(Path(scratch) / 'repo', identity)
                        git.publish(source, base64.b64decode(exported['data'], validate=True),
                                    'refs/heads/private/' + body.session_id, git.ZERO, body.commit)
                        name = group_recovery.snapshot(source, destination, {'placement_id': placement_id})
                # A personal receipt: outside the group audit projection.
                receipt(who.username, group_id, 'private_backup', project_id, placement_id=placement_id, snapshot=name)
                return name
            name = await asyncio.to_thread(save)
            return {'snapshot': name, 'commit': body.commit}
        except (ValueError, BrokerError) as error:
            raise HTTPException(409, str(error)) from None

    @api.get('/placements/{placement_id}/private-backups/{snapshot}')
    def private_download(group_id: str, project_id: int, placement_id: int, snapshot: str, who=Depends(ctx.principal)):
        if not re.fullmatch('[0-9a-f]{32}', snapshot):
            raise HTTPException(404, 'Private backup not found.')
        try:
            with serialized(False) as s:
                _, identity, destination = personal(s, group_id, project_id, placement_id, who)
                target = destination / snapshot
                if not (target / 'metadata.json').is_file():
                    raise HTTPException(404, 'Private backup not found.')
                saved = git.initialize(target / 'repo.git', identity)
                commit = next(iter(git.refs(saved).values()))
                data = base64.b64encode(git.export(saved, commit)).decode()
            receipt(who.username, group_id, 'private_download', project_id, placement_id=placement_id, snapshot=snapshot)
            return {'commit': commit, 'data': data}
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    return api
