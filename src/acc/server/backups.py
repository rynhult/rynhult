"""Supervisor: git backup of every project to the main ACC (M15, docs/MULTIHOST.md "Git backup").

Mixed into acc.server.supervisor.Supervisor. Every backup_interval (and right after a session in the project stops)
each project's broker is asked for a digest of its branches and tags; when it changed since the last good backup,
accd mints a one-push token and has the broker push (`git_backup`) to /api/git/<id>.git on this ACC. Projects on
this machine go the same way, through the same address. Status lives in the observation `backup.<project id>`.
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from . import gitbackup
from .broker_client import BrokerError, BrokerUnavailable
from .db import WRITE_INTENT, MaintenanceBusy
from .machine_presence import connected
from .models import AuditLog, Event, Observation, Project
from .node_tasks import NodeTasks, joined_thread
from .supervisor_common import log, now

SCAN_EVERY = 60.0  # seconds between looks for projects that are due
# A capture holds writes for milliseconds. A 503 that lasts this long is something else (no HA authority, a proxy, a
# stuck maintenance lock): record it as a failure so the project shows it, instead of deferring forever.
DEFER_LIMIT = 900.0
MAX_KEPT = 20  # rewritten branches remembered in a project's status


class BackupMixin:
    def init_backups(self) -> None:
        self.backup_tokens = gitbackup.BackupTokens()
        self.backup_grants = gitbackup.BackupGrants(self.backup_tokens)
        self._backup_due: set[int] = set()
        self._backup_deferred: dict[int, float] = {}  # project -> first 503 of the current run of them
        self._backup_task: asyncio.Task | None = None
        self._backup_tasks = NodeTasks("backup-rounds")
        self._backup_locks: dict[int, asyncio.Lock] = {}
        self._node_backup_locks: dict[str, asyncio.Lock] = {}  # a machine runs one roaming backup job at a time
        self._backup_scan = -1e9
        try:  # repositories made before the update hook existed get it now, and a changed one is put back
            gitbackup.install_hooks(self.settings.git_dir)
        except OSError as error:
            log.warning("git backup: could not check the backup repositories' hooks: %s", error)

    def backup_soon(self, project_id: int) -> None:
        """A session in the project stopped: look at its commits on the next pass."""
        self._backup_due.add(project_id)

    def backup_status(self, project_id: int) -> dict:
        with self.db() as db:
            observation = db.get(Observation, f"backup.{project_id}")
            return dict(observation.value) if observation else {}

    def backup_total(self) -> dict:
        """All backups' size on disk (Host page), recounted at most once a minute."""
        cached = getattr(self, "_backup_total", None)
        if cached is None or time.monotonic() - cached[0] > SCAN_EVERY:
            cached = self._backup_total = (time.monotonic(), {**gitbackup.total_size(self.settings.git_dir),
                                                               "enabled": bool(self.settings.backup_base)})
        return cached[1]

    async def schedule_backups(self) -> None:
        """Supervisor stage (cheap, every pass): start a backup round in the background when one is due."""
        if not self.settings.backup_base or (self._backup_task is not None and not self._backup_task.done()):
            return
        clock = asyncio.get_running_loop().time()
        if not self._backup_due and clock - self._backup_scan < SCAN_EVERY:
            return
        self._backup_scan = clock
        due, self._backup_due = self._backup_due, set()
        self._backup_task = self._backup_tasks.start(self._backup_round(due), "round")

    async def _backup_round(self, due: set[int]) -> None:
        try:
            await self._run_backup_round(due)
        except MaintenanceBusy:
            self._backup_due.update(due)
            self._backup_scan = -1e9
            log.info("git backup pass skipped: backup capture or maintenance is busy")

    async def _run_backup_round(self, due: set[int]) -> None:
        with self.db() as db:
            projects = [p.id for p in db.scalars(select(Project).where(
                Project.git_backup.is_(True), Project.archived.is_(False)))]
        for project_id in projects:
            checked = self.backup_status(project_id).get("checked_at", 0)
            if project_id in due or time.time() - checked >= self.settings.backup_interval:
                try:
                    with self.db() as db:
                        project = db.get(Project, project_id)
                        if project and not connected(self.broker, project.host or 'local'):
                            continue
                    host = (project.host if project else None) or 'local'
                    if host != 'local' and self.broker.hub is not None:
                        await self.broker.hub.start(host, self.backup_project(project_id), f"backup:{project_id}")
                    else:
                        await self.backup_project(project_id)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise  # supervisor shutdown, rather than just this node disconnecting
                except MaintenanceBusy:
                    raise  # the whole round is retried (_backup_round); a node job's task re-raises it here
                except Exception:  # one project's trouble never stops the others'
                    log.exception("git backup of project %s failed", project_id)

    async def backup_project(self, project_id: int, force: bool = False) -> dict:
        """Back one project up if its branches or tags changed since the last good backup (always with `force`).
        Returns its status."""
        lock = self._backup_locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            return await self._backup_project(project_id, force)

    async def _backup_project(self, project_id: int, force: bool) -> dict:
        base = self.settings.backup_base
        with self.db() as db:
            project = db.get(Project, project_id)
            if project is None or not base:
                return {}
            slug, path, account, host = project.slug, project.workspace_path, project.account, project.host or "local"
            owner, created = gitbackup.identity(project), gitbackup.identity_time(project)
        status = self.backup_status(project_id)
        status["checked_at"] = time.time()
        try:
            refs = await self.broker.call("git_refs", account=account, host=host, path=path)
        except BrokerError as error:
            status["error"] = f"host {host} is not reachable" if isinstance(error, BrokerUnavailable) else str(error)[:300]
            return await joined_thread(self._save_backup, project_id, status)
        status["git"] = bool(refs.get("git"))
        if not status["git"]:
            status.pop("error", None)
            return await joined_thread(self._save_backup, project_id, status)
        exists = ((gitbackup.repo_path(self.settings.git_dir, project_id) / "HEAD").is_file()
                  and gitbackup.recorded_identity(gitbackup.repo_path(self.settings.git_dir, project_id)) == owner)
        if not force and exists and not status.get("error") and refs.get("digest") == status.get("digest"):
            return await joined_thread(self._save_backup, project_id, status)  # no new commits
        ca_pem = None
        if base.startswith("https://") and self.settings.node_ca_file and self.settings.node_ca_file.is_file():
            ca_pem = self.settings.node_ca_file.read_text()
        with self.db() as db:
            self.telemetry.count(db, 'backup.run')
            db.commit()
        started = time.monotonic()
        try:
            await joined_thread(gitbackup.ensure_repo, self.settings.git_dir, project_id, owner, created)
            hub = getattr(self.broker, 'hub', None)
            link = hub.links.get(host) if hub and host != 'local' else None
            with self.db() as db:
                public_origin = gitbackup.public_backup_origin(db, link.node_id if link else None)

            async def private_push():
                token = self.backup_tokens.mint(project_id)
                client = self.broker.fresh(account, host, timeout=self.settings.backup_timeout)
                try:
                    return await client.call("git_backup", path=path, url=f"{base}/api/git/{project_id}.git", token=token,
                                             ca_pem=ca_pem)
                finally:
                    self.backup_tokens.revoke(token)
                    await client.close()
            # The node job takes bare HTTPS origins only; a backup URL with a path or plain http keeps the broker push.
            if (public_origin and link and 'git-backup-v1' in link.info.get('capabilities', [])
                    and gitbackup.bare_https_origin(base)):
                # Wait for this machine's other project instead of failing it; the grant's time starts after.
                async with self._node_backup_locks.setdefault(link.node_id, asyncio.Lock()):
                    # Consent/global readiness may have changed while another project held the machine slot.
                    with self.db() as db:
                        public_origin = gitbackup.public_backup_origin(db, link.node_id)
                    if not public_origin:
                        result = await private_push()
                    else:
                        grant = self.backup_grants.mint(project_id, link.node_id)
                        exchanged = self.backup_grants.lookup(grant, project_id)
                        try:
                            result = await link.git_backup({'project_id': project_id, 'account': account, 'path': path,
                                'private_origin': base, 'public_origin': public_origin, 'grant': grant,
                                'token_sha256': self.backup_grants.commitment(grant, project_id)},
                                timeout=self.settings.backup_timeout)
                            # A deferral makes no success claim and may precede token exchange.
                            # A success names the path its grant was exchanged on (never "neither" for an unused grant).
                            if result.get('retry') is not True and (not exchanged or result.get('via') not in ('private', 'public')
                                                                    or exchanged.get('via') != result['via']):
                                raise RuntimeError('Node backup result did not match the authenticated token path')
                        finally:
                            self.backup_grants.revoke(grant)
            else:
                result = await private_push()
        except MaintenanceBusy:
            self.backup_soon(project_id)
            raise
        except (BrokerError, RuntimeError, OSError, TimeoutError) as error:
            status["error"] = (f"host {host} is not reachable" if isinstance(error, BrokerUnavailable)
                               else str(error)[:300])
            status["failed_at"] = now().isoformat()
            self._backup_deferred.pop(project_id, None)
            log.warning("git backup %s (%s): failed: %s", slug, host, status["error"])
            return await joined_thread(self._save_backup, project_id, status)
        if result.get('retry') is True:
            # Not saved, so checked_at stays due: the next scan (SCAN_EVERY) retries, not every supervisor pass.
            waited = time.time() - self._backup_deferred.setdefault(project_id, time.time())
            if waited < DEFER_LIMIT:
                log.info("git backup %s (%s): temporarily unavailable; retry next scan", slug, host)
                return self.backup_status(project_id)
            status["error"] = f"the server has answered 503 (temporarily unavailable) for {int(waited // 60)} minutes"
            status["failed_at"] = now().isoformat()
            log.warning("git backup %s (%s): failed: %s", slug, host, status["error"])
            return await joined_thread(self._save_backup, project_id, status)
        self._backup_deferred.pop(project_id, None)
        await joined_thread(gitbackup.set_head, self.settings.git_dir, project_id, result.get("head"))
        kept = result.get("kept") or []
        log.info("git backup %s (%s): %d ref(s) updated, %d kept as backup/ (%.1fs)", slug, host,
                 len(result.get("updated") or []), len(kept), time.monotonic() - started)
        status.pop("error", None)
        status.update({"digest": result.get("digest"), "last_at": now().isoformat(),
                       "updated": len(result.get("updated") or []), "note": result.get("note")})
        if result.get('via') in ('private', 'public'):
            status['via'] = result['via']
        else:
            status['via'] = 'private'
        if kept:
            stamped = [{**k, "at": now().isoformat()} for k in kept]
            status["kept"] = (stamped + list(status.get("kept") or []))[:MAX_KEPT]
        public = (link.node_id, host, slug) if status['via'] == 'public' else None
        return await joined_thread(self._save_backup, project_id, status, public,
                                   {"project": slug, "kept": kept} if kept else None)

    def _save_backup(self, project_id: int, status: dict, public: tuple[str, str, str] | None = None,
                     kept: dict | None = None) -> dict:
        intent = WRITE_INTENT.set(True)
        try:
            with self.db() as db:
                db.merge(Observation(key=f"backup.{project_id}", value=status, source="git backup", observed_at=time.time()))
                if public:
                    node_id, host, slug = public
                    db.merge(Observation(key=f'backup.public.{node_id}', value={'at': status['last_at'],
                                         'project_id': project_id}, source='git backup token at public edge', observed_at=time.time()))
                    db.add(AuditLog(actor=f'node:{host}', action='git_backup.public', target=slug,
                                    detail={'project_id': project_id, 'node_id': node_id, 'via': 'public'}))
                if kept:
                    db.add(Event(source='supervisor', kind='git_backup_kept', payload=kept))
                db.commit()
            return status
        finally:
            WRITE_INTENT.reset(intent)
