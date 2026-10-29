"""Upgrade rehearsal: synthetic data at 0066 survives the app's own migration to head, value for value, and a
downgrade to 0066 restores the schema. SQLite always; PostgreSQL from local binaries when present (a throwaway cluster,
Unix socket only). scripts/upgrade_rehearsal.py adds the e2e server start on the upgraded database."""
import os
from pathlib import Path

import pytest
import upgrade_rehearsal as rehearsal
from sqlalchemy import text

from acc.server.db import make_engine, migrate

POSTGRES_BIN = Path(os.environ.get('ACC_TEST_POSTGRES_BIN', '/usr/lib/postgresql/16/bin'))


@pytest.fixture(params=['sqlite', 'postgresql'])
def url(request, tmp_path):
    if request.param == 'sqlite':
        yield f'sqlite:///{tmp_path}/acc.db'
        return
    if not (POSTGRES_BIN / 'initdb').is_file():
        pytest.skip(f'no PostgreSQL binaries at {POSTGRES_BIN} (ACC_TEST_POSTGRES_BIN)')
    pytest.importorskip('psycopg', reason='PostgreSQL tests require the project postgres extra (psycopg)')
    try:
        cluster = rehearsal.Cluster(POSTGRES_BIN, tmp_path)
        cluster_url = cluster.__enter__()
    except rehearsal.ClusterUnavailable as error:
        pytest.skip(f'no throwaway PostgreSQL on this machine: {error}')
    try:
        yield cluster_url
    finally:
        cluster.__exit__(None, None, None)


def test_upgrade_keeps_every_value_and_downgrade_restores_the_schema(url, tmp_path):
    rehearsal.upgrade(url, rehearsal.BASE)
    counts = rehearsal.seed(url, tmp_path / 'data')
    assert sum(counts.values()) > 250 and counts['event'] == 120
    base = rehearsal.schema(url)
    before = rehearsal.snapshot(url, base)

    migrate(url)  # the path accd takes at startup
    assert rehearsal.version(url) == rehearsal.head(url)
    after = rehearsal.snapshot(url, base)
    assert {t for t in before if before[t] != after[t]} == set()
    assert rehearsal.integrity(url) == []
    engine = make_engine(url)
    try:  # new rows still get ids after explicit seeded ones (PostgreSQL sequences)
        with engine.begin() as db:
            db.execute(text("insert into audit_log (ts, actor, action, detail) values (:ts, 'alex', 'rehearsal.check', '{}')"),
                       {'ts': rehearsal.at(1)})
    finally:
        engine.dispose()

    added = rehearsal.after_upgrade(url)
    rehearsal.downgrade(url, rehearsal.BASE)
    assert rehearsal.version(url) == rehearsal.BASE
    assert rehearsal.schema(url) == base
    assert rehearsal.integrity(url) == []
    down = rehearsal.snapshot(url, base)
    # 0067's downgrade deletes what older code would misread (security review 9, SR9-8): the phone-bound sign-in and
    # the pending phone pairing added after the upgrade are gone; every seeded row is as it was.
    assert {t for t in before if before[t] != down[t]} == {'audit_log'}
    if 'device_pairing' in added:
        assert added['auth_session'] == 1
        assert down['device_pairing'] == before['device_pairing'] and down['auth_session'] == before['auth_session']


@pytest.fixture
def as_root(monkeypatch, tmp_path):
    """Root's code path, run as this account: a fake runuser that drops `-u USER --`, and no chown."""
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'runuser').write_text('#!/bin/sh\n[ "$1" = -u ] && [ "$3" = -- ] || exit 64\necho "$2" >> "$RUNUSER_LOG"\n'
                                   'shift 3\nexec "$@"\n')
    (tools / 'runuser').chmod(0o755)
    log = tmp_path / 'runuser.log'
    monkeypatch.setenv('PATH', f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv('RUNUSER_LOG', str(log))
    monkeypatch.setattr(rehearsal.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(rehearsal.os, 'chown', lambda *a: None)
    return log


@pytest.mark.skipif(os.geteuid() == 0, reason='real root: the PostgreSQL case above already runs through the real runuser '
                    'as the unprivileged account; this simulation can only stand in for it when not root')
def test_as_root_postgres_runs_as_an_unprivileged_account(as_root, tmp_path, monkeypatch):
    if not (POSTGRES_BIN / 'initdb').is_file():
        pytest.skip(f'no PostgreSQL binaries at {POSTGRES_BIN}')
    pytest.importorskip('psycopg')
    import pwd
    monkeypatch.setattr(rehearsal.pwd, 'getpwnam', lambda name: pwd.getpwuid(os.getuid()) if name == 'nobody' else None)
    cluster = rehearsal.Cluster(POSTGRES_BIN, tmp_path)
    assert cluster.home.parent == Path('/tmp')
    with cluster as url:
        rehearsal.upgrade(url, '0001')
        assert rehearsal.version(url) == '0001'
    assert as_root.read_text().split() == ['nobody'] * 4  # initdb, pg_ctl start, createdb, pg_ctl stop
    assert not cluster.home.exists()


def test_as_root_without_an_unprivileged_account_skips_instead_of_failing(as_root, tmp_path, monkeypatch):
    def missing(name):
        raise KeyError(name)
    monkeypatch.setattr(rehearsal.pwd, 'getpwnam', missing)
    with pytest.raises(rehearsal.ClusterUnavailable, match="no 'nobody' account"):
        rehearsal.Cluster(POSTGRES_BIN, tmp_path)


def test_a_cluster_that_cannot_start_is_unavailable_not_an_error(tmp_path, monkeypatch):
    # The error conversion itself, on the unprivileged path also under a real root run (the fake initdb lives in the
    # test's private folder, which the unprivileged account couldn't reach).
    monkeypatch.setattr(rehearsal.os, 'geteuid', lambda: 1000)
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'initdb').write_text('#!/bin/sh\necho "initdb: cannot be run as root" >&2\nexit 1\n')
    (fake / 'initdb').chmod(0o755)
    with pytest.raises(rehearsal.ClusterUnavailable, match='initdb failed: initdb: cannot be run as root'):
        with rehearsal.Cluster(fake, tmp_path):
            pass
