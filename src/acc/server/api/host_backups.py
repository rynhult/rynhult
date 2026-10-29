"""Owners only; destinations are provisioned locally, never accepted as API paths."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ...backup_server import BackendBusy, Refused, RepositoryLocked, ResticFailed, canonical, private_write
from ...backup_targets import MAX_MAX_SIZE, MIN_MAX_SIZE, node_max_size, target_max_size
from ...maintenance import LeaseBusy
from ..backup_jobs import BackupJobs
from ..db import MaintenanceBusy

CHECK_ADVICE = ('Check connection, writer permissions, receiver or S3 setup and pinned restic. '
                'Credentials are never shown.')

class Confirmed(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: Literal[True]


class Schedule(Confirmed):
    schedule_seconds: Literal[3600, 86400]
    verify_interval_seconds: Literal[0, 86400, 604800]


class Verify(Confirmed):
    backup_id: str
    snapshot_id: str | None = None


class NodeTarget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    node_id: str
    max_size_bytes: int | None = Field(default=None, strict=True, ge=MIN_MAX_SIZE, le=MAX_MAX_SIZE)


class S3Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    credentials: dict[str, SecretStr]


class S3Target(S3Credentials):
    endpoint: str = Field(max_length=253)
    bucket: str = Field(max_length=63)
    region: str = Field(max_length=64)


class Drill(Confirmed):
    target_id: str
    backup_id: str
    snapshot_id: str | None = Field(None, pattern=r'^[0-9a-f]{64}$')


class BackupRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                return await original(request)
            except (LeaseBusy, BlockingIOError, MaintenanceBusy):
                return JSONResponse({'detail': 'A backup is running; try again shortly.'}, status_code=409)
        return handle


def router(ctx):
    api = APIRouter(prefix='/api/host/backups', route_class=BackupRoute)
    jobs = BackupJobs(ctx.settings, ctx.db, ctx.broker.hub)
    ctx.backup_jobs = jobs

    @api.get('')
    def state(who=Depends(ctx.owner)):
        return jobs.state()

    @api.post('/targets', status_code=201)
    def add_target(body: NodeTarget, who=Depends(ctx.owner)):
        from ..models import Node
        with ctx.db() as db:
            node = db.get(Node, body.node_id)
            if node is None or node.status != 'active' or node.info.get('os', '').lower() != 'linux':
                raise HTTPException(409, 'Choose an active Linux machine. Add one in Host → Machines first.')
            node_id, node_name = node.id, node.name
        # Never hold the DB maintenance lease while waiting for the capture coordinator.
        try:
            link = ctx.broker.hub.links.get(node_name) if ctx.broker.hub else None
            ceiling = body.max_size_bytes if body.max_size_bytes is not None else node_max_size(link)
            target = jobs.service.add_node_target(node_id, node_name, ceiling)
        except Refused as error:
            raise HTTPException(409, str(error)) from None
        jobs.audit('backup.target_add', who.username, target['id'], {'kind': 'node'})
        return target

    @api.post('/s3-targets', status_code=201)
    def add_s3_target(body: S3Target, who=Depends(ctx.owner)):
        from ...backup_s3 import fields, location
        from .. import infrastructure
        from ..models import InfrastructureCredential
        from ..vault import Vault, new_id
        try:
            location(body.endpoint, body.bucket, body.region)
            credentials = fields({k: v.get_secret_value() for k, v in body.credentials.items()})
        except (ValueError, Refused):
            raise HTTPException(422, 'Enter an HTTPS S3 endpoint, bucket, region, access key and secret key.') from None
        # The infrastructure table has no project grants or general-vault reveal path.
        with ctx.db() as db:
            key = InfrastructureCredential(id=new_id(), name='S3 backup writer', provider='s3',
                                           purpose='backup-writer', owner=who.username, revision=1)
            key.ciphertext = infrastructure.seal(db, Vault(ctx.settings.vault_key_path), key, credentials)
            db.add(key)
            db.commit()
            credential_id = key.id
        try:
            target = jobs.service.add_s3_target(body.endpoint, body.bucket, body.region, credential_id)
        except Refused:
            raise HTTPException(409, "Couldn't add the S3 target. Wait for the running backup to finish, then try again.") from None
        jobs.audit('backup.target_add', who.username, target['id'], {'kind': 's3'})
        return target

    @api.post('/{target_id}/credentials')
    def replace_s3_credentials(target_id: str, body: S3Credentials, who=Depends(ctx.owner)):
        from ...backup_s3 import fields
        from ...maintenance import lease
        from .. import infrastructure
        from ..models import InfrastructureCredential
        from ..vault import Vault
        try:
            credentials = fields({k: v.get_secret_value() for k, v in body.credentials.items()})
            target = jobs.service.target(target_id)
            if target['kind'] != 's3':
                raise Refused('S3 target required')
            with lease(jobs.service.root / f'.upload-{target_id}', timeout=1), lease(jobs.service.root / '.intent'), lease(jobs.service.root / '.coordinator'):
                target = jobs.service.target(target_id)
                # Invalidate before replacing, so a crash cannot leave a trusted receipt for a different key.
                target.update(setup_verified_at=None, immutability=None, enabled=False)
                private_write(jobs.service.root / 'targets.json', canonical([
                    target if t['id'] == target_id else t for t in jobs.service.targets()]))
                result_path = jobs.service.root / f'result-{target_id}.json'
                if result_path.exists():
                    import json
                    result = json.loads(result_path.read_bytes())
                    result.pop('immutable_until', None)
                    result.pop('immutability_checked_at', None)
                    private_write(result_path, canonical(result))
                with ctx.db() as db:
                    key = db.get(InfrastructureCredential, target['credential_id'])
                    if key is None or key.provider != 's3' or key.purpose != 'backup-writer':
                        raise Refused('Backup writer credential unavailable')
                    infrastructure.replace(db, Vault(ctx.settings.vault_key_path), key, credentials)
                    db.commit()
        except (ValueError, Refused):
            raise HTTPException(409, "Couldn't save the S3 credentials. Check the target and the credential fields, then try again.") from None
        jobs.audit('backup.credentials_replace', who.username, target_id)
        return target

    @api.get('/{target_id}/setup')
    def setup(target_id: str, response: Response, who=Depends(ctx.owner)):
        response.headers['Cache-Control'] = 'no-store'
        try:
            target = jobs.service.target(target_id)
            if target['kind'] == 's3':
                jobs.audit('backup.target_setup', who.username, target_id)
                return {'kind': 's3', **target}
            commands = jobs.service.target_setup(target_id)
        except Refused as error:
            raise HTTPException(409, str(error)) from None
        # Root commands carrying the writer credential's hash: record who looked.
        jobs.audit('backup.target_setup', who.username, target_id)
        return {'commands': commands, 'max_size_bytes': target_max_size(jobs.service.target(target_id))}

    @api.post('/{target_id}/check')
    def check(target_id: str, who=Depends(ctx.owner)):
        try:
            target = jobs.service.check_target(target_id)
        except (LeaseBusy, BlockingIOError, MaintenanceBusy):
            raise
        except RepositoryLocked as error:
            jobs.audit('backup.target_check', who.username, target_id, {'ok': False, 'locks': error.locks})
            raise HTTPException(409, str(error)) from None
        except ResticFailed as error:
            # Which step failed helps; the exit code alone doesn't say what to check.
            jobs.audit('backup.target_check', who.username, target_id, {'ok': False})
            raise HTTPException(409, f'Target check failed ({error}). {CHECK_ADVICE}') from None
        except Refused as error:
            jobs.audit('backup.target_check', who.username, target_id, {'ok': False})
            raise HTTPException(409, str(error)) from None
        except Exception:
            jobs.audit('backup.target_check', who.username, target_id, {'ok': False})
            raise HTTPException(409, f'Target check failed. {CHECK_ADVICE}') from None
        jobs.audit('backup.target_check', who.username, target_id, {'ok': True})
        return target

    @api.get('/{target_id}/snapshots')
    def snapshots(target_id: str, who=Depends(ctx.owner)):
        try:
            history = jobs.service.history(target_id)
            captures = {j.get('result', {}).get('snapshot_id'): j for j in jobs.jobs()
                        if j['target_id'] == target_id and j['action'] == 'capture'
                        and j['state'] == 'complete' and j.get('missed_captures')}
            for item in history['items']:
                job = captures.get(item['snapshot_id'])
                if job and job.get('backup_id') == item['backup_id']:
                    item['note'] = job['note']
            return history
        except (LeaseBusy, BlockingIOError, MaintenanceBusy):
            raise
        except (RepositoryLocked, BackendBusy) as error:
            raise HTTPException(409, str(error)) from None
        except Exception:
            raise HTTPException(409, "Couldn't list this target's backups. Select Check setup and check the connection.") from None

    @api.post('/kit')
    def kit(body: Confirmed, who=Depends(ctx.owner)):
        jobs.service.initialize()
        value = jobs.service.kit()
        jobs.audit('backup.kit_export', who.username)
        private_write(jobs.service.root / 'kit-exported', canonical({'targets': [t['id'] for t in value['targets']], 'repositories': {t['id']: t['repository_id'] for t in value['targets']}}))
        return Response(canonical(value), media_type='application/json', headers={
            'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': 'attachment; filename="acc-recovery-kit.json"'})

    @api.post('/{target_id}/run', status_code=202)
    def capture(target_id: str, who=Depends(ctx.owner)):
        try:
            return jobs.request(target_id, 'capture', who.username)
        except Refused as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/{target_id}/verify', status_code=202)
    def verify(target_id: str, body: Verify, who=Depends(ctx.owner)):
        try:
            return jobs.request(target_id, 'verify', who.username, body.backup_id, body.snapshot_id)
        except Refused as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/{target_id}/prune', status_code=202)
    def prune(target_id: str, body: Confirmed, who=Depends(ctx.owner)):
        try:
            return jobs.request(target_id, 'prune', who.username)
        except Refused as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/{target_id}/unlock', status_code=202)
    def unlock(target_id: str, body: Confirmed, who=Depends(ctx.owner)):
        try:
            return jobs.request(target_id, 'unlock', who.username)
        except Refused as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/{target_id}/schedule')
    def schedule(target_id: str, body: Schedule, who=Depends(ctx.owner)):
        try:
            return jobs.set_schedule(target_id, who.username, body.schedule_seconds, body.verify_interval_seconds)
        except Refused as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/{target_id}/enable')
    def enable(target_id: str, body: Confirmed, who=Depends(ctx.owner)):
        try:
            return jobs.enable(target_id, who.username)
        except Refused as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/{target_id}/disable')
    def disable(target_id: str, body: Confirmed, who=Depends(ctx.owner)):
        try:
            return jobs.disable(target_id, who.username)
        except Refused as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/{target_id}/remove')
    def remove(target_id: str, body: Confirmed, who=Depends(ctx.owner)):
        try:
            return jobs.remove(target_id, who.username)
        except Refused as error:
            raise HTTPException(409, str(error)) from None

    @api.post('/drill')
    def drill(body: Drill, who=Depends(ctx.owner)):
        try:
            jobs.record_drill(who.username, body.target_id, body.backup_id, body.snapshot_id)
            return jobs.state()
        except Refused as error:
            raise HTTPException(409, str(error)) from None

    return api
