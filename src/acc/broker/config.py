from __future__ import annotations

import pwd
from dataclasses import dataclass, field
from pathlib import Path

from acc.env import environment

from .. import providers as _providers


def _uid_of(user: str) -> int | None:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


@dataclass(frozen=True)
class BrokerConfig:
    socket_path: Path = Path("/srv/acc/sock/broker.sock")
    run_dir: Path = Path("/srv/acc/run")
    tmux_socket: str = "acc"
    allowed_roots: tuple[Path, ...] = (Path("/srv/acc/workspaces"),)
    allowed_uids: frozenset[int] = field(default_factory=frozenset)
    # provider name -> executable; tests point these at a fake CLI
    providers: dict[str, str] = field(default_factory=lambda: _providers.executables())
    hook_path: str = "/srv/acc/bin/acc-hook"
    catalog_dir: Path = Path("/srv/acc/app/catalog")
    catalog_local_dir: Path = Path("/srv/acc/catalog-local")
    git_remote_base: str = "https://github.com"  # where promoted workspaces are pushed (tests use a local path)
    # "agents": runs one account's CLI sessions. "services": only brings project Docker stacks up (the account that
    # owns them), never runs an agent.
    role: str = "agents"
    # Per-session containers: the acc-containers root helper's socket, and the Podman this account runs (cleanup checks).
    containers_socket: Path = Path("/run/acc-containers.sock")
    podman: str = "/usr/bin/podman"
    # Where the launcher's unit relays a container session's terminal (one 0700 folder per unit, never in the container)
    containers_attach_root: Path = Path("/run/acc-ct-attach")
    container_policy: Path = Path("/etc/acc/containers/policy.json")

    # Root publishes a target-local snapshot over a root-authenticated IPC frame.
    # Provisioned installs refuse filesystem work after restart until that reload.
    folder_policy_required: bool = False
    folder_revision: int = 0
    allowed_folders: bool = True
    folder_homes: tuple[str, ...] = ()
    folder_protected: tuple[str, ...] = ()

    @property
    def statusline_path(self) -> str:
        """Installed next to acc-hook everywhere (/srv/acc/bin, a node's prefix, the Mac's ACC folder)."""
        return str(Path(self.hook_path).with_name("acc-statusline"))

    @classmethod
    def from_env(cls) -> "BrokerConfig":
        env = environment
        uids = {int(u) for u in env.get("ACC_ALLOWED_UIDS", "").split(",") if u}
        if not uids and (acc := _uid_of("acc")) is not None:
            uids = {acc}
        providers = _providers.executables(dict(item.split("=", 1) for item in env.get("ACC_PROVIDERS", "").split(",") if "=" in item))
        roots = tuple(Path(p) for p in env.get("ACC_ALLOWED_ROOTS", "/srv/acc/workspaces").split(":") if p)
        return cls(
            folder_policy_required=env.get("ACC_FOLDER_POLICY_REQUIRED") == "1",
            socket_path=Path(env.get("ACC_BROKER_SOCKET", "/srv/acc/sock/broker.sock")),
            run_dir=Path(env.get("ACC_RUN_DIR", "/srv/acc/run")),
            tmux_socket=env.get("ACC_TMUX_SOCKET", "acc"),
            allowed_roots=roots,
            allowed_uids=frozenset(uids),
            providers=providers,
            hook_path=env.get("ACC_HOOK_PATH", "/srv/acc/bin/acc-hook"),
            catalog_dir=Path(env.get("ACC_CATALOG_DIR", "/srv/acc/app/catalog")),
            catalog_local_dir=Path(env.get("ACC_CATALOG_LOCAL_DIR", "/srv/acc/catalog-local")),
            role=env.get("ACC_BROKER_ROLE", "agents"),
            containers_socket=Path(env.get("ACC_CONTAINERS_SOCKET", "/run/acc-containers.sock")),
            containers_attach_root=Path(env.get("ACC_CONTAINERS_ATTACH_ROOT", "/run/acc-ct-attach")),
        )
