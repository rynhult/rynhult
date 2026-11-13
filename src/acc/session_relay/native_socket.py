"""Pinned native TUI WebSocket-over-Unix boundary. No provider RPC tunnel.

A broker-owned immutable writer route is required for mutations in exclusive
mode. Reads remain independent. No client name, keypress or current-holder guess
can create a route, and no general provider RPC tunnel is exposed.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

from acc import peercred

from .protocol import MAX_FRAME_BYTES, ErrorCode, RelayError, valid_request_id

CLI_VERSION = '0.167.1'
# All other native methods remain unavailable, including config and auth writes,
# provider/thread creation, forks, steering and replies to native approvals.
READS = frozenset({'account/read', 'config/read', 'configRequirements/read', 'model/list',
                  'thread/read', 'thread/resume', 'thread/turns/list', 'thread/items/list'})
SCOPED = frozenset({'thread/read', 'thread/resume', 'thread/turns/list', 'thread/items/list'})
CONFIG_FIELDS = frozenset({'model', 'model_provider', 'model_reasoning_effort', 'approval_policy',
                           'approvals_reviewer', 'sandbox_mode', 'sandbox_workspace_write', 'service_tier'})


class NativeSocket:
    def __init__(self, directory: Path, native_id, read, authorize, *, initialized,
                 capacity=16, writer_route=None, start_settings=None):
        self.directory, self.native_id = directory, native_id
        self.reader_bootstrapped = False
        self.bootstrapped_clients = set()
        self.read, self.authorize, self.initialized = read, authorize, initialized
        self.writer_route = writer_route
        from .native_turn import NativeTurnPolicy
        self.turn_policy = NativeTurnPolicy.from_start(native_id, start_settings) if start_settings is not None else None
        self.capacity = capacity
        self.path = directory / 'native-client.sock'
        self.server = None
        self.clients = set()
        self.client_routes = {}
        from .native_approval_aliases import ApprovalAliases
        self.approval_aliases = ApprovalAliases(native_id)
        self.identity = None
        self.socket_identity = None
        self.directory_fd = None
        self.attested = False  # requires qualified TerminalHost + exclusive writer retirement

    async def start(self):
        from websockets.asyncio.server import unix_serve
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        self.identity = info.st_dev, info.st_ino
        if self.path.exists() or self.path.is_symlink():
            raise RelayError(ErrorCode.BUSY)
        self.directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        # A busy owned TUI or broker event loop can delay pongs. Liveness is the
        # pinned process/Unix stream and current authority, never a wall-clock
        # pong deadline. Keep pings, without disconnecting a descheduled reader.
        self.server = await unix_serve(self.connection, f'/proc/self/fd/{self.directory_fd}/native-client.sock', compression=None,
            max_size=MAX_FRAME_BYTES, max_queue=16, ping_interval=20, ping_timeout=None,
            open_timeout=5, close_timeout=5)
        os.chmod(self.path, 0o600)
        socket = self.path.lstat()
        self.socket_identity = socket.st_dev, socket.st_ino
        return self.path

    @property
    def connect_path(self):
        return f'/proc/{os.getpid()}/fd/{self.directory_fd}/native-client.sock'

    async def offer_approval(self, route, **question):
        from .native_approval_aliases import NATIVE_APPROVAL_DECODER_QUALIFIED
        if not NATIVE_APPROVAL_DECODER_QUALIFIED or route is None:
            raise RelayError(ErrorCode.UNSUPPORTED)
        self.validate()
        peers = [peer for peer, bound in self.client_routes.items() if bound is route and peer in self.bootstrapped_clients]
        if len(peers) != 1 or not await self.authorize():
            raise RelayError(ErrorCode.STALE_AUTHORITY)
        peer = peers[0]
        request = await self.approval_aliases.offer(peer, route, **question)
        if self.client_routes.get(peer) is not route or not await self.authorize():
            self.approval_aliases.retire(peer)
            raise RelayError(ErrorCode.STALE_AUTHORITY)
        self.validate()
        try:
            await asyncio.wait_for(peer.send(json.dumps(request)), 1)
        except BaseException:
            self.approval_aliases.retire(peer)
            raise
        return request['id']

    async def withdraw_approval(self, route, offer):
        """Send exact owned alias resolution. A sent notification is not an ACK."""
        from .native_approval_aliases import NATIVE_APPROVAL_DECODER_QUALIFIED
        if not NATIVE_APPROVAL_DECODER_QUALIFIED:
            raise RelayError(ErrorCode.UNSUPPORTED)
        # Invalidate locally even if authority/transport was lost. A stale native
        # response must never survive failure to update the private display.
        bound = self.approval_aliases.withdraw(route, offer)
        if bound is None:
            return False
        self.validate()
        if not await self.authorize() or self.client_routes.get(bound.peer) is not route:
            raise RelayError(ErrorCode.STALE_AUTHORITY)
        self.validate()
        await asyncio.wait_for(bound.peer.send(json.dumps({
            'method': 'serverRequest/resolved',
            'params': {'threadId': self.native_id, 'requestId': offer.alias},
        })), 1)
        return True

    async def publish(self, method, params):
        from websockets.exceptions import ConnectionClosed

        if method not in ('turn/started', 'turn/completed', 'item/started', 'item/completed', 'item/agentMessage/delta'):
            return
        if params.get('threadId') != self.native_id:
            return
        self.validate()
        encoded = json.dumps({'method': method, 'params': params})
        if len(encoded.encode()) > MAX_FRAME_BYTES:
            raise RelayError(ErrorCode.LIMIT)
        async def send(client):
            try:
                if not await self.authorize():
                    await client.close(code=1008)
                    return
                self.validate()
                # Bounded frame/client counts already bound this broadcast.
                # Backpressure must slow delivery, not kill a healthy native
                # connection because a scheduler or reader took over a second.
                await client.send(encoded)
            except (OSError, ConnectionError, TimeoutError, ConnectionClosed):
                await client.close(code=1011)
        await asyncio.gather(*(send(client) for client in tuple(self.clients)))

    def validate(self):
        info = self.directory.lstat()
        socket = self.path.lstat()
        if ((info.st_dev, info.st_ino) != self.identity or stat.S_IMODE(info.st_mode) != 0o700
                or not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != os.getuid()
                or (socket.st_dev, socket.st_ino) != self.socket_identity
                or stat.S_IMODE(socket.st_mode) != 0o600):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)

    async def connection(self, websocket):
        from websockets.exceptions import ConnectionClosed
        self.validate()
        sock = websocket.transport.get_extra_info('socket')
        if peercred.peer_uid(sock) != os.getuid() or len(self.clients) >= self.capacity:
            await websocket.close(code=1008)
            return
        self.clients.add(websocket)
        initialized = False
        # Broker-owned route captured once for this native connection. Never
        # replace it with whichever person happens to hold control later.
        try:
            try:
                route = self.writer_route(websocket) if self.writer_route is not None else None
            except RelayError:
                await websocket.close(code=1008)
                return
            self.client_routes[websocket] = route
            async for raw in websocket:
                request_id = None
                try:
                    self.validate()
                    if not await self.authorize():
                        raise RelayError(ErrorCode.STALE_AUTHORITY)
                    self.validate()
                    if not isinstance(raw, str):
                        raise RelayError(ErrorCode.UNSUPPORTED)
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise RelayError(ErrorCode.INTEGRITY_FAILURE)
                    request_id = request.get('id')
                    if request_id is not None and not valid_request_id(request_id):
                        raise RelayError(ErrorCode.INTEGRITY_FAILURE)
                    # A response to a native approval is a mutation too. It must
                    # never be forwarded directly to the provider's pending ID.
                    if 'method' not in request:
                        from .native_approval_aliases import NATIVE_APPROVAL_DECODER_QUALIFIED
                        if not initialized or not NATIVE_APPROVAL_DECODER_QUALIFIED:
                            raise RelayError(ErrorCode.UNSUPPORTED)
                        await self.approval_aliases.reply(websocket, route, request)
                        continue
                    if request.keys() - {'id', 'method', 'params'}:
                        raise RelayError(ErrorCode.UNSUPPORTED)
                    method, params = request['method'], request.get('params', {})
                    if params is None and method in ('initialized', 'configRequirements/read'):
                        params = {}
                    if not isinstance(params, dict):
                        raise RelayError(ErrorCode.UNSUPPORTED)
                    if method == 'initialize' and not initialized:
                        client = params.get('clientInfo', {})
                        if not isinstance(client, dict):
                            raise RelayError(ErrorCode.UNSUPPORTED)
                        if client.get('name') != 'codex-tui' or client.get('version') != CLI_VERSION:
                            raise RelayError(ErrorCode.UNSUPPORTED)
                        initialized = True
                        result = self.initialized
                    elif initialized and method == 'initialized' and request_id is None:
                        continue
                    elif initialized and method in READS:
                        if method in SCOPED and params.get('threadId') != self.native_id:
                            raise RelayError(ErrorCode.STALE_RUN)
                        # Scope and option validation happens before the native
                        # read callback. Resume projects this already-owned run;
                        # it cannot launch or select another conversation.
                        fields = {
                            'thread/read': {'threadId', 'includeTurns'},
                            'thread/resume': {'threadId', 'excludeTurns', 'initialTurnsPage',
                                'history', 'path', 'model', 'modelProvider', 'cwd',
                                'runtimeWorkspaceRoots', 'approvalPolicy', 'approvalsReviewer',
                                'sandbox', 'permissions', 'config', 'baseInstructions',
                                'developerInstructions', 'personality'},
                            'thread/turns/list': {'threadId', 'cursor', 'limit', 'sortDirection', 'itemsView'},
                            'thread/items/list': {'threadId', 'turnId', 'cursor', 'limit', 'sortDirection'},
                            'account/read': {'refreshToken'}, 'config/read': {'includeLayers', 'cwd'},
                            'configRequirements/read': set(), 'model/list': {'cursor', 'limit', 'includeHidden'},
                        }[method]
                        if params.keys() - fields:
                            raise RelayError(ErrorCode.UNSUPPORTED)
                        if method == 'thread/turns/list' and params.get('itemsView', 'notLoaded') != 'notLoaded':
                            raise RelayError(ErrorCode.UNSUPPORTED)
                        if method == 'thread/resume':
                            # Stock 0.167.1 serializes absent overrides as null.
                            # Only display selection is discarded; no non-null
                            # configuration can cross this read projection.
                            if any(value is not None for key, value in params.items()
                                   if key not in ('threadId', 'excludeTurns')):
                                raise RelayError(ErrorCode.UNSUPPORTED)
                            if 'excludeTurns' in params and type(params['excludeTurns']) is not bool:
                                raise RelayError(ErrorCode.UNSUPPORTED)
                            params = {'threadId': self.native_id}
                        if params.get('cursor') is not None:
                            raise RelayError(ErrorCode.UNSUPPORTED)
                        if method in ('thread/turns/list', 'thread/items/list', 'model/list'):
                            limit = params.get('limit')
                            if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
                                raise RelayError(ErrorCode.LIMIT)
                        if method in ('thread/turns/list', 'thread/items/list'):
                            if params.get('sortDirection') not in (None, 'asc', 'desc'):
                                raise RelayError(ErrorCode.UNSUPPORTED)
                        if method == 'account/read':
                            # Account credentials/identity are not session data.
                            result = {'account': None, 'requiresOpenaiAuth': False}
                        elif method == 'config/read':
                            value = await self.read(method, {'includeLayers': False})
                            result = {'config': {key: value['config'][key] for key in CONFIG_FIELDS if key in value['config']}, 'origins': {}}
                        else:
                            result = await self.read(method, params)
                    elif initialized and method in ('turn/start', 'turn/interrupt') and route is not None:
                        if params.get('threadId') != self.native_id:
                            raise RelayError(ErrorCode.STALE_RUN)
                        if method == 'turn/start':
                            if self.turn_policy is not None:
                                projected = self.turn_policy.project(params)
                            else:
                                from .native_turn import basic_text
                                if params.keys() != {'threadId', 'input'}:
                                    raise RelayError(ErrorCode.UNSUPPORTED)
                                projected = basic_text(params)
                            if hasattr(route, 'native_request'):
                                result = await route.native_request('turn.submit', projected, params.get('clientUserMessageId'))
                            else:
                                result = await route('turn.submit', projected)
                        else:
                            if params.keys() != {'threadId', 'turnId'}:
                                raise RelayError(ErrorCode.UNSUPPORTED)
                            result = await route('turn.interrupt', {'turnId': params['turnId']})
                    else:
                        raise RelayError(ErrorCode.UNSUPPORTED)
                    # No stale reply after an awaited read or admission.
                    if not await self.authorize():
                        raise RelayError(ErrorCode.STALE_AUTHORITY)
                    self.validate()
                    if request_id is not None:
                        encoded = json.dumps({'id': request_id, 'result': result})
                        if len(encoded.encode()) > MAX_FRAME_BYTES:
                            raise RelayError(ErrorCode.LIMIT)
                        await websocket.send(encoded)
                        if method == 'thread/resume':
                            self.reader_bootstrapped = True
                            self.bootstrapped_clients.add(websocket)
                except (RelayError, ValueError, KeyError, TypeError):
                    if request_id is not None:
                        await websocket.send(json.dumps({'id': request_id, 'error': {
                            'code': -32001, 'message': 'This native operation is unavailable through the session hub.'}}))
                    else:
                        await websocket.close(code=1008)
                        return
        except ConnectionClosed:
            pass  # An exited/detached owned view need not send a close frame.
        finally:
            self.approval_aliases.retire(websocket)
            self.client_routes.pop(websocket, None)
            self.clients.discard(websocket)
            self.bootstrapped_clients.discard(websocket)

    async def close(self):
        self.attested = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        # Only remove the socket created in the still-pinned directory.
        try:
            if self.identity is not None and self.path.exists():
                self.validate()
                self.path.unlink()
        finally:
            if self.directory_fd is not None:
                os.close(self.directory_fd)
                self.directory_fd = None
