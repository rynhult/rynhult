"""The session relay contract: every other relay package builds on what these pin.

Language-neutral cases live in tests/fixtures/session_relay/ so the browser types can reuse them. Python-only
facts, such as bounds that would make a fixture huge, stay here.
"""
import ast
import asyncio
import base64 as b64
import hashlib
import inspect
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from acc.session_relay import protocol as p
from acc.session_relay.adapters import base

FIXTURES = Path(__file__).parent / "fixtures" / "session_relay"
PERSON = p.Actor(p.ActorKind.PERSON, "person-1", p.Channel.CHAT)
OTHER = p.Actor(p.ActorKind.PERSON, "person-2", p.Channel.PHONE)
SERVICE = p.Actor(p.ActorKind.SERVICE, "team-1", p.Channel.TEAM)
EVERYTHING = {name: p.Support.QUALIFIED for name in p.CAPABILITIES}
R1 = p.Negotiated(1, 0, p.R1_FEATURES)
DIGEST = "sha256:" + "a" * 64


def cases(kind):
    return p.validate_fixture(json.loads((FIXTURES / f"{kind}.json").read_text(encoding="utf-8")))


def each(kind):
    return pytest.mark.parametrize("case", cases(kind), ids=lambda case: case["name"])


def frame(value):
    return value.encode() if isinstance(value, str) else json.dumps(value).encode()


