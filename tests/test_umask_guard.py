"""The test-only guard restores the worker mask even when it reports a leak."""
import os

import pytest

import conftest


@pytest.mark.parametrize('leak', [False, True])
def test_worker_umask_guard_restores_and_reports_changes(leak):
    before = os.umask(0o022)
    os.umask(before)
    guard = conftest.umask_is_restored.__wrapped__()
    next(guard)
    try:
        if leak:
            os.umask(0o077 if before != 0o077 else 0o022)
            with pytest.raises(pytest.fail.Exception, match='test left the umask'):
                next(guard)
        else:
            with pytest.raises(StopIteration):
                next(guard)
        actual = os.umask(before)
        assert actual == before
    finally:
        os.umask(before)
