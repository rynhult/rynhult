"""Only test-created PostgreSQL containers; never accept an external database URL."""
import json
import os
import shutil
import subprocess
import time
import uuid

import pytest
from sqlalchemy import text

from acc.server.db import make_engine


@pytest.fixture(scope='session')
def postgres_url(tmp_path_factory):
    pytest.importorskip('psycopg', reason='PostgreSQL tests require the project postgres extra (psycopg)')
    # Docker shares a host daemon; it cannot provide an isolated store for a root gate.
    runtimes = [r for r in ('podman',) if shutil.which(r)]
    root = tmp_path_factory.mktemp('postgres-runtime')
    (root / 'runtime').mkdir(mode=0o700)
    storage = root / 'storage.conf'
    storage.write_text('[storage]\ndriver = "vfs"\ngraphroot = ' + json.dumps(str(root / 'store'))
                       + '\nrunroot = ' + json.dumps(str(root / 'run')) + '\n')
    isolated_env = {**os.environ, 'HOME': str(root), 'XDG_CONFIG_HOME': str(root / 'config'),
                    'XDG_DATA_HOME': str(root / 'data'), 'TMPDIR': str(root),
                    'CONTAINERS_CONF': '/dev/null', 'CONTAINERS_CONF_OVERRIDE': '/dev/null',
                    'CONTAINERS_STORAGE_CONF': str(storage), 'XDG_RUNTIME_DIR': str(root / 'runtime')}
    reasons = []
    for runtime in runtimes:
        command = [runtime, '--root', str(root / 'store'), '--runroot', str(root / 'run'),
                   '--tmpdir', str(root / 'tmp'), '--storage-driver', 'vfs', '--events-backend', 'file',
                   '--cgroup-manager', 'cgroupfs']
        result = subprocess.run([*command, 'info'], env=isolated_env, capture_output=True, timeout=20)
        if result.returncode:
            reasons.append(f'{runtime}: unavailable to this account')
            continue
        name = 'acc-test-' + uuid.uuid4().hex
        password = uuid.uuid4().hex
        try:
            started = subprocess.run([*command, 'run', '--detach', '--rm', '--name', name, '--network', 'slirp4netns',
                                      '-e', f'POSTGRES_PASSWORD={password}', '-e', 'POSTGRES_DB=acc_test',
                                      '-p', '127.0.0.1::5432', 'postgres:17-alpine'],
                                     env=isolated_env, capture_output=True, timeout=120)
            if started.returncode:
                reasons.append(f'{runtime}: could not start throwaway PostgreSQL')
                continue
            port = subprocess.check_output([*command, 'port', name, '5432/tcp'], env=isolated_env, text=True).strip().rsplit(':', 1)[1]
            url = f'postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/acc_test?sslmode=disable'
            engine = make_engine(url)
            try:
                for attempt in range(60):
                    try:
                        with engine.connect() as db:
                            db.execute(text('SELECT 1'))
                        break
                    except Exception:
                        if attempt == 59:
                            raise
                        time.sleep(0.5)
            finally:
                engine.dispose()
            yield url
            return
        finally:
            subprocess.run([*command, 'rm', '--force', name], env=isolated_env, capture_output=True, timeout=30)
            subprocess.run([*command, 'system', 'reset', '--force'], env=isolated_env, capture_output=True, timeout=30)
    pytest.skip('No usable throwaway PostgreSQL container runtime: ' + ('; '.join(reasons) or 'Podman not installed; shared Docker daemon is not isolated'))


@pytest.fixture(params=['sqlite', 'postgresql'])
def backend_url(request, tmp_path):
    if request.param == 'sqlite':
        return f'sqlite:///{tmp_path}/acc.db'
    url = request.getfixturevalue('postgres_url')
    engine = make_engine(url)
    try:
        with engine.begin() as db:
            # A previous test's pooled connection can still hold session-level advisory locks
            # (HA leadership), which survive a schema reset: end those sessions first.
            db.exec_driver_sql('SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                               'WHERE datname = current_database() AND pid <> pg_backend_pid()')
            db.exec_driver_sql('DROP SCHEMA public CASCADE')
            db.exec_driver_sql('CREATE SCHEMA public')
    finally:
        engine.dispose()
    return url
