"""acc-broker: stateless executor for native CLI sessions, one per agent account, run as that account.

Speaks newline-delimited JSON over a UNIX socket and accepts connections only from
the allowed peer uids (accd's `acc` user). It owns `tmux -L <socket>` and never
decides anything on its own: accd holds desired state and calls these methods.

Request:  {"id": 1, "method": "launch", "params": {...}}
Response: {"id": 1, "ok": true, "result": ...} | {"id": 1, "ok": false, "error": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import secrets
import socket
from pathlib import Path

from .. import peercred, tooling
from ..broker_protocol import request_from
from ..providers.rpc import cancellable
from . import attach, brief_references, document_extract, gate_ops, group_git, image_probe, logins, models, ops
from .config import BrokerConfig

log = logging.getLogger("acc.broker")
MAX_LINE = 16 * 1024 * 1024


SERVICES_METHODS = ("ping", "compose_up", "compose_ps", "compose_digest")  # all a services broker does


class RequestError(Exception):
    pass


def peer_uid(writer: asyncio.StreamWriter) -> int:
    return peercred.peer_uid(writer.get_extra_info("socket"))


class Broker:
    def __init__(self, config: BrokerConfig):
        self.config = config
        self._workspace_key = secrets.token_bytes(32)
        self.logins = logins.Logins(config)
        from ..node import group_proof
        self.methods = {
            "group_machine_verify": lambda p: {"verified": group_proof.verify(self.config.run_dir, p["node_id"], p["proof_account"], p["request_id"], p["code"])},
            "files": lambda p: ops.files(self.config, **p),
            "auth_health": lambda p: logins.auth_health(self.config),
            "brief_document_extract": lambda p: document_extract.run(self.config, **p),
            "brief_reference": lambda p: brief_references.receive(self.config, **p, binding_key=self._workspace_key),
            "image_identity": lambda p: image_probe.identity(self.config, p["provider"]),
            "image_probe": lambda p: image_probe.run(self.config, p["provider"], p["model"]),
            "model_catalog": lambda p: models.model_catalog(self.config),
            "usage": lambda p: logins.usage(self.config, p["provider"]),
            "installed_providers": lambda p: ops.installed_providers(self.config),
            "login_start": lambda p: self.logins.start(p["provider"]),
            "login_submit": lambda p: self.logins.submit(p["flow_id"], p["code"]),
            "login_poll": lambda p: self.logins.poll(p["flow_id"]),
            "login_cancel": lambda p: self.logins.cancel(p["flow_id"]),
            "ping": self.ping,
            "list": lambda p: ops.list_sessions(self.config),
            "launch": lambda p: ops.launch(self.config, p, workspace_key=self._workspace_key),
            "kill": lambda p: ops.kill(self.config, p["tmux_name"], p.get("container"), p.get("acc_id")),
            "make_private_checkout": lambda p: ops.make_private_checkout(self.config, p["repo"], p["root"], p["name"],
                                                                         p["branch"], p.get("base_commit")),
            "clear_env": lambda p: ops.clear_env(self.config, p["acc_id"], p.get("files")),
            "interrupt": lambda p: ops.interrupt(self.config, p["tmux_name"], p.get("provider")),
            "send_text": lambda p: ops.send_text(self.config, p["tmux_name"], p["text"], p.get("provider", "claude")),
            "team_handoffs": lambda p: ops.team_handoffs(self.config, p["path"], p["branch"], p["helper"]),
            "team_inspect": lambda p: ops.team_inspect(self.config, p["path"], p["branch"], p.get("base")),
            "team_bundle": lambda p: ops.team_bundle(self.config, p["path"], p["branch"], p.get("base_commit")),
            "team_import": lambda p: ops.team_import(self.config, p["path"], p["data"], p["commit"]),
            "team_checkout": lambda p: ops.team_checkout(self.config, p["name"], p["branch"], p["data"], p["commit"]),
            "team_reply": lambda p: ops.team_reply(self.config, p["acc_id"], p["request_id"], p["result"]),
            "team_prepare_merge": lambda p: ops.team_prepare_merge(self.config, p["path"], p["branch"], p["acc_id"]),
            "team_clear_bundles": lambda p: ops.team_clear_bundles(self.config, p["acc_id"], p.get("branch")),
            "team_merge": lambda p: ops.team_merge(self.config, p["path"], p["branch"], p["expected_branch"], expected_head=p.get("expected_head"), expected_base=p.get("expected_base"), report_heads=p.get("report_heads")),
            "read_spool": lambda p: ops.read_spool(self.config, p["acc_id"], p["offset"]),
            "make_worktree": lambda p: ops.make_worktree(self.config, p["repo"], p["root"], p["name"], p["branch"], p.get("base_commit")),
            "remove_worktree": lambda p: ops.remove_worktree(self.config, p["repo"], p["path"]),
            "workspace_changes": lambda p: ops.workspace_changes(self.config, p["path"], p.get("base"),
                                                                 bool(p.get("detail"))),
            "automation_screen": lambda p: ops.automation_screen(self.config, p["tmux_name"], p["provider"]),
            "terminal_output": lambda p: ops.terminal_output(self.config, p["tmux_name"]),
            "screen_prompt": lambda p: ops.screen_prompt(self.config, p["tmux_name"], p["provider"]),
            "transcript_messages": lambda p: ops.transcript_messages(p["provider"], p["native_id"], p.get("stamp")),
            "transcript_exists": lambda p: ops.transcript_exists(p["provider"], p["native_id"]),
            "project_first_commit": lambda p: ops.project_first_commit(self.config, p["path"], p.get("title", ""), bool(p.get("create")), p.get("name"), p.get("email")),
            "git_identity": lambda p: ops.git_identity(self.config, p["path"], p.get("name"), p.get("email"), bool(p.get("replace"))),
            "trust_workspace": lambda p: ops.trust_workspace(self.config, p["path"]),
            "claude_agents": lambda p: ops.claude_agents(self.config),
            "claude_remote": lambda p: ops.claude_remote(p["pid"]),
            "compose_up": lambda p: ops.compose_up(self.config, p["path"], p.get("expect")),
            "compose_digest": lambda p: ops.compose_digest(self.config, p["path"]),
            "sync_codex_skills": lambda p: ops.sync_codex_skills(self.config, p["skills"]),
            "codex_plugins": lambda p: ops.codex_plugins(),
            "prepare_private_workspace": lambda p: ops.prepare_private_workspace(self.config, p["path"]),
            "make_workspace": lambda p: ops.make_workspace(self.config, p["parent"], p["name"], p.get("title", ""),
                                                           bool(p.get("project"))),
            "push_workspace": lambda p: ops.push_workspace(self.config, p["path"], p["repo"]),
            "workspace_identity": lambda p: ops.workspace_identity(self.config, p["path"]),
            "project_delete_runtime": lambda p: ops.project_delete_runtime(self.config, p["path"]),
            "project_delete_identity": lambda p: ops.project_delete_identity(self.config, p["path"], p.get("worktrees", [])),
            "delete_project_checkout": lambda p: ops.delete_project_checkout(self.config, p["path"], p.get("worktrees", []), p["expected"]),
            "delete_workspace": lambda p: ops.delete_workspace(self.config, p["parent"], p["name"]),
            "wiki_tree": lambda p: ops.wiki_tree(self.config, p["path"]),
            "wiki_page": lambda p: ops.wiki_page(self.config, p["path"], p["rel"]),
            "wiki_attachment": lambda p: ops.wiki_attachment(self.config, p["path"], p["rel"]),
            "list_artifacts": lambda p: ops.list_artifacts(self.config, p["path"], p.get("helper_id")),
            "read_artifact": lambda p: ops.read_artifact(self.config, p["path"], p["name"], p.get("offset", 0),
                                                        p.get("helper_id")),
            "explanation_result": lambda p: ops.explanation_result(self.config, p["acc_id"]),
            "clone_repo": lambda p: ops.clone_repo(self.config, p["repo"], p["dest"]),
            "clone_url": lambda p: ops.clone_url(self.config, p["url"], p["dest"]),
            "github_api": lambda p: ops.github_api(self.config, p["verb"], p["path"], p.get("body"), int(p.get("tail") or 0)),
            "scan_docs": lambda p: ops.scan_docs(self.config, p["path"]),
            "read_doc": lambda p: ops.read_doc(self.config, p["path"], p["rel"]),
            "compose_ps": lambda p: ops.compose_ps(self.config, p["path"]),
            "git_refs": lambda p: ops.git_refs(self.config, p["path"]),
            "group_observe": lambda p: group_git.observe(self.config, p["path"], p["session_id"], p["base"]),
            "group_context": lambda p: group_git.refresh_context(self.config, p["session_id"], p["content"]),
            "group_provision": lambda p: group_git.provision(self.config, p["path"]),
            "group_export": lambda p: group_git.send(self.config, p["path"], p["session_id"], p["commit"]),
            "group_receive": lambda p: group_git.receive(self.config, p["path"], p["data"], p["commit"], p["session_id"]),
            "git_backup": lambda p: ops.git_backup(self.config, p["path"], p["url"], p["token"], p.get("ca_pem")),
            "tool_change": self.tool_change,
            **gate_ops.methods(self.config),  # orchestrator v2 slice 5 (gate queue, restack)
        }

        if config.role == "services":
            self.methods = {name: self.methods[name] for name in SERVICES_METHODS}
        elif config.role != "agents":
            raise ValueError(f"unknown broker role {config.role!r}")

    def ping(self, _params: dict) -> dict:
        """Who and where this broker is: accd compiles a session on another machine with that machine's own run folder,
        hook and statusline paths and home (Q4), never its own."""
        if _params.get("wsl_isolation"):
            from ..wsl import windows_drive_readable
            return {"role": self.config.role,
                    "windows_drive_readable": windows_drive_readable() if self.config.role == "agents" else None}
        if _params.get('machine_snapshot'):
            from ..node.identity import node_info
            from ..server.host import live
            return {'machine': node_info(), 'snapshot': live()}
        return {"user": pwd.getpwuid(os.getuid()).pw_name, "tmux_socket": self.config.tmux_socket,
                "run_dir": str(self.config.run_dir), "role": self.config.role, "hook_path": self.config.hook_path,
                "statusline_path": self.config.statusline_path, "home": str(Path.home()),
                # the first is where this account's new projects go (New project on another machine)
                "roots": [str(root) for root in self.config.allowed_roots]}

    def tool_change(self, params: dict) -> dict:
        if os.geteuid() == 0 or self.config.role != "agents":
            raise RequestError("user tools must run as an unprivileged agent account, never root")
        if set(params) != {"manager", "name", "version", "operation"}:
            raise RequestError("bad tool fields")
        return tooling.execute(**params, home=Path.home(), system=False)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            uid = peer_uid(writer)
        except OSError as error:  # PeerCredError included: an unknown peer is never allowed
            log.warning("rejected connection: cannot read the peer's uid (%s)", error)
            writer.close()
            return
        if uid not in self.config.allowed_uids:
            log.warning("rejected connection from uid %s", uid)
            writer.close()
            return
        try:
            while line := await reader.readline():
                try:
                    request = request_from(line)
                except ValueError as error:
                    response = {"id": None, "ok": False, "error": str(error)}
                else:
                    response = None
                if response is None and request["method"] == "attach" and self.config.role == "agents":
                    # the connection becomes a terminal stream for its remaining lifetime
                    await attach.attach(self.config, request["params"], reader, writer)
                    return
                response = response or await self._dispatch(request)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def dispatch(self, line: bytes) -> dict:
        try:
            request = request_from(line)
        except ValueError as error:
            return {"id": None, "ok": False, "error": str(error)}
        return await self._dispatch(request)

    async def _dispatch(self, request: dict) -> dict:
        request_id = request["id"]
        try:
            method = self.methods.get(request.get("method"))
            if method is None:
                raise RequestError(f"unknown method {request.get('method')!r}")
            if request["method"] in ("model_catalog", "usage"):
                result = await cancellable(method, request["params"])
            else:
                result = await asyncio.to_thread(method, request["params"])
            return {"id": request_id, "ok": True, "result": result}
        except (RequestError, ops.OpError, logins.LoginError, KeyError, ValueError) as error:
            return {"id": request_id, "ok": False, "error": f"{type(error).__name__}: {error}"}
        except Exception as error:  # keep serving other requests
            log.exception("method failed")
            return {"id": request_id, "ok": False, "error": f"internal: {type(error).__name__}"}


MAX_SOCKET_PATH = 107  # bytes: sockaddr_un.sun_path is 108, with room for the terminating NUL


def check_socket_path(path: Path) -> None:
    """Refuse a path the kernel can't bind, naming it, rather than leave callers waiting for a socket that can't exist."""
    size = len(os.fsencode(path))
    if size > MAX_SOCKET_PATH:
        raise ValueError(f"socket path {path} is {size} bytes; Unix socket paths can be at most {MAX_SOCKET_PATH} "
                         "bytes: use a shorter folder")


async def serve(config: BrokerConfig) -> None:
    broker = Broker(config)
    # Launch requests carry the session's compiled files (skills can be ~100s of KB): raise the line limit.
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        # acc-broker@.socket: systemd made the socket (root-owned folder, group acc), so this account's own
        # processes can neither replace it nor connect to it.
        listener = socket.socket(fileno=3)
        server = await asyncio.start_unix_server(broker.handle, sock=listener, limit=MAX_LINE)
        where = "the systemd socket"
    else:
        path = Path(config.socket_path)
        check_socket_path(path)
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(broker.handle, path=str(path), limit=MAX_LINE)
        os.chmod(path, 0o666)  # the directory (0750, group acc) and the peer uid check do the gating
        where = str(path)
    if config.role == "agents":
        ops.prepare_tmux(config)
    log.info("%s broker listening on %s (allowed uids %s)", config.role, where, sorted(config.allowed_uids))
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve(BrokerConfig.from_env()))


if __name__ == "__main__":
    main()
