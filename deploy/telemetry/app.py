"""ASGI factory: uvicorn --factory deploy.telemetry.app:create_app --no-access-log."""

from pathlib import Path

from acc.env import environment
from acc.telemetry_collector import MAX_BYTES, create_collector


def create_app():
    configured = environment.get("ACC_COLLECTOR_DB")
    if configured is None:
        legacy = Path("./telemetry.sqlite")
        if legacy.exists() or legacy.is_symlink():
            raise ValueError("Legacy collector database found; secure its storage and set ACC_COLLECTOR_DB explicitly")
        configured = "./telemetry-data/telemetry.sqlite"
    return create_collector(Path(configured), int(environment.get("ACC_COLLECTOR_MAX_BYTES", MAX_BYTES)))
