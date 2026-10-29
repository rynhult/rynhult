"""Owner intent and bounded reconciliation through the uninstalled fixed root socket."""
import asyncio
import json
import logging

from fastapi import HTTPException

from acc.peercred import peer_uid
from acc.remote_access_helper import revision

from . import devices, edge
from .models import AuditLog, Setting
from .node_tasks import joined_thread

log = logging.getLogger(__name__)
SWEEP_SECONDS = 15  # ends public streams whose identity expired, retired or was switched off
DEFAULT = {'desired_enabled': False, 'enabled': False, 'hostname': '', 'last_applied_revision': None,
           'devices_allowed': 0, 'last_reload_result': 'never', 'pending': False, 'registry_revision': None}


def state(s):
    row = s.get(Setting, 'remote_access_runtime')
    return {**DEFAULT, **(row.value if row else {})}


async def request(message):
    async with asyncio.timeout(30):
        reader, writer = await asyncio.open_unix_connection('/run/acc-remote-access.sock', limit=1048577)
        try:
            if peer_uid(writer.get_extra_info('socket')) != 0:
                raise OSError('untrusted remote access helper')
            writer.write(json.dumps(message).encode() + b'\n')
            await writer.drain()
            raw = await reader.readline()
            if len(raw) > 1048576:
                raise OSError('oversize response')
            result = json.loads(raw)
            if not isinstance(result, dict) or 'error' in result:
                raise OSError('helper refused')
            return result
        finally:
            writer.close()
            await writer.wait_closed()


class RemoteAccess:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = asyncio.Lock()

    def payload(self):
        with self.ctx.db() as s:
            config = devices.settings(s)
            leaves = [d.certificate_pem for d in edge.active_leaves(s, self.ctx.settings.mobile_enabled)]
        return {'hostname': config['public_hostname'], 'leaves': leaves}

    def save(self, value, actor=None, action=None):
        with self.ctx.db() as s:
            s.merge(Setting(key='remote_access_runtime', value=value, updated_by=actor))
            if action:
                s.add(AuditLog(actor=actor or 'system', action=action, target='remote_access', detail={
                    k: value[k] for k in ('enabled', 'last_applied_revision', 'last_reload_result')}))
            s.commit()

    async def review(self):
        try:
            return await request({'action': 'review', **self.payload()})
        except (OSError, ValueError, KeyError, TimeoutError):
            raise HTTPException(503, 'The remote access helper is unavailable or refused the configuration. An owner must install and qualify it on the main machine first.') from None

    async def change(self, enabled, actor, expected=None):
        if not enabled:
            await self.halt()
        async with self.lock:
            if enabled:
                self.ctx.edge.halted = False  # the owner enables again after a Turn off
            return await self._change(enabled, actor, expected)

    async def halt(self):
        """Turn off takes effect in accd at once, even while a reconciliation holds the lock on a slow helper."""
        self.ctx.edge.halted = True
        with self.ctx.db() as s:
            value = state(s)
        value.update(desired_enabled=False, pending=True)
        self.save(value)
        await self.ctx.edge.sweep()

    async def _change(self, enabled, actor, expected=None):
        with self.ctx.db() as s:
            value = state(s)
        payload = self.payload() if enabled else {}
        if enabled:
            if self.ctx.edge.halted:  # a reconciliation must not undo a Turn off that is waiting for the lock
                raise HTTPException(409, 'Remote access was turned off. Review it again before applying.')
            if self.ctx.settings.ha_node_id:
                raise HTTPException(409, 'Public remote access can\'t be turned on while High availability is set up.')
            review = await self.review()
            if expected != review['revision']:
                raise HTTPException(409, 'The configuration changed. Select Review Caddy configuration again before you apply it.')
        value.update(desired_enabled=enabled, pending=True)
        self.save(value)
        if not enabled:
            await self.ctx.edge.sweep()  # end public streams now, before and regardless of the edge's answer
        try:
            result = await request({'action': 'apply', **payload, 'revision': expected} if enabled else {'action': 'off'})
            if (result.get('enabled') is not enabled or result.get('last_reload_result') != 'verified'
                    or (enabled and (result.get('last_applied_revision') != expected or self.ctx.edge.halted))):
                raise ValueError('unverified result')
            value.update({k: result[k] for k in ('enabled', 'hostname', 'last_applied_revision', 'devices_allowed', 'last_reload_result')})
            value.update(pending=False, registry_revision=revision(payload) if enabled else None)
            self.save(value, actor, 'remote_access.apply' if enabled else 'remote_access.off')
            return value
        except (OSError, ValueError, KeyError, TimeoutError):
            # An uncertain apply must never retain an enable intent or silently retry exposure.
            value.update(desired_enabled=False, enabled=None, last_reload_result='failed', pending=True)
            self.save(value, actor, 'remote_access.failed')
            await self.ctx.edge.sweep()
            try:
                result = await request({'action': 'off'})
                if result.get('enabled') is False and result.get('last_reload_result') == 'verified':
                    value.update(result, desired_enabled=False, pending=False, last_reload_result='failed; public routing removed')
                    self.save(value)
            except (OSError, ValueError, KeyError, TimeoutError):
                pass
            detail = ('Public access could not be turned on, so Rynhult turned it off again and checked that it is off. Select Review Caddy configuration and try again.'
                      if not value['pending'] else
                      'Rynhult could not confirm the change, and public access may still be on. Open Rynhult at its private address and select Turn off public access again.')
            raise HTTPException(503, detail) from None

    async def reconcile(self):
        async with self.lock:
            with self.ctx.db() as s:
                value = state(s)
            from .admin_features import enabled
            if not enabled(self.ctx.settings, 'remote_access') and value['desired_enabled']:
                await self._change(False, 'system')
                return
            if not value['desired_enabled']:
                if value['pending']:
                    await self._change(False, 'system')
                return
            try:
                observed = await request({'action': 'status'})
                if observed.get('enabled') is not True or observed.get('last_applied_revision') != value['last_applied_revision']:
                    await self._change(False, 'system')
                    return
            except (OSError, ValueError, KeyError, TimeoutError):
                await self._change(False, 'system')
                return
            payload = self.payload()
            if revision(payload) != value['registry_revision']:
                try:
                    review = await self.review()
                    await self._change(True, 'system', review['revision'])
                except HTTPException:
                    # No known-current policy: persist OFF intent and remove all public routing.
                    await self._change(False, 'system')

    async def sweep(self):
        """Retire overdue superseded machine leaves and end streams whose identity is no longer usable."""
        def retire():
            with self.ctx.db() as s:
                count = edge.retire_overdue(s)
                s.commit()
                return count
        await joined_thread(retire)
        await self.ctx.edge.sweep()

    async def sweeping(self):
        # Its own schedule: a reconciliation can wait on the lock or the helper for minutes, a cut-off must not.
        while True:
            try:
                await self.sweep()
            except Exception:
                log.warning('Remote access identity sweep failed; retrying')
            await asyncio.sleep(SWEEP_SECONDS)

    async def run(self):
        sweeper = asyncio.create_task(self.sweeping())
        try:
            while True:
                try:
                    await self.reconcile()
                except Exception:
                    log.warning('Remote access reconciliation pending; check owner status')
                await asyncio.sleep(15)
        finally:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
