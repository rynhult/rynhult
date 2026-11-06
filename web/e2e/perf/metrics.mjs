/** CDP accounting: finished transfer size replaces chunk sizes; it never adds to them. */
export function median(values) {
  const sorted = values.filter(v => typeof v === 'number' && Number.isFinite(v)).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}
const pathOf = url => { const u = new URL(url, 'http://fixture.invalid'); return u.pathname + u.search; };
export function polling(events, start, end) {
  const by_path = {}; let streams = 0;
  for (const event of events) {
    if (event.time < start || event.time >= end || event.method !== 'GET' || !pathOf(event.url).startsWith('/api/')) continue;
    if (event.type === 'EventSource' || pathOf(event.url).includes('/events/stream')) { streams++; continue; }
    const path = pathOf(event.url); by_path[path] = (by_path[path] ?? 0) + 1;
  }
  const requests = Object.values(by_path).reduce((n, count) => n + count, 0);
  return { requests, requests_per_minute: requests * 60000 / (end - start), streams, by_path };
}
export class Traffic {
  records = []; current = new Map();
  request(event, time = performance.now()) {
    if (event.redirectResponse) this.finish({ requestId: event.requestId, encodedDataLength: event.redirectResponse.encodedDataLength }, time);
    const record = { time, url: pathOf(event.request.url), method: event.request.method, type: event.type,
      chunks: [], status: null, finish: null, failure: null };
    this.records.push(record); this.current.set(event.requestId, record);
  }
  response(event) { const record = this.current.get(event.requestId); if (record) record.status = event.response.status; }
  data(event, time = performance.now()) {
    this.current.get(event.requestId)?.chunks.push({ time, wire: event.encodedDataLength, decoded: event.dataLength });
  }
  finish(event, time = performance.now()) {
    const record = this.current.get(event.requestId);
    if (record) record.finish = { time, wire: event.encodedDataLength };
  }
  failed(event, time = performance.now()) {
    const record = this.current.get(event.requestId); if (record) record.failure = { time, reason: event.errorText, canceled: event.canceled === true };
  }
  window(start, end) {
    const result = { requests: 0, completed: 0, failed: 0, wire_bytes: 0, decoded_body_bytes: 0,
      js_wire_bytes: 0, css_wire_bytes: 0, js_decoded_bytes: 0, css_decoded_bytes: 0, by_path: {} };
    for (const record of this.records) {
      if (record.time < start || record.time >= end) continue;
      const chunks = record.chunks.filter(c => c.time < end);
      const completed = record.finish && record.finish.time < end;
      const wire = completed ? record.finish.wire : chunks.reduce((n, c) => n + c.wire, 0);
      const decoded = chunks.reduce((n, c) => n + c.decoded, 0);
      result.requests++; result.completed += Number(!!completed); result.failed += Number(!!record.failure && record.failure.time < end);
      result.wire_bytes += wire; result.decoded_body_bytes += decoded;
      const asset = record.type === 'Script' ? 'js' : record.type === 'Stylesheet' ? 'css' : null;
      if (asset) { result[`${asset}_wire_bytes`] += wire; result[`${asset}_decoded_bytes`] += decoded; }
      const path = result.by_path[record.url] ??= { requests: 0, wire_bytes: 0 };
      path.requests++; path.wire_bytes += wire;
    }
    return result;
  }
}

export function sockets(events, start, end) {
  const result = { connections: 0, received_payload_bytes: 0, sent_payload_bytes: 0 };
  for (const event of events) {
    if (event.time < start || event.time >= end) continue;
    if (event.kind === 'open') result.connections++;
    else result[`${event.kind}_payload_bytes`] += Buffer.byteLength(event.payload, event.opcode === 2 ? 'base64' : 'utf8');
  }
  return result;
}

export function resumeTrials(source, buildHash) {
  if (source.smoke !== false) throw new Error('Cannot resume smoke measurements as a baseline');
  if (source.build_manifest_sha256 !== buildHash) throw new Error('Product build changed since the saved measurements');
  if (source.runs_per_condition !== 5 || source.session_dwell_ms !== 300000 || source.cold_window_ms !== 10000 || source.polling_window_ms !== 60000) throw new Error('Measurement windows changed');
  const seen = new Set();
  return source.trials.map(row => {
    const key = `${row.projects}:${row.shell}:${row.run}`;
    if (seen.has(key)) throw new Error('Duplicate completed observation'); seen.add(key);
    if (![10, 100, 500].includes(row.projects) || row.sessions !== ({ 10: 5, 100: 50, 500: 200 })[row.projects] || !['old', 'new'].includes(row.shell) || !Number.isInteger(row.run) || row.run < 1 || row.run > 5) throw new Error('Invalid observation identity');
    if (!(row.memory_after_ms >= 299990) || !Number.isFinite(row.memory_5m?.js_heap_used_bytes)) throw new Error('Incomplete five-minute memory observation');
    if (row.polling?.visible.observed_visibility !== 'visible' || row.polling?.hidden.observed_visibility !== 'hidden' || row.page_errors?.length) throw new Error('Invalid visibility or page-error observation');
    if (['Projects', 'Overview', 'Session'].some(k => !Number.isFinite(row.pages?.[k]?.interactive_proxy_ms) || row.pages[k].failed !== (row.pages[k].expected_cancellations?.length ?? 0) || row.pages[k].expected_cancellations?.some(r => !expectedCancellation(r)))) throw new Error('Incomplete cold-route observation');
    return { ...row, harness_revision: row.harness_revision ?? source.revision };
  });
}

export function expectedCancellation(request) {
  return request.canceled === true && request.reason === 'net::ERR_ABORTED' && request.type === 'Image'
    && ['/rynhult-mark-dark.svg', '/rynhult-mark-light.svg'].includes(request.url);
}
