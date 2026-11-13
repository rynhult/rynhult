"""Concrete SDK/tmux ownership under one private native-conversation lease.

The tmux path retains its native decoder. Without a qualified processed-input
boundary it refuses handover after admitted terminal input; no invented ACK.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pwd
import select
import signal
import stat
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from time import monotonic as auth_clock
from types import SimpleNamespace

from acc.env import Environment, interpreter_variable
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import CloseMode, InputGrant, RuntimeAttestation, TerminalRun
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
from acc.session_relay.adapters.claude_switch import LaunchedClaudeOwner
from acc.session_relay.adapters.claude_terminal import ClaudeTerminalOwner
from acc.session_relay.approval_facts import native_facts
from acc.session_relay.claude_input_host import ClaudeInputHost
from acc.session_relay.terminal_host import ObserverTerminalHost

from . import ops
from .claude_hooks import ClaudeHookServer
from .relay_observer import OwnedInputAttachment, OwnedObserver
from .session_relay import PrivateRuntime

CLI_DIGEST = 'sha256:356733f185fa988e6bf3aa9a555269fdd167982a2e2a80d3c8a78037bb92ad58'


def installation_probe(config):
    """Read-only local prerequisites, never a paid probe or runtime attestation."""
    import shutil
    from importlib.metadata import PackageNotFoundError, version
    try:
        if (os.name != 'posix' or not hasattr(os, 'pidfd_open') or not Path('/proc/self/stat').is_file()
                or not Path('/usr/bin/bwrap').is_file() or not shutil.which('tmux') or not shutil.which('socat')
                or version('claude-agent-sdk') != '0.1.50'):
            return {'available': False}
        executable = shutil.which(config.providers.get('claude', ''))
        return {'available': bool(executable) and p.sha256_digest(Path(executable).resolve(strict=True).read_bytes()) == CLI_DIGEST}
    except (OSError, PackageNotFoundError):
        return {'available': False}


def credential_environment(spec, env):
    from acc.providers.claude_auth import AUTH_ENV
    return {key: value for key, value in env.items()
            if spec.spec.get('auth_mode') != 'subscription' or key not in AUTH_ENV}


class TmuxProcess:
    def __init__(self, config, name, enclosure=None):
        self.config, self.name = config, name
        self.enclosure = enclosure
        value = ops.tmux(config, 'display-message', '-p', '-t', '=' + name + ':', '#{session_id} #{pane_id} #{pane_pid} #{pid}', check=True)
        fields = value.stdout.strip().split()
        if (len(fields) != 4 or not fields[0].startswith('$') or not fields[0][1:].isdigit()
                or not fields[1].startswith('%') or not fields[1][1:].isdigit()):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        self.session_id, self.pane_id, pane_pid, server_pid = fields
        self.pid, self.server_pid = int(pane_pid), int(server_pid)
        from .relay_enclosure import incarnation
        self.server_started = incarnation(self.server_pid)
        self.view_retired = False
        self.started = Path(f'/proc/{self.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        self.fd = os.pidfd_open(self.pid)
        self.children = []
        self.snapshot = ''
        # A /proc descendant snapshot cannot exclude a detached or racing child.
        # Only the per-run enclosure receipt can establish whole-tree exit.
        self.tree_unknown = True
        self.returncode = None

    def current(self):
        if self.fd is None or self.returncode is not None or select.select([self.fd], [], [], 0)[0]:
            return False
        from .relay_enclosure import incarnation
        value = ops.tmux(self.config, 'display-message', '-p', '-t', '=' + self.name + ':', '#{pane_pid}')
        return value.returncode == 0 and value.stdout.strip() == str(self.pid) and incarnation(self.pid) == self.started

    def terminate(self):
        if select.select([self.fd], [], [], 0)[0]:
            self.returncode, self.tree_unknown = 0, True
            return
        value = ops.tmux(self.config, 'display-message', '-p', '-t', '=' + self.name + ':', '#{pane_pid}')
        if value.returncode or value.stdout.strip() != str(self.pid):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        current = Path(f'/proc/{self.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        if current != self.started:
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        value = ops.tmux(self.config, 'capture-pane', '-p', '-J', '-S', '-200', '-t', '=' + self.name + ':')
        if value.returncode == 0:
            self.snapshot = value.stdout[-(128 << 10):]
        import psutil
        # Pin the observed owned tree before kill; no name-based PID signalling.
        for child in psutil.Process(self.pid).children(recursive=True):
            try:
                self.children.append(os.pidfd_open(child.pid))
            except ProcessLookupError:
                pass
        if self.enclosure is not None:
            signal.pidfd_send_signal(self.fd, signal.SIGTERM)
        else:
            ops.kill(self.config, self.name)

    async def wait(self):
        while not select.select([self.fd], [], [], 0)[0]:
            await asyncio.sleep(.02)
        self.returncode = 0  # exit observed, not a claimed successful vendor outcome
        return 0

    async def tree_exited(self, _):
        if self.enclosure is not None:
            return self.returncode is not None and self.enclosure.proof('exit.json', f'{self.pid}:{self.started}')
        deadline = asyncio.get_running_loop().time() + 1
        while self.returncode is not None and not self.tree_unknown:
            if all(select.select([fd], [], [], 0)[0] for fd in self.children):
                return True
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(.02)
        return False

    async def retire_view(self):
        # Switching and EOF cleanup can converge on the same old view. Consume
        # its one atomic deletion once; a second caller must not interpret the
        # first caller's successful deletion as an unknown/replaced view.
        lock = getattr(self, '_retirement_lock', None)
        if lock is None:
            lock = self._retirement_lock = asyncio.Lock()
        async with lock:
            await self._retire_view()

    async def _retire_view(self):
        """Remove only this proven exited, still-identical dead pane atomically.

        The snapshot belongs to the previous read-only view. Neither name reuse
        nor root PID disappearance authorizes killing a tmux session.
        """
        if self.view_retired:
            return
        if self.enclosure is None or not await self.tree_exited(self):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        def retire():
            from .relay_enclosure import incarnation
            if incarnation(self.server_pid) != self.server_started:
                raise p.RelayError(p.ErrorCode.STALE_RUN)
            # tmux evaluates this guard and the kill on its own command queue.
            # Even a changed/reused target can never kill a live pane.
            checks = [('session_id', self.session_id), ('pane_id', self.pane_id),
                ('pane_pid', str(self.pid)), ('pane_dead', '1'), ('pid', str(self.server_pid)),
                ('session_name', self.name)]
            condition = '#{==:#{' + checks[0][0] + '},' + checks[0][1] + '}'
            for key, expected in checks[1:]:
                condition = '#{&&:' + condition + ',#{==:#{' + key + '},' + expected + '}}'
            import time
            deadline = time.monotonic() + 1
            while True:
                result = ops.tmux(self.config, 'if-shell', '-F', '-t', self.session_id, condition,
                    "kill-pane -t '" + self.pane_id + "'", 'display-message -p refused-ended-view')
                if not result.returncode and not result.stdout.strip():
                    return
                # The kernel's exit receipt may precede tmux reaping its pane.
                # Wait only for that same pinned pane to become dead. A reused
                # pane/server or an unexplained transport failure stays refused.
                if (result.returncode or result.stdout.strip() != 'refused-ended-view'
                        or incarnation(self.server_pid) != self.server_started):
                    raise p.RelayError(p.ErrorCode.STALE_RUN)
                observed = ops.tmux(self.config, 'display-message', '-p', '-t', self.session_id,
                    ' '.join('#{' + key + '}' for key, _ in checks))
                pending = [expected if key != 'pane_dead' else '0' for key, expected in checks]
                if (observed.returncode or observed.stdout.strip().split() != pending
                        or time.monotonic() >= deadline):
                    raise p.RelayError(p.ErrorCode.STALE_RUN)
                time.sleep(.01)
        await asyncio.to_thread(retire)
        self.view_retired = True

    def close(self):
        if self.fd is not None and self.returncode is not None and select.select([self.fd], [], [], 0)[0]:
            # remain-on-exit preserves the dead pane. Retire its exact tmux ID,
            # never a possibly replaced session name, before the next owner.
            value = ops.tmux(self.config, 'display-message', '-p', '-t', '=' + self.name + ':',
                             '#{pane_id}:#{pane_pid}:#{pane_dead}')
            fields = value.stdout.strip().split(':')
            if (len(fields) == 3 and value.returncode == 0 and fields[0].startswith('%')
                    and fields[0][1:].isdigit() and fields[1:] == [str(self.pid), '1']):
                ops.tmux(self.config, 'kill-pane', '-t', fields[0], check=True)
        for fd in (self.fd, *self.children):
            if fd is not None:
                os.close(fd)
        self.fd = None
        self.children.clear()


class ClaudeOwnerFactory:
    # Only the per-run supervisor started receipt can qualify this instance;
    # a root PID/descendant snapshot alone remains insufficient.
    exit_qualified = False
    launch_attempt = None

    def __init__(self, transport, spec, value, runtime, *, recovered=False):
        if spec.spec.get('auth_mode') not in ('api_key', 'subscription'):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        from .claude_paths import check
        check(transport.config,spec)
        self.transport, self.config, self.base, self.runtime = transport, transport.config, spec, runtime
        self.incarnation = uuid.uuid4().hex
        self.directory = Path(spec.spec['run_dir']) / 'relay'
        self.files, self.env = value['files'], credential_environment(spec, value['env'])
        from acc.session_relay.claude_metadata_policy import validate
        try:
            for name in ('settings.json','relay-sdk-settings.json'):
                validate(json.loads(self.files[name]), spec.spec['run_dir'])
        except (KeyError,ValueError,TypeError):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE) from None
        if json.loads(self.files.get('mcp.json', '{}')) != {'mcpServers':{}}:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        args=spec.spec['terminal_resume_args']
        if (args.count('--setting-sources') != 1 or
                tuple(args[args.index('--setting-sources')+1:args.index('--setting-sources')+2]) != ('',) or
                args.count('--strict-mcp-config') != 1 or args.count('--mcp-config') != 1):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        from .claude_guard import InstalledGuard
        self.installed_guard = InstalledGuard(self.config.hook_path)
        self.lease = None
        self.hooks = None
        self.current = None
        self.observer = None
        self.input_attachment = None
        self.processes = []
        self.enclosures = {}
        self.enclosure = None
        self.snapshot = ''
        self.native_history_observed = False
        self.closed = self.close_complete = False
        self.retained_clients = {}
        self.guards = {name: p.sha256_digest(content.encode()) for name, content in self.files.items()
                       if name in (*ops.GUARD_FILES,'mcp.json')}
        if (spec.binding.account != pwd.getpwuid(os.getuid()).pw_name or spec.binding.provider != 'claude'
                or 'guardrails.json' not in self.guards or
                p.sha256_digest(p.canonical_json(self.guards)) != spec.binding.launch_policy_digest):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        executable = Path(self.config.providers['claude']).resolve(strict=True)
        if 'sha256:' + hashlib.sha256(executable.read_bytes()).hexdigest() != CLI_DIGEST:
            raise p.RelayError(p.ErrorCode.VERSION_MISMATCH)
        if str(executable) != spec.spec['cli_path']:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        self.executable, info = executable, executable.stat()
        self.executable_identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        if any(not ops.ENV_KEY.fullmatch(key) or key in ops.RESERVED_ENV or interpreter_variable(key)
               or not isinstance(value, str) for key, value in self.env.items()):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        # Keep the conversation-wide mutex across factory incarnations. Only
        # the one-use start marker changes after authenticated whole-tree cleanup.
        self.lease = PrivateRuntime(self.directory / 'exclusive', spec)
        try:
            marker_name='recovered-'+spec.run_id if recovered else 'started'
            marker = os.open(marker_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.lease.fd)
            try:
                os.write(marker, spec.run_id.encode())
                os.fsync(marker)
            finally:
                os.close(marker)
            os.fsync(self.lease.fd)
            ops._materialize_run(Path(spec.spec['run_dir']), self.files, self.env, guard_files=tuple(self.guards))
        except BaseException:
            os.close(self.lease.lock_fd)
            os.close(self.lease.fd)
            self.lease.closed = True
            raise

    def guard_check(self):
        self.lease.validate()
        info = self.executable.stat()
        if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != self.executable_identity:
            raise p.RelayError(p.ErrorCode.VERSION_MISMATCH)
        ops._under_roots(self.transport.config, self.base.binding.workspace)
        if self.transport.config.folder_policy_required and not self.transport.config.folder_revision:
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        for name, digest in self.guards.items():
            path = Path(self.base.spec['run_dir']) / name
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o222 or info.st_size > p.MAX_FRAME_BYTES):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                if p.sha256_digest(os.read(fd, p.MAX_FRAME_BYTES + 1)) != digest:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            finally:
                os.close(fd)

    def environment(self, spec):
        env = credential_environment(spec, self.env)
        guards = ' '.join(name + '=' + value.removeprefix('sha256:') for name, value in self.guards.items())
        home = pwd.getpwuid(os.getuid()).pw_dir
        return {**env, **dict(Environment({'ACC_RUN_DIR': spec.spec['run_dir'],
            'ACC_SESSION_ID': spec.spec['session_id'], 'ACC_GUARD_FILES': guards})),
            # SDK environment masking must not turn an inherited config root
            # into an empty string: Claude interprets that as the workspace.
            'HOME': home, 'CLAUDE_CONFIG_DIR': str(Path(home) / '.claude'),
            'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}

    async def authorize(self, spec=None, operation=None, *, lifecycle=False):
        self.guard_check()
        async def current():
            if operation is None and getattr(self.runtime,'recovery_plan',None):
                return await self.runtime.peer.call('authority.recovery_verify',{'plan':self.runtime.recovery_plan}) is True
            if lifecycle and self.runtime.controller and self.runtime.controller.plan:
                return await self.runtime.peer.call('authority.switch_verify', {'plan': self.runtime.controller.plan}) is True
            if operation is not None and operation.method == 'decision.reply':
                return (self.runtime.answer_grant is not None and
                    await self.runtime.peer.call('authority.answer_verify', {'grant': self.runtime.answer_grant}) is True)
            if operation is not None:
                return await self.runtime.verify(operation)
            return await self.runtime.owner_authorized()

        result = await current()
        if result:
            from .claude_auth import probe
            key = (spec or self.runtime.spec).run_id
            if not hasattr(self,'_auth_lock'):
                self._auth_lock=asyncio.Lock()
            async with self._auth_lock:
                cached=getattr(self,'_auth_cache',None)
                if cached is None or cached[0]!=key or auth_clock()>=cached[1]:
                    reported = await asyncio.to_thread(probe, str(self.executable), self.base.binding.workspace,
                        self.environment(spec or self.runtime.spec), list(self.base.spec['terminal_resume_args']), inherit=False)
                    self._auth_cache=(key,auth_clock()+30,reported)
                reported=self._auth_cache[2]
            result = reported == self.base.spec['auth_mode'] and await current()
        self.guard_check()
        return result

    async def prepare(self, spec):
        if self.closed or self.launch_attempt is not None:
            raise p.RelayError(p.ErrorCode.OFFLINE)
        if self.current is not None and (not self.exit_qualified or not await self.exited()):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        from .claude_native_prompt import close
        await close(self)
        self.runtime.native_prompt_active = False
        if self.input_attachment:
            await self.input_attachment.close()
            self.input_attachment = None
        if self.observer:
            await self.observer.close()
            self.observer = None
        if self.hooks:
            await self.hooks.close()
        if self.current and hasattr(self.current, 'process') and hasattr(self.current.process, 'snapshot'):
            self.snapshot = self.current.process.snapshot
        process = getattr(self.current, 'process', None)
        if isinstance(process, TmuxProcess):
            await process.retire_view()
        if self.enclosure is not None:
            self.enclosures.pop(self.enclosure.run_id, None)
            self.enclosure.close()
        process = getattr(self.current, 'process', None)
        if process in self.processes:
            process.close()
            self.processes.remove(process)
        retained=getattr(getattr(self.current,'host',None),'retained_client',None)
        if retained is not None:
            self.retained_clients[retained.run_id]=retained
        # The previous owner and its enclosure have now been positively
        # reclaimed. Never verify that old process against the target enclosure.
        self.current = None
        self.exit_qualified = False
        self.enclosure = None
        from .relay_enclosure import Enclosure, incarnation
        self.enclosure = Enclosure(self.directory, spec, self.executable,
            lifetime={'pid': os.getpid(), 'started': incarnation(os.getpid())})
        self.enclosures[spec.run_id] = self.enclosure
        self.runtime.spec = spec
        self.runtime.native_transcript = None
        self.hooks = ClaudeHookServer(self.directory, spec, self.authorize,
            self.runtime.publish, self.runtime.observe, self.permission_closed, self.prepare_application, self.pretool_guard,
            accept_pid=self._accepts_pid,
            sdk_background=lambda: self.sdk_background(spec),
            sdk_permission_owner=lambda: self.sdk_permission_owner(spec),
            allow_policy=not json.loads(self.files['settings.json']).get('permissions', {}).get('ask'))
        await self.hooks.start()
        if not await self.authorize():
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)

    async def pretool_guard(self, payload):
        if not await self.authorize():
            return False
        self.runtime.guard_check()
        allowed = await self.installed_guard.check(payload, self.environment(self.runtime.spec), self.runtime.spec.binding.workspace)
        self.runtime.guard_check()
        return allowed and await self.authorize()

    def prepare_application(self, payload, identifier):
        from acc.session_relay.adapters.claude_terminal import ClaudeTerminalOwner
        owner = self.current
        if not isinstance(owner, ClaudeTerminalOwner):
            return None  # SDK permission application remains separately unqualified.
        host = owner.host
        operation = host.boundary.operation if host.boundary else None
        reader = host.admission.reader
        if operation is None or reader is None:
            return None
        tool, inputs = payload.get('tool_name'), payload.get('tool_input')
        def observe(effect):
            if self.current is not owner or self.runtime.spec.run_id != operation.run_id:
                raise p.RelayError(p.ErrorCode.STALE_RUN)
            self.runtime.guard_check()
            if effect == 'deny':
                reference = reader.denied_application(operation, tool, inputs, identifier)
            else:
                tool_id = payload.get('_relay_native_tool_id')
                entry = self.hooks.tool_requests.get(tool_id)
                if not entry or entry['decision'] != identifier or not entry['completed']:
                    raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
                reference = reader.allowed_application(operation, tool, inputs, identifier, tool_id)
            self.runtime.guard_check()
            return reference
        return {'deny': lambda: observe('deny'),
                'managed_bash_allow': tool == 'Bash' and p.valid_opaque(payload.get('_relay_native_tool_id')),
                'allow_once': (lambda: observe('allow_once')) if payload.get('_relay_native_tool_id') else None}

    def _accepts_pid(self,pid):
        from .relay_enclosure import incarnation
        from .relay_process_tree import descends
        current=self.current
        process=getattr(current,'process',None)
        if process is None:
            process=getattr(getattr(getattr(current,'client',None),'_transport',None),'_process',None)
        if process is None:
            process=getattr(getattr(self,'launch_attempt',None),'process',None)
        if process is None or self.enclosure is None:return False
        try:
            root=process.pid
            start=getattr(process,'started',None) or incarnation(root)
            identity=f'{root}:{start}'
            return self.enclosure.proof('started.json',identity) and descends(pid,root,start)
        except (OSError,ValueError,AttributeError):return False

    def sdk_background(self, spec):
        owner = self.current
        return (not self.closed and isinstance(owner, ClaudeAgentAdapter)
                and owner.spec is not None and owner.spec.run_id == spec.run_id
                and owner.spec.binding == spec.binding and not owner.closing
                and not owner.failure)

    def permission_closed(self, identifier):
        if self.current and hasattr(self.current, 'permission_closed'):
            self.current.permission_closed(identifier)

    async def preflight(self, view, spec):
        # Initial SDK control initialization does not materialize a resumable
        # conversation in the pinned CLI. Refuse before stopping the good owner;
        # never invent history, submit a prompt or substitute another native ID.
        if view == 'terminal' and not self.native_history_observed:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)

    def sdk_permission_owner(self, spec):
        owner=self.current
        return (isinstance(owner,ClaudeAgentAdapter) and owner.spec is not None
            and owner.spec.run_id==spec.run_id and owner.client is not None
            and owner.client.options.can_use_tool==owner._permission
            and owner.owner is not None and not owner.owner.done() and not owner.closing)

    async def chat(self, spec, *, initial=False):
        await self.prepare(spec)
        environment = self.environment(spec)
        reviewed = replace(spec, spec={**spec.spec, 'prior_owner_exit_verified': True,
                                     'initial_chat': initial, 'launch_env': environment, 'supervised_cli_path': str(self.enclosure.cli)})
        adapter = ClaudeAgentAdapter(policy=lambda *_: self.guard_check() is None and not self.runtime.peer.closed,
            publish=self.sdk_publish, authorize=self.authorize, exit_proof=self.sdk_exit_proof)
        self.current = adapter
        try:
            observed = await adapter.start(reviewed)
        except BaseException:
            self.exit_qualified = (adapter.attestation is not None and
                self.enclosure.proof('started.json', adapter.attestation.process_identity))
            raise
        self.exit_qualified = self.enclosure.proof('started.json', observed.process_identity)
        if not self.exit_qualified:
            await adapter.close(CloseMode.STOP)
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if not await self.authorize():
            await adapter.close(CloseMode.STOP)
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return LaunchedClaudeOwner(adapter, observed)

    async def sdk_exit_proof(self, observed):
        enclosure = self.enclosures.get(observed.run_id) if observed else None
        return enclosure is not None and enclosure.proof('exit.json', observed.process_identity)

    async def sdk_publish(self, event):
        if event['kind']=='decision.applied':
            await self.runtime.peer.call('publication',event)
        elif event['kind'] == 'provider_message':
            message = event['message']
            if event['sdk_type'] == 'AssistantMessage':
                text = '\n'.join(block.get('text', '') for block in message.get('content', []) if isinstance(block, dict) and isinstance(block.get('text'), str))
                if text:
                    await self.runtime.publish({'kind': 'message', 'itemId': uuid.uuid4().hex, 'text': text})
            # Raw SDK objects/tool args never enter main's durable event stream.
        elif event['kind'] == 'decision':
            from acc.session_relay.native_approval import describe_tool
            description = describe_tool(event.get('tool'), event.get('input'), self.runtime.spec.binding.workspace)
            await self.runtime.publish({'kind': 'decision', 'decisionId': event['id'],
                'actionDigest': event['actionDigest'], 'expiresAtMs': event['expiresAtMs'],
                'description': description, 'choices': ['accept', 'decline'] if description else ['decline'], 'approval': native_facts(
                    event.get('tool'), event.get('input'), self.runtime.spec.binding.workspace)})
        elif event['kind'] in ('operation.accepted', 'turn.completed', 'decision.expired'):
            if event['kind'] == 'operation.accepted':
                settle_service = getattr(self.runtime, 'service_admitted', None)
                if settle_service is not None:
                    settle_service(event)
            await self.runtime.publish(event)
            if event['kind'] == 'turn.completed':
                self.native_history_observed = True
            if event['kind'] == 'turn.completed' and self.runtime.active_operation == event['operationId']:
                self.runtime.active_operation = None
            if event['kind'] == 'turn.completed':
                self.runtime.reconcile_late_applications()

    async def terminal(self, spec):
        await self.prepare(spec)
        if not await self.authorize():
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        attempt = SimpleNamespace(started=False, cancelled=False, lock=threading.Lock(), process=None, task=None)
        self.launch_attempt = attempt
        loop = asyncio.get_running_loop()
        def before_start():
            checked = asyncio.run_coroutine_threadsafe(self.authorize(spec, lifecycle=True), loop)
            try:
                authorized = checked.result(timeout=10)
            except BaseException:
                checked.cancel()
                raise
            with attempt.lock:
                if not authorized or attempt.cancelled or self.closed:
                    raise ops.OpError('Supervised owner launch was revoked before start.')
                attempt.started = True  # from here an error is an unknown fork outcome
        attempt.task = asyncio.create_task(asyncio.to_thread(ops.launch, self.config, {'acc_id': spec.spec['session_id'],
            'tmux_name': spec.spec['tmux_name'], 'provider': 'claude', 'cwd': spec.binding.workspace,
            'run_dir': spec.spec['run_dir'], 'args': list(spec.spec['terminal_resume_args']),
            'files': self.files, 'env': self.env}, inherit_environment=False, owner_executable=str(self.enclosure.cli),
            guard_files=tuple(self.guards), before_owner_start=before_start))
        try:
            await asyncio.shield(attempt.task)
        except BaseException:
            with attempt.lock:
                attempt.cancelled = True
            if attempt.task.done() and not attempt.started:
                self.launch_attempt = None  # trusted pre-fork refusal, not a fabricated tree receipt
            raise
        process = TmuxProcess(self.config, spec.spec['tmux_name'], self.enclosure)
        attempt.process = process
        self.processes.append(process)
        deadline = asyncio.get_running_loop().time() + 1
        while not self.enclosure.proof('started.json', f'{process.pid}:{process.started}'):
            if asyncio.get_running_loop().time() >= deadline:
                process.terminate()
                await process.wait()
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            await asyncio.sleep(.01)
        self.exit_qualified = True
        run = TerminalRun(spec.spec['session_id'], spec.run_id)
        self.observer = OwnedObserver(self.config, spec.spec['tmux_name'], run,
            lambda: self.authorize(spec, lifecycle=True), process.current)
        from .claude_native_input import NativeInput, OwnedAdmission
        native = NativeInput(self.runtime)
        async def validate(candidate):
            if not isinstance(candidate, InputGrant):
                return False
            try:
                return await self.runtime.terminal_value(candidate.grant, 'held') == candidate
            except (p.RelayError, ConnectionError, TimeoutError):
                return False
        observer_host = ObserverTerminalHost(run, self.observer.open, validate, validate,
            validate_read=lambda candidate: self.observer.current(candidate))
        self.input_attachment = OwnedInputAttachment(self.config, spec.spec['tmux_name'], run,
            lambda: validate(host.grant), process.current)
        host = ClaudeInputHost(run, self.input_attachment.open, validate, observer=observer_host,
            native_id=spec.binding.native_conversation, reader=OwnedAdmission(self, spec),
            register=native.register, settle=native.settle, processed=native.processed,
            authorize_boundary=lambda: self.authorize(spec, lifecycle=True))
        grant = SimpleNamespace(epoch=1, run=run)  # not input authority, never written
        boundary = host.freeze_boundary
        owner = ClaudeTerminalOwner(spec=spec, process=process, host=host, grant=grant,
            boundary=boundary, authorize=lambda spec: self.authorize(spec, lifecycle=True), tree_exited=process.tree_exited)
        self.current = owner
        self.launch_attempt = None
        from .claude_native_prompt import watch
        self.native_prompt_stop = asyncio.Event()
        self.native_prompt_task = asyncio.create_task(watch(self, owner, self.observer, self.hooks, spec))
        if not await self.authorize():
            await owner.close(CloseMode.STOP)
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return LaunchedClaudeOwner(owner, RuntimeAttestation(spec.binding.binding_id, spec.run_id,
            spec.binding.native_conversation, f'{process.pid}:{process.started}'))

    async def exited(self):
        # Completed close already verified every possible owner before closing
        # its receipt descriptors. No new launch is allowed on this factory.
        if getattr(self,'close_complete',False):
            return True
        if self.launch_attempt is not None:
            return False
        if self.current is not None and not self.exit_qualified:
            return False
        if self.current and hasattr(self.current, 'process'):
            process = self.current.process
            if process is None or process.returncode is None:
                return False
            if hasattr(process, 'tree_exited'):
                return await process.tree_exited(process)
            # A denied PreToolUse need not produce a post-tool event. Those
            # conservative ledgers still fence idle handover, but cannot undo
            # an authenticated whole-tree exit receipt after explicit STOP.
            return (self.current.exit_verified and self.enclosure is not None
                    and self.enclosure.proof('exit.json', self.current.attestation.process_identity))
        return self.current is None

    async def reap_sdk_owner(self):
        """Cancel-scope failure cannot strand the exact attested supervisor.

        A dead/missing supervisor still cannot attest orphan descendants. Only
        its private empty-tree receipt permits cleanup; never use a name kill.
        """
        owner = self.current
        observed = getattr(owner, 'attestation', None)
        process = getattr(owner, 'process', None)
        enclosure = self.enclosures.get(observed.run_id) if observed else None
        if (not isinstance(owner, ClaudeAgentAdapter) or process is None or enclosure is None
                or not enclosure.proof('started.json', observed.process_identity)):
            return False
        from .relay_enclosure import incarnation
        pid, started = observed.process_identity.split(':')[:2]
        fd = None
        try:
            if process.returncode is None:
                fd = os.pidfd_open(int(pid))
                if incarnation(int(pid)) != started:
                    return False
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), 5)
            if not enclosure.proof('exit.json', observed.process_identity):
                return False
            owner.exit_verified = True
            return True
        except (OSError, ValueError, TimeoutError):
            return False
        finally:
            if fd is not None:
                os.close(fd)

    async def close(self):
        from .claude_native_prompt import close
        await close(self)
        if self.close_complete:
            return
        self.closed = True
        failure = None
        try:
            attempt = self.launch_attempt
            if attempt is not None:
                with attempt.lock:
                    attempt.cancelled = True
                try:
                    await asyncio.wait_for(asyncio.shield(attempt.task), 10)
                except (TimeoutError, asyncio.CancelledError):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE) from None
                except Exception:
                    pass  # phase below determines whether a fork was possible
                if attempt.started:
                    process = attempt.process
                    if process is None:
                        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 10)
                    if not await process.tree_exited(process):
                        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                    self.snapshot = process.snapshot
                    await self.retire_owned_view(process)
                self.launch_attempt = None
            if self.current:
                try:
                    await self.current.close(CloseMode.STOP)
                except (Exception, asyncio.CancelledError):
                    if not await self.reap_sdk_owner():
                        raise
                if not await self.exited() and not await self.reap_sdk_owner():
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                process = getattr(self.current, 'process', None)
                if isinstance(process, TmuxProcess):
                    self.snapshot = process.snapshot
                    await self.retire_owned_view(process)
        except (Exception, asyncio.CancelledError) as error:
            failure = error
        finally:
            if self.input_attachment:
                await self.input_attachment.close()
            if self.observer:
                await self.observer.close()
            if self.hooks:
                await self.hooks.close()
            proved = failure is None
            if not proved:
                try:
                    proved = await self.exited()
                except (Exception, asyncio.CancelledError):
                    proved = False
            # Unknown trees retain their proof FDs and conversation lease. Only
            # releasable transports close; a reused view is never targeted.
            if proved:
                for process in self.processes:
                    process.close()
                for enclosure in self.enclosures.values():
                    enclosure.close()
                await self.lease.stop()
            self.close_complete = failure is None
        if failure is not None:
            raise failure


    async def retire_owned_view(self, process):
        try:
            await process.retire_view()
        except (Exception, asyncio.CancelledError):
            self.retirement_unproved = True
            raise
