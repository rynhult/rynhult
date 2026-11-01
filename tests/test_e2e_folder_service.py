"""The disposable browser harness must never dial a machine's installed root helper."""
import pytest


async def test_disposable_harness_refuses_real_folder_worker_calls(monkeypatch):
    from e2e_folder_service import install

    from acc import folder_channel
    monkeypatch.setattr(folder_channel, 'request', folder_channel.request)
    install()
    with pytest.raises(OSError, match='disposable'):
        await folder_channel.request({'action': 'status'})
