"""Break-it pass 10 (#364: phone pairing, rollover, public git-backup consent): careless and impatient use."""
from sqlalchemy import select
from test_devices import confirmation_body, enable, signer, start  # noqa: F401  (signer is a fixture)
from test_passkeys import setup

from acc.server.models import AuditLog, DevicePairing


async def claim(api, pending):
    return await api.post('/api/device-pairing/claim', json={'token': pending['link'].split('#')[1]})


async def test_closing_a_pairing_cancels_its_link(api, env, monkeypatch):
    """B10-1: Close pairing used to leave the one-use link claimable for five minutes, counted against the limit."""
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    cancelled = await api.post(f"/api/devices/pairings/{pending['id']}/cancel", headers=headers, json={})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()['status'] == 'cancelled'
    assert (await claim(api, pending)).status_code == 410
    with api.app.state.db() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == 'device.pairing_cancel')).target == pending['id']
    # A claimed pairing (the new device opened the link) is cancelled too; closing again changes nothing.
    second = await start(api, headers)
    assert (await claim(api, second)).status_code == 200
    assert (await api.post(f"/api/devices/pairings/{second['id']}/cancel", headers=headers, json={})).json()['status'] == 'cancelled'
    assert (await api.post(f"/api/devices/pairings/{second['id']}/cancel", headers=headers, json={})).json()['status'] == 'cancelled'
    confirmed = await api.post(f"/api/devices/pairings/{second['id']}/confirm", headers=headers,
                               json=await confirmation_body(api, second))
    assert confirmed.status_code == 410


async def test_closing_after_confirmation_keeps_the_import(api, env, monkeypatch, signer):  # noqa: F811
    """Closing the dialog once the owner confirmed must not take the identity away from the device mid-download."""
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    receipt = (await claim(api, pending)).json()['receipt']
    assert (await api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers,
                           json=await confirmation_body(api, pending))).status_code == 200
    kept = await api.post(f"/api/devices/pairings/{pending['id']}/cancel", headers=headers, json={})
    assert kept.status_code == 200 and kept.json()['status'] == 'ready'
    assert (await api.post('/api/device-pairing/download', json={'receipt': receipt})).status_code == 200


async def test_a_second_renewal_of_one_device_replaces_the_first(api, env, monkeypatch, signer):  # noqa: F811
    """B10-2: tapping Renew twice left two live renewals of one device; confirming both silently replaced the
    identity the first phone had imported."""
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    pending = await start(api, headers)
    receipt = (await claim(api, pending)).json()['receipt']
    await api.post(f"/api/devices/pairings/{pending['id']}/confirm", headers=headers, json=await confirmation_body(api, pending))
    assert (await api.post('/api/device-pairing/download', json={'receipt': receipt})).status_code == 200
    device = (await api.get('/api/devices')).json()['devices'][0]
    first = (await api.post(f"/api/devices/{device['id']}/renew", headers=headers, json={})).json()
    second = (await api.post(f"/api/devices/{device['id']}/renew", headers=headers, json={})).json()
    assert (await claim(api, first)).status_code == 410
    assert (await claim(api, second)).status_code == 200
    with api.app.state.db() as s:
        assert s.get(DevicePairing, first['id']).status == 'cancelled'


async def test_the_pairing_qr_code_is_an_image_a_browser_and_a_phone_camera_can_read(api, env, monkeypatch):
    """B10-3: the dialog shows the QR as an <img> data: URL, which needs a standalone SVG (xmlns); the inline SVG
    rendered as alt text, and its light-grey modules on no background wouldn't scan on a light screen anyway."""
    headers, _ = await setup(api, env, monkeypatch)
    await enable(api, headers)
    svg = (await start(api, headers))['qr_svg']
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert 'fill="#fff"' in svg and '#e6e8eb' not in svg  # dark modules on a white quiet zone


def test_the_authenticator_qr_scans_on_either_theme():
    """The setup and invite QR is inline SVG on the page: it carries its own white quiet zone and black modules, so a
    camera reads it whatever the theme behind it (it was light grey on transparent)."""
    from acc.server import auth
    svg = auth.qr_svg('otpauth://totp/Rynhult:alex?secret=ABC&issuer=Rynhult')
    assert 'fill="#fff"' in svg and 'stroke="#000"' in svg and '#e6e8eb' not in svg
