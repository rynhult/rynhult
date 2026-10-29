"""Test-only companion: real desktop main + fixture broker, never a user account.

Used ONLY through a test-owned interpreter wrapper. Production has no test-mode
config, manifest override or same-user execution option.
"""
import asyncio
import json
import os
import shutil
import sys
import uuid
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'desktop'), str(ROOT / 'tests')]

import execution  # noqa: E402
import main as desktop_main  # noqa: E402
from owned_tmux import Owner  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from acc.broker.config import BrokerConfig  # noqa: E402
from acc.broker.server import serve  # noqa: E402
from acc.server import app as server_app  # noqa: E402
from acc.server import broker_client  # noqa: E402
from acc.server.config import Settings  # noqa: E402

base = Path(sys.argv[1])
config = json.loads(sys.stdin.buffer.readline(16384))
agent = base / 'agent'
agent.mkdir()
workspaces, run, binaries = base / 'projects', base / 'run', agent / '.local/bin'
for path in (workspaces, run, binaries):
    path.mkdir(parents=True)
(run / agent.name).mkdir()
(base / 'sockets').mkdir()
for provider in ('claude', 'codex'):
    (binaries / f'fake-{provider}').symlink_to(ROOT / 'tests/fake_cli.py')
os.environ['HOME'] = str(agent)
os.environ['FAKE_HOOK'] = str(ROOT / 'bin/acc-hook')
assets = base / 'ui'
shutil.copytree(ROOT / 'desktop/web-dist', assets)
index = assets / 'index.html'
index.write_text(index.read_text().replace('</head>', '<script src="/assets/layout-probe.js" defer></script></head>'))
(assets / 'assets/layout-probe.js').write_text('''setInterval(() => {
 const page = document.querySelector('main>div.mx-auto'), main = document.querySelector('main');
 fetch('/api/desktop/test-layout', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
   viewport:innerWidth, scale:devicePixelRatio, font:getComputedStyle(document.documentElement).fontSize,
   width:page?.getBoundingClientRect().width, main:main?.getBoundingClientRect().width,
   terminal:document.querySelector('.xterm-rows')?.innerText.slice(0,1024) || ''
 })});
}, 500);''')
owner = Owner()
socket_name = 'rd-test-' + uuid.uuid4().hex[:8]
owner.register(socket_name)
broker = BrokerConfig(socket_path=base / 'sockets' / f'{agent.name}.sock', run_dir=run / agent.name, tmux_socket=socket_name,
    allowed_roots=(workspaces,), allowed_uids=frozenset({os.getuid()}),
    providers={name: str(binaries / f'fake-{name}') for name in ('claude', 'codex')},
    hook_path=str(ROOT / 'bin/acc-hook'), catalog_dir=ROOT / 'catalog', catalog_local_dir=base / 'catalog-local')
# Socket/account identity is simulated only in this fixture process.
broker_client.uid_of = lambda account: os.getuid()
Settings.account_home = staticmethod(lambda account: agent)
original_settings = desktop_main.settings_for
info = {'token': config['token']}


def settings_for(root, static, origin):
    info['origin'] = origin
    return replace(original_settings(root, assets, origin), ingest_interval=.2, reconcile_interval=.3, codex_settle_seconds=1, claude_verify_timeout=5)


def create_app(settings):
    app = original_app(settings)

    async def layout(request):
        value = await request.json()
        (base / 'layout.json').write_text(json.dumps(value))
        return JSONResponse({'ok': True})

    app.router.routes.insert(0, Route('/api/desktop/test-layout', layout, methods=['POST']))
    return app


original_app = server_app.create_app
original_bootstrap = execution.bootstrap_owner


def bootstrap(app):
    session, csrf = original_bootstrap(app)
    info.update(session=session, csrf=csrf)
    path = base / 'info.json'
    path.write_text(json.dumps(info))
    path.chmod(0o600)
    return session, csrf


desktop_main.settings_for = settings_for
server_app.create_app = create_app
execution.bootstrap_owner = bootstrap
execution.read_manifest = lambda uid: {'home': str(agent), 'run_dir': str(run), 'projects': str(workspaces),
    'socket': str(broker.socket_path), 'resources': str(ROOT)}


async def run_test_companion():
    task = asyncio.create_task(serve(broker))
    try:
        while not broker.socket_path.exists():
            if task.done():
                await task
            await asyncio.sleep(.02)
        await desktop_main.serve(config)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(owner.close)


asyncio.run(run_test_companion())
