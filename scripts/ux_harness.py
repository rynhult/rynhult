"""Read-only UI review harness: serve the current web build against a COPY of the live database.

    uv run python scripts/ux_harness.py <state-dir> [port, default 18751]

Copies acc.db (SQLite online backup) into <state-dir>, creates a login session for the first user
and writes {origin, cookie, csrf} to <state-dir>/harness.json for web/scripts/ux-shots.mjs. The copy
has no broker (bogus socket), no secrets (no GitHub/Cloudflare calls) and its own publish/run dirs,
so nothing it does reaches sessions, DNS or Caddy. Ctrl-C to stop; delete <state-dir> afterwards.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import uvicorn

from acc.server import auth
from acc.server.app import create_app
from acc.server.config import Settings
from acc.server.db import make_engine, make_sessionmaker
from acc.server.models import User

REPO = Path(__file__).resolve().parents[1]


def main() -> None:
    state = Path(sys.argv[1]).resolve()
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 18751
    for sub in ("data", "run", "publish/requests", "publish/results"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect("file:/srv/acc/data/acc.db?mode=ro", uri=True)
    copy = sqlite3.connect(state / "data" / "acc.db")
    with copy:
        source.backup(copy)
    copy.close()
    source.close()
    origin = f"http://127.0.0.1:{port}"
    settings = Settings(data_dir=state / "data", run_dir=state / "run", broker_socket=state / "no-broker.sock",
                        publish_dir=state / "publish", allowed_origins=(origin,), cookie_secure=False,
                        static_dir=REPO / "src/acc/server/static", github_interval=1e12)
    engine = make_engine(settings.db_url)
    db = make_sessionmaker(engine)
    with db() as s:
        user = s.query(User).first()
        token, csrf = auth.create_session(s, user, "ux-harness")
        s.commit()
    (state / "harness.json").write_text(json.dumps({"origin": origin, "cookie": token, "csrf": csrf}))
    print(f"serving {origin} (copy of live data, no broker)", flush=True)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
