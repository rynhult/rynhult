"""Typed envelopes shared by broker request and response readers."""
from __future__ import annotations

import json
import re


def valid_id(value) -> bool:
    return type(value) is int and 0 <= value <= (1 << 53) - 1


def request_from(line: bytes) -> dict:
    try:
        value = json.loads(line)
    except ValueError:
        raise ValueError("protocol: invalid JSON") from None
    if (
        not isinstance(value, dict)
        or not valid_id(value.get("id"))
        or not isinstance(value.get("method"), str)
        or not re.fullmatch(r"[a-z_]{1,64}", value["method"])
        or not isinstance(value.get("params"), dict)
    ):
        raise ValueError("protocol: expected bounded id, method and object params")
    return value


def response_from(line: bytes, request_id: int, *, attach: bool = False) -> dict:
    try:
        value = json.loads(line)
    except ValueError:
        raise ValueError("malformed broker answer") from None
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise ValueError("malformed broker answer")
    # Attach predates request ids and switches to binary framing after this reply.
    if not attach and (not valid_id(value.get("id")) or value["id"] != request_id):
        raise ValueError("malformed broker answer")
    if value["ok"]:
        valid = type(value.get("readonly")) is bool if attach else "result" in value
    else:
        valid = isinstance(value.get("error"), str)
    if not valid:
        raise ValueError("malformed broker answer")
    return value
