"""Bounded native display facts; never authorization or executable input."""
import json


def native_facts(tool, inputs, cwd, *, source='sdk', native_tool_id=None):
    rendered = json.dumps(inputs, ensure_ascii=False, separators=(',', ':')) if isinstance(inputs, dict) else None
    if rendered is not None and len(rendered.encode()) > 8192:
        rendered = None
    return {'source': source, 'native_tool_id': native_tool_id, 'tool_name': tool[:200] if isinstance(tool, str) else '', 'acc_complete_input': rendered,
            'cwd': cwd if isinstance(cwd, str) and len(cwd.encode()) <= 2000 else None}


def complete(payload):
    rendered = payload.get('acc_complete_input') if isinstance(payload, dict) else None
    if not isinstance(rendered, str) or len(rendered.encode()) > 8192:
        return False
    try:
        return isinstance(json.loads(rendered), dict)
    except ValueError:
        return False


def matches_action(payload, digest):
    """Bind complete pre-redaction facts to the native request, never a label."""
    from . import protocol as p
    if not complete(payload) or not isinstance(payload.get('tool_name'), str):
        return False
    body = {'tool': payload['tool_name'], 'input': json.loads(payload['acc_complete_input'])}
    if payload.get('source') == 'native_hook':
        identifier = payload.get('native_tool_id')
        if identifier is not None and not p.valid_opaque(identifier):
            return False
        body['nativeToolId'] = identifier
    elif payload.get('source') != 'sdk':
        return False
    try:
        return p.sha256_digest(p.canonical_json(body)) == digest
    except (ValueError, TypeError):
        return False
