"""Coordinator-only broker/ACL/PTY smoke, bound to this owner's desktop receipt.

Never connects to a server HTTP API or deployment broker. Real CLI probes only
run --version when that provider is installed AND logged in as the test agent.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path


def account_checks(info, *, ssh_denial=None, run=subprocess.run, owner_uid=0):
    """Account properties the broker cannot report: Remote Login refused, no FileVault Secure Token."""
    path = Path(ssh_denial or f"/etc/ssh/sshd_config.d/100-rynhult-desktop-{info['owner_uid']}.conf")
    try:
        meta, text = path.lstat(), path.read_text()
    except OSError:
        raise ValueError('Remote Login denial for the agent is missing') from None
    if meta.st_uid != owner_uid or meta.st_mode & 0o022 or f"DenyUsers {info['agent']}" not in text.splitlines():
        raise ValueError('Remote Login denial for the agent is not the protected setup file')
    token = run(['/usr/sbin/sysadminctl', '-secureTokenStatus', info['agent']], capture_output=True, text=True,
                env={'PATH': '/usr/bin:/bin:/usr/sbin', 'LC_ALL': 'C'})
    said = (token.stdout + token.stderr).strip()
    if 'Secure token is DISABLED' not in said or 'ENABLED' in said:
        raise ValueError(f'Agent Secure Token status is not disabled: {said!r}')
    return {'remote_login': 'denied', 'secure_token': 'disabled'}


async def terminal_stream(info, tmux_name):
    from acc.broker.attach import frame, read_frame
    from acc.peercred import peer_uid

    reader, writer = await asyncio.open_unix_connection(info['socket'])
    try:
        if peer_uid(writer.get_extra_info('socket')) not in {0, info['agent_uid']}:
            raise ValueError('Terminal peer is not the protected desktop broker')
        writer.write(json.dumps({'id':1,'method':'attach','params':{'tmux_name':tmux_name,'readonly':True}}).encode()+b'\n')
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(),10))
        if not reply.get('ok'):
            raise ValueError('Terminal attach refused')
        kind, data = await asyncio.wait_for(read_frame(reader),10)
        if kind != b'd' or not data:
            raise ValueError('Terminal did not stream PTY bytes')
        writer.write(frame(b'x',b''))
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def probe(info, client):
    ping = await client.call('ping')
    if (ping['user'] != info['agent'] or ping['home'] != info['home']
            or ping['run_dir'] != str(Path(info['run_dir'])/info['agent'])
            or ping['roots'] != [info['projects']]):
        raise ValueError('Broker is not the dedicated desktop account/layout')
    report = {'broker_account':ping['user'],'files':'pending','providers':{},'spools':[]}
    name = 'desktop-mac-smoke-'+secrets.token_hex(6)
    parent = Path(info['projects'])/'local'
    created = False
    try:
        made = await client.call('make_workspace',parent=str(parent),name=name,title='Desktop smoke')
        created = True
        workspace = Path(made['path'])
        # Written by agent, read directly by owner: exercises actual macOS ACL inheritance.
        await client.call('files',path=str(workspace),action='create',rel='smoke.txt',content='Desktop smoke: café\n')
        if (workspace/'smoke.txt').read_text() != 'Desktop smoke: café\n':
            raise ValueError('Owner could not read the agent-created file')
        if not (workspace/'docs/handoffs').is_dir():
            raise ValueError('Owner cannot traverse inherited project folders')
        report['files'] = 'passed'
        installed = await client.call('installed_providers')
        health = await client.call('auth_health')
        for provider in ('claude','codex'):
            if not installed.get(provider):
                report['providers'][provider] = {'skipped':'CLI not installed for the agent account'}
                continue
            if not health.get(provider,{}).get('logged_in'):
                report['providers'][provider] = {'skipped':'CLI is not logged in for the agent account'}
                continue
            acc_id = secrets.token_hex(6)
            tmux_name = 'acc-mac-smoke-'+acc_id
            report['spools'].append(acc_id)
            try:
                await client.call('launch',acc_id=acc_id,tmux_name=tmux_name,provider=provider,
                    cwd=str(workspace),args=['--version'],files={},env={},
                    run_dir=str(Path(info['run_dir'])/info['agent']/acc_id))
                for _ in range(100):
                    output = await client.call('terminal_output',tmux_name=tmux_name)
                    if output['text'].strip():
                        break
                    await asyncio.sleep(.1)
                else:
                    raise ValueError('Provider version did not render')
                await terminal_stream(info,tmux_name)
                report['providers'][provider] = {'terminal':'passed','command':'--version; no inference'}
            finally:
                await client.call('kill',tmux_name=tmux_name)
                await client.call('clear_env',acc_id=acc_id)
    finally:
        if created:
            await client.call('delete_workspace',parent=str(parent),name=name)
    return report


def main():
    if sys.platform != 'darwin' or os.geteuid() == 0:
        raise SystemExit('Run this probe as the ordinary desktop owner on macOS only')
    resources = Path('/Library/Application Support/Rynhult Desktop/Runtime')
    sys.path[:0] = [str(resources/'desktop'),str(resources/'src')]
    from execution import read_manifest

    from acc.server.broker_client import BrokerClient

    info = read_manifest(os.getuid())
    if info is None:
        raise SystemExit('Install the reviewed macOS execution package first')

    checks = account_checks(info)

    async def run():
        client = BrokerClient(Path(info['socket']),trusted_uids=frozenset({0,info['agent_uid']}))
        try:
            return {**checks, **await probe(info,client)}
        finally:
            await client.close()
    print(json.dumps(asyncio.run(run()),indent=2))


if __name__ == '__main__':
    main()
