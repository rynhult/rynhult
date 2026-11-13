"""The two seams behind the hub: a provider adapter and a terminal host.

An adapter translates one known native interface. It never makes authority choices for Rynhult: it returns typed
observations with evidence and refuses what it cannot do. A provider advertising a method is necessary evidence,
not qualification. TerminalHost keeps client operations and receipt meaning stable while tmux stays the v1 host
and a later owned PTY can replace it.

Unavailable* are the only implementations at M0. They refuse every call, so anything wired to them fails closed.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping, NoReturn

from ..protocol import (
    NO_CAPABILITIES,
    Actor,
    Binding,
    CapabilityDeclaration,
    ErrorCode,
    Evidence,
    EvidenceKind,
    Fence,
    NativeAction,
    ReceiptState,
    RelayError,
)


class Outcome(enum.StrEnum):
    """The boundary of one native call. Unknown is a real answer: recovery reconciles, it never resends."""
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class InterruptOutcome(enum.StrEnum):
    REQUESTED = "requested"  # admitted; the receipt is accepted, which is not proof the turn stopped
    CONFIRMED = "confirmed"
    NOT_ACTIVE = "not_active"
    UNKNOWN = "unknown"


class HistoryState(enum.StrEnum):
    OK = "ok"
    UNSUPPORTED = "unsupported"  # not the same as empty
    GAP = "gap"
    RESET = "reset"


class CloseMode(enum.StrEnum):
    DETACH = "detach"  # the provider keeps running
    STOP = "stop"  # an authorized provider stop


class ExitState(enum.StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    UNKNOWN = "unknown"  # not proof of death: a switch stops at recovery required


@dataclass(frozen=True)
class AdapterOperation:
    """One admitted mutation, with the single-operation grant the broker verified and claimed before this call."""
    operation_id: str
    session_id: str
    run_id: str
    method: str
    payload_digest: str
    actor: Actor
    fence: Fence
    deadline_ms: int
    params: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        # Snapshot caller-owned containers before any verifier or adapter awaits.
        object.__setattr__(self, 'params', _freeze(self.params))

    def native_params(self, required, *, optional=()):
        """Validate the complete registered payload before selecting native fields.

        Public envelope fields remain in the operation and its digest/journal.
        The compact form remains usable by internal native-proxy operations.
        No actor substitution or second digest is introduced by translation.
        """
        from ..protocol import operation_digest
        if self.payload_digest != operation_digest(self.method, self.params):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        envelope = {'sessionId', 'expectedRunId'}
        keys = self.params.keys()
        if keys & envelope:
            if (self.params.get('sessionId'), self.params.get('expectedRunId')) != (self.session_id, self.run_id):
                raise RelayError(ErrorCode.STALE_RUN)
        else:
            envelope = set()
        if not set(required) <= keys or keys - set(required) - set(optional) - envelope:
            raise RelayError(ErrorCode.UNSUPPORTED)
        return {key: self.params[key] for key in required}


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


@dataclass(frozen=True)
class ReviewedSpec:
    """A launch, resume or switch that already passed review. Attach, reads and reconnects can never produce one."""
    binding: Binding
    run_id: str
    spec: Mapping[str, Any] = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, 'spec', _freeze(self.spec))


@dataclass(frozen=True)
class RuntimeAttestation:
    binding_id: str
    run_id: str
    native_conversation: str = field(repr=False)
    process_identity: str = field(repr=False)


@dataclass(frozen=True)
class ProbeResult:
    executable_version: str
    executable_digest: str
    protocol_version: str
    transport: str
    guard_intact: bool
    advertised: frozenset[str]  # what the provider says; only a CapabilityDeclaration says what is qualified


@dataclass(frozen=True)
class HistoryPage:
    state: HistoryState
    items: tuple[Mapping[str, Any], ...] = ()  # normalized and redacted before they leave the adapter
    cursor: str | None = None
    source_epoch: int | None = None


@dataclass(frozen=True)
class AdmissionResult:
    """Submission, steering, decision or reconciliation. Accepted or rejected must carry conclusive evidence."""
    outcome: Outcome
    evidence: Evidence | None = None
    turn_id: str | None = None

    def __post_init__(self) -> None:
        expected = {Outcome.ACCEPTED: {EvidenceKind.PROVIDER_ADMISSION},
                    Outcome.REJECTED: {EvidenceKind.PROVIDER_REJECTION, EvidenceKind.BROKER_REFUSAL}}.get(self.outcome)
        if expected and (self.evidence is None or self.evidence.kind not in expected):
            raise ValueError("a definite outcome needs matching evidence")
        if self.outcome is Outcome.UNKNOWN and self.evidence is not None:
            raise ValueError("conclusive evidence is not an unknown outcome")


@dataclass(frozen=True)
class InterruptResult:
    """The turn's fate and the receipt's are separate: `turn.interrupted` reports the first, `settles` the second."""
    outcome: InterruptOutcome
    run_id: str
    turn_id: str
    evidence: Evidence | None = None

    def __post_init__(self) -> None:
        expected = {InterruptOutcome.REQUESTED: EvidenceKind.PROVIDER_ADMISSION, InterruptOutcome.CONFIRMED: EvidenceKind.PROVIDER_ADMISSION,
                    InterruptOutcome.NOT_ACTIVE: EvidenceKind.PROVIDER_REJECTION}.get(self.outcome)
        if (self.evidence.kind if self.evidence else None) is not expected:
            raise ValueError("an interrupt outcome needs its matching evidence, and unknown has none")
        if self.evidence and self.evidence.run_id != self.run_id:
            raise ValueError("evidence belongs to another run")

    @property
    def settles(self) -> ReceiptState:
        """The receipt state this result moves `turn.interrupt` to."""
        return {InterruptOutcome.NOT_ACTIVE: ReceiptState.FAILED, InterruptOutcome.UNKNOWN: ReceiptState.UNCERTAIN}.get(self.outcome, ReceiptState.ACCEPTED)


@dataclass(frozen=True)
class FieldConfiguration:
    """One of model or effort; either can be independently unknown, and none of these values stands in for another."""
    supported: tuple[str, ...] | None = None  # None: unsupported or unknown
    requested: str | None = None
    pending: str | None = None
    acknowledged_next_turn: str | None = None
    observed_last_turn: str | None = None


@dataclass(frozen=True)
class ConfigurationResult:
    model: FieldConfiguration
    effort: FieldConfiguration
    partial: bool = False  # one field applied: reported, never rolled back by assertion


@dataclass(frozen=True)
class SwitchEvidence:
    """Positive idle and exit evidence for a switch. Quiet screen output is neither."""
    idle: bool
    exit: ExitState
    native_conversation: str = field(repr=False)
    policy_digest: str = ""
    retained_input: dict | None = None  # private exact old-client proof; never draft text or processing ACK


class NativeClientProxy(ABC):
    """The per-session mediated path for a native TUI. Not a provider-RPC tunnel."""

    @abstractmethod
    async def admit(self, action: NativeAction) -> AdmissionResult:
        """Pass one allowlisted native message through hub admission, or refuse it before the provider sees it."""

    @abstractmethod
    async def close(self) -> None: ...


class SessionAdapter(ABC):
    """One broker-owned execution owner. Every entry point of the design's adapter table, and nothing more."""

    @abstractmethod
    def declaration(self) -> CapabilityDeclaration: ...

    @abstractmethod
    async def probe(self, binding: Binding) -> ProbeResult:
        """Version, transport, guard integrity and advertised capabilities, with no inference or other conversation."""

    @abstractmethod
    async def start(self, spec: ReviewedSpec) -> RuntimeAttestation: ...

    @abstractmethod
    async def reattach(self, attestation: RuntimeAttestation) -> RuntimeAttestation:
        """The same owner or an error. A failed lookup never starts a replacement."""

    @abstractmethod
    async def read_snapshot(self) -> HistoryPage: ...

    @abstractmethod
    def follow(self, cursor: str | None) -> AsyncIterator[HistoryPage]: ...

    @abstractmethod
    async def submit(self, operation: AdapterOperation) -> AdmissionResult:
        """Called at most once per operation: no generic auto-retry, here or in callers."""

    @abstractmethod
    async def steer(self, operation: AdapterOperation) -> AdmissionResult: ...

    @abstractmethod
    async def interrupt(self, operation: AdapterOperation) -> InterruptResult: ...

    @abstractmethod
    async def reply_decision(self, operation: AdapterOperation) -> AdmissionResult:
        """The exact still-pending request and choice, as a one-time scoped answer."""

    @abstractmethod
    async def read_configuration(self) -> ConfigurationResult: ...

    @abstractmethod
    async def apply_next_turn(self, operation: AdapterOperation) -> ConfigurationResult: ...

    @abstractmethod
    async def native_client_proxy(self, binding: Binding) -> NativeClientProxy: ...

    @abstractmethod
    async def prepare_switch(self) -> SwitchEvidence: ...

    @abstractmethod
    async def stop_owner(self) -> SwitchEvidence: ...

    @abstractmethod
    async def resume_owner(self, spec: ReviewedSpec) -> RuntimeAttestation:
        """Always a new run, only after the old owner is proven gone."""

    @abstractmethod
    async def reconcile(self, operation: AdapterOperation) -> AdmissionResult:
        """Evidence for an earlier call or still unknown. It never replays as a recovery technique."""

    @abstractmethod
    async def close(self, mode: CloseMode) -> None:
        """Close adapter resources and owned sockets and tasks. DETACH must leave the provider running."""


