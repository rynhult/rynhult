"""Session relay wire contract: one vocabulary for clients, the hub and the broker.

docs/design/session-relay.md is the authority for meaning; this module fixes the shape. It is pure and
stdlib-only so main, the broker and tests share it without importing each other. It parses, validates and
describes legal transitions. It holds no state, authorizes nobody and enables no provider path: every check
here sits underneath the existing authentication and session authorization, never instead of them.

The method and event tables are allowlists. Anything absent is refused, and omitted capabilities or features
mean unsupported. R1_METHODS is the subset the opt-in Codex pilot may offer; the rest of the v1 surface parses,
so its shape stays fixed, and is then refused by admit().
"""
from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import json
import re
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date
from typing import Any, Callable, Iterable, Mapping

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
EVENT_SCHEMA = 1
METHOD_PREFIX = "ry/"
EVENT_METHOD = "ry/event"
FEATURES = frozenset({"queue", "steer", "decisions", "configuration", "attachments", "switch", "terminal", "writer"})
R1_FEATURES = frozenset({"queue", "decisions", "terminal", "writer"})

# Proposed bounds from the design; numeric values still need pilot measurement.
MAX_ID = (1 << 53) - 1  # exactly representable in a browser
MAX_FRAME_BYTES = 1 << 20
MAX_PROMPT_BYTES = 8 << 10
MAX_TERMINAL_INPUT_BYTES = 64 << 10  # this and the chunk bound are not in the design: decoded, under the frame bound
MAX_UPLOAD_CHUNK_BYTES = 512 << 10
MAX_FILE_BYTES = 4 << 20
MAX_MESSAGE_FILES = 4
HISTORY_DEFAULT_ITEMS = 100
HISTORY_MAX_ITEMS = 200
HISTORY_MAX_BYTES = 256 << 10
MAX_CLIENT_QUEUE_BYTES = 1 << 20
MAX_PENDING_DECISIONS = 16
MAX_REASON_CHARS = 200
MAX_TERMINAL_DIMENSION = 1000


