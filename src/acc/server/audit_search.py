"""Connection-scoped budgets for owner audit reads; never leave a pooled callback behind."""
from contextlib import contextmanager
from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

MAX_SECONDS = 0.25
MAX_SQLITE_STEPS = 500000
PROGRESS_STEPS = 1000


class SearchBudgetExceeded(Exception):
    pass


@contextmanager
def budget(db):
    connection = db.connection()
    sqlite = connection.dialect.name == 'sqlite'
    raw = connection.connection.driver_connection if sqlite else None
    exhausted = False
    steps = 0
    deadline = monotonic() + MAX_SECONDS

    def progress():
        nonlocal exhausted, steps
        steps += PROGRESS_STEPS
        exhausted = steps >= MAX_SQLITE_STEPS or monotonic() >= deadline
        return int(exhausted)

    try:
        if sqlite:
            raw.set_progress_handler(progress, PROGRESS_STEPS)
        elif connection.dialect.name == 'postgresql':
            # pg_settings expresses this setting in milliseconds. Preserve any
            # stricter owner timeout. LOCAL is rolled back when this read session closes.
            connection.execute(text("SELECT set_config('statement_timeout', "
                                    "(CASE WHEN setting::bigint BETWEEN 1 AND 249 THEN setting ELSE '250' END) || 'ms', true) "
                                    "FROM pg_settings WHERE name = 'statement_timeout'"))
        yield
    except DBAPIError as error:
        code = getattr(error.orig, 'sqlstate', None) or getattr(error.orig, 'pgcode', None)
        if exhausted or code == '57014':
            raise SearchBudgetExceeded from error
        raise
    finally:
        if sqlite:
            raw.set_progress_handler(None, 0)
