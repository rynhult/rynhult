"""Pinned SDK query/result observations, never permission from a socket write."""
import asyncio

from . import protocol as p
from .claude_hook import denial_message


class SDKApplications:
    def __init__(self):
        self.current = None
        self.records = {}

    def begin(self, operation, native_id):
        self.current = {'operation': operation, 'native': native_id, 'uses': {}, 'results': [], 'failed': False}

    def request(self, identifier, tool, inputs):
        if identifier in self.records:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if len(self.records) >= 128:
            raise p.RelayError(p.ErrorCode.LIMIT)
        record = {'turn': self.current, 'tool': tool, 'inputs': p.sha256_digest(p.canonical_json(inputs)),
                  'digest': p.sha256_digest(p.canonical_json({'tool': tool, 'input': inputs})),
                  'future': asyncio.get_running_loop().create_future(), 'effect': None, 'answer': None, 'bound': None}
        self.records[identifier] = record
        return record

    def bind(self, identifier, operation, effect):
        record = self.records.get(identifier)
        if not record or record['answer'] is not None:
            raise p.RelayError(p.ErrorCode.DECISION_RESOLVED)
        if (operation.method != 'decision.reply' or operation.params.get('decisionId') != identifier
                or operation.params.get('actionDigest') != record['digest']
                or operation.payload_digest != p.operation_digest(operation.method, operation.params)):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        turn = record['turn']
        if turn and (turn['operation'].run_id != operation.run_id or turn['operation'].session_id != operation.session_id):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        record['answer'], record['effect'] = operation, effect
        if turn:
            matches = [key for key, value in turn['uses'].items() if value == (record['tool'], record['inputs'])]
            if len(matches) == 1 and not any(result['tool'] == matches[0] for result in turn['results']):
                record['bound'] = matches[0]

    def observe(self, turn, message):
        from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage
        if turn is None or turn['failed']:
            return
        if isinstance(message, AssistantMessage) and message.error is not None:
            turn['failed'] = True
            return
        content = getattr(message, 'content', None)
        if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None and isinstance(content, list):
            for block in content:
                if isinstance(block, ToolUseBlock):
                    if not p.valid_opaque(block.id) or block.id in turn['uses'] or len(turn['uses']) >= 64:
                        turn['failed'] = True
                        return
                    turn['uses'][block.id] = (block.name, p.sha256_digest(p.canonical_json(block.input)))
        elif isinstance(message, UserMessage) and message.parent_tool_use_id is None and isinstance(content, list):
            for block in content:
                if isinstance(block, ToolResultBlock):
                    if not p.valid_opaque(block.tool_use_id) or not p.valid_opaque(message.uuid) or len(turn['results']) >= 64:
                        turn['failed'] = True
                        return
                    turn['results'].append({'tool': block.tool_use_id, 'uuid': message.uuid,
                        'error': block.is_error, 'content': block.content if isinstance(block.content, str) and len(block.content) <= 256 else None})
        elif isinstance(message, ResultMessage):
            if (message.subtype != 'success' or message.is_error is not False
                    or message.session_id != turn['native'] or message.stop_reason not in (None, 'end_turn')):
                turn['failed'] = True
                return
            for identifier, record in self.records.items():
                if record['turn'] is not turn or record['answer'] is None or record['future'].done():
                    continue
                matches = [key for key, value in turn['uses'].items() if value == (record['tool'], record['inputs'])]
                results = [result for result in turn['results'] if result['tool'] in matches]
                if record['effect'] == 'deny':
                    results = [result for result in results if result['error'] is True and result['content'] == denial_message(identifier)]
                else:
                    results = [result for result in results if result['tool'] == record['bound']
                        and (result['error'] is None or result['error'] is False)]
                if len(results) != 1 or results[0]['tool'] not in turn['uses']:
                    continue
                result = results[0]
                reference = ':'.join((turn['native'], turn['operation'].operation_id, identifier, result['tool'], result['uuid']))
                record['future'].set_result({'outcome': 'accepted', 'application': {'decisionId': identifier,
                    'actionDigest': record['answer'].params['actionDigest'], 'effect': record['effect'],
                    'nativeRef': reference, 'nativeToolId': result['tool']}})

    async def observe_answer(self, operation, effect, timeout=3):
        record = self.records.get(operation.params['decisionId'])
        if not record or record['answer'] != operation or record['effect'] != effect:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        try:
            return await asyncio.wait_for(asyncio.shield(record['future']), timeout)
        except TimeoutError:
            return {'outcome': 'unknown'}
