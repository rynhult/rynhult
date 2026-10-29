"""Update commands arrive only on the authenticated primary link; acc-node only queues them."""

from __future__ import annotations

import platform
import time
import uuid
from pathlib import Path

from acc.env import environment

from ..update_helper import read_json, write_json
from ..updates import BUILD, version
from .identity import node_info
from .packages import RateLimit


class Updates:
    def __init__(self, root=None):
        self.root = root or Path(environment.get("ACC_UPDATE_ROOT", "/srv/acc/updates"))
        self.refused = None
        self.rate = RateLimit(limit=4)

    def handle(self, message):
        from ..backup_targets import BINDING
        if BINDING.exists() or BINDING.is_symlink():
            self.refused = {'detail': 'Backup target updates require independent owner review', 'at': time.time()}
            return
        try:
            if platform.system() == "Darwin":
                raise ValueError("Update this Mac with the new installer")
            if (
                not isinstance(message, dict)
                or set(message) != {"t", "version", "build", "sha256"}
                or message["t"] != "update"
            ):
                raise ValueError("Invalid update frame")
            version(message["version"])
            if not all(isinstance(message[k], str) and BUILD.fullmatch(message[k]) for k in ("build", "sha256")):
                raise ValueError("Invalid build or checksum")
            if not self.rate.allow():
                raise ValueError("Too many update requests; wait a minute")
            requests = self.root / "requests"
            if not requests.is_dir():
                raise ValueError("Install the acc-update helper with deploy/node/install.sh")
            if any(requests.glob("*.json")) or self.message()["result"].get("running"):
                raise ValueError("An update is already waiting or running")
            write_json(
                requests / f"{uuid.uuid4().hex}.json",
                {"action": "update", **{k: message[k] for k in ("version", "build", "sha256")}},
            )
            self.refused = None
        except (ValueError, OSError):
            self.refused = {
                "detail": "Update refused; check the helper installation and pending requests",
                "at": time.time(),
            }

    def message(self):
        try:
            result = read_json(self.root / "state.json")
        except (OSError, ValueError):
            result = {}
        return {
            "t": "update-state",
            "info": node_info(),
            "result": result,
            "refused": self.refused,
            "helper": (self.root / "requests").is_dir(),
            "mac": platform.system() == "Darwin",
        }