def command(method, /, **params):
    return p.parse_command(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ry/" + method, "params": params}).encode())


def turn(**overrides):
    return command("turn.submit", **{"sessionId": "s", "expectedRunId": "r", "operationId": "o", "text": "hello",
                                     "expectedConfigurationRevision": 1, **overrides})


def error_data(raw):
    return json.loads(raw)["error"]["data"]


@each("commands")
def test_command_fixture(case):
    expect = case["expect"]
    if "rpc" in expect:
        with pytest.raises(p.RpcError) as refused:
            p.parse_command(frame(case["frame"]))
        assert (int(refused.value.code), refused.value.request_id) == (expect["rpc"], expect["id"])
        return
    parsed = p.parse_command(frame(case["frame"]))
    assert parsed.method == expect["method"]
    if "capabilities" in expect:
        assert p.required_capabilities(parsed) == set(expect["capabilities"])
    if expect["admit"] == "ok":
        p.admit(parsed, R1, EVERYTHING)
    else:
        with pytest.raises(p.RelayError) as refused:
            p.admit(parsed, R1, EVERYTHING)
        assert refused.value.code == expect["admit"]


def test_allowlist_is_exactly_the_design_surface():
    # Copied from the design's operation table on purpose: a new method is a contract change, not a table edit.
    assert set(p.OPERATIONS) == {
        "initialize", "session.attach", "session.detach", "session.snapshot", "history.page", "events.read", "events.ack",
        "operation.register", "operation.get", "operation.cancel", "turn.submit", "turn.steer", "turn.interrupt",
        "decision.reply", "configuration.read", "configuration.request", "control.claim", "control.take", "writer.take", "control.release",
        "terminal.attach", "terminal.detach", "terminal.input", "terminal.resize", "terminal.drain", "switch.prepare",
        "switch.commit", "switch.cancel", "switch.recover", "session.stop", "session.resume", "attachment.begin", "attachment.chunk",
        "attachment.finish", "attachment.get", "attachment.read", "attachment.remove", "presence.update", "security.read"}
    assert set(p.OPERATIONS) - p.R1_METHODS == {
        "turn.steer", "configuration.request", "attachment.begin",
        "attachment.chunk", "attachment.finish", "attachment.get", "attachment.read", "attachment.remove"}
    assert {case["expect"]["method"] for case in cases("commands") if case["name"].startswith("valid ")} == set(p.OPERATIONS)


def test_durable_mutations_bind_a_run_and_an_operation():
    for spec in p.OPERATIONS.values():
        if spec.correlation is p.Correlation.OPERATION:
            assert {"sessionId", "expectedRunId", "operationId"} <= spec.required, spec.name
        else:
            assert "operationId" not in spec.required or spec.correlation in (p.Correlation.READ, p.Correlation.CAS), spec.name


def test_events_are_exactly_the_design_surface():
    # Copied from the design's event table on purpose, like the operations above.
    assert p.DURABLE_EVENTS == {
        "session.stateChanged", "run.changed", "capabilities.changed", "security.changed", "operation.changed", "queue.changed",
        "turn.started", "turn.completed", "turn.interrupted", "turn.failed", "message.upsert", "message.delta", "tool.changed",
        "plan.changed", "decision.requested", "decision.changed", "decision.resolved", "configuration.changed", "control.changed",
        "switch.changed", "attachment.changed", "history.reset", "history.gap"}
    assert p.TRANSIENT_EVENTS == {
        "session.snapshot", "presence.changed", "terminal.output", "terminal.ack", "terminal.closed", "connection.state", "flow.slow",
        "access.revoked", "notice"}


def test_control_permission_methods_are_pinned():
    # Nothing reads this flag at M0, and the hub will rely on it.
    assert {name for name, spec in p.OPERATIONS.items() if spec.needs_control} == {
        "turn.submit", "turn.steer", "turn.interrupt", "configuration.request", "control.claim", "control.take", "writer.take", "control.release",
        "switch.prepare", "switch.commit", "switch.cancel", "switch.recover", "session.stop", "session.resume"}


def test_every_durable_method_declares_who_settles_it():
    durable = {name for name, spec in p.OPERATIONS.items() if spec.correlation is p.Correlation.OPERATION}
    assert p.PROVIDER_METHODS | p.HUB_COMMIT_METHODS == durable
    assert p.PROVIDER_METHODS.isdisjoint(p.HUB_COMMIT_METHODS)


@pytest.mark.parametrize("hello, features", [
    (dict(major=1, minor=0, features=[]), set()),
    (dict(major=1, minor=0, features=["queue", "steer", "from_the_future"]), {"queue"}),
    (dict(major=1, minor=9, features=["terminal"]), {"terminal"}),
])
def test_negotiation_offers_only_what_both_sides_name(hello, features):
    negotiated = p.negotiate(hello)
    assert (negotiated.major, negotiated.minor, negotiated.features) == (1, 0, features)


@pytest.mark.parametrize("major", [0, 2])
def test_unsupported_major_refuses_before_attachment(major):
    with pytest.raises(p.RelayError) as refused:
        p.negotiate(dict(major=major, minor=0, features=[]))
    assert refused.value.code is p.ErrorCode.VERSION_MISMATCH
    assert refused.value.details == {"supportedMajor": 1}


@pytest.mark.parametrize("method, feature", [("control_claim", "terminal"), ("reply", "decisions"), ("queue", "queue")])
def test_feature_left_out_of_negotiation_is_refused(method, feature):
    built = {
        "control_claim": lambda: command("control.claim", sessionId="s", expectedRunId="r", operationId="o", attachmentId="a", expectedRevision=1),
        "reply": lambda: command("decision.reply", sessionId="s", expectedRunId="r", operationId="o", decisionId="d", choiceId="c", expectedRevision=1, actionDigest=DIGEST),
        "queue": lambda: turn(ifBusy="queue"),
    }[method]()
    with pytest.raises(p.RelayError) as refused:
        p.admit(built, p.Negotiated(1, 0, frozenset()), EVERYTHING)
    assert refused.value.code is p.ErrorCode.UNSUPPORTED and refused.value.details["feature"] == feature


def test_r1_refuses_later_methods_even_when_every_feature_and_capability_is_offered():
    offered = p.Negotiated(1, 0, p.FEATURES)
    refused_methods = set()
    for case in cases("commands"):
        if case["name"].startswith("valid ") and case["expect"]["method"] not in p.R1_METHODS:
            with pytest.raises(p.RelayError) as refused:
                p.admit(p.parse_command(frame(case["frame"])), offered, EVERYTHING)
            assert refused.value.code is p.ErrorCode.UNSUPPORTED and "feature" not in refused.value.details
            refused_methods.add(case["expect"]["method"])
    assert refused_methods == set(p.OPERATIONS) - p.R1_METHODS


def test_registering_a_later_method_is_refused_even_when_its_feature_is_offered():
    registered = command("operation.register", sessionId="s", registrationKey="k", method="turn.steer", expectedRunId="r", payloadDigest=DIGEST)
    with pytest.raises(p.RelayError) as refused:
        p.admit(registered, p.Negotiated(1, 0, p.FEATURES), EVERYTHING)
    assert refused.value.code is p.ErrorCode.UNSUPPORTED


def test_registration_cannot_reach_around_negotiation():
    registered = command("operation.register", sessionId="s", registrationKey="k", method="decision.reply", expectedRunId="r", payloadDigest=DIGEST)
    with pytest.raises(p.RelayError) as refused:
        p.admit(registered, p.Negotiated(1, 0, frozenset()), EVERYTHING)
    assert refused.value.details["feature"] == "decisions"


@pytest.mark.parametrize("params, needed", [
    (dict(), {"text"}),
    (dict(text="one\ntwo"), {"text", "multiline"}),
    (dict(ifBusy="queue"), {"text", "queue"}),
])
def test_turn_capabilities_follow_what_it_actually_asks(params, needed):
    assert p.required_capabilities(turn(**params)) == needed


def test_nothing_with_a_capability_passes_the_stub():
    capabilities = p.effective_capabilities(p.NO_CAPABILITIES, live=p.CAPABILITIES, placement=p.CAPABILITIES, actor=p.CAPABILITIES)
    assert set(capabilities.values()) == {p.Support.UNSUPPORTED}
    for mutation in (turn(), command("turn.interrupt", sessionId="s", expectedRunId="r", operationId="o", turnId="t")):
        with pytest.raises(p.RelayError) as refused:
            p.admit(mutation, R1, capabilities)
        assert refused.value.code is p.ErrorCode.UNSUPPORTED and "capability" in refused.value.details


def test_policy_blocked_is_a_policy_refusal():
    with pytest.raises(p.RelayError) as refused:
        p.admit(turn(), R1, {"text": p.Support.POLICY_BLOCKED})
    assert refused.value.code is p.ErrorCode.POLICY_REFUSED


@pytest.mark.parametrize("build", [
    lambda: command("session.snapshot", sessionId="s"),
    lambda: command("events.read", sessionId="s", attachmentId="a"),
    lambda: command("control.release", sessionId="s", expectedRunId="r", operationId="o", attachmentId="a", expectedRevision=1),
], ids=["snapshot", "events", "release"])
def test_hub_state_and_letting_go_need_no_adapter_capability(build):
    p.admit(build(), R1, {})


def test_history_page_still_needs_the_adapter():
    with pytest.raises(p.RelayError):
        p.admit(command("history.page", sessionId="s"), R1, {})


def test_release_keeps_its_feature():
    release = command("control.release", sessionId="s", expectedRunId="r", operationId="o", attachmentId="a", expectedRevision=1)
    with pytest.raises(p.RelayError) as refused:
        p.admit(release, p.Negotiated(1, 0, frozenset()), {})
    assert refused.value.details["feature"] == "terminal"


@pytest.mark.parametrize("support", [p.Support.UNQUALIFIED, p.Support.UNSUPPORTED])
def test_only_qualified_passes_admission(support):
    with pytest.raises(p.RelayError) as refused:
        p.admit(turn(), R1, {"text": support})
    assert refused.value.code is p.ErrorCode.UNSUPPORTED and refused.value.details["capability"] == "text"


def register(method):
    return command("operation.register", sessionId="s", registrationKey="k", method=method, expectedRunId="r", payloadDigest=DIGEST)


def reply():
    return command("decision.reply", sessionId="s", expectedRunId="r", operationId="o", decisionId="d", choiceId="c", expectedRevision=1, actionDigest=DIGEST)


@pytest.mark.parametrize("method, capability", [("turn.submit", "text"), ("turn.interrupt", "interrupt"), ("control.claim", "terminal.input")])
def test_registration_answers_for_its_target_capabilities(method, capability):
    with pytest.raises(p.RelayError) as refused:
        p.admit(register(method), R1, {})
    assert refused.value.code is p.ErrorCode.UNSUPPORTED and refused.value.details["capability"] == capability
    p.admit(register(method), R1, {capability: p.Support.QUALIFIED})


@pytest.mark.parametrize("method", ["session.stop", "session.resume"])
def test_stop_and_resume_need_no_capability(method):
    p.admit(register(method), R1, {})
    p.admit(command(method, sessionId="s", expectedRunId="r", operationId="o", **({"expectedRevision": 1} if method == "session.resume" else {})), R1, {})


@pytest.mark.parametrize("build", [reply, lambda: register("decision.reply")], ids=["reply", "registration"])
def test_decision_needs_one_qualified_family(build):
    with pytest.raises(p.RelayError) as refused:
        p.admit(build(), R1, {})
    assert refused.value.code is p.ErrorCode.UNSUPPORTED and refused.value.details["capability"] == "decision.question"
    with pytest.raises(p.RelayError) as refused:
        p.admit(build(), R1, {"decision.tool_approval": p.Support.UNQUALIFIED, "decision.question": p.Support.UNSUPPORTED})
    assert refused.value.code is p.ErrorCode.UNSUPPORTED
    with pytest.raises(p.RelayError) as refused:
        p.admit(build(), R1, {"decision.tool_approval": p.Support.POLICY_BLOCKED})
    assert refused.value.code is p.ErrorCode.POLICY_REFUSED and refused.value.details["capability"] == "decision.tool_approval"
    for family in p.DECISION_CAPABILITIES.values():
        p.admit(build(), R1, {family: p.Support.QUALIFIED})


def test_decision_kind_capabilities_are_published():
    assert {kind.value: name for kind, name in p.DECISION_CAPABILITIES.items()} == {
        "question": "decision.question", "tool_approval": "decision.tool_approval"}
    assert p.DecisionKind.PRIVILEGED not in p.DECISION_CAPABILITIES


@pytest.mark.parametrize("valid", [p.valid_opaque, p.valid_request_id])
@pytest.mark.parametrize("value", ["abc/def", "abc def", "abc\n", "-abc", "", "a" * 129])
def test_ids_must_match_whole(valid, value):
    assert not valid(value)


@pytest.mark.parametrize("value", ["sha256:" + "a" * 64 + "x", "sha256:" + "a" * 64 + "\n", "xsha256:" + "a" * 64])
def test_digests_must_match_whole(value):
    assert not p.valid_digest(value)


def test_counts_stop_at_the_browser_safe_integer():
    assert p.valid_count(p.MAX_ID) and p.MAX_ID == (1 << 53) - 1
    assert not p.valid_count(p.MAX_ID + 1)
    assert not p.valid_count(True)


def take(reason):
    return command("control.take", sessionId="s", expectedRunId="r", operationId="o", attachmentId="a", expectedRevision=1, reason=reason)


def test_take_reason_is_bounded_and_one_printable_line():
    assert take("x" * p.MAX_REASON_CHARS)
    for reason in ("x" * (p.MAX_REASON_CHARS + 1), "one\ntwo", "bell\x07"):
        with pytest.raises(p.RpcError):
            take(reason)


def test_staged_ids_are_unique_and_bounded():
    assert turn(stagedIds=["a", "b", "c", "d"])
    for staged in (["a", "a"], ["a", "b", "c", "d", "e"]):
        with pytest.raises(p.RpcError):
            turn(stagedIds=staged)


@pytest.mark.parametrize("data", ["aGV$sbG8=", "aGVsbG8=\n", "aGVs bG8="])
def test_terminal_data_is_strict_base64(data):
    with pytest.raises(p.RpcError):
        command("terminal.input", sessionId="s", attachmentId="a", inputGrant="g", inputEpoch=1, frameSeq=1, data=data)


def test_runtime_capability_is_the_intersection_of_every_input():
    entry = p.CapabilityEntry(p.Support.QUALIFIED, evidence=("record",))
    declaration = p.CapabilityDeclaration("example", "1.0.0", DIGEST, "1", "1", "unix", "q", "2026-11-02", {},
                                          {"text": entry, "interrupt": entry, "steer": entry, "history": entry})
    effective = p.effective_capabilities(declaration, live={"text", "interrupt", "steer"}, placement={"text", "interrupt", "steer", "history"},
                                         actor={"text", "steer", "history"}, policy_blocked={"steer"})
    assert effective["text"] is p.Support.QUALIFIED
    assert effective["interrupt"] is p.Support.UNSUPPORTED  # actor lacks it
    assert effective["history"] is p.Support.UNSUPPORTED  # not live
    assert effective["steer"] is p.Support.POLICY_BLOCKED
    assert effective["shared_tui"] is p.Support.UNSUPPORTED


@each("capabilities")
def test_capability_fixture(case):
    if case["expect"].get("invalid"):
        with pytest.raises(ValueError):
            p.CapabilityDeclaration.from_wire(case["declaration"])
        return
    declaration = p.CapabilityDeclaration.from_wire(case["declaration"])
    assert {name: declaration.support(name).value for name in case["expect"]} == case["expect"]


@each("digests")
def test_digest_fixture(case):
    assert p.operation_digest(case["method"], case["params"]) == case["digest"]


def test_digest_matches_an_independent_canonical_spelling():
    spelled = '{"method":"turn.interrupt","params":{"sessionId":"s","text":"å","turnId":"t"}}'
    assert p.operation_digest("turn.interrupt", {"turnId": "t", "text": "å", "operationId": "ignored", "sessionId": "s"}) \
        == "sha256:" + hashlib.sha256(spelled.encode()).hexdigest()


def test_digest_covers_method_and_body_but_not_key_order():
    assert p.operation_digest("turn.submit", {"a": 1, "b": 2}) == p.operation_digest("turn.submit", {"b": 2, "a": 1})
    assert p.operation_digest("turn.submit", {"a": 1}) != p.operation_digest("turn.steer", {"a": 1})
    assert p.operation_digest("turn.submit", {"a": 1}) != p.operation_digest("turn.submit", {"a": 2})


@pytest.mark.parametrize("value", [{"a": 1.5}, {"a": [1.0]}, {"a": float("inf")}, {1: "a"}])
def test_canonical_json_refuses_ambiguous_values(value):
    with pytest.raises(ValueError):
        p.canonical_json(value)


def test_frame_bounds():
    padding = " " * (p.MAX_FRAME_BYTES - 2)
    assert p.decode_frame(f"{{{padding}}}".encode()) == {}
    with pytest.raises(p.RelayError) as refused:
        p.decode_frame(f"{{{padding} }}".encode())
    assert refused.value.code is p.ErrorCode.LIMIT and refused.value.retry is p.Retry.AFTER_REVIEW
    with pytest.raises(p.RelayError):
        p.encode_frame("x" * p.MAX_FRAME_BYTES)


@pytest.mark.parametrize("encode", [
    lambda text: p.event_frame(p.Event("message.delta", "s", {"text": text}, event_seq=1, history_epoch=1)),
    lambda text: p.result_frame(1, {"text": text}),
], ids=["event", "result"])
def test_lone_surrogate_cannot_stop_a_stream(encode):
    wire = json.loads(encode("before\ud800after 🙂"))
    assert "before\ufffdafter 🙂" in json.dumps(wire, ensure_ascii=False)


def test_digests_still_refuse_a_lone_surrogate():
    with pytest.raises(ValueError):
        p.operation_digest("turn.submit", {"text": "\ud800"})


def test_deep_nesting_and_invalid_utf8_are_parse_errors():
    for raw in (b"[" * 200_000, b'"\xff"'):
        with pytest.raises(p.RpcError) as refused:
            p.decode_frame(raw)
        assert refused.value.code is p.RpcCode.PARSE_ERROR


def test_prompt_bound_counts_utf8_bytes_not_characters():
    assert turn(text="a" * p.MAX_PROMPT_BYTES).params["text"]
    for text in ("a" * (p.MAX_PROMPT_BYTES + 1), "å" * (p.MAX_PROMPT_BYTES // 2 + 1), "\ud800"):
        with pytest.raises(p.RpcError):
            turn(text=text)


def test_terminal_input_bound_applies_to_decoded_bytes():
    params = dict(sessionId="s", attachmentId="a", inputGrant="g", inputEpoch=1, frameSeq=1)
    assert command("terminal.input", **params, data=b64.b64encode(b"x" * p.MAX_TERMINAL_INPUT_BYTES).decode())
    with pytest.raises(p.RpcError):
        command("terminal.input", **params, data=b64.b64encode(b"x" * (p.MAX_TERMINAL_INPUT_BYTES + 1)).decode())


@each("errors")
def test_error_fixture(case):
    code = p.ErrorCode(case["code"])
    receipt = make_receipt(p.ReceiptState.UNCERTAIN) if code is p.ErrorCode.OUTCOME_UNKNOWN else None
    wire = error_data(p.error_frame(p.RelayError(code, receipt=receipt), 7))
    assert (wire["code"], wire["retry"]) == (case["code"], case["retry"])
    assert set(wire) <= {"code", "message", "operationId", "stateRevision", "retry", "details", "receipt"}


def test_every_error_code_has_a_fixture():
    assert {case["code"] for case in cases("errors")} == set(p.ErrorCode)


def test_error_envelope_shape():
    wire = json.loads(p.error_frame(p.RelayError(p.ErrorCode.REVISION_CONFLICT, operation_id="op-1", state_revision=4), 7))
    assert wire["id"] == 7 and wire["error"]["code"] == -32000
    assert wire["error"]["data"]["operationId"] == "op-1" and wire["error"]["data"]["stateRevision"] == 4
    assert json.loads(p.error_frame(p.RpcError(p.RpcCode.PARSE_ERROR))) == {
        "jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}


def test_messages_are_fixed_and_details_are_allowlisted():
    assert "message" not in inspect.signature(p.RelayError).parameters
    for leak in ({"path": "/srv/other"}, {"nativeId": "n"}, {"reason": {"nested": 1}},
                 {"reason": "/srv/other/.codex provider said: token=abc"}, {"limit": "provider said no"}, {"method": "ry/turn.submit"},
                 {"method": "secret"}, {"feature": "secret"}, {"capability": "secret"}, {"maximum": "1"}, {"maximum": True},
                 {"supportedMajor": "1"}):
        with pytest.raises(ValueError):
            p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED, details=leak)


def test_every_detail_this_contract_raises_is_allowed():
    p.RelayError(p.ErrorCode.LIMIT, details={"limit": "frame", "maximum": 1})
    p.RelayError(p.ErrorCode.INPUT_CONFLICT, details={"reason": "unattributed"})
    p.RelayError(p.ErrorCode.UNSUPPORTED, details={"method": "turn.steer", "feature": "steer", "capability": "text", "supportedMajor": 1})


def test_refused_field_names_are_not_echoed():
    secret = {"jsonrpc": "2.0", "id": 1, "method": "ry/security.read", "params": {"sessionId": "s", "/srv/secret": 1}}
    with pytest.raises(p.RpcError) as refused:
        p.parse_command(json.dumps(secret).encode())
    assert "secret" not in p.error_frame(refused.value).decode()


def test_uncertain_outcome_must_carry_its_receipt():
    for receipt in (None, make_receipt()):
        with pytest.raises(ValueError):
            p.RelayError(p.ErrorCode.OUTCOME_UNKNOWN, receipt=receipt)
    wire = error_data(p.error_frame(p.RelayError(p.ErrorCode.OUTCOME_UNKNOWN, receipt=make_receipt(p.ReceiptState.UNCERTAIN))))
    assert wire["operationId"] == "op-1" and wire["receipt"]["state"] == "uncertain"
    assert wire["retry"] == "same_operation_query"


def make_receipt(state=p.ReceiptState.QUEUED):
    return p.Receipt("op-1", "s", "r", "turn.submit", DIGEST, PERSON, state)


@each("receipts")
def test_receipt_fixture(case):
    evidence = p.Evidence(p.EvidenceKind(case["evidence"]), "op-1", "r") if case["evidence"] else None
    reason = p.FailureReason(case["reason"]) if case["reason"] else None
    receipt = replace(make_receipt(p.ReceiptState(case["from"])), method=case["method"])
    move = lambda: p.advance_receipt(receipt, p.ReceiptState(case["to"]), reason=reason, evidence=evidence)
    if case["valid"]:
        moved = move()
        assert moved.state == case["to"] and moved.revision == 1
    else:
        with pytest.raises(ValueError):
            move()


@pytest.mark.parametrize("operation_id, run_id", [("op-2", "r"), ("op-1", "r2")])
def test_evidence_for_another_operation_or_run_does_not_settle_a_receipt(operation_id, run_id):
    with pytest.raises(ValueError):
        p.advance_receipt(make_receipt(p.ReceiptState.UNCERTAIN), p.ReceiptState.ACCEPTED,
                          evidence=p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, operation_id, run_id))


@pytest.mark.parametrize("weak", ["http_success", "echoed_text", "tool_result", "rpc_id"])
def test_weak_signals_are_not_evidence(weak):
    with pytest.raises(ValueError):
        p.EvidenceKind(weak)


def claim_receipt(state=p.ReceiptState.ACCEPTED, method="control.claim"):
    return replace(make_receipt(state), method=method)


@pytest.mark.parametrize("method", ["control.claim", "control.take"])
def test_accepted_claim_or_take_carries_a_grant(method):
    assert p.ControlResult(claim_receipt(method=method), 1, PERSON, "g", 1).input_grant == "g"


def test_a_claim_that_lost_needs_no_grant():
    assert p.ControlResult(claim_receipt(p.ReceiptState.FAILED), 1).input_grant is None


def result_builders():
    actor = p.Actor(p.ActorKind.PERSON, "person-1", p.Channel.CHAT)
    queued = p.Receipt("op-1", "s", "r", "control.claim", DIGEST, actor)
    accepted = replace(queued, state=p.ReceiptState.ACCEPTED, revision=2)
    snapshot = p.Snapshot("s", "r", 1, 41, 3, 2, {"text": p.Support.QUALIFIED, "history": p.Support.UNSUPPORTED}, p.Support.UNSUPPORTED, receipts=(queued,))
    return {
        "initialize": p.negotiate(dict(major=1, minor=0, features=["terminal", "queue"])),
        "session.attach": p.AttachResult("att-1", snapshot),
        "operation.register": p.RegistrationResult("op-1", queued),
        "operation.register repeated": p.RegistrationResult("op-1", queued, existing=True),
        "control.claim": p.ControlResult(accepted, 4, actor, "grant-1", 7, "geo-1"),
        "control.release": p.ControlResult(accepted, 5),
    }


@each("results")
def test_result_fixture(case):
    assert p.to_wire(result_builders()[case["name"]]) == case["wire"]
    assert json.loads(p.result_frame(1, result_builders()[case["name"]]))["result"] == case["wire"]


def test_every_result_shape_has_a_fixture():
    assert {case["name"] for case in cases("results")} == set(result_builders())


def test_a_claim_supplies_what_terminal_input_must_send():
    wire = p.to_wire(result_builders()["control.claim"])
    frame_params = dict(sessionId="s", attachmentId="att-1", inputGrant=wire["inputGrant"], inputEpoch=wire["inputEpoch"], frameSeq=1, data="aGVsbG8=")
    assert command("terminal.input", **frame_params).method == "terminal.input"


def test_snapshot_keeps_decisions_with_what_a_reply_needs():
    decision = make_decision()
    wire = p.to_wire(replace(result_builders()["session.attach"].snapshot, pending_decisions=(decision,)))
    assert {"decisionId", "revision", "actionDigest", "choices"} <= wire["pendingDecisions"][0].keys()


def test_grants_stay_out_of_logs():
    assert "grant-1" not in repr(result_builders()["control.claim"]) and "geo-1" not in repr(result_builders()["control.claim"])


@pytest.mark.parametrize("build", [
    lambda: p.ControlResult(make_receipt(), 1, PERSON, input_grant="g"),
    lambda: p.ControlResult(make_receipt(), 1, PERSON, input_epoch=1),
    lambda: p.ControlResult(make_receipt(), 1, None, "g", 1),
    lambda: p.ControlResult(make_receipt(), 1, geometry_grant="g"),
    lambda: p.RegistrationResult("other", make_receipt()),
    lambda: p.ControlResult(claim_receipt(p.ReceiptState.FAILED), 1, PERSON, "g", 1),
    lambda: p.ControlResult(claim_receipt(p.ReceiptState.QUEUED), 1, PERSON, "g", 1),
    lambda: p.ControlResult(claim_receipt(p.ReceiptState.UNCERTAIN), 1, PERSON, geometry_grant="g"),
    lambda: p.ControlResult(claim_receipt(method="control.release"), 1, PERSON, "g", 1),
    lambda: p.ControlResult(claim_receipt(method="control.release"), 1, PERSON, geometry_grant="g"),
    lambda: p.ControlResult(claim_receipt(), 1, None, "", 1),
    lambda: p.ControlResult(claim_receipt(), 1, None, geometry_grant=""),
])
def test_results_refuse_inconsistent_shapes(build):
    with pytest.raises(ValueError):
        build()


def test_private_evidence_stays_off_the_wire():
    evidence = p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, "op-1", "r", native_ref="native-secret")
    assert "native-secret" not in repr(evidence) and "nativeRef" not in p.to_wire(evidence)


@pytest.mark.parametrize("change, code", [
    (dict(actor=OTHER), p.ErrorCode.NOT_FOUND_OR_DENIED),
    (dict(actor=SERVICE), p.ErrorCode.NOT_FOUND_OR_DENIED),
    (dict(actor=replace(PERSON, kind=p.ActorKind.SERVICE)), p.ErrorCode.NOT_FOUND_OR_DENIED),
    (dict(session_id="s2"), p.ErrorCode.NOT_FOUND_OR_DENIED),
    (dict(run_id="r2"), p.ErrorCode.INTEGRITY_FAILURE),
    (dict(method="turn.steer"), p.ErrorCode.INTEGRITY_FAILURE),
    (dict(payload_digest="sha256:" + "b" * 64), p.ErrorCode.INTEGRITY_FAILURE),
])
def test_operation_id_reuse_is_a_conflict(change, code):
    same = dict(actor=PERSON, session_id="s", run_id="r", method="turn.submit", payload_digest=DIGEST)
    p.check_registered(make_receipt(), **same)
    p.check_registered(make_receipt(), **{**same, "actor": replace(PERSON, channel=p.Channel.PHONE)})  # same person, other device
    with pytest.raises(p.RelayError) as refused:
        p.check_registered(make_receipt(), **{**same, **change})
    assert refused.value.code is code


CURRENT = dict(run_id="r", policy_revision=2, action_digest=DIGEST, now_ms=1000)


def make_decision(**overrides):
    choices = (p.DecisionChoice("allow-once", "Allow once", p.ChoiceEffect.ALLOW_ONCE), p.DecisionChoice("deny", "Deny", p.ChoiceEffect.DENY))
    return p.Decision(**{**dict(decision_id="d", session_id="s", run_id="r", kind=p.DecisionKind.TOOL_APPROVAL, action_digest=DIGEST,
                                description="Run the test suite", choices=choices, expires_at_ms=1500, policy_revision=2, revision=4), **overrides})


@each("decisions")
def test_decision_fixture(case):
    answer_case = case["reply"]
    decision = make_decision(status=p.DecisionStatus(answer_case["status"]), kind=p.DecisionKind(answer_case["kind"]))
    answer = lambda: p.reserve_answer(decision, actor=SERVICE if answer_case["actor"] == "service" else PERSON, choice_id=answer_case["choice"],
                                      run_id="r" if answer_case["run"] == "match" else "r2", policy_revision=answer_case["policyRevision"],
                                      expected_revision=answer_case["revision"], now_ms=answer_case["nowMs"],
                                      action_digest=DIGEST if answer_case["digest"] == "match" else "sha256:" + "b" * 64)
    if case["expect"] == "reserved":
        won = answer()
        assert (won.status, won.winner, won.choice_id, won.revision) == (p.DecisionStatus.RESERVED, PERSON, "allow-once", 5)
    else:
        with pytest.raises(p.RelayError) as refused:
            answer()
        assert refused.value.code == case["expect"]


def test_first_valid_answer_wins_and_forbidden_answers_do_not_consume_it():
    decision = make_decision()
    for bad in (dict(actor=SERVICE, choice_id="allow-once"), dict(actor=OTHER, choice_id="allow-always")):
        with pytest.raises(p.RelayError):
            p.reserve_answer(decision, **CURRENT, expected_revision=4, **bad)
    won = p.reserve_answer(decision, **CURRENT, actor=PERSON, choice_id="deny", expected_revision=4)
    with pytest.raises(p.RelayError) as late:
        p.reserve_answer(won, **CURRENT, actor=OTHER, choice_id="allow-once", expected_revision=5)
    assert late.value.code is p.ErrorCode.DECISION_RESOLVED and won.winner == PERSON


def test_decision_outcomes_never_reopen_and_lost_answer_stays_recorded():
    won = p.reserve_answer(make_decision(), **CURRENT, actor=PERSON, choice_id="allow-once", expected_revision=4)
    unknown = p.advance_decision(won, p.DecisionStatus.OUTCOME_UNKNOWN)
    assert unknown.winner == PERSON
    settled = p.advance_decision(unknown, p.DecisionStatus.ANSWERED)
    for decision, status in ((settled, p.DecisionStatus.PENDING), (unknown, p.DecisionStatus.PENDING), (unknown, p.DecisionStatus.RESERVED),
                             (make_decision(), p.DecisionStatus.RESERVED), (won, p.DecisionStatus.EXPIRED), (won, p.DecisionStatus.CANCELLED)):
        with pytest.raises(ValueError):
            p.advance_decision(decision, status)


def test_binding_keeps_private_fields_out_of_logs_and_the_wire():
    binding = p.Binding("b", "install", "node", "agent-account", "/workspace/secret", "codex", "native-secret", DIGEST)
    for rendered in (repr(binding), json.dumps(p.to_wire(binding))):
        assert not any(value in rendered for value in ("agent-account", "/workspace/secret", "native-secret"))


def test_one_time_choices_cannot_become_standing_permission():
    assert {effect.value for effect in p.ChoiceEffect} == {"answer", "allow_once", "deny"}


def test_decision_kind_fails_closed():
    with pytest.raises(ValueError):
        p.DecisionKind("assistant_text")


def test_delayed_native_request_stays_with_the_controller_that_typed_it():
    first, taken = p.control_handover(p.ControlBinding(PERSON, "a1", 7, 1, 1), actor=OTHER, attachment_id="a2", control_revision=2, until_watermark=100)
    late = p.NativeAction("native-1", input_epoch=7, watermark=99)
    assert p.attribute_native_action(late, [taken, first]).actor == PERSON
    assert p.attribute_native_action(p.NativeAction("native-2", 8, 1), [first, taken]).actor == OTHER


def test_every_holder_change_bumps_the_epoch_and_restarts_frames():
    current = p.ControlBinding(PERSON, "a1", 7, 1, 1)
    closed, successor = p.control_handover(current, actor=PERSON, attachment_id="a2", control_revision=2, until_watermark=6)
    assert (closed.input_epoch, closed.until_watermark) == (7, 6)
    assert (successor.actor, successor.attachment_id, successor.input_epoch, successor.control_revision) == (PERSON, "a2", 8, 2)
    assert (successor.from_watermark, successor.until_watermark) == (1, None)


def test_the_same_frame_number_is_unambiguous_across_a_takeover():
    closed, successor = p.control_handover(p.ControlBinding(PERSON, "a1", 7, 1, 1), actor=OTHER, attachment_id="a2", control_revision=2, until_watermark=9)
    assert p.attribute_native_action(p.NativeAction("n", 7, 5), [closed, successor]).actor == PERSON
    assert p.attribute_native_action(p.NativeAction("n", 8, 5), [closed, successor]).actor == OTHER


@pytest.mark.parametrize("action", [p.NativeAction("n", 6, 50), p.NativeAction("n", 7, 5)])
def test_uncorrelated_native_request_is_refused_not_guessed(action):
    with pytest.raises(p.RelayError) as refused:
        p.attribute_native_action(action, [p.ControlBinding(PERSON, "a", 7, 1, 10)])
    assert refused.value.code is p.ErrorCode.INPUT_CONFLICT


@each("selector")
def test_selector_fixture(case):
    state = dict(case["state"], switch_state=p.SwitchState(case["state"]["switch_state"]))
    result = p.select_view(**state)
    assert (result.action.value, result.reason) == (case["expect"]["action"], case["expect"]["reason"])


def test_switch_moves_are_exactly_the_design_graph():
    S = p.SwitchState
    assert p.SWITCH_MOVES == {
        S.OWNED: {S.PREPARED}, S.PREPARED: {S.OWNED, S.DRAINING}, S.DRAINING: {S.OWNED, S.STOPPING},
        S.STOPPING: {S.STARTING, S.RECOVERY_REQUIRED}, S.STARTING: {S.OWNED, S.RECOVERY_REQUIRED}, S.RECOVERY_REQUIRED: set()}


def test_selector_cannot_be_called_without_an_idle_answer():
    with pytest.raises(TypeError):
        p.select_view(shared_live=False, owner="terminal", target="chat")


@each("events")
def test_event_fixture(case):
    read = p.read_event(case["params"])
    assert isinstance(read, p.Event if case["expect"] == "event" else p.UnsupportedEvent)


def test_event_frame_round_trips():
    event = p.Event("decision.resolved", "s", {"status": "answered"}, event_seq=9, history_epoch=1, run_id="r", actor=PERSON, decision_id="d", revision=6)
    wire = json.loads(p.event_frame(event))
    assert (wire["method"], "id" in wire) == ("ry/event", False)
    assert p.read_event(wire["params"]) == event


@pytest.mark.parametrize("kind, order", [("turn.started", dict(attachment_seq=1)), ("terminal.output", dict(event_seq=1, history_epoch=1)), ("invented", dict(event_seq=1, history_epoch=1))])
def test_event_order_matches_its_lifetime(kind, order):
    with pytest.raises(ValueError):
        p.Event(kind, "s", **order)


@pytest.mark.parametrize("value", [
    None, {}, {"schema": "other", "kind": "commands", "cases": [{}]}, {"schema": p.FIXTURE_SCHEMA, "kind": "mystery", "cases": [{}]},
    {"schema": p.FIXTURE_SCHEMA, "kind": "events", "cases": []},
    {"schema": p.FIXTURE_SCHEMA, "kind": "events", "cases": [{"name": "x"}]},
    {"schema": p.FIXTURE_SCHEMA, "kind": "errors", "cases": [{"name": "x", "code": "A", "retry": "never"}] * 2},
])
def test_fixture_schema_refuses_malformed_files(value):
    with pytest.raises(ValueError):
        p.validate_fixture(value)


def test_every_fixture_file_has_a_known_kind():
    assert {path.stem for path in FIXTURES.glob("*.json")} == set(p.FIXTURE_CASE_FIELDS)


def test_fixtures_use_artificial_identities():
    for path in FIXTURES.glob("*.json"):
        assert not re.search(r"josh|@(?!example\.)|/home/|/srv/acc", path.read_text(encoding="utf-8"), re.I), path.name


def test_adapter_covers_the_design_entry_points():
    assert set(base.SessionAdapter.__abstractmethods__) == {
        "declaration", "probe", "start", "reattach", "read_snapshot", "follow", "submit", "steer", "interrupt", "reply_decision",
        "read_configuration", "apply_next_turn", "native_client_proxy", "prepare_switch", "stop_owner", "resume_owner", "reconcile", "close"}
    assert set(base.TerminalHost.__abstractmethods__) == {
        "attach_read", "claim_input", "write_frames", "drain", "resize", "detach", "observe_exit"}


@pytest.mark.parametrize("name", sorted(base.SessionAdapter.__abstractmethods__ - {"declaration", "close", "follow"}))
def test_unavailable_adapter_refuses_every_call(name):
    with pytest.raises(p.RelayError) as refused:
        asyncio.run(getattr(base.UnavailableAdapter(), name)(None))
    assert refused.value.code is p.ErrorCode.UNSUPPORTED


@pytest.mark.parametrize("name", ["claim_input", "write_frames", "drain", "resize", "observe_exit"])
def test_unavailable_terminal_host_refuses_input(name):
    with pytest.raises(p.RelayError):
        asyncio.run(getattr(base.UnavailableTerminalHost(), name)(None))


def test_watermark_carries_its_epoch_through_drain_and_ack():
    assert list(inspect.signature(base.TerminalHost.drain).parameters) == ["self", "grant", "watermark"]
    wire = p.to_wire(base.Watermark(8, 5, 4))
    assert wire == {"epoch": 8, "admitted": 5, "processed": 4}
    assert p.Event("terminal.ack", "s", wire, attachment_seq=1).payload["epoch"] == 8


def test_resize_returns_the_size_applied():
    assert inspect.signature(base.TerminalHost.resize).return_annotation == "Dimensions"
    assert p.to_wire(base.Dimensions(120, 40)) == {"cols": 120, "rows": 40}


def test_unavailable_streams_refuse_immediately_and_cleanup_still_finishes():
    with pytest.raises(p.RelayError):
        base.UnavailableAdapter().follow(None)
    with pytest.raises(p.RelayError):
        base.UnavailableTerminalHost().attach_read(None)
    assert asyncio.run(base.UnavailableAdapter().close(base.CloseMode.DETACH)) is None
    assert base.UnavailableAdapter().declaration().support("text") is p.Support.UNSUPPORTED


@pytest.mark.parametrize("outcome, kind, settles", [
    (base.InterruptOutcome.REQUESTED, p.EvidenceKind.PROVIDER_ADMISSION, p.ReceiptState.ACCEPTED),
    (base.InterruptOutcome.CONFIRMED, p.EvidenceKind.PROVIDER_ADMISSION, p.ReceiptState.ACCEPTED),
    (base.InterruptOutcome.NOT_ACTIVE, p.EvidenceKind.PROVIDER_REJECTION, p.ReceiptState.FAILED),
    (base.InterruptOutcome.UNKNOWN, None, p.ReceiptState.UNCERTAIN),
])
def test_interrupt_result_settles_its_receipt(outcome, kind, settles):
    evidence = p.Evidence(kind, "op-1", "r") if kind else None
    result = base.InterruptResult(outcome, "r", "t", evidence)
    assert result.settles is settles
    receipt = replace(make_receipt(p.ReceiptState.SUBMITTING), method="turn.interrupt")
    reason = p.FailureReason.REJECTED if settles is p.ReceiptState.FAILED else None
    assert p.advance_receipt(receipt, result.settles, reason=reason, evidence=result.evidence).state is settles


@pytest.mark.parametrize("outcome, evidence", [
    (base.InterruptOutcome.REQUESTED, None),
    (base.InterruptOutcome.CONFIRMED, p.Evidence(p.EvidenceKind.HUB_COMMIT, "op-1", "r")),
    (base.InterruptOutcome.NOT_ACTIVE, p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, "op-1", "r")),
    (base.InterruptOutcome.UNKNOWN, p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, "op-1", "r")),
    (base.InterruptOutcome.CONFIRMED, p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, "op-1", "other-run")),
])
def test_interrupt_result_cannot_outrun_its_evidence(outcome, evidence):
    with pytest.raises(ValueError):
        base.InterruptResult(outcome, "r", "t", evidence)


