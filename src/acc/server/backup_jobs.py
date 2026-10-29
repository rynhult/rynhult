"""Durable backup intents, one local worker lease, owner-only observations and reminders.

Running jobs interrupted by a crash fail closed for operator retry. Complete but
unacknowledged uploads are retained; no crash advances last verified capture age.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..backup_policy import DAILY, estimate
from ..backup_server import (
    OVER_LIMIT,
    RETENTION,
    STAGING_FULL,
    LocalTarget,
    Refused,
    RepositoryLocked,
    RolledBack,
    Service,
    canonical,
    private_write,
)
from ..maintenance import lease
from ..restic_process import cancelled
from . import auth
from .db import write_intent
from .models import AttentionItem, AuditLog, Observation, Setting
from .supervisor_common import log


def failure(error, code):
    """The job's error: a fixed code plus why. Refused messages are fixed, bounded texts (restic's exit status, never its
    stderr, plus allow-listed lock metadata); anything else adds only its class, since subprocess, sqlite and filesystem errors may hold secrets/paths."""
    if isinstance(error, OSError) and error.errno == 28:
        return 'The disk is full. Free some space, then back up again.'
    if isinstance(error, RolledBack):
        return 'The target no longer has the last verified backup. Check the target machine or storage.'
    return (f'{code}: {error}' if isinstance(error, Refused)
            else f'{code} ({type(error).__name__}). Try again, and check the Rynhult log if it keeps failing.')


class BackupJobs:
    def __init__(self, settings, db, hub=None):
        self.settings, self.db = settings, db
        self.wake = asyncio.Event()
        self.service = Service(settings)
        self.service.lock_audit = lambda target, detail: self.audit(
            'backup.unlock', detail.get('actor', 'system'), target, detail)
        def s3_credentials(target):
            from . import infrastructure
            from .models import InfrastructureCredential
            from .vault import Vault
            with self.db() as session:
                key = session.get(InfrastructureCredential, target['credential_id'])
                if key is None or key.provider != 's3' or key.purpose != 'backup-writer':
                    raise Refused("Rynhult can't read the S3 writer credentials. Save them again in Target setup.")
                return infrastructure.value(Vault(settings.vault_key_path), key)
        self.service.s3_credentials = s3_credentials
        if hub:
            from contextlib import contextmanager

            from ..backup_targets import node_transport
            @contextmanager
            def transport(target, *, preflight=False):
                # Enrollment identity, not a reused display name, authorizes IO.
                from .models import Node
                with self.db() as session:
                    node = session.get(Node, target['node_id'])
                    if node is None or node.status != 'active':
                        raise Refused("This target's machine was removed or is unavailable. Check Host → Machines.")
                    name = node.name
                link = hub.get(name)
                if link.node_id != target['node_id']:
                    raise Refused("This target's machine changed since setup. Check Host → Machines, then try again.")
                with node_transport(link, target, self.service.kit()['installation_id'], preflight=preflight) as url:
                    yield url
            self.service.transport = transport
            def receiver_check(target):
                with transport(target, preflight=True):
                    pass
            self.service.receiver_check = receiver_check

    @write_intent
    def audit(self, action, actor, target='', detail=None):
        with self.db() as db:
            db.add(AuditLog(actor=actor, action=action, target=target, detail=detail or {}))
            db.commit()

    @write_intent
    def store(self, job):
        with self.db() as db:
            db.merge(Observation(key='server-backup.job.' + job['id'], value=job, source='backup', observed_at=time.time()))
            db.commit()

    def jobs(self):
        with self.db() as db:
            return [dict(row.value) for row in db.scalars(select(Observation).where(
                Observation.key.startswith('server-backup.job.')))]

    def pending(self):
        return sorted([j for j in self.jobs() if j['state'] in ('queued', 'running')], key=lambda j: j['at'])

    @write_intent
    def request(self, target_id, action, actor, backup_id=None, snapshot_id=None, *, reconnect_at=None, missed_captures=0):
        if action not in ('capture', 'prune', 'verify', 'unlock') or not any(t['id'] == target_id for t in self.service.targets()):
            raise Refused('That backup target was not found. Reload Host → Backups and try again.')
        if action == 'verify':
            from ..backup_server import ID, SNAPSHOT
            if not isinstance(backup_id, str) or not ID.fullmatch(backup_id):
                raise Refused('Choose a backup from the list first.')
            if snapshot_id is not None and (not isinstance(snapshot_id, str) or not SNAPSHOT.fullmatch(snapshot_id)):
                raise Refused('That snapshot ID is not valid. Choose a backup from the list.')
        target = self.service.target(target_id)
        self.require_machine(target)
        if target['kind'] in ('node', 's3'):
            if action == 'prune':
                raise Refused('Machine and S3 targets are pruned on the target itself, with separate maintenance credentials. Follow the Retention maintenance guide.')
            if action != 'unlock' and not target.get('setup_verified_at'):
                raise Refused('Finish Target setup and select Check setup first.')
        self.service.initialize()
        with lease(self.service.root / '.intent'):
            if reconnect_at is not None:
                handled = next((j for j in self.jobs() if j['target_id'] == target_id and j.get('reconnect_at') == reconnect_at), None)
                if handled:
                    return handled
            previous = next((j for j in self.pending() if j['target_id'] == target_id
                             and (reconnect_at is None or j['action'] == 'capture')), None)
            if previous:
                if action == 'unlock' and previous['action'] != 'unlock':
                    raise Refused('A backup task is still active on this target. Wait for it to finish before removing stale locks.')
                if reconnect_at is not None and previous['action'] == 'capture' and previous['state'] == 'queued':
                    previous.update(reconnect_at=reconnect_at, missed_captures=missed_captures, note=f'caught up after {missed_captures} missed')
                    self.store(previous)
                return previous
            if action == 'prune' and not (self.service.root / f'result-{target_id}.json').exists():
                raise Refused('Prune needs at least one verified backup. Select Back up now first.')
            job = {'id': uuid.uuid4().hex, 'target_id': target_id, 'action': action, 'actor': actor,
                   'at': time.time(), 'attempt': 0, 'state': 'queued'}
            if reconnect_at is not None:
                job.update(reconnect_at=reconnect_at, missed_captures=missed_captures, note=f'caught up after {missed_captures} missed')
            if action == 'verify':
                job['backup_id'], job['snapshot_id'] = backup_id, snapshot_id
            # Intent and audit are one transaction, before any filesystem or transport effect.
            with self.db() as db:
                db.add(Observation(key='server-backup.job.' + job['id'], value=job, source='backup', observed_at=job['at']))
                db.add(AuditLog(actor=actor, action='backup.request', target=target_id,
                                detail={'job_id': job['id'], 'action': action, **({'missed_captures': missed_captures, 'reconnect_at': reconnect_at} if reconnect_at is not None else {})}))
                db.commit()
            return job

    def reconcile(self):
        import shutil
        for path in self.service.root.glob('s3-job-*'):
            if path.is_dir() and not path.is_symlink():
                try:
                    # Owner check/list requests also use scratch, outside the durable worker lease.
                    with lease(path / '.active', timeout=.01):
                        shutil.rmtree(path)
                except (TimeoutError, FileNotFoundError):
                    pass  # active operation or another operation completed its cleanup
        for job in self.pending():
            if job['state'] == 'running':
                # Nothing for the owner to review: an interrupted capture's stage is removed once a newer one exists.
                self.store({**job, 'state': 'failed', 'error': 'Interrupted: Rynhult stopped during the job. ' + (
                    'The next backup runs it again.' if job['action'] == 'capture' else 'Run it again.'),
                            'finished_at': time.time()})
                self.audit('backup.result', 'system', job['target_id'], {'job_id': job['id'], 'ok': False})

    def target_presence(self, target, at=None):
        from .machine_presence import planned_downtime
        from .models import Node
        with self.db() as db:
            node = db.get(Node, target['node_id']) if target['kind'] == 'node' else None
            return planned_downtime(node, at)

    def machine_removed(self, target):
        from .models import Node
        if target['kind'] != 'node':
            return False
        with self.db() as db:
            node = db.get(Node, target['node_id'])
            return node is None or node.status == 'revoked'

    def require_machine(self, target):
        if self.machine_removed(target):
            raise Refused("This target's machine was removed. Select Remove target, then add a target on an active machine.")

    def state(self, now=None):
        from ..backup_s3 import retention_runway

        now, targets = time.time() if now is None else now, []
        jobs = self.jobs()
        receipt_path = self.service.root / 'kit-exported'
        exported = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
        for target in self.service.targets():
            removed = self.machine_removed(target)
            result_path = self.service.root / f"result-{target['id']}.json"
            result = json.loads(result_path.read_bytes()) if result_path.exists() else {}
            last = max((j for j in jobs if j['target_id'] == target['id']), key=lambda j: j['at'], default=None)
            interval = target['schedule_seconds']
            stale_seconds = 2 * interval
            full_fresh = target['verify_interval_seconds'] + stale_seconds
            runway = retention_runway(result, now) if target['kind'] == 's3' else {}
            protected = (target['kind'] == 's3' and bool(target.get('repository_id'))
                         and exported.get('repositories', {}).get(target['id']) == target['repository_id']
                         and runway.get('retention_runway_days') is not None
                         and 0 <= now - (result.get('captured_at') or 0) < stale_seconds
                         and 0 <= now - (result.get('verified_at') or 0) < full_fresh
                         and 0 <= now - (result.get('immutability_checked_at') or 0) < stale_seconds
                         and result.get('immutable_until', 0) >= now + 30 * 86400
                         and not (last and last['state'] == 'failed'))
            downtime = self.target_presence(target, datetime.fromtimestamp(now, timezone.utc))
            latest_capture = max((j['at'] for j in jobs if j['target_id'] == target['id'] and j['action'] == 'capture'), default=0)
            next_capture = max(downtime['ends_at'].timestamp(), latest_capture + interval) if downtime['ends_at'] and target['enabled'] else None
            if next_capture is not None:
                due_presence = self.target_presence(target, datetime.fromtimestamp(next_capture, timezone.utc))
                if due_presence['active']:
                    next_capture = due_presence['ends_at'].timestamp() if due_presence['ends_at'] else None
            targets.append({**target, **result, **runway, 'last_job': last, 'protected': protected, 'machine_removed': removed,
                'planned_downtime': {**downtime, 'ends_at': downtime['ends_at'].timestamp() if downtime['ends_at'] else None},
                'next_capture_expected_at': next_capture, 'cost_estimate': estimate(target, result), 'stale_seconds':stale_seconds,
                'last_successful_off_machine_at': result.get('captured_at') if target['kind'] in ('node', 's3') else None,
                'stale': not removed and now - (result.get('captured_at') or 0) > stale_seconds,
                'status': ('machine removed' if removed else 'protected · verified 30-day Object Lock' if protected else 'append-only receiver · independent offline copy still required' if target['kind'] == 'node' and target.get('setup_verified_at') and result else 'backed up, not protected' if result else 'no verified backup')})
        with self.db() as db:
            drill = db.get(Setting, 'backup.drill')
            drill = dict(drill.value) if drill else {}
        return {'targets': targets, 'protected': any(t['protected'] for t in targets), 'retention': RETENTION, 'schedule_seconds': DAILY,
                'stale_seconds': 2 * DAILY, 'limit_mib': 10, 'drill': drill,
                'quarterly_due': bool(targets) and now - drill.get('at', min((t['created_at'] for t in targets), default=now)) > 90 * 86400,
                'coverage': 'Rynhult database, secrets, catalog overlay, project bare git repositories. Main agent state needs separate backups.'}

    def enable(self, target_id, actor):
        self.require_machine(self.service.target(target_id))
        with lease(self.service.root / '.intent'):
            targets = self.service.targets()
            target = next((t for t in targets if t['id'] == target_id), None)
            if target is None:
                raise Refused('That backup target was not found. Reload Host → Backups and try again.')
            if not (self.service.root / 'kit-exported').exists() or not (self.service.root / f'result-{target_id}.json').exists():
                raise Refused('Export the recovery kit and finish one verified Back up now before enabling scheduled backups.')
            exported = json.loads((self.service.root / 'kit-exported').read_bytes())
            if target_id not in exported.get('targets', []):
                raise Refused('Export a new recovery kit so it includes this target, then enable scheduled backups.')
            if target['kind'] in ('node', 's3') and (not target.get('setup_verified_at') or
                    exported.get('repositories', {}).get(target_id) != target['repository_id']):
                raise Refused('Export a new recovery kit after Check setup passes, then enable scheduled backups.')
            target['enabled'] = True
            private_write(self.service.root / 'targets.json', canonical(targets))
        self.audit('backup.enable', actor, target_id)
        return self.state()

    def set_schedule(self, target_id, actor, schedule_seconds, verify_interval_seconds):
        self.require_machine(self.service.target(target_id))
        with lease(self.service.root / '.intent'):
            targets = self.service.targets()
            target = next(t for t in targets if t['id'] == target_id)
            target.update(schedule_seconds=schedule_seconds, verify_interval_seconds=verify_interval_seconds)
            private_write(self.service.root / 'targets.json', canonical(targets))
        self.audit('backup.schedule', actor, target_id, {
            'schedule_seconds':schedule_seconds, 'verify_interval_seconds':verify_interval_seconds})
        self.remind()
        return self.state()

    def disable(self, target_id, actor):
        self.service.target(target_id)
        with lease(self.service.root / '.intent'):
            targets = self.service.targets()
            for target in targets:
                if target['id'] == target_id:
                    target['enabled'] = False
            private_write(self.service.root / 'targets.json', canonical(targets))
        self.audit('backup.disable', actor, target_id)
        return self.state()

    def remove(self, target_id, actor):
        with lease(self.service.root / '.intent'), lease(self.service.root / '.coordinator'):
            target = self.service.target(target_id)
            if target['kind'] != 's3' and not self.machine_removed(target):
                raise Refused('Only cloud targets or targets whose machine was removed can be removed here.')
            if any(j['target_id'] == target_id for j in self.pending()):
                raise Refused('A backup task is still running for this target. Wait for it to finish, then remove the target.')
            # Refresh reminders and record intent before filesystem effects.
            self.remind()
            self.audit('backup.target_remove', actor, target_id,
                       {'kind': 's3'} if target['kind'] == 's3' else {'node_id': target['node_id']})
            # Archive configuration first. Capture results, keys and durable job/audit history remain.
            private_write(self.service.root / f'removed-target-{target_id}.json', canonical(target))
            private_write(self.service.root / 'targets.json', canonical([
                t for t in self.service.targets() if t['id'] != target_id]))
        with self.db() as db:
            for cause in ('retention', 'stale', 'failed'):
                item = db.scalar(select(AttentionItem).where(
                    AttentionItem.dedupe_key == f'backup:{cause}:{target_id}'))
                if item:
                    item.resolved_at = auth.now()
            db.commit()
        self.remind()
        return self.state()

    @write_intent
    def record_drill(self, actor, target_id, backup_id, snapshot_id=None):
        from ..backup_server import ID, SNAPSHOT
        if not ID.fullmatch(backup_id) or not any(t['id'] == target_id for t in self.service.targets()):
            raise Refused('Choose a backup target and enter its backup ID.')
        if snapshot_id is not None and not SNAPSHOT.fullmatch(snapshot_id):
            raise Refused('Enter a valid snapshot ID from this backup target.')
        selected = {'backup_id': backup_id, **({'snapshot_id': snapshot_id} if snapshot_id is not None else {})}
        with self.db() as db:
            db.merge(Setting(key='backup.drill', value={'at': time.time(), 'target_id': target_id, **selected}))
            row = db.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == 'backup:quarterly'))
            if row:
                row.resolved_at = auth.now()
            db.add(AuditLog(actor=actor, action='backup.drill', target=target_id, detail=selected))
            db.commit()

    @write_intent
    def remind(self, now=None):
        now = time.time() if now is None else now
        state = self.state(now=now)
        with self.db() as db:
            drill = db.get(Setting, 'backup.drill')
            created = min((t['created_at'] for t in state['targets']), default=now)
            causes = [('quarterly', bool(state['targets']) and now - (drill.value['at'] if drill else created) >= 90 * 86400,
                       'Test your Rynhult recovery kit',
                       'Use your saved kit to restore a backup on a separate machine, then select Record restore drill in '
                       'Host → Backups. Keep the kit in a password manager plus one offline copy.')]
            for t in state['targets']:
                suppressed = t['machine_removed'] or t['planned_downtime']['active']
                causes.extend([
                    ('retention:' + t['id'], t['enabled'] and t.get('retention_warning', False),
                     'S3 backups need retention maintenance',
                     'Within 7 days the S3 backups will no longer be protected for 30 days. Follow the Retention '
                     'maintenance guide in Host → Backups, or move backups to a fresh S3 location.'),
                    ('stale:' + t['id'], not suppressed and t['enabled'] and now - (t.get('captured_at') or 0) > t['stale_seconds'],
                     'Rynhult backup is overdue',
                     'No backup has finished and passed its check within twice the target capture interval. Open Host → Backups to see why.'),
                    ('failed:' + t['id'], not suppressed and bool(t['last_job'] and t['last_job']['state'] == 'failed'),
                     'Rynhult backup failed',
                     'Your last good backup is still kept. Open Host → Backups to see what went wrong.')])
            for cause, active, title, detail in causes:
                key = 'backup:' + cause
                item = db.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == key))
                if active and item is None:
                    db.add(AttentionItem(dedupe_key=key, kind='backup', severity='warn', source='backup',
                                         title=title, detail=detail, owner=None))
                elif item:
                    if active and item.resolved_at is not None:
                        item.resolved_at, item.acked_at = None, None
                        item.snoozed_until = None
                    elif not active:
                        item.resolved_at = auth.now()
            db.commit()

    def work(self, *, binary=Path('/usr/local/bin/restic')):
        pending = self.pending()
        captures = [j for j in pending if j['action'] == 'capture' and j['state'] == 'queued']
        stage = None
        if captures:
            # Only the newest failed stage stays for review; older ones must not use up the next capture's budget.
            try:
                newest = self.service.newest_stage()
            except OSError:  # staging unreadable or missing: capture() recreates it or says why
                newest = None
            self.discard_stages(keep={newest.name} if newest else set())
            try:
                try:
                    stage = self.service.capture()
                except Refused as error:
                    # The kept stage counts against the budget: a fresh capture always outranks keeping it for review.
                    if newest is None or str(error) not in (STAGING_FULL, OVER_LIMIT):
                        raise
                    self.discard_stages(keep=set())
                    stage = self.service.capture()
            except Exception as error:
                code = failure(error, 'Backup could not be prepared')
                log.warning('server backup: capture failed: %s', code)
                for job in captures:
                    self.store({**job, 'state': 'failed', 'error': code, 'finished_at': time.time()})
                    self.audit('backup.result', 'system', job['target_id'], {'job_id': job['id'], 'ok': False})
                pending = [j for j in pending if j not in captures]
        captures_ok = True
        for job in pending:
            if job['state'] != 'queued':
                continue
            job = {**job, 'state': 'running', 'attempt': job['attempt'] + 1}
            if job['action'] == 'capture':
                job['backup_id'] = stage.name
            self.store(job)
            try:
                if job['action'] == 'capture':
                    result = self.service.back_up(job['target_id'], binary=binary, stage=stage)
                elif job['action'] == 'verify':
                    result = self.service.verify(job['target_id'], job['backup_id'], snapshot_id=job.get('snapshot_id'),
                                                 binary=binary)
                elif job['action'] == 'unlock':
                    result = self.service.unlock(job['target_id'], binary=binary, actor=job['actor'])
                    job['repository_locked'] = bool(result['remaining'])
                    job['locks'] = result['locks']
                    job['note'] = f"Stale lock check: {len(result['removed'])} removed; {result['remaining']} remain"
                else:
                    target = next(t for t in self.service.targets() if t['id'] == job['target_id'])
                    if target['kind'] != 'local':
                        raise Refused('Machine and S3 targets are pruned on the target itself, with separate maintenance credentials outside Rynhult.')
                    backend = self.service.configure_backend(
                        LocalTarget(target['repository'], self.service.root / 'keys' / target['id'], binary), target)
                    if backend.repository_id() != target['repository_id']:
                        raise Refused("The backup storage on this target isn't the one Rynhult set up. Check that the target still points at the same storage.")
                    backend.command('check', '--read-data')
                    verified = json.loads((self.service.root / f"result-{target['id']}.json").read_bytes())
                    backend.command('forget', '--keep-tag', verified['backup_id'], '--keep-hourly', '48', '--keep-daily', '30', '--keep-monthly', '12',
                                    '--group-by', 'host', '--host', self.service.kit()['installation_id'], '--prune')
                    result = {}
                job.update(state='complete', result=result)
            except Exception as error:
                code = failure(error, 'Backup or its check failed')
                log.warning('server backup: %s job %s for target %s failed: %s', job['action'], job['id'][:8],
                            job['target_id'][:8], code)
                job.update(state='failed', error=code)
                if isinstance(error, RepositoryLocked):
                    job.update(repository_locked=True, locks=error.locks)
                if job['action'] == 'capture':
                    captures_ok = False
            job['finished_at'] = time.time()
            self.store(job)
            self.audit('backup.result', 'system', job['target_id'], {'job_id': job['id'], 'ok': job['state'] == 'complete'})

        if stage is not None:
            # A capture that reached every target needs no stage; otherwise this one supersedes any older one.
            self.discard_stages(keep=set() if captures_ok else {stage.name})

    def discard_stages(self, keep):
        # Never let cleanup stop a capture or hide why: a stage that can't be removed counts against the budget, and
        # the capture then fails with that reason.
        try:
            removed, size = self.service.discard_stages(keep)
        except (OSError, TimeoutError) as error:
            log.warning('server backup: could not remove superseded staging: %s', failure(error, 'staging cleanup failed'))
            return
        if removed:
            log.info('server backup: removed %d superseded staging folder(s), %d bytes', removed, size)
            self.audit('backup.staging_discarded', 'system', detail={'stages': removed, 'bytes': size})

    def schedule(self):
        """Queue at most one capture for a reconnect, then resume the target cadence.

        The reconnect timestamp lives on the machine; the job stores the consumed
        timestamp in the same durable intent as the request. Worker lease serializes
        schedule and execution, including across restart.
        """
        from .machine_presence import planned_downtime
        from .models import Node
        now = time.time()
        jobs = self.jobs()
        for target in self.service.targets():
            interval = target['schedule_seconds']
            if not target['enabled']:
                continue
            captures = [j for j in jobs if j['target_id'] == target['id'] and j['action'] == 'capture']
            latest = max(captures, key=lambda j: j['at'], default=None)
            node = None
            if target['kind'] == 'node':
                with self.db() as db:
                    node = db.get(Node, target['node_id'])
                # Removal, reconnect and downtime must share this snapshot. Separate
                # reads can straddle a reconnect and lose the offline snapshot.
                if node is None or node.status == 'revoked':
                    continue  # One removed target must not stop other targets' tick.
                if node.status == 'active' and node.offline_since is None and node.last_reconnected_at:
                    reconnect = node.last_reconnected_at.timestamp()
                    start = reconnect - (node.last_downtime_seconds or 0)
                    before = [j['at'] for j in captures if j['at'] <= start]
                    anchor = max(before, default=max(start, target['created_at']))
                    missed = max(0, int((reconnect - anchor) // interval))
                    successful = sum(1 for j in captures if start < j['at'] < reconnect and j['state'] == 'complete')
                    missed = max(0, missed - successful)
                    handled = any(j.get('reconnect_at') == reconnect for j in captures)
                    # A successful manual capture after reconnect already restores coverage.
                    recovered = any(j['at'] >= reconnect and j['state'] == 'complete' for j in captures)
                    if missed and not handled and not recovered:
                        self.request(target['id'], 'capture', 'system', reconnect_at=reconnect, missed_captures=missed)
                        continue
            # Reuse the reconnect snapshot: a second read can see the one-shot
            # flag cleared and queue an ordinary capture without catch-up metadata.
            if planned_downtime(node, datetime.fromtimestamp(now, timezone.utc))['active']:
                continue
            if now - (latest['at'] if latest else 0) >= interval:
                self.request(target['id'], 'capture', 'system')

    def tick(self):
        if not self.service.targets():
            return
        with lease(self.service.root / '.worker', timeout=.1):
            self.reconcile()
            self.schedule()
            self.work()
            if not any(t['enabled'] for t in self.service.targets()):
                # No schedule will supersede a kept failed stage, and it's a plaintext copy: don't keep it.
                self.discard_stages(keep=set())
            self.remind()

    async def run(self):
        while True:
            self.wake.clear()
            stopping = threading.Event()
            token = cancelled.set(stopping)
            try:
                task = asyncio.create_task(asyncio.to_thread(self.tick))
            finally:
                cancelled.reset(token)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Signal the owned command, allow lock cleanup, then join the mutation thread.
                stopping.set()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue  # repeated shutdown cancellation must not abandon the thread
                    except InterruptedError:
                        break
                if not task.cancelled():
                    task.exception()  # retrieve cleanup failure; preserve the caller's cancellation
                raise
            except (OSError, ValueError, TimeoutError):
                pass  # next tick retries; durable jobs and age remain visible
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