class RpcCode(enum.IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    DOMAIN = -32000  # the typed relay error travels in error.data


class Retry(enum.StrEnum):
    READ_ONLY = "read_only"
    SAME_OPERATION_QUERY = "same_operation_query"
    AFTER_REVIEW = "after_review"
    NEVER = "never"


class ErrorCode(enum.StrEnum):
    NOT_FOUND_OR_DENIED = "NOT_FOUND_OR_DENIED"
    STALE_RUN = "STALE_RUN"
    STALE_AUTHORITY = "STALE_AUTHORITY"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    UNSUPPORTED = "UNSUPPORTED"
    BUSY = "BUSY"
    INPUT_CONFLICT = "INPUT_CONFLICT"
    DECISION_RESOLVED = "DECISION_RESOLVED"
    DECISION_EXPIRED = "DECISION_EXPIRED"
    POLICY_REFUSED = "POLICY_REFUSED"
    OFFLINE = "OFFLINE"
    LIMIT = "LIMIT"
    CURSOR_RESET = "CURSOR_RESET"
    DEFINITIVE_REJECTION = "DEFINITIVE_REJECTION"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    VERSION_MISMATCH = "VERSION_MISMATCH"


# Messages are fixed per code: nothing a provider, path or foreign session said can reach a client through one.
_ERRORS: dict[ErrorCode, tuple[str, Retry]] = {
    ErrorCode.NOT_FOUND_OR_DENIED: ("Not found, or you do not have access.", Retry.NEVER),
    ErrorCode.STALE_RUN: ("The session has moved to a different run.", Retry.AFTER_REVIEW),
    ErrorCode.STALE_AUTHORITY: ("This action was issued under authority that has ended.", Retry.READ_ONLY),
    ErrorCode.REVISION_CONFLICT: ("This changed since you last read it.", Retry.AFTER_REVIEW),
    ErrorCode.UNSUPPORTED: ("This action is not available for this session.", Retry.NEVER),
    ErrorCode.BUSY: ("The session is busy.", Retry.AFTER_REVIEW),
    ErrorCode.INPUT_CONFLICT: ("Input is not available right now.", Retry.AFTER_REVIEW),
    ErrorCode.DECISION_RESOLVED: ("This decision has already been answered.", Retry.READ_ONLY),
    ErrorCode.DECISION_EXPIRED: ("This decision has expired.", Retry.READ_ONLY),
    ErrorCode.POLICY_REFUSED: ("Security settings do not allow this.", Retry.NEVER),
    ErrorCode.OFFLINE: ("The machine running this session is not reachable.", Retry.READ_ONLY),
    ErrorCode.LIMIT: ("This exceeds a limit.", Retry.AFTER_REVIEW),
    ErrorCode.CURSOR_RESET: ("History changed; read a new snapshot.", Retry.READ_ONLY),
    ErrorCode.DEFINITIVE_REJECTION: ("This was rejected and did not take effect.", Retry.AFTER_REVIEW),
    ErrorCode.OUTCOME_UNKNOWN: ("The outcome is unknown; check the receipt.", Retry.SAME_OPERATION_QUERY),
    ErrorCode.INTEGRITY_FAILURE: ("This does not match the registered operation.", Retry.NEVER),
    ErrorCode.VERSION_MISMATCH: ("This client version is not supported.", Retry.NEVER),
}
_RPC_MESSAGES = {
    RpcCode.PARSE_ERROR: "Parse error",
    RpcCode.INVALID_REQUEST: "Invalid request",
    RpcCode.METHOD_NOT_FOUND: "Method not found",
    RpcCode.INVALID_PARAMS: "Invalid params",
}
ERROR_LIMITS = frozenset({"frame"})
ERROR_REASONS = frozenset({"not_supported_in_release", "unattributed", "unsupported_terminal_approval_key", "terminal_review_required"})
# Values are closed too: a free-form string here would be a way out for provider text, paths and foreign IDs.
ERROR_DETAILS: dict[str, Callable[[Any], bool]] = {
    "capability": lambda value: type(value) is str and value in CAPABILITIES,
    "feature": lambda value: type(value) is str and value in FEATURES,
    "limit": lambda value: type(value) is str and value in ERROR_LIMITS,
    "maximum": lambda value: type(value) is int,
    "method": lambda value: type(value) is str and value in OPERATIONS,
    "reason": lambda value: type(value) is str and value in ERROR_REASONS,
    "supportedMajor": lambda value: type(value) is int,
}


class RpcError(Exception):
    """A standard JSON-RPC refusal. Echoes the request id and no client-supplied content."""

    def __init__(self, code: RpcCode, request_id: int | str | None = None):
        super().__init__(_RPC_MESSAGES[code])
        self.code = code
        self.request_id = request_id

    def to_wire(self) -> dict:
        return {"code": int(self.code), "message": _RPC_MESSAGES[self.code]}


class RelayError(Exception):
    """A typed safe domain error: `{code, message, operationId?, stateRevision?, retry, details}`.

    Details must pass ERROR_DETAILS, key and value. An OUTCOME_UNKNOWN must carry its uncertain receipt, because an
    error after a possible write may not imply that nothing happened.
    """

    def __init__(self, code: ErrorCode, *, operation_id: str | None = None, state_revision: int | None = None,
                 retry: Retry | None = None, details: Mapping[str, str | int] | None = None,
                 receipt: Receipt | None = None, request_id: int | str | None = None):
        message, default_retry = _ERRORS[code]
        details = dict(details or {})
        if not details.keys() <= ERROR_DETAILS.keys() or not all(ERROR_DETAILS[k](v) for k, v in details.items()):
            raise ValueError("relay error details outside the safe allowlist")
        if code is ErrorCode.UNSUPPORTED:
            message = {
                'not_supported_in_release': 'This combination is not supported in this release. Recover into a new session; nothing is replayed.',
                'unsupported_terminal_approval_key': 'This key was not sent. Only Enter or Escape can answer a basic command approval. Use the review buttons for other choices.',
                'terminal_review_required': 'Terminal keys are unavailable for this review. Use the review buttons. No key was sent.',
            }.get(details.get('reason'), message)
        if code is ErrorCode.OUTCOME_UNKNOWN and (receipt is None or receipt.state is not ReceiptState.UNCERTAIN):
            raise ValueError("OUTCOME_UNKNOWN requires the uncertain receipt")
        super().__init__(message)
        self.code = code
        self.message = message
        self.operation_id = operation_id or (receipt.operation_id if receipt else None)
        self.state_revision = state_revision
        self.retry = retry or default_retry
        self.details = details
        self.receipt = receipt
        self.request_id = request_id

    def to_wire(self) -> dict:
        data: dict[str, Any] = {"code": self.code.value, "message": self.message, "retry": self.retry.value,
                                "details": self.details}
        if self.operation_id is not None:
            data["operationId"] = self.operation_id
        if self.state_revision is not None:
            data["stateRevision"] = self.state_revision
        if self.receipt is not None:
            data["receipt"] = to_wire(self.receipt)
        return {"code": int(RpcCode.DOMAIN), "message": self.message, "data": data}


def to_wire(value: Any) -> Any:
    """Plain JSON from contract types: camelCase keys, enum values, and no absent optional fields."""
    if is_dataclass(value) and not isinstance(value, type):
        items = ((f.name, getattr(value, f.name)) for f in fields(value) if f.repr)
        return {_camel(k): to_wire(v) for k, v in items if v is not None}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Mapping):
        return {k: to_wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_wire(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(to_wire(v) for v in value)
    return value


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.title() for word in rest)


def _reject_constant(_name: str):
    raise ValueError("non-finite number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    # Two readers can disagree on a duplicated key, so it could change a payload without changing its digest.
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate key")
    return value


def decode_frame(raw: bytes) -> Any:
    """Strict bounded JSON: one UTF-8 value with no duplicate keys or non-finite numbers."""
    if len(raw) > MAX_FRAME_BYTES:
        raise RelayError(ErrorCode.LIMIT, details={"limit": "frame", "maximum": MAX_FRAME_BYTES})
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise RpcError(RpcCode.PARSE_ERROR) from None


def encode_frame(value: Any) -> bytes:
    """One frame. A lone surrogate from provider text becomes U+FFFD, so one bad delta cannot stop a stream."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    raw = _SURROGATE.sub("\ufffd", text).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise RelayError(ErrorCode.LIMIT, details={"limit": "frame", "maximum": MAX_FRAME_BYTES})
    return raw


def canonical_json(value: Any) -> bytes:
    """The bytes a digest covers. Sorted keys, no whitespace; floats are refused because languages print them differently."""
    def check(item: Any) -> None:
        if isinstance(item, float):
            raise ValueError("canonical payloads carry no floats")
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("canonical object keys must be strings")
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def operation_digest(method: str, params: Mapping[str, Any]) -> str:
    """The digest `operation.register` binds. Excludes operationId, which does not exist until registration returns."""
    return sha256_digest(canonical_json({"method": method, "params": {k: to_wire(v) for k, v in params.items() if k != "operationId"}}))


_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CURSOR = re.compile(r"[A-Za-z0-9._:=-]{1,512}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FEATURE = re.compile(r"[a-z][a-z0-9_.]{0,63}")
_MIME = re.compile(r"[a-z0-9.+-]{1,64}/[a-z0-9.+-]{1,64}")
_FILE_NAME = re.compile(r"[^/\\\x00-\x1f\x7f]{1,255}")
# Every C0, DEL and C1 control but tab, LF, CR and NEL. U+009B can act as CSI, and no one types these.
_PROMPT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]")
_SURROGATE = re.compile("[\ud800-\udfff]")
_LINE_BREAK = re.compile("[\n\r\x85\u2028\u2029]")  # CR is Enter on some adapters, so it is never one line


def valid_request_id(value: Any) -> bool:
    return (type(value) is int and 0 <= value <= MAX_ID) or (type(value) is str and _OPAQUE.fullmatch(value) is not None)


def valid_opaque(value: Any) -> bool:
    return type(value) is str and _OPAQUE.fullmatch(value) is not None


def valid_count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_ID


def valid_digest(value: Any) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _valid_date(value: Any) -> bool:
    try:
        return type(value) is str and bool(_DATE.fullmatch(value)) and bool(date.fromisoformat(value))
    except ValueError:
        return False


def _utf8_size(value: Any) -> int | None:
    if type(value) is not str:
        return None
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:  # JSON can spell a lone surrogate
        return None


def _base64_size(value: Any) -> int | None:
    if type(value) is not str or len(value) > MAX_FRAME_BYTES:
        return None
    try:
        return len(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        return None


def _bounded_text(limit: int) -> Callable[[Any], bool]:
    return lambda value: (size := _utf8_size(value)) is not None and 0 < size <= limit


def _prompt(value: Any) -> bool:
    return _bounded_text(MAX_PROMPT_BYTES)(value) and _PROMPT_CONTROL.search(value) is None


def _bounded_base64(limit: int) -> Callable[[Any], bool]:
    return lambda value: (size := _base64_size(value)) is not None and 0 < size <= limit


def _one_of(*allowed: str) -> Callable[[Any], bool]:
    return lambda value: type(value) is str and value in allowed


def _int_between(low: int, high: int) -> Callable[[Any], bool]:
    return lambda value: type(value) is int and low <= value <= high


def _matches(pattern: re.Pattern[str]) -> Callable[[Any], bool]:
    return lambda value: type(value) is str and pattern.fullmatch(value) is not None


def _opaque_list(maximum: int) -> Callable[[Any], bool]:
    return lambda value: type(value) is list and len(value) <= maximum and len(set(map(str, value))) == len(value) \
        and all(valid_opaque(item) for item in value)


def _feature_list(value: Any) -> bool:
    return type(value) is list and len(value) <= 32 and all(_matches(_FEATURE)(item) for item in value)


def _registrable(value: Any) -> bool:
    spec = OPERATIONS.get(value) if type(value) is str else None
    return spec is not None and spec.correlation is Correlation.OPERATION


def _reason(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= MAX_REASON_CHARS and value.isprintable()


# One type per wire name, so a field means the same thing in every method.
_FIELDS: dict[str, Callable[[Any], bool]] = {
    **dict.fromkeys(("sessionId", "expectedRunId", "attachmentId", "operationId", "turnId", "decisionId", "choiceId",
                     "clientId", "registrationKey", "inputGrant", "geometryGrant", "uploadKey", "uploadId", "stagedId",
                     "planId", "model", "effort"), valid_opaque),
    **dict.fromkeys(("payloadDigest", "actionDigest", "digest", "chunkDigest", "planDigest"), valid_digest),
    **dict.fromkeys(("expectedRevision", "expectedConfigurationRevision", "expectedControlRevision", "eventSeq", "connectionSeq", "frameSeq",
                     "inputEpoch", "major", "minor", "offset"), valid_count),
    "cursor": _matches(_CURSOR),
    "limit": _int_between(1, HISTORY_MAX_ITEMS),
    "method": _registrable,
    "text": _prompt,
    "stagedIds": _opaque_list(MAX_MESSAGE_FILES),
    "ifBusy": _one_of("reject", "queue"),
    "reason": _reason,
    "data": _bounded_base64(MAX_TERMINAL_INPUT_BYTES),
    "chunk": _bounded_base64(MAX_UPLOAD_CHUNK_BYTES),
    "cols": _int_between(1, MAX_TERMINAL_DIMENSION),
    "rows": _int_between(1, MAX_TERMINAL_DIMENSION),
    "target": _one_of("chat", "terminal"),
    "view": _one_of("chat", "terminal"),
    "focused": lambda value: type(value) is bool,
    "features": _feature_list,
    "name": _matches(_FILE_NAME),
    "mimeType": _matches(_MIME),
    "size": _int_between(1, MAX_FILE_BYTES),
}


class Correlation(enum.StrEnum):
    """What makes a retry of this method safe. Only OPERATION consumes a registered operationId."""
    READ = "read"  # no state changes
    OPERATION = "operation"  # durable mutation, bound to a hub-issued operationId
    CAS = "cas"  # acts on another operation's record at an expected revision; its operationId is the target
    REGISTRATION = "registration"  # the caller's registrationKey
    UPLOAD = "upload"  # upload key, offset and digest
    CONNECTION = "connection"  # ephemeral attachment/presence change, ordered by connectionSeq
    FRAME = "frame"  # terminal stream, ordered by grant, epoch and frameSeq


@dataclass(frozen=True)
class OperationSpec:
    name: str
    correlation: Correlation
    required: frozenset[str]
    optional: frozenset[str] = frozenset()
    r1: bool = False
    feature: str | None = None  # negotiated optional feature this method needs
    capabilities: frozenset[str] = frozenset()  # adapter capabilities this method always needs
    needs_control: bool = False  # existing session-control permission; the hub enforces it
    check: Callable[[Mapping[str, Any]], bool] | None = None

    @property
    def wire_name(self) -> str:
        return METHOD_PREFIX + self.name


def _spec(name: str, correlation: Correlation, required: str, optional: str = "", *, r1: bool = True,
          feature: str | None = None, capabilities: str = "", control: bool = False,
          check: Callable[[Mapping[str, Any]], bool] | None = None) -> OperationSpec:
    return OperationSpec(name, correlation, frozenset(required.split()), frozenset(optional.split()), r1, feature,
                         frozenset(capabilities.split()), control, check)


_READ, _OP, _CONNECTION = Correlation.READ, Correlation.OPERATION, Correlation.CONNECTION
_ATTACHED = "sessionId attachmentId"
_DURABLE = "sessionId expectedRunId operationId"
_CONTROL = f"{_DURABLE} attachmentId expectedRevision"
OPERATIONS: dict[str, OperationSpec] = {spec.name: spec for spec in (
    _spec("initialize", _READ, "major minor features clientId"),
    _spec("session.attach", _CONNECTION, "sessionId clientId connectionSeq"),
    _spec("session.detach", _CONNECTION, f"{_ATTACHED} connectionSeq"),
    _spec("session.snapshot", _READ, "sessionId"),  # hub state; Snapshot.history reports the adapter
    _spec("history.page", _READ, "sessionId", "cursor limit", capabilities="history"),
    _spec("events.read", _READ, _ATTACHED, "cursor limit"),  # the hub's own log, needing no adapter
    _spec("events.ack", _READ, f"{_ATTACHED} eventSeq"),
    _spec("operation.register", Correlation.REGISTRATION, "sessionId registrationKey method expectedRunId payloadDigest"),
    _spec("operation.get", _READ, "sessionId operationId"),
    _spec("operation.cancel", Correlation.CAS, "sessionId operationId expectedRevision"),
    _spec("turn.submit", _OP, f"{_DURABLE} text expectedConfigurationRevision", "ifBusy stagedIds expectedControlRevision",
          capabilities="text", control=True),
    _spec("turn.steer", _OP, f"{_DURABLE} turnId text", r1=False, feature="steer", capabilities="steer", control=True),
    _spec("turn.interrupt", _OP, f"{_DURABLE} turnId", "expectedControlRevision", capabilities="interrupt", control=True),
    _spec("decision.reply", _OP, f"{_DURABLE} decisionId choiceId expectedRevision actionDigest", feature="decisions"),
    _spec("configuration.read", _READ, "sessionId"),  # turn.submit needs its revision
    _spec("configuration.request", _OP, f"{_DURABLE} expectedRevision", "model effort", r1=False,
          feature="configuration", control=True, check=lambda params: "model" in params or "effort" in params),
    _spec("control.claim", _OP, _CONTROL, feature="terminal", capabilities="terminal.input", control=True),
    _spec("control.take", _OP, f"{_CONTROL} reason", feature="terminal", capabilities="terminal.input", control=True),
    _spec("writer.take", _OP, f"{_CONTROL} reason view", feature="writer", capabilities="terminal.input", control=True),
    _spec("control.release", _OP, _CONTROL, feature="terminal", control=True),  # a holder can always let go
    _spec("terminal.attach", _CONNECTION, f"{_ATTACHED} connectionSeq", feature="terminal"),
    _spec("terminal.detach", _CONNECTION, f"{_ATTACHED} connectionSeq", feature="terminal"),
    _spec("terminal.input", Correlation.FRAME, f"{_ATTACHED} inputGrant inputEpoch frameSeq data", feature="terminal",
          capabilities="terminal.input"),
    _spec("terminal.resize", _CONNECTION, f"{_ATTACHED} connectionSeq geometryGrant cols rows", feature="terminal",
          capabilities="terminal.geometry"),
    _spec("terminal.drain", _READ, f"{_ATTACHED} inputGrant", feature="terminal", capabilities="terminal.input"),
    _spec("switch.prepare", _OP, f"{_DURABLE} target expectedRevision", feature="switch", control=True),
    _spec("switch.commit", _OP, f"{_DURABLE} planId planDigest expectedRevision", feature="switch", control=True),
    _spec("switch.recover", _OP, f"{_DURABLE} target expectedRevision reason", feature="switch", control=True),
    _spec("switch.cancel", _OP, f"{_DURABLE} planId", feature="switch", control=True),
    _spec("session.stop", _OP, _DURABLE, control=True),
    _spec("session.resume", _OP, f"{_DURABLE} expectedRevision", control=True),
    _spec("attachment.begin", Correlation.UPLOAD, "sessionId expectedRunId uploadKey name mimeType size digest",
          r1=False, feature="attachments"),
    _spec("attachment.chunk", Correlation.UPLOAD, "sessionId uploadId offset chunk chunkDigest", r1=False,
          feature="attachments"),
    _spec("attachment.finish", Correlation.UPLOAD, "sessionId uploadId digest", r1=False, feature="attachments"),
    _spec("attachment.get", _READ, "sessionId stagedId", r1=False, feature="attachments"),
    _spec("attachment.read", _READ, "sessionId stagedId", "offset", r1=False, feature="attachments"),
    _spec("attachment.remove", _OP, f"{_DURABLE} stagedId", r1=False, feature="attachments"),
    _spec("presence.update", _CONNECTION, f"{_ATTACHED} connectionSeq view focused"),
    _spec("security.read", _READ, "sessionId"),
)}
for _operation in OPERATIONS.values():
    assert _operation.required.isdisjoint(_operation.optional) and _operation.required | _operation.optional <= _FIELDS.keys()
R1_METHODS = frozenset(name for name, spec in OPERATIONS.items() if spec.r1)


@dataclass(frozen=True)
class Command:
    request_id: int | str
    spec: OperationSpec
    params: Mapping[str, Any]

    @property
    def method(self) -> str:
        return self.spec.name


def parse_command(raw: bytes) -> Command:
    """One client→hub request, or RpcError/RelayError. There are no batches and no client notifications."""
    value = decode_frame(raw)
    if not isinstance(value, dict) or value.keys() != {"jsonrpc", "id", "method", "params"} or value["jsonrpc"] != "2.0":
        raise RpcError(RpcCode.INVALID_REQUEST, value.get("id") if isinstance(value, dict) and valid_request_id(value.get("id")) else None)
    request_id, method, params = value["id"], value["method"], value["params"]
    if not valid_request_id(request_id) or type(method) is not str:
        raise RpcError(RpcCode.INVALID_REQUEST, request_id if valid_request_id(request_id) else None)
    spec = OPERATIONS.get(method.removeprefix(METHOD_PREFIX)) if method.startswith(METHOD_PREFIX) else None
    if spec is None:
        raise RpcError(RpcCode.METHOD_NOT_FOUND, request_id)
    # Unknown names are refused, which is also how a spoofed actor, account, provider, path or native ID stops here.
    if not isinstance(params, dict) or not spec.required <= params.keys() or not params.keys() <= spec.required | spec.optional \
            or not all(_FIELDS[name](item) for name, item in params.items()) or (spec.check and not spec.check(params)):
        raise RpcError(RpcCode.INVALID_PARAMS, request_id)
    return Command(request_id, spec, params)


def required_capabilities(command: Command) -> frozenset[str]:
    """What this command needs. A registration answers for the method it registers, so no receipt is stranded."""
    target = OPERATIONS[command.params["method"]] if command.method == "operation.register" else command.spec
    needed = command.spec.capabilities | target.capabilities
    if _LINE_BREAK.search(command.params.get("text", "")):
        needed |= {"multiline"}
    if command.params.get("ifBusy") == "queue":
        needed |= {"queue"}
    return needed


def admit(command: Command, negotiated: Negotiated, capabilities: Mapping[str, Support]) -> None:
    """Refuse what this contract does not offer. Passing is necessary and grants nothing: the hub still authorizes."""
    def refuse(code: ErrorCode = ErrorCode.UNSUPPORTED, **details: str) -> RelayError:
        return RelayError(code, details={"method": command.method, **details}, request_id=command.request_id)

    def refusal(name: str, support: Support | None) -> RelayError:
        return refuse(ErrorCode.POLICY_REFUSED if support is Support.POLICY_BLOCKED else ErrorCode.UNSUPPORTED, capability=name)

    # Registering a later method would strand a receipt for something that cannot run, so it answers for its target too.
    target = OPERATIONS[command.params["method"]] if command.method == "operation.register" else command.spec
    if target.name not in R1_METHODS:
        raise refuse()
    for feature in filter(None, (command.spec.feature, target.feature)):
        if feature not in negotiated.features:
            raise refuse(feature=feature)
    if command.params.get("ifBusy") == "queue" and "queue" not in negotiated.features:
        raise refuse(feature="queue")
    if command.params.get("stagedIds"):  # attachments are refused on every transport until R2
        raise refuse(feature="attachments")
    for name in sorted(required_capabilities(command)):
        if capabilities.get(name) is not Support.QUALIFIED:
            raise refusal(name, capabilities.get(name))
    if target.name == "decision.reply":
        # The kind is on the stored record, so one family must exist here; the hub then checks DECISION_CAPABILITIES[kind].
        family = sorted(DECISION_CAPABILITIES.values())
        if not any(capabilities.get(name) is Support.QUALIFIED for name in family):
            raise next((refusal(name, Support.POLICY_BLOCKED) for name in family if capabilities.get(name) is Support.POLICY_BLOCKED),
                       refusal(family[0], None))


@dataclass(frozen=True)
class Negotiated:
    major: int
    minor: int
    features: frozenset[str]


def negotiate(params: Mapping[str, Any], offered: frozenset[str] = R1_FEATURES) -> Negotiated:
    """Runs on every connection. An unknown major closes before attachment; a feature not named by both sides is off."""
    if params["major"] != PROTOCOL_MAJOR:
        raise RelayError(ErrorCode.VERSION_MISMATCH, details={"supportedMajor": PROTOCOL_MAJOR})
    return Negotiated(PROTOCOL_MAJOR, min(params["minor"], PROTOCOL_MINOR), frozenset(params["features"]) & offered & FEATURES)


def result_frame(request_id: int | str, result: Any) -> bytes:
    return encode_frame({"jsonrpc": "2.0", "id": request_id, "result": to_wire(result)})


def error_frame(error: RpcError | RelayError, request_id: int | str | None = None) -> bytes:
    request_id = request_id if request_id is not None else error.request_id
    return encode_frame({"jsonrpc": "2.0", "id": request_id, "error": error.to_wire()})


class ActorKind(enum.StrEnum):
    PERSON = "person"
    SERVICE = "service"


class Channel(enum.StrEnum):
    CHAT = "chat"
    TERMINAL = "terminal"
    INBOX = "inbox"
    PHONE = "phone"
    DESKTOP = "desktop"
    API = "api"
    TEAM = "team"
    SCHEDULE = "schedule"


@dataclass(frozen=True)
class Actor:
    """Server-derived from authentication and storage, never read from a command. Not a permission grant."""
    kind: ActorKind
    principal: str
    channel: Channel
    device: str | None = None


@dataclass(frozen=True)
class Binding:
    """Server-held launch mapping behind `bindingId`. No result carries one, so clients cannot substitute any field.

    `repr=False` also keeps these fields out of to_wire, should one reach it by mistake.
    """
    binding_id: str
    installation: str
    node_enrollment: str
    account: str = field(repr=False)
    workspace: str = field(repr=False)
    provider: str
    native_conversation: str = field(repr=False)
    launch_policy_digest: str


@dataclass(frozen=True)
class Fence:
    """Stale-leadership identities carried by grants. Epoch order survives restart; a new socket proves nothing."""
    authority_epoch: int
    broker_incarnation: str
    hub_incarnation: str


class ReceiptState(enum.StrEnum):
    QUEUED = "queued"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class FailureReason(enum.StrEnum):
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    RUN_ENDED = "run_ended"
    AUTHORITY_ENDED = "authority_ended"
    BUSY = "busy"
    LIMIT = "limit"
    POLICY_REFUSED = "policy_refused"
    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"  # lost a compare-and-set, such as a decision race or a contested take
    REJECTED = "rejected"


class EvidenceKind(enum.StrEnum):
    """The only conclusive evidence. An HTTP success, echoed text, tool result or RPC id has no member here."""
    PROVIDER_ADMISSION = "provider_admission"
    PROVIDER_REJECTION = "provider_rejection"
    BROKER_REFUSAL = "broker_refusal"  # proven never dispatched
    HUB_COMMIT = "hub_commit"  # the hub committed the transition; only for HUB_COMMIT_METHODS


# Durable methods no provider admits. Everything else needs provider admission, including any method not listed here.
# HUB_COMMIT is minted after the outcome, not at intent: Stop needs the run sealed, resume the new run assigned and
# switch.commit the verified new owner. Exit unknown stays uncertain.
PROVIDER_METHODS = frozenset({"turn.submit", "turn.steer", "turn.interrupt", "decision.reply", "configuration.request"})
HUB_COMMIT_METHODS = frozenset({"writer.take", "control.claim", "control.take", "control.release", "session.stop", "session.resume",
                                "switch.prepare", "switch.commit", "switch.cancel", "switch.recover", "attachment.remove"})


@dataclass(frozen=True)
class Evidence:
    kind: EvidenceKind
    operation_id: str
    run_id: str
    native_ref: str = field(default="", repr=False)  # private: to_wire omits it


@dataclass(frozen=True)
class Receipt:
    operation_id: str
    session_id: str
    run_id: str
    method: str
    payload_digest: str
    actor: Actor
    state: ReceiptState = ReceiptState.QUEUED
    revision: int = 0
    reason: FailureReason | None = None


_RECEIPT_MOVES = {
    ReceiptState.QUEUED: {ReceiptState.SUBMITTING, ReceiptState.FAILED},
    ReceiptState.SUBMITTING: {ReceiptState.ACCEPTED, ReceiptState.FAILED, ReceiptState.UNCERTAIN},
    ReceiptState.UNCERTAIN: {ReceiptState.ACCEPTED, ReceiptState.FAILED},  # never back to queued or submitting
    ReceiptState.ACCEPTED: set(),
    ReceiptState.FAILED: set(),
}
_UNSENT_REASONS = frozenset(FailureReason) - {FailureReason.REJECTED}


def advance_receipt(receipt: Receipt, state: ReceiptState, *, reason: FailureReason | None = None,
                    evidence: Evidence | None = None) -> Receipt:
    """The next receipt, or ValueError. Accepted, and failed once submission began, need evidence for this operation and run."""
    if state not in _RECEIPT_MOVES[receipt.state]:
        raise ValueError(f"receipt cannot move from {receipt.state} to {state}")
    if evidence is not None and (evidence.operation_id != receipt.operation_id or evidence.run_id != receipt.run_id):
        raise ValueError("evidence belongs to another operation or run")
    if state is ReceiptState.ACCEPTED:
        needed = EvidenceKind.HUB_COMMIT if receipt.method in HUB_COMMIT_METHODS else EvidenceKind.PROVIDER_ADMISSION
        if evidence is None or evidence.kind is not needed:
            raise ValueError(f"accepted requires {needed} evidence for this method")
    if state is ReceiptState.FAILED:
        if receipt.state is ReceiptState.QUEUED:  # nothing was sent, so nothing can be evidence of an outcome
            valid = reason in _UNSENT_REASONS and evidence is None
        else:
            valid = reason is FailureReason.REJECTED and evidence is not None \
                and evidence.kind in (EvidenceKind.PROVIDER_REJECTION, EvidenceKind.BROKER_REFUSAL)
        if not valid:
            raise ValueError("failed needs a specific reason, and conclusive evidence once submission began")
    elif reason is not None:
        raise ValueError("only failed carries a reason")
    return replace(receipt, state=state, reason=reason, revision=receipt.revision + 1)


def check_registered(receipt: Receipt, *, actor: Actor, session_id: str, run_id: str, method: str, payload_digest: str) -> None:
    """A retry must match its registration. Someone else's ID looks unknown; the owner's altered retry is a conflict."""
    if (receipt.actor.kind, receipt.actor.principal) != (actor.kind, actor.principal) or receipt.session_id != session_id:
        raise RelayError(ErrorCode.NOT_FOUND_OR_DENIED)
    if (receipt.run_id, receipt.method, receipt.payload_digest) != (run_id, method, payload_digest):
        raise RelayError(ErrorCode.INTEGRITY_FAILURE, operation_id=receipt.operation_id)


class Support(enum.StrEnum):
    QUALIFIED = "qualified"
    UNSUPPORTED = "unsupported"
    UNQUALIFIED = "unqualified"
    POLICY_BLOCKED = "policy_blocked"


class SupportLevel(enum.StrEnum):
    """Convenient labels, not a ladder: a level never proves a capability. Each is declared separately."""
    L0_READ_ONLY = "L0"
    L1_EXCLUSIVE_SWITCH = "L1"
    L2_STRUCTURED_SIDECAR = "L2"
    L3_SHARED_LIVE = "L3"


CAPABILITIES = frozenset({
    "history", "follow", "rich_tools", "text", "multiline", "image_input", "document_input", "model_modality",
    "queue", "steer", "interrupt", "decision.question", "decision.tool_approval", "configuration.model.current",
    "configuration.model.next", "configuration.effort.current", "configuration.effort.next", "shared_tui",
    "switch.to_chat", "switch.to_terminal", "reattach", "reconcile", "terminal.input", "terminal.geometry",
})


@dataclass(frozen=True)
class CapabilityEntry:
    status: Support
    reason: str = ""
    limits: Mapping[str, int] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.evidence) is not tuple or not all(type(item) is str for item in self.evidence):
            raise ValueError("capability evidence must be a tuple of strings")


@dataclass(frozen=True)
class CapabilityDeclaration:
    """One adapter's qualification record for an exact CLI, transport and placement. No blanket `supports_chat`."""
    provider: str
    cli_version: str
    cli_digest: str
    adapter_version: str
    schema_version: str
    transport: str
    qualification_id: str
    qualification_date: str
    scope: Mapping[str, str]
    operations: Mapping[str, CapabilityEntry]

    def __post_init__(self) -> None:
        if not self.operations.keys() <= CAPABILITIES:
            raise ValueError("unknown capability")
        if any(entry.status is Support.QUALIFIED for entry in self.operations.values()):
            pinned = all((self.provider, self.cli_version, self.adapter_version, self.schema_version, self.transport,
                          self.qualification_id)) and valid_digest(self.cli_digest) and _valid_date(self.qualification_date)
            if not pinned or not all(entry.evidence for entry in self.operations.values() if entry.status is Support.QUALIFIED):
                raise ValueError("a qualified capability needs a full pin and recorded evidence")

    def support(self, capability: str) -> Support:
        entry = self.operations.get(capability)
        return entry.status if entry else Support.UNSUPPORTED

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> CapabilityDeclaration:
        """A declaration from JSON, or ValueError."""
        try:
            texts = [value[name] for name in ("provider", "cliVersion", "cliDigest", "adapterVersion", "schemaVersion", "transport",
                                              "qualificationId", "qualificationDate")]
            if not all(type(text) is str for text in texts):
                raise ValueError("capability pin fields must be strings")
            operations = {}
            for name, entry in value["operations"].items():
                evidence = entry.get("evidence", [])
                if type(evidence) is not list:  # tuple() would split a bare string into characters
                    raise ValueError("capability evidence must be a list")
                operations[name] = CapabilityEntry(Support(entry["status"]), entry.get("reason", ""), entry.get("limits", {}), tuple(evidence))
            return cls(*texts, dict(value["scope"]), operations)
        except (KeyError, TypeError, AttributeError):
            raise ValueError("malformed capability declaration") from None


# The only declaration M0 ships: everything refused.
NO_CAPABILITIES = CapabilityDeclaration("none", "", "", "", "", "none", "", "", {}, {})


def effective_capabilities(declaration: CapabilityDeclaration, *, live: Iterable[str] = (), placement: Iterable[str] = (),
                           actor: Iterable[str] = (), policy_blocked: Iterable[str] = ()) -> dict[str, Support]:
    """Runtime capability is the intersection of the record and every live input; anything missing lowers support."""
    live, placement, actor, policy_blocked = set(live), set(placement), set(actor), set(policy_blocked)
    result = {}
    for name in CAPABILITIES:
        support = declaration.support(name)
        if name in policy_blocked:
            support = Support.POLICY_BLOCKED
        elif support is Support.QUALIFIED and not (name in live and name in placement and name in actor):
            support = Support.UNSUPPORTED
        result[name] = support
    return result


class DecisionKind(enum.StrEnum):
    QUESTION = "question"
    TOOL_APPROVAL = "tool_approval"
    PRIVILEGED = "privileged"  # linked to its existing workflow; never answerable here


class ChoiceEffect(enum.StrEnum):
    """One-time only. There is deliberately no remembered or wider grant to pick."""
    ANSWER = "answer"
    ALLOW_ONCE = "allow_once"
    DENY = "deny"


class DecisionStatus(enum.StrEnum):
    PENDING = "pending"
    RESERVED = "reserved"  # Answer recorded — applying
    OUTCOME_UNKNOWN = "outcome_unknown"  # Answer recorded — outcome unknown
    ANSWERED = "answered"
    DENIED = "denied"
    INVALIDATED = "invalidated"  # proven pre-dispatch revocation: fresh review, not the next answer
    EXPIRED = "expired"
    CANCELLED = "cancelled"


_DECISION_MOVES = {
    DecisionStatus.PENDING: {DecisionStatus.RESERVED, DecisionStatus.EXPIRED, DecisionStatus.CANCELLED},
    DecisionStatus.RESERVED: {DecisionStatus.ANSWERED, DecisionStatus.DENIED, DecisionStatus.OUTCOME_UNKNOWN,
                              DecisionStatus.INVALIDATED},
    DecisionStatus.OUTCOME_UNKNOWN: {DecisionStatus.ANSWERED, DecisionStatus.DENIED, DecisionStatus.INVALIDATED},
}


# Publishes the hub's exact check once a record is loaded. A kind missing here has no capability: privileged never answers.
DECISION_CAPABILITIES = {DecisionKind.QUESTION: "decision.question", DecisionKind.TOOL_APPROVAL: "decision.tool_approval"}


@dataclass(frozen=True)
class DecisionChoice:
    id: str
    label: str
    effect: ChoiceEffect


@dataclass(frozen=True)
class Decision:
    """A pending record's contract fields. The native request mapping stays private to the run, outside this type."""
    decision_id: str
    session_id: str
    run_id: str
    kind: DecisionKind
    action_digest: str
    description: str
    choices: tuple[DecisionChoice, ...]
    expires_at_ms: int
    policy_revision: int
    revision: int = 0
    status: DecisionStatus = DecisionStatus.PENDING
    winner: Actor | None = None
    choice_id: str | None = None


def reserve_answer(decision: Decision, *, actor: Actor, run_id: str, policy_revision: int, choice_id: str, expected_revision: int,
                   action_digest: str, now_ms: int) -> Decision:
    """The first valid answer's compare-and-set. Call after rechecking authority, and persist with the same revision guard.

    `run_id` is the session's current run, and the hub first checks the command's expected run against it; a stale client
    could otherwise reserve on an old record. `policy_revision` is also the current one. Decisions never migrate between runs.
    Any refusal leaves the winner slot untouched. A late reply hears DECISION_RESOLVED before any other check.
    """
    def refuse(code: ErrorCode) -> RelayError:
        return RelayError(code, state_revision=decision.revision)

    if decision.status is DecisionStatus.EXPIRED:
        raise refuse(ErrorCode.DECISION_EXPIRED)
    if decision.status is not DecisionStatus.PENDING:
        raise refuse(ErrorCode.DECISION_RESOLVED)
    if run_id != decision.run_id:
        raise refuse(ErrorCode.STALE_RUN)
    if now_ms >= decision.expires_at_ms:
        raise refuse(ErrorCode.DECISION_EXPIRED)
    if decision.kind is DecisionKind.PRIVILEGED or actor.kind is not ActorKind.PERSON:
        raise refuse(ErrorCode.POLICY_REFUSED)
    if expected_revision != decision.revision or action_digest != decision.action_digest or policy_revision != decision.policy_revision:
        raise refuse(ErrorCode.REVISION_CONFLICT)
    if choice_id not in {choice.id for choice in decision.choices}:
        raise refuse(ErrorCode.POLICY_REFUSED)
    return replace(decision, status=DecisionStatus.RESERVED, revision=decision.revision + 1, winner=actor, choice_id=choice_id)


def advance_decision(decision: Decision, status: DecisionStatus) -> Decision:
    if status not in _DECISION_MOVES.get(decision.status, ()) or status is DecisionStatus.RESERVED:
        raise ValueError(f"decision cannot move from {decision.status} to {status}")
    return replace(decision, status=status, revision=decision.revision + 1)


@dataclass(frozen=True)
class ControlBinding:
    """Who held terminal input over one half-open watermark range. Retained until every action in it is correlated."""
    actor: Actor
    attachment_id: str
    input_epoch: int
    control_revision: int
    from_watermark: int
    until_watermark: int | None = None  # None while still held


def control_handover(current: ControlBinding, *, actor: Actor, attachment_id: str, control_revision: int,
                     until_watermark: int) -> tuple[ControlBinding, ControlBinding]:
    """The closed binding and its successor. Every holder change bumps the epoch, and frames restart at 1 inside it.

    A watermark means nothing without its epoch: "processed 5" could otherwise be either side of a takeover.
    """
    return (replace(current, until_watermark=until_watermark),
            ControlBinding(actor, attachment_id, current.input_epoch + 1, control_revision, 1))


@dataclass(frozen=True)
class NativeAction:
    """A native TUI request, located by the drained input watermark that produced it."""
    native_ref: str = field(repr=False)
    input_epoch: int
    watermark: int


def attribute_native_action(action: NativeAction, bindings: Iterable[ControlBinding]) -> ControlBinding:
    """The controller whose input produced this action, not whoever holds control as it arrives.

    Without a binding the action is refused rather than attributed. Takeover and switch wait on this boundary.
    """
    for binding in bindings:
        if binding.input_epoch == action.input_epoch and binding.from_watermark <= action.watermark \
                and (binding.until_watermark is None or action.watermark < binding.until_watermark):
            return binding
    raise RelayError(ErrorCode.INPUT_CONFLICT, details={"reason": "unattributed"})


class SwitchState(enum.StrEnum):
    OWNED = "owned"
    PREPARED = "prepared"
    DRAINING = "draining"
    STOPPING = "stopping"
    STARTING = "starting"
    RECOVERY_REQUIRED = "recovery_required"


SWITCH_MOVES: dict[SwitchState, frozenset[SwitchState]] = {
    SwitchState.OWNED: frozenset({SwitchState.PREPARED}),
    SwitchState.PREPARED: frozenset({SwitchState.OWNED, SwitchState.DRAINING}),
    SwitchState.DRAINING: frozenset({SwitchState.OWNED, SwitchState.STOPPING}),
    SwitchState.STOPPING: frozenset({SwitchState.STARTING, SwitchState.RECOVERY_REQUIRED}),
    SwitchState.STARTING: frozenset({SwitchState.OWNED, SwitchState.RECOVERY_REQUIRED}),
    SwitchState.RECOVERY_REQUIRED: frozenset(),  # explicit recovery only, never an automatic restart
}
_VIEWS = ("chat", "terminal")
SWITCH_BLOCKERS = frozenset({"active_turn", "background_tool", "pending_decision", "login_or_trust_prompt", "terminal_draft",
                             "queued_send", "uncertain_operation"})


class SelectorAction(enum.StrEnum):
    VIEW_ONLY = "view_only"  # presentation change: no control, process or conversation effect
    SWITCH = "switch"  # L1: this one selection is prepare + commit, with no dialog
    PENDING = "pending"  # a switch is running; a repeat click shares that operation
    REFUSED = "refused"  # shown inline; never queued for later


@dataclass(frozen=True)
class SelectorResult:
    action: SelectorAction
    reason: str | None = None


def select_view(*, shared_live: bool, owner: str, target: str, idle_verified: bool, direction_qualified: bool = False,
                may_control: bool = False, blockers: Iterable[str] = (), switch_state: SwitchState = SwitchState.OWNED) -> SelectorResult:
    """What one deliberate Chat/Terminal tab selection does. Opens, restores, deep links and defaults never come here.

    Takes facts rather than a support level, since a level is not a ladder: a Claude L2 session can still own an L1 switch.
    An empty blocker list is not idle: only `idle_verified`, from positive evidence, is. Only activation needs control;
    the non-owning view stays readable, with its limit shown.
    """
    if owner not in _VIEWS or target not in _VIEWS:
        return SelectorResult(SelectorAction.REFUSED, "unknown_state")
    if shared_live or target == owner:
        return SelectorResult(SelectorAction.VIEW_ONLY)
    if not direction_qualified:
        return SelectorResult(SelectorAction.VIEW_ONLY, "direction_unqualified")
    if not may_control:
        return SelectorResult(SelectorAction.VIEW_ONLY, "not_controller")
    if switch_state is SwitchState.RECOVERY_REQUIRED:
        return SelectorResult(SelectorAction.REFUSED, "recovery_required")
    if switch_state is not SwitchState.OWNED:
        return SelectorResult(SelectorAction.PENDING)
    blocking = sorted(set(blockers))
    if not set(blocking) <= SWITCH_BLOCKERS:  # an unknown blocker is not idle evidence
        return SelectorResult(SelectorAction.REFUSED, "unknown_state")
    if blocking:
        return SelectorResult(SelectorAction.REFUSED, blocking[0])
    return SelectorResult(SelectorAction.SWITCH) if idle_verified else SelectorResult(SelectorAction.REFUSED, "unknown_state")


@dataclass(frozen=True)
class Snapshot:
    """A consistent authorized view at `watermark`: the result of `session.snapshot` and the reconnect baseline.

    Hub state is always here. `history` reports the adapter separately, because unsupported history is not an empty one.
    """
    session_id: str
    run_id: str | None  # None with no live run
    history_epoch: int
    watermark: int  # replay events after this eventSeq
    control_revision: int
    configuration_revision: int
    capabilities: Mapping[str, Support]
    history: Support
    pending_decisions: tuple[Decision, ...] = ()
    receipts: tuple[Receipt, ...] = ()
    controller: Actor | None = None
    writer: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AttachResult:
    """`session.attach`: an observer attachment, granting no control."""
    attachment_id: str
    snapshot: Snapshot


@dataclass(frozen=True)
class RegistrationResult:
    """`operation.register`. Repeating a key returns the same registration, with `existing` set; no provider effect."""
    operation_id: str
    receipt: Receipt
    existing: bool = False

    def __post_init__(self) -> None:
        if self.operation_id != self.receipt.operation_id:
            raise ValueError("registration and receipt disagree")


GRANTING_METHODS = frozenset({"control.claim", "control.take"})


@dataclass(frozen=True, repr=False)
class ControlResult:
    """`control.claim`, `take` and `release`, and the only source of the stream grant `terminal.input` must send.

    Every holder change bumps the epoch, and frames in a new epoch start at frameSeq 1. Only an accepted claim or take
    carries a grant; a release, or a claim that did not win, has none.
    """
    receipt: Receipt
    control_revision: int
    holder: Actor | None = None
    input_grant: str | None = None
    input_epoch: int | None = None
    geometry_grant: str | None = None

    def __post_init__(self) -> None:
        if (self.input_grant is None) != (self.input_epoch is None):
            raise ValueError("an input grant and its epoch travel together")
        if self.input_grant is not None or self.geometry_grant is not None:
            if self.holder is None:
                raise ValueError("a grant needs a holder")
            if self.receipt.state is not ReceiptState.ACCEPTED or self.receipt.method not in GRANTING_METHODS:
                raise ValueError("only an accepted claim or take carries a grant")

    def __repr__(self) -> str:  # grants travel in to_wire but stay out of logs
        return f"ControlResult(receipt={self.receipt!r}, control_revision={self.control_revision}, holder={self.holder!r})"


DURABLE_EVENTS = frozenset({
    "session.stateChanged", "run.changed", "capabilities.changed", "security.changed", "operation.changed",
    "queue.changed", "turn.started", "turn.completed", "turn.interrupted", "turn.failed", "message.upsert",
    "message.delta", "tool.changed", "plan.changed", "decision.requested", "decision.changed", "decision.resolved",
    "configuration.changed", "control.changed", "switch.changed", "attachment.changed", "history.reset", "history.gap",
})
# Attachment-local and never replayed. A snapshot is the baseline at a watermark, not an entry in the log.
TRANSIENT_EVENTS = frozenset({
    "session.snapshot", "presence.changed", "terminal.output", "terminal.ack", "terminal.closed", "connection.state",
    "flow.slow", "access.revoked", "notice",
})


TERMINAL_ACK_FIELDS = frozenset({"epoch", "admitted", "processed"})  # a watermark means nothing outside its epoch


@dataclass(frozen=True)
class Event:
    kind: str
    session_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema: int = EVENT_SCHEMA
    event_seq: int | None = None  # durable: per-session order
    history_epoch: int | None = None
    attachment_seq: int | None = None  # transient: per-attachment order
    run_id: str | None = None
    actor: Actor | None = None
    operation_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    decision_id: str | None = None
    revision: int | None = None
    redacted: bool = False
    gap: bool = False

    def __post_init__(self) -> None:
        if self.kind in DURABLE_EVENTS:
            valid = valid_count(self.event_seq) and valid_count(self.history_epoch) and self.attachment_seq is None
        elif self.kind in TRANSIENT_EVENTS:
            valid = valid_count(self.attachment_seq) and self.event_seq is None
            if self.kind == "terminal.ack":
                valid = valid and self.payload.keys() == TERMINAL_ACK_FIELDS and all(map(valid_count, self.payload.values()))
        else:
            valid = False
        if not valid:
            raise ValueError("event kind and sequence do not match")


def event_frame(event: Event) -> bytes:
    return encode_frame({"jsonrpc": "2.0", "method": EVENT_METHOD, "params": to_wire(event)})


@dataclass(frozen=True)
class UnsupportedEvent:
    """Show an unsupported notice and resnapshot if needed. Never infer an action from it."""
    resnapshot: bool = True


def read_event(params: Mapping[str, Any]) -> Event | UnsupportedEvent:
    """Client-side reader. A newer kind, schema or malformed event degrades rather than acts."""
    try:
        if params["schema"] != EVENT_SCHEMA or params["kind"] not in DURABLE_EVENTS | TRANSIENT_EVENTS:
            return UnsupportedEvent()
        actor = params.get("actor")
        return Event(params["kind"], params["sessionId"], params.get("payload", {}), params["schema"],
                     params.get("eventSeq"), params.get("historyEpoch"), params.get("attachmentSeq"), params.get("runId"),
                     Actor(ActorKind(actor["kind"]), actor["principal"], Channel(actor["channel"]), actor.get("device")) if actor else None,
                     params.get("operationId"), params.get("turnId"), params.get("itemId"), params.get("decisionId"),
                     params.get("revision"), params.get("redacted", False), params.get("gap", False))
    except (KeyError, TypeError, ValueError):
        return UnsupportedEvent()


FIXTURE_SCHEMA = "rynhult.session-relay.fixture/1"
FIXTURE_CASE_FIELDS: dict[str, frozenset[str]] = {
    "commands": frozenset({"name", "frame", "expect"}),
    "digests": frozenset({"name", "method", "params", "digest"}),
    "errors": frozenset({"name", "code", "retry"}),
    "receipts": frozenset({"name", "from", "to", "valid"}),
    "decisions": frozenset({"name", "reply", "expect"}),
    "capabilities": frozenset({"name", "declaration", "expect"}),
    "selector": frozenset({"name", "state", "expect"}),
    "events": frozenset({"name", "params", "expect"}),
    "results": frozenset({"name", "wire"}),
}


def validate_fixture(value: Any) -> list[dict]:
    """The cases of a checked-in fixture file: `{schema, kind, cases: [...]}`, language neutral for B and C."""
    if not isinstance(value, dict) or value.get("schema") != FIXTURE_SCHEMA or value.get("kind") not in FIXTURE_CASE_FIELDS \
            or not isinstance(value.get("cases"), list) or not value["cases"]:
        raise ValueError("not a session relay fixture")
    required = FIXTURE_CASE_FIELDS[value["kind"]]
    names = [case.get("name") for case in value["cases"] if isinstance(case, dict)]
    if len(names) != len(value["cases"]) or len(set(names)) != len(names) or not all(required <= case.keys() for case in value["cases"]):
        raise ValueError("fixture cases need unique names and their required fields")
    return value["cases"]
