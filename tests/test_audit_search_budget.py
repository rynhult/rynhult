"""Budget cleanup and PostgreSQL transaction-local cancellation, without a live database."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import DBAPIError

from acc.server import audit_search


def postgres():
    connection = Mock(dialect=SimpleNamespace(name='postgresql'))
    return Mock(connection=Mock(return_value=connection)), connection


def test_postgres_budget_is_local_and_preserves_stricter_owner_timeout():
    db, connection = postgres()
    with audit_search.budget(db):
        pass
    sql = str(connection.execute.call_args.args[0])
    assert "set_config('statement_timeout'" in sql and "true)" in sql
    assert "BETWEEN 1 AND 249 THEN setting" in sql and "'250'" in sql


@pytest.mark.parametrize('during_setup', [False, True])
def test_postgres_cancellation_becomes_budget_error(during_setup):
    db, connection = postgres()
    original = Exception('statement timeout')
    original.sqlstate = '57014'
    error = DBAPIError('audit query', {}, original)
    if during_setup:
        connection.execute.side_effect = error
    with pytest.raises(audit_search.SearchBudgetExceeded):
        with audit_search.budget(db):
            if not during_setup:
                raise error


def test_other_database_errors_are_not_disguised_as_timeouts():
    db, _ = postgres()
    error = DBAPIError('audit query', {}, Exception('different failure'))
    with pytest.raises(DBAPIError):
        with audit_search.budget(db):
            raise error


def test_sqlite_wall_clock_budget_and_callback_cleanup(monkeypatch):
    raw = Mock()
    connection = Mock(dialect=SimpleNamespace(name='sqlite'),
                      connection=SimpleNamespace(driver_connection=raw))
    db = Mock(connection=Mock(return_value=connection))
    ticks = iter([10.0, 10.3])
    monkeypatch.setattr(audit_search, 'monotonic', lambda: next(ticks))
    with audit_search.budget(db):
        callback, interval = raw.set_progress_handler.call_args.args
        assert interval == 1000
        assert callback() == 1  # elapsed >250 ms, before reaching the VM-step cap
    raw.set_progress_handler.assert_called_with(None, 0)