def test_adapter_cannot_claim_a_hub_commit():
    with pytest.raises(ValueError):
        base.AdmissionResult(base.Outcome.ACCEPTED, p.Evidence(p.EvidenceKind.HUB_COMMIT, "op-1", "r"))


def admission(kind):
    return p.Evidence(kind, "op-1", "r")


@pytest.mark.parametrize("outcome, evidence", [
    (base.Outcome.ACCEPTED, None),
    (base.Outcome.ACCEPTED, admission(p.EvidenceKind.PROVIDER_REJECTION)),
    (base.Outcome.REJECTED, None),
    (base.Outcome.REJECTED, admission(p.EvidenceKind.PROVIDER_ADMISSION)),
    (base.Outcome.UNKNOWN, admission(p.EvidenceKind.PROVIDER_ADMISSION)),
])
def test_adapter_outcome_cannot_outrun_its_evidence(outcome, evidence):
    with pytest.raises(ValueError):
        base.AdmissionResult(outcome, evidence)


def test_contract_modules_import_only_the_standard_library_and_each_other():
    root = Path(p.__file__).parent
    # Implementations may depend on broker transport; the M0 contracts stay pure.
    paths = [root / "__init__.py", root / "protocol.py", root / "adapters" / "__init__.py",
             root / "adapters" / "base.py"]
    assert {root / "protocol.py", root / "adapters" / "base.py"} <= set(paths)

    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                depth = len(path.relative_to(root).parts)  # 1 at the package root
                assert node.level <= depth and {*(node.module or "").split(".")} <= {"protocol", "adapters", "base", ""}, path
            else:
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
                assert all(name.split(".")[0] in sys.stdlib_module_names for name in names), (path, names)
