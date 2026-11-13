import { api, ApiError } from './api';
import type { RelayReceipt, RelaySnapshot } from './sessionRelayTypes';
export { selectRelayView } from './sessionRelaySelector';

import { operationDigest } from './sessionRelayDigest';
export { operationDigest } from './sessionRelayDigest';
export class RelayReadReset extends Error {}
/** One authenticated HTTP binding for contract JSON-RPC. Never retries a mutation. */
export class RelayClient {
  pendingOperationId?: string;
  registrationUncertain = false;
  connectionToken?: string;
  attachmentId?: string;
  constructor(readonly sessionId: string, readonly signal?: AbortSignal) {}
  async call<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const reply = await api<{ result?: T; error?: { message: string; data?: { code?: string; receipt?: RelayReceipt } } }>(`/api/sessions/${encodeURIComponent(this.sessionId)}/relay`, {
      signal: this.signal, headers: this.connectionToken ? { 'X-ACC-Relay-Connection': this.connectionToken } : undefined, json: { jsonrpc: '2.0', id: crypto.randomUUID(), method: `ry/${method}`, params: method === 'initialize' ? params : { sessionId: this.sessionId, ...params } },
    });
    if (reply.error) {
      if (['CURSOR_RESET', 'NOT_FOUND_OR_DENIED'].includes(reply.error.data?.code ?? '')) throw new RelayReadReset(reply.error.message);
      throw new Error(reply.error.message);
    }
    if (reply.result === undefined) throw new Error('Relay response is incomplete');
    if (method === 'initialize') this.connectionToken = (reply.result as { connectionToken?: string }).connectionToken;
    if (method === 'session.attach') this.attachmentId = (reply.result as { attachmentId: string }).attachmentId;
    if (method === 'session.detach') this.attachmentId = undefined;
    return reply.result;
  }
  terminalRead(runId: string) {
    if (!this.attachmentId || !this.connectionToken) throw new Error('Attach before reading Terminal');
    const query = new URLSearchParams({ expectedRunId: runId, attachmentId: this.attachmentId });
    return api<{ runId: string; text: string; truncated: boolean }>(`/api/sessions/${encodeURIComponent(this.sessionId)}/relay/terminal?${query}`, {
      signal: this.signal, headers: { 'X-ACC-Relay-Connection': this.connectionToken },
    });
  }
  async ownershipRead(runId: string) {
    if (!this.attachmentId || !this.connectionToken) throw new Error('Attach before observing ownership');
    const query = new URLSearchParams({ expectedRunId: runId, attachmentId: this.attachmentId });
    try {
      return await api<Pick<RelaySnapshot, 'selector' | 'execution' | 'ownershipRevision'>>(`/api/sessions/${encodeURIComponent(this.sessionId)}/relay/ownership?${query}`, {
        signal: this.signal, headers: { 'X-ACC-Relay-Connection': this.connectionToken },
      });
    } catch (failure) {
      const code = failure instanceof ApiError
        ? (failure.data as { error?: { data?: { code?: string } } } | undefined)?.error?.data?.code : undefined;
      if (failure instanceof ApiError && failure.status === 404 && code === 'NOT_FOUND_OR_DENIED') {
        // A run change expires the observer binding. Revalidate the central
        // person/role boundary and session access before retaining private drafts.
        // Neither read creates input authority or retries the failed operation.
        await api('/api/auth/state', { signal: this.signal });
        await api(`/api/sessions/${encodeURIComponent(this.sessionId)}`, { signal: this.signal });
        throw new RelayReadReset('The observer attachment changed.');
      }
      throw failure;
    }
  }
  retainedRuns() {
    return api<{ currentRunId: string; runs: { runId: string; exitOutcome: string; endedAt?: string | null }[]; more: boolean }>(`/api/sessions/${encodeURIComponent(this.sessionId)}/relay/runs`, { signal: this.signal });
  }
  retainedEvents(runId: string, cursor?: string) {
    const query = new URLSearchParams({ runId, limit: '100', ...(cursor ? { cursor } : {}) });
    return api<{ events: import('./sessionRelayTypes').RelayEvent[]; cursor: string; more: boolean }>(`/api/sessions/${encodeURIComponent(this.sessionId)}/relay/events?${query}`, { signal: this.signal });
  }
  snapshot() { return this.call<RelaySnapshot>('session.snapshot'); }
  async mutate<T = RelayReceipt>(method: string, snapshot: RelaySnapshot, payload: Record<string, unknown>) {
    if (!snapshot.runId) throw new Error('This session has no active run');
    if (method === 'writer.take' && !this.attachmentId) throw new Error('Attach before taking input');
    const params = { sessionId: this.sessionId, expectedRunId: snapshot.runId, ...payload,
      ...(method === 'writer.take' ? { attachmentId: this.attachmentId } : {}) };
    this.registrationUncertain = true;
    const registration = await this.call<{ operationId: string; receipt: RelayReceipt; existing?: boolean }>('operation.register', {
      registrationKey: crypto.randomUUID(), method, expectedRunId: snapshot.runId,
      payloadDigest: await operationDigest(method, params),
    });
    this.registrationUncertain = false;
    this.pendingOperationId = registration.operationId;
    // Registration returning an existing operation is evidence, never permission to resend.
    if (registration.existing || registration.receipt.state !== 'queued') throw new Error('Operation already has an outcome. Check its receipt.');
    const result = await this.call<T>(method, { ...params, operationId: registration.operationId });
    this.pendingOperationId = undefined;
    return result;
  }
}
