"""Owner recovery boundaries, exercised only against disposable fixture databases."""
from pathlib import Path

import pytest
from helpers import TOTP_AT, write_db
from sqlalchemy import select
from test_auth import ORIGIN, PASSWORD, cookie_client

from acc import owner_recovery
from acc.server import auth
from acc.server.models import (
    AuditLog,
    AuthSession,
    Invite,
    Passkey,
    PasskeyChallenge,
    PasskeyPolicy,
    RecoveryCode,
    User,
)


async def setup_codes(api, env):
    token = env['settings'].setup_token_path.read_text().strip()
    begin = await api.post('/api/auth/setup/begin', headers=ORIGIN, json={'setup_token': token})
    secret = begin.json()['totp_secret']
    done = await api.post('/api/auth/setup/complete', headers=ORIGIN, json={
        'setup_token': token, 'username': 'casey', 'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT)})
    assert done.status_code == 200, done.text
    assert done.headers['cache-control'] == 'no-store'
    return done.json()['recovery_codes'], secret


async def login(api, code, password=PASSWORD):
    api.cookies.clear()
    return await api.post('/api/auth/login', headers=ORIGIN,
                          json={'username': 'casey', 'password': password, 'code': code})


async def test_setup_hashes_single_use_policy_and_audit(api, env):
    codes, _ = await setup_codes(api, env)
    assert len(set(codes)) == 10
    with write_db(api.app.state.db) as db:
        rows = db.scalars(select(RecoveryCode)).all()
        assert len(rows) == 10
        assert len({r.salt for r in rows}) == 10
        assert all(r.code_hash not in codes and r.salt not in codes for r in rows)
        assert all(auth.recovery_digest(r.salt, c) == r.code_hash for r, c in zip(rows, codes))
        db.merge(PasskeyPolicy(id=1, owners='require', members='allow'))
        db.commit()
    cookie_client(api)
    assert (await login(api, codes[0], password='wrong')).status_code == 401
    assert (await login(api, codes[0])).status_code == 200
    assert (await login(api, codes[0])).status_code == 401
    assert (await login(api, codes[1])).status_code == 200
    with api.app.state.db() as db:
        assert len(db.scalars(select(RecoveryCode)).all()) == 8
        actions = list(db.scalars(select(AuditLog.action)))
        assert 'auth.recovery_codes_created' in actions
        assert actions.count('auth.recovery_code_used') == 2
        assert all(c not in str(db.scalars(select(AuditLog.detail)).all()) for c in codes)


async def test_recovery_rate_limit(api, env):
    codes, _ = await setup_codes(api, env)
    cookie_client(api)
    for _ in range(5):
        assert (await login(api, 'incorrect-recovery')).status_code == 401
    assert (await login(api, codes[0])).status_code == 429
    with api.app.state.db() as db:
        assert len(db.scalars(select(RecoveryCode)).all()) == 10


async def test_regenerate_requires_reverification_invalidates_old(api, env, monkeypatch):
    codes, secret = await setup_codes(api, env)
    cookie_client(api)
    headers = {**ORIGIN, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
    route = '/api/auth/passkeys/recovery-codes'
    assert (await api.post(route, headers=headers, json={})).status_code == 403
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 30)
    verify = await api.post('/api/auth/passkeys/reauth', headers=headers, json={
        'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 30)})
    assert verify.status_code == 200, verify.text
    result = await api.post(route, headers=headers, json={})
    assert result.status_code == 200, result.text
    assert result.headers['cache-control'] == 'no-store'
    new = result.json()['codes']
    assert (await login(api, codes[0])).status_code == 401
    assert (await login(api, new[0])).status_code == 200


async def test_root_reset_and_one_time_link(api, env, monkeypatch):
    codes, _ = await setup_codes(api, env)
    with write_db(api.app.state.db) as db:
        user = db.scalar(select(User))
        uid = user.id
        db.add(Passkey(user_id=uid, credential_id='fixture', public_key='fixture', rp_id='localhost', name='fixture'))
        db.merge(PasskeyPolicy(id=1, owners='require', members='allow'))
        db.commit()
    monkeypatch.setattr(owner_recovery.os, 'getuid', lambda: 1000)
    monkeypatch.setattr(owner_recovery.os, 'geteuid', lambda: 1000)
    with write_db(api.app.state.db) as db:
        with pytest.raises(PermissionError):
            owner_recovery.reset_owner_mfa(db, 'casey')
    monkeypatch.setattr(owner_recovery.os, 'getuid', lambda: 0)
    # Effective root alone is insufficient.
    with write_db(api.app.state.db) as db:
        with pytest.raises(PermissionError):
            owner_recovery.reset_owner_mfa(db, 'casey')
    monkeypatch.setattr(owner_recovery.os, 'geteuid', lambda: 0)
    with write_db(api.app.state.db) as db:
        link = owner_recovery.reset_owner_mfa(db, 'casey')
        db.commit()
    token = link.rsplit('/', 1)[1]
    with api.app.state.db() as db:
        user = db.get(User, uid)
        assert user.totp_secret == '' and user.passkey_handle is None
        assert not auth.password_ok(user.password_hash, PASSWORD)
        for model in (RecoveryCode, AuthSession, Passkey, PasskeyChallenge):
            assert db.scalar(select(model).where(model.user_id == uid)) is None
        invite = db.scalar(select(Invite))
        assert invite.token_hash == auth.digest(token) and invite.token_hash != token
        event = db.scalar(select(AuditLog).where(AuditLog.action == 'auth.owner_mfa_reset'))
        assert event.actor == '@root-console' and token not in str(event.detail)
        assert db.get(PasskeyPolicy, 1).owners == 'require'
    cookie_client(api)
    api.cookies.clear()
    begin = await api.post('/api/auth/invite/begin', headers=ORIGIN, json={'token': token})
    assert begin.status_code == 200, begin.text
    body = {'token': token, 'password': PASSWORD, 'code': auth.totp(begin.json()['totp_secret'], TOTP_AT)}
    done = await api.post('/api/auth/invite/complete', headers=ORIGIN, json=body)
    assert done.status_code == 200, done.text
    assert len(done.json()['recovery_codes']) == 10
    assert (await api.post('/api/auth/invite/complete', headers=ORIGIN, json=body)).status_code == 410
    assert (await login(api, codes[0])).status_code == 401
    assert (await login(api, done.json()['recovery_codes'][0])).status_code == 200


async def test_nonowner_refused_unless_force_and_no_http_reset(api, env, monkeypatch):
    await setup_codes(api, env)
    monkeypatch.setattr(owner_recovery.os, 'getuid', lambda: 0)
    monkeypatch.setattr(owner_recovery.os, 'geteuid', lambda: 0)
    with write_db(api.app.state.db) as db:
        db.add(User(username='alex', password_hash='unused', totp_secret='', role='member'))
        db.commit()
    with write_db(api.app.state.db) as db:
        with pytest.raises(ValueError, match='not an owner'):
            owner_recovery.reset_owner_mfa(db, 'alex')
    with write_db(api.app.state.db) as db:
        assert owner_recovery.reset_owner_mfa(db, 'alex', force=True).startswith('/invite/')
        db.commit()
    # No route imports the privileged module; a known authenticated API caller has no reset command.
    assert all('reset-owner-mfa' not in getattr(r, 'path', '') for r in api.app.routes)
    api_dir = Path(__file__).parents[1] / 'src/acc/server/api'
    assert all('owner_recovery' not in p.read_text() for p in api_dir.glob('*.py'))


async def test_root_link_expires_and_reissue_invalidates_previous(api, env, monkeypatch):
    await setup_codes(api, env)
    monkeypatch.setattr(owner_recovery.os, 'getuid', lambda: 0)
    monkeypatch.setattr(owner_recovery.os, 'geteuid', lambda: 0)
    with write_db(api.app.state.db) as db:
        first = owner_recovery.reset_owner_mfa(db, 'casey').rsplit('/', 1)[1]
        db.commit()
    with write_db(api.app.state.db) as db:
        second = owner_recovery.reset_owner_mfa(db, 'casey').rsplit('/', 1)[1]
        db.commit()
    cookie_client(api)
    api.cookies.clear()
    assert (await api.post('/api/auth/invite/begin', headers=ORIGIN, json={'token': first})).status_code == 410
    with write_db(api.app.state.db) as db:
        row = db.scalar(select(Invite).where(Invite.token_hash == auth.digest(second)))
        row.expires_at = auth.now()
        db.commit()
    assert (await api.post('/api/auth/invite/begin', headers=ORIGIN, json={'token': second})).status_code == 410


def test_console_rejects_nonroot_before_opening_database(monkeypatch):
    import sys

    monkeypatch.setattr(sys, 'argv', ['rynhult-admin', 'reset-owner-mfa', 'casey',
                                     '--database', 'sqlite:////tmp/unused-owner-recovery.db'])
    monkeypatch.setattr(owner_recovery.os, 'getuid', lambda: 1000)
    monkeypatch.setattr(owner_recovery.os, 'geteuid', lambda: 1000)

    def must_not_open(*args, **kwargs):
        pytest.fail('nonroot attempted to open a database')

    monkeypatch.setattr(owner_recovery, 'make_engine', must_not_open)
    with pytest.raises(SystemExit) as exit_info:
        owner_recovery.main()
    assert exit_info.value.code == 2


async def test_recovery_code_race_only_one_signin_succeeds(api, env):
    import asyncio

    codes, _ = await setup_codes(api, env)
    cookie_client(api)
    api.cookies.clear()
    body = {'username': 'casey', 'password': PASSWORD, 'code': codes[0]}
    results = await asyncio.gather(*(api.post('/api/auth/login', headers=ORIGIN, json=body) for _ in range(2)))
    assert sorted(r.status_code for r in results) == [200, 401]
    with api.app.state.db() as db:
        assert len(db.scalars(select(RecoveryCode)).all()) == 9


async def test_root_console_prints_link_only_after_commit(api, env, monkeypatch, capsys):
    import sys

    await setup_codes(api, env)
    monkeypatch.setattr(owner_recovery.os, 'getuid', lambda: 0)
    monkeypatch.setattr(owner_recovery.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(sys, 'argv', ['rynhult-admin', 'reset-owner-mfa', 'casey',
                                     '--database', env['settings'].db_url])
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: False)
    with pytest.raises(SystemExit) as exit_info:
        owner_recovery.main()
    assert exit_info.value.code == 2
    with api.app.state.db() as db:
        assert db.scalar(select(Invite)) is None
    capsys.readouterr()
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    owner_recovery.main()  # Only the disposable fixture database above.
    output = capsys.readouterr()
    assert output.err == ''
    assert output.out.count('/invite/') == 1
    token = output.out.strip().rsplit('/', 1)[1]
    with api.app.state.db() as db:
        assert db.scalar(select(Invite)).token_hash == auth.digest(token)
        assert token not in str(db.scalars(select(AuditLog.detail)).all())


async def test_recovery_input_normalized(api, env):
    codes, _ = await setup_codes(api, env)
    cookie_client(api)
    formatted = ' - '.join(codes[0][i:i+4].upper() for i in range(0, 32, 4))
    assert (await login(api, formatted)).status_code == 200
    assert (await login(api, codes[0])).status_code == 401


@pytest.mark.parametrize('operation', ['enroll', 'regenerate', 'expire'])
async def test_guarded_recovery_enrollment_scoped_and_expires(api, env, operation):
    from datetime import timedelta

    from test_passkeys import ORIGIN as PASSKEY_ORIGIN
    from test_passkeys import Authenticator, enroll

    codes, _ = await setup_codes(api, env)
    cookie_client(api)
    with write_db(api.app.state.db) as db:
        db.merge(PasskeyPolicy(id=1, owners='require', members='allow'))
        db.commit()
    assert (await login(api, codes[0])).status_code == 200
    headers = {**ORIGIN, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
    route = '/api/auth/passkeys/recovery-reauth'
    assert (await api.post('/api/auth/passkeys/register/begin', headers=headers)).status_code == 403
    assert (await api.post(route, headers=headers, json={'password': PASSWORD, 'code': codes[0]})).status_code == 401
    assert (await api.post(route, headers=headers, json={'password': 'wrong', 'code': codes[1]})).status_code == 401
    result = await api.post(route, headers=headers, json={'password': PASSWORD, 'code': codes[1].upper()})
    assert result.status_code == 200, result.text
    for endpoint in ('policy', 'recovery-code'):
        body = {'owners': 'allow', 'members': 'allow'} if endpoint == 'policy' else {}
        assert (await api.post('/api/auth/passkeys/' + endpoint, headers=headers, json=body)).status_code == 403
    headers['Origin'] = PASSKEY_ORIGIN
    if operation == 'enroll':
        registered, _ = await enroll(api, headers, Authenticator())
        assert (await api.post(f"/api/auth/passkeys/{registered['id']}/remove", headers=headers)).status_code == 403
    elif operation == 'regenerate':
        result = await api.post('/api/auth/passkeys/recovery-codes', headers=headers)
        assert result.status_code == 200, result.text
        assert result.headers['cache-control'] == 'no-store'
    with write_db(api.app.state.db) as db:
        record = db.scalar(select(AuthSession).where(AuthSession.method == 'recovery_enroll'))
        assert record.reauthenticated_at is None
        if operation == 'expire':
            record.passkey_enroll_until = auth.now() - timedelta(minutes=1)
        else:
            assert record.passkey_enroll_until is None
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'auth.passkey_recovery_enrollment_verified'))
        db.commit()
    assert (await api.post('/api/auth/passkeys/recovery-codes', headers=headers)).status_code == 403
    assert (await api.post('/api/auth/passkeys/register/begin', headers=headers)).status_code == 403
    if operation == 'regenerate':
        assert (await login(api, codes[2])).status_code == 401
        assert (await login(api, result.json()['codes'][0])).status_code == 200


@pytest.mark.parametrize('state', ['one_code', 'guarded', 'expired', 'consumed', 'full', 'reauthenticated'])
async def test_owner_self_reset_recovery_guard(api, env, monkeypatch, state):
    from datetime import timedelta

    codes, secret = await setup_codes(api, env)
    cookie_client(api)
    with api.app.state.db() as db:
        uid = db.scalar(select(User.id))
    if state != 'full':
        assert (await login(api, codes[0])).status_code == 200
    headers = {**ORIGIN, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
    if state in ('guarded', 'expired', 'consumed'):
        verified = await api.post('/api/auth/passkeys/recovery-reauth', headers=headers,
                                  json={'password': PASSWORD, 'code': codes[1]})
        assert verified.status_code == 200, verified.text
        if state == 'expired':
            with write_db(api.app.state.db) as db:
                record = db.scalar(select(AuthSession).where(AuthSession.method == 'recovery_enroll'))
                record.passkey_enroll_until = auth.now() - timedelta(seconds=1)
                db.commit()
        elif state == 'consumed':
            assert (await api.post('/api/auth/passkeys/recovery-codes', headers=headers)).status_code == 200
    elif state == 'reauthenticated':
        monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 30)
        assert (await api.post('/api/auth/passkeys/reauth', headers=headers, json={
            'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 30)})).status_code == 200
    result = await api.post(f'/api/users/{uid}/reset', headers=headers)
    allowed = state in ('guarded', 'full', 'reauthenticated')
    assert result.status_code == (201 if allowed else 403), result.text
    with api.app.state.db() as db:
        assert (db.scalar(select(Invite)) is not None) == allowed
        if not allowed:
            assert db.scalar(select(RecoveryCode)) is not None
            assert 'second unused recovery code' in result.text


async def test_owner_reset_invalidates_codes_before_completion(api, env, monkeypatch):
    codes, secret = await setup_codes(api, env)
    cookie_client(api)
    monkeypatch.setattr(auth.time, 'time', lambda: TOTP_AT + 30)
    headers = {**ORIGIN, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
    assert (await api.post('/api/auth/passkeys/reauth', headers=headers, json={
        'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 30)})).status_code == 200
    with write_db(api.app.state.db) as db:
        # Artificial second person, with both forms of recovery material.
        user = User(username='alex', role='member', password_hash=auth.hash_password(PASSWORD),
                    totp_secret=secret, recovery_hash=auth.digest('legacy'))
        db.add(user)
        db.flush()
        uid = user.id
        other_codes = auth.generate_recovery_codes(db, user)
        db.commit()
    result = await api.post(f'/api/users/{uid}/reset', headers=headers)
    assert result.status_code == 201, result.text
    with api.app.state.db() as db:
        assert not db.scalars(select(RecoveryCode).where(RecoveryCode.user_id == uid)).all()
        assert db.get(User, uid).recovery_hash is None
        assert len(db.scalars(select(RecoveryCode)).all()) == len(codes)
    api.cookies.clear()
    result = await api.post('/api/auth/login', headers=ORIGIN, json={
        'username': 'alex', 'password': PASSWORD, 'code': other_codes[0]})
    assert result.status_code == 401


async def test_recovery_reauth_rate_limit_and_nonrecovery_session(api, env):
    codes, _ = await setup_codes(api, env)
    cookie_client(api)
    headers = {**ORIGIN, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
    route = '/api/auth/passkeys/recovery-reauth'
    # A setup session cannot spend recovery material on this limited grant.
    assert (await api.post(route, headers=headers, json={'password': PASSWORD, 'code': codes[0]})).status_code == 401
    assert (await login(api, codes[0])).status_code == 200
    headers['X-ACC-CSRF'] = api.cookies.get('acc_csrf')
    for _ in range(5):
        assert (await api.post(route, headers=headers, json={'password': PASSWORD, 'code': 'bad'})).status_code == 401
    assert (await api.post(route, headers=headers, json={'password': PASSWORD, 'code': codes[1]})).status_code == 429
    with api.app.state.db() as db:
        assert len(db.scalars(select(RecoveryCode)).all()) == 9
