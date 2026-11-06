"""The private fixture seeder must not upgrade a deferred SQLite snapshot."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import event


def test_seed_reserves_the_writer_before_its_first_read(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "web/e2e/perf/seed.py"
    spec = importlib.util.spec_from_file_location("perf_seed", source)
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    base = tmp_path / "fixture"
    state = base / "state"
    (state / "data").mkdir(parents=True)
    (base / "perf-owned.json").write_text("{}")
    (base / "info.json").write_text(json.dumps({"state": str(state)}))
    path = state / "data/acc.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE guard (value INTEGER)")
        db.execute("INSERT INTO guard VALUES (0)")
    engine = seed.make_engine(f"sqlite:///{path}")
    monkeypatch.setattr(seed, "make_engine", lambda url: engine)
    monkeypatch.setattr(sys, "argv", [seed.__file__, str(base), "500", "200", "fake-live-id"])
    intervened = []

    class ReadObserved(Exception):
        pass

    def before_read(conn, cursor, statement, parameters, context, many):
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        # A second process could otherwise commit between the seed's read and
        # INSERT. Raw SQLite deliberately does not share the in-process gate.
        with sqlite3.connect(path, timeout=0) as other:
            try:
                other.execute("UPDATE guard SET value = value + 1")
                other.commit()
                intervened.append(True)
            except sqlite3.OperationalError as error:
                assert "locked" in str(error)
                intervened.append(False)
        raise ReadObserved

    event.listen(engine, "before_cursor_execute", before_read)
    try:
        with pytest.raises(ReadObserved):
            seed.main()
        assert intervened == [False]
    finally:
        engine.dispose()
