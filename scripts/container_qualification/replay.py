"""Loopback-only scripted model, never a replacement CLI or hook invocation.

Real CLIs receive two tool calls through their ordinary API transport. No auth,
model service or Internet is needed. Unsupported wire/tool formats fail closed.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def tool_arguments(name, command):
    if name in ('Bash', 'bash', 'shell_command'):
        return {'command': command}
    if name == 'exec_command':
        return {'cmd': command}
    if name == 'shell':
        return {'command': ['/bin/sh', '-c', command]}
    raise ValueError('unsupported shell tool')


def shell_tool(tools):
    names = [tool.get('name') or tool.get('function', {}).get('name') for tool in tools]
    return next((name for name in ('Bash', 'bash', 'exec_command', 'shell_command', 'shell') if name in names), None)


class Replay:
    def __init__(self, commands):
        self.commands = commands
        self.calls = 0
        self.errors = 0
        replay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # No request bodies, tokens or provider output in host logs.

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'object': 'list', 'data': [
                    {'id': name, 'object': 'model'} for name in ('qualification', 'gpt-4o', 'claude-sonnet-4-5')
                ]}).encode())

            def do_POST(self):
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 8 * 1024 * 1024:
                        raise ValueError('request size')
                    body = json.loads(self.rfile.read(size))
                    route = urlsplit(self.path).path
                    if route.endswith('/count_tokens'):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"input_tokens":100}')
                        return
                    name = shell_tool(body.get('tools', []))
                    if not name:
                        raise ValueError('shell tool not advertised')
                    index = replay.calls
                    replay.calls += 1
                    args = tool_arguments(name, replay.commands[index]) if index < len(replay.commands) else None
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    if route.endswith('/messages'):
                        self.anthropic(name, args, index)
                    elif route.endswith('/responses'):
                        self.send_responses(name, args, index)
                    elif route.endswith('/chat/completions'):
                        self.chat(name, args, index)
                    else:
                        raise ValueError('unsupported wire format')
                except (ValueError, KeyError, TypeError, OSError):
                    replay.errors += 1

            def event(self, name, value):
                self.wfile.write((('event: ' + name + '\n') if name else '').encode())
                self.wfile.write(('data: ' + json.dumps(value) + '\n\n').encode())
                self.wfile.flush()

            def anthropic(self, name, args, index):
                self.event('message_start', {'type': 'message_start', 'message': {
                    'id': f'msg_{index}', 'type': 'message', 'role': 'assistant', 'model': 'qualification',
                    'content': [], 'stop_reason': None, 'usage': {'input_tokens': 100, 'output_tokens': 1}}})
                content = ({'type': 'tool_use', 'id': f'tool_{index}', 'name': name, 'input': {}} if args else
                           {'type': 'text', 'text': ''})
                self.event('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': content})
                delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(args)} if args else
                         {'type': 'text_delta', 'text': 'Qualification complete.'})
                self.event('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': delta})
                self.event('content_block_stop', {'type': 'content_block_stop', 'index': 0})
                self.event('message_delta', {'type': 'message_delta', 'delta': {
                    'stop_reason': 'tool_use' if args else 'end_turn', 'stop_sequence': None},
                    'usage': {'output_tokens': 20}})
                self.event('message_stop', {'type': 'message_stop'})

            def send_responses(self, name, args, index):
                response = {'id': f'resp_{index}', 'object': 'response', 'status': 'in_progress', 'output': []}
                self.event('response.created', {'type': 'response.created', 'response': response})
                if args:
                    item = {'id': f'fc_{index}', 'type': 'function_call', 'call_id': f'call_{index}',
                            'name': name, 'arguments': json.dumps(args), 'status': 'completed'}
                else:
                    item = {'id': f'msg_{index}', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': 'Qualification complete.', 'annotations': []}]}
                self.event('response.output_item.added', {'type': 'response.output_item.added',
                           'output_index': 0, 'item': item})
                self.event('response.output_item.done', {'type': 'response.output_item.done',
                           'output_index': 0, 'item': item})
                self.event('response.completed', {'type': 'response.completed', 'response': response | {
                    'status': 'completed', 'output': [item], 'usage': {'input_tokens': 100, 'output_tokens': 20,
                                                                   'total_tokens': 120}}})

            def chat(self, name, args, index):
                delta = {'role': 'assistant', 'content': 'Qualification complete.'}
                if args:
                    delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': f'call_{index}',
                             'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
                chunk = {'id': f'chat_{index}', 'object': 'chat.completion.chunk', 'created': 1,
                         'model': 'qualification', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
                self.event('', chunk)
                self.event('', chunk | {'choices': [{'index': 0, 'delta': {},
                                                    'finish_reason': 'tool_calls' if args else 'stop'}]})
                self.wfile.write(b'data: [DONE]\n\n')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
