"""Security review 8 Lows (2026-10-26): one regression per fixed finding."""
import importlib.machinery
import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def launcher():
    loader = importlib.machinery.SourceFileLoader('acc_containers_sr8', str(REPO / 'deploy/containers/acc-containers'))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def test_a_trickling_client_cannot_hold_the_launcher_past_its_read_deadline(monkeypatch):
    # SR8-4: only each recv had a timeout, so a byte every few seconds held the instance until TimeoutStartSec (240 s).
    helper = launcher()
    monkeypatch.setattr(helper, 'READ_DEADLINE', 0.6)
    monkeypatch.setattr(helper, 'peer', lambda stream: (1, 1004))
    handled = []
    monkeypatch.setattr(helper, 'handle', lambda *args: handled.append(args) or {})
    server, client = socket.socketpair()
    stop = threading.Event()
    def trickle():
        while not stop.is_set():
            try:
                client.send(b'{')
            except OSError:
                return
            time.sleep(0.2)
    sender = threading.Thread(target=trickle, daemon=True)
    sender.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            helper.serve(server)
    finally:
        stop.set()
        sender.join(2)
        server.close()
        client.close()
    assert time.monotonic() - started < 2 and not handled


# SR8-7: any signed-in person could file unlimited pending own-machine requests (an audit row and an owner review
# entry each), one per machine and account name.
from test_auth import live  # noqa: E402,F401
from test_group_remote import remote_case  # noqa: E402,F401
from test_groups import grouped, two  # noqa: E402,F401


async def test_pending_own_machine_requests_are_capped(remote_case, monkeypatch):  # noqa: F811
    from helpers import write_db
    from test_users import csrf

    from acc.server.api import group_machines
    from acc.server.models import Node
    case = remote_case
    with write_db(case['owner'].app.state.db) as s:
        s.add(Node(id='a' * 32, name='alex-box', status='active', created_by='casey'))
        s.commit()
    member = case['member']
    async def request(account):
        return await member.post('/api/group-machines', json={'node_id': 'a' * 32, 'account': account},
                                 headers=csrf(member))
    for n in range(group_machines.MAX_PENDING_PER_PERSON):
        assert (await request(f'agent{n}')).status_code == 201
    refused = await request('agent-extra')
    assert refused.status_code == 429
    assert 'requests waiting' in refused.json()['detail'] and 'Withdraw one' in refused.json()['detail']
    # Withdrawing one frees a slot.
    first = (await member.get('/api/group-machines')).json()[0]
    assert (await member.post(f"/api/group-machines/{first['id']}/revoke", headers=csrf(member))).status_code == 200
    assert (await request('agent-extra')).status_code == 201
    # And an installation-wide ceiling.
    monkeypatch.setattr(group_machines, 'MAX_PENDING', group_machines.MAX_PENDING_PER_PERSON)
    owner = case['owner']
    overall = await owner.post('/api/group-machines', json={'node_id': 'a' * 32, 'account': 'owner-agent'},
                               headers=csrf(owner))
    assert overall.status_code == 429 and 'instance owner' in overall.json()['detail']


# SR8-6: the installer runs `codex sandbox` as the agent account; it must not inherit root's terminal (stdin or the
# controlling tty), which agent-account code could read from or keep after the timeout.
def test_the_installers_codex_check_never_gets_roots_terminal(tmp_path):
    import fcntl
    import os
    import pty
    import pwd
    import shutil
    import subprocess
    import termios
    tools = tmp_path / 'tools'
    tools.mkdir()
    report = tmp_path / 'report'
    def stub(name, body):
        (tools / name).write_text(f'#!/bin/sh\n{body}\n')
        (tools / name).chmod(0o755)
    stub('runuser', 'shift 3; exec "$@"')  # -u <account> --
    stub('timeout', 'shift; exec "$@"')
    stub('codex', f'{{ [ -t 0 ] && echo stdin-is-a-tty; (: </dev/tty) 2>/dev/null && echo has-controlling-tty; '
                  f'echo ran; }} > {report}')
    for name in ('cat', 'cut', 'env', 'getent', 'sed', 'sh', 'tail', 'tr', 'setsid'):
        (tools / name).symlink_to(shutil.which(name))
    account = pwd.getpwuid(os.getuid()).pw_name
    master, secondary = pty.openpty()
    def own_terminal():  # like root's interactive install: a session whose controlling tty is this pty
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    result = subprocess.run(['/bin/bash', '-c', 'source "$1"; check_codex_sandbox "$2"', '-',
                             str(REPO / 'deploy/lib/apparmor-userns.sh'), account],
                            stdin=secondary, capture_output=True, text=True, timeout=30, preexec_fn=own_terminal,
                            env={'PATH': str(tools), 'ACC_AGENT_PATH': str(tools)})
    os.close(secondary)
    os.close(master)
    assert result.stdout.strip() == f'Codex sandbox: works for {account}', result.stderr
    assert report.read_text().split() == ['ran']


# SR8-8: downgrading below 0063 dropped every machine-account binding, the approved and revoked rows that keep a
# removed person's account from going to someone else. It now refuses while such rows exist.
def test_downgrade_below_0063_refuses_while_accounts_are_reserved(tmp_path):
    from alembic import command
    from sqlalchemy import MetaData, Table, create_engine, inspect, text

    from acc.server.db import alembic_config
    from acc.server.models import Node
    url = f'sqlite:///{tmp_path / "reserved.db"}'
    config = alembic_config(url)
    command.upgrade(config, '0063')
    engine = create_engine(url)
    # Insert through the table as it exists at 0063, not the current model (later migrations add node columns).
    present = {c['name'] for c in inspect(engine).get_columns('node')}
    row = {'id': 'a' * 32, 'name': 'alex-box', 'created_by': 'casey'}
    for col in Node.__table__.columns:
        if col.name in present and col.name not in row and col.default is not None:
            arg = col.default.arg
            row[col.name] = arg(None) if callable(arg) else arg
    with engine.begin() as c:
        node_0063 = Table('node', MetaData(), autoload_with=c)
        c.execute(node_0063.insert().values({k: v for k, v in row.items() if k in present}))
    with engine.begin() as c:
        c.execute(text("INSERT INTO group_machine_account (id,node_id,person,account,status,approved_by) VALUES "
                       "(:a,:node,'alex','agent','revoked','casey'), (:b,:node,'sam','agent2','pending',NULL)"),
                  {'a': 'a' * 32, 'b': 'b' * 32, 'node': 'a' * 32})
    with pytest.raises(RuntimeError, match='reserv'):
        command.downgrade(config, '0054')
    with engine.connect() as c:
        assert c.scalar(text('SELECT version_num FROM alembic_version')) == '0063'
        assert c.scalar(text('SELECT count(*) FROM group_machine_account')) == 2
    with engine.begin() as c:
        c.execute(text("DELETE FROM group_machine_account WHERE approved_by IS NOT NULL"))
    command.downgrade(config, '0054')  # only pending requests left: nothing reserved is lost
    assert 'group_machine_account' not in inspect(engine).get_table_names()
    engine.dispose()
