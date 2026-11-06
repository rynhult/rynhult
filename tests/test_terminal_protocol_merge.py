"""The observer contract must not opt into interactive fencing with extra query fields."""
import asyncio

import pytest
import websockets
from test_auth import ORIGIN, live  # noqa: F401
from test_terminal_run_attach import running


async def test_observer_cannot_select_fenced_protocol_with_both_run_parameters(api, env, project, live):  # noqa: F811
    sid = await running(api, env, project)
    ref = (await api.get(f'/api/sessions/{sid}/terminal-reference')).json()
    headers = {'Cookie': f'acc_session={api.cookies.get("acc_session")}', **ORIGIN}
    query = f'mode=read&run={ref["runId"]}&run_id={ref["runId"]}&instance={ref["installation"]}'
    async with websockets.connect(f'{live}/api/sessions/{sid}/tty?{query}', additional_headers=headers) as ws:
        with pytest.raises(websockets.ConnectionClosed) as error:
            await asyncio.wait_for(ws.recv(), 5)
        assert error.value.rcvd.code == 4404
    assert sid not in api.app.state.ctx.writers.by_session