@dataclass(frozen=True)
class TerminalRun:
    session_id: str
    run_id: str


@dataclass(frozen=True)
class InputGrant:
    """A sequenced stream grant from main. Only main renews it; the host cannot extend an invalid epoch."""
    run: TerminalRun
    attachment_id: str
    actor: Actor
    grant: str = field(repr=False)
    epoch: int
    fence: Fence


@dataclass(frozen=True)
class GeometryGrant:
    run: TerminalRun
    attachment_id: str
    grant: str = field(repr=False)
    epoch: int


@dataclass(frozen=True)
class Dimensions:
    cols: int
    rows: int


@dataclass(frozen=True)
class Watermark:
    """Admitted is not processed, and neither is proof a prompt was accepted. Frame numbers mean nothing outside their epoch."""
    epoch: int
    admitted: int
    processed: int


@dataclass(frozen=True)
class WriteResult:
    watermark: Watermark
    partial: bool = False  # bytes may have reached the host: disclose it, never call it not sent or replay the rest


@dataclass(frozen=True)
class ExitObservation:
    state: ExitState
    status: int | None = None


class TerminalHost(ABC):
    """Native terminal bytes for one run. Implementations keep the normal client input path and never type commands."""

    @abstractmethod
    def attach_read(self, run: TerminalRun) -> AsyncIterator[bytes]:
        """Observer output. Attaching grants no input and launches nothing."""

    @abstractmethod
    async def claim_input(self, grant: InputGrant) -> None: ...

    @abstractmethod
    async def write_frames(self, grant: InputGrant, sequence: int, data: bytes) -> WriteResult:
        """One sequenced bounded frame. Refuses before the first byte when it can; no offline replay."""

    @abstractmethod
    async def drain(self, grant: InputGrant, watermark: int) -> Watermark:
        """Progress up to this frame. Handover uses this, never an empty client send buffer."""

    @abstractmethod
    async def resize(self, grant: GeometryGrant, dimensions: Dimensions) -> Dimensions:
        """Clamp to what the host allows, as `broker/attach.py` `set_size` does, and return the size actually applied."""

    @abstractmethod
    async def detach(self, run: TerminalRun) -> None:
        """Ends this view only. Execution and accepted work continue."""

    @abstractmethod
    async def observe_exit(self, run: TerminalRun) -> ExitObservation: ...


def _refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise RelayError(ErrorCode.UNSUPPORTED)


async def _refuse_async(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse()


class UnavailableAdapter(SessionAdapter):
    """Stub capability refusal: declares nothing and refuses everything."""

    def declaration(self) -> CapabilityDeclaration:
        return NO_CAPABILITIES

    probe = start = reattach = read_snapshot = submit = steer = interrupt = reply_decision = _refuse_async
    read_configuration = apply_next_turn = native_client_proxy = _refuse_async
    prepare_switch = stop_owner = resume_owner = reconcile = _refuse_async
    follow = _refuse

    async def close(self, mode: CloseMode) -> None:
        return None  # owns nothing, so a cleanup path can always finish


class UnavailableTerminalHost(TerminalHost):
    """Stub refusal for the seam B fills with the tmux host."""

    claim_input = write_frames = drain = resize = observe_exit = _refuse_async
    attach_read = _refuse

    async def detach(self, run: TerminalRun) -> None:
        return None
