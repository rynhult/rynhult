/** Camel-case wire mirrors of session_relay/protocol.py. Missing capability means unsupported. */
export type RelayView = 'chat' | 'terminal';
export interface RelayRetainedInput { sourceRunId: string; view: RelayView; text: string; readOnly: true }
export interface SelectorState {
  shared_live: boolean; owner: string; target: string; direction_qualified: boolean;
  may_control: boolean; blockers: string[]; switch_state: string; idle_verified: boolean;
}
export interface SelectorResult { action: 'view_only' | 'switch' | 'pending' | 'refused'; reason: string | null }
export interface RelayActor { kind: string; principal: string; channel: string }
export interface RelayReceipt {
  operationId: string; sessionId: string; runId: string; method: string; payloadDigest: string;
  actor: RelayActor; state: 'queued' | 'submitting' | 'accepted' | 'failed' | 'uncertain'; revision: number; reason?: string;
}
export interface RelayDecision {
  decisionId: string; runId: string; actionDigest: string; description: string; revision: number;
  expiresAtMs: number; status: string; winner?: RelayActor; choiceId?: string; choices: { id: string; label: string; effect: string }[];
}
export interface RelaySnapshot {
  sessionId: string; runId?: string; historyEpoch: number; watermark: number;
  controlRevision: number; configurationRevision: number; capabilities: Record<string, string>;
  history: string; pendingDecisions: RelayDecision[]; receipts: RelayReceipt[];
  /** Optional hub presentation facts; absence never proves idle or grants control. */
  selector?: Omit<SelectorState, 'target' | 'direction_qualified'>;
  ownershipRevision?: number;
  execution?: { waitingForFirstChat?: boolean; view: RelayView; phase: string; holder?: RelayActor | null; mine: boolean; available: boolean; inputEpoch: number; activeOperationId?: string | null };
  activeTurnId?: string;
  exitOutcome?: 'normal' | 'abnormal' | 'unknown';
  finalEventsDrained?: boolean;
  writer?: { view: RelayView; holder?: RelayActor | null; revision: number; active: boolean; available: boolean; mine: boolean };
  retainedInputs?: RelayRetainedInput[];
  l1?: { owner: RelayView; phase: string; revision: number; available: boolean; recoveryAvailable?: boolean; terminalWaitingForChat?: boolean; recoveryMode?: 'new_session'; terminalWaitingReason?: string };
  decisionOutcomes?: RelayDecision[];
  nativePrompt?: { kind: 'overwrite'; message: string; remoteAllow: false; terminalUrl: string };
}
export interface RelayEvent { kind: string; sessionId: string; eventSeq?: number; historyEpoch?: number; payload: Record<string, unknown> }
export interface RelayMessage { itemId: string; role: string; text: string }
