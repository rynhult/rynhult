export type ChatItem = { id: string; revision: string; role: 'user' | 'assistant'; text: string };
export type ChatPage = {
  session_id: string; run_id: number | null; state: 'ready' | 'empty' | 'unknown' | 'unsupported' | 'offline' | 'error' | 'gap' | 'busy';
  historical_run_attribution: 'unknown'; observed_at: string | null; items: ChatItem[];
  omitted: number; incomplete?: boolean; reset: boolean; more?: boolean; notices?: { tools: number }; reason?: string | null; cursor: string; before_cursor: string | null;
  capabilities: { read_history: boolean; follow_messages: boolean; rich_tools: boolean };
};
export function canFollow(page: ChatPage): boolean {
  if (['offline', 'error', 'unsupported', 'busy'].includes(page.state)) return false;
  return page.capabilities.follow_messages || (['unknown', 'gap'].includes(page.state) && page.reason !== 'discovery_budget' && (!!page.more || page.reason === 'partial_record'));
}
/** Observations are not acknowledgements. Failed windows cannot move position. */
export function acceptChatPage(previous: ChatPage | undefined, next: ChatPage, fresh = false): ChatPage {
  const consumable = canFollow(next);
  const replace = fresh && consumable;
  return { ...next, cursor: consumable ? next.cursor : previous?.cursor ?? '',
    before_cursor: consumable ? next.before_cursor : previous?.before_cursor ?? null,
    omitted: replace ? next.omitted : Math.max(previous?.omitted ?? 0, next.omitted),
    incomplete: next.state === 'gap' || next.omitted > 0 || (!replace && !!previous?.incomplete) || (!replace && previous?.state === 'gap') };
}
export function mergeChatItems(current: ChatItem[], incoming: ChatItem[], mode: 'older' | 'follow') {
  const map = new Map(current.map(item => [item.id, item]));
  if (mode === 'older') {
    const older = incoming.filter(item => !map.has(item.id));
    for (const item of older) map.set(item.id, item);
    if (!older.length) return { items: current, overflow: false };
    const ordered = [...new Map([...older, ...current].map(item => [item.id, item])).values()];
    return bound(current, ordered);
  }
  let changed = false;
  for (const item of incoming) {
    const previous = map.get(item.id);
    if (!previous || previous.revision !== item.revision || previous.text !== item.text || previous.role !== item.role) { map.set(item.id, item); changed = true; }
  }
  return changed ? bound(current, [...map.values()]) : { items: current, overflow: false };
}
function bound(original: ChatItem[], items: ChatItem[]) {
  const overflow = items.length > 1000 || items.reduce((sum, item) => sum + new TextEncoder().encode(item.text).length, 0) > 2_097_152;
  return { items: overflow ? original : items, overflow };
}
export function cleanChatText(text: string): string {
  const parts: string[] = [];
  let start = 0, i = 0;
  while (i < text.length) {
    const c = text.charCodeAt(i);
    if (c === 27) {
      if (i > start) parts.push(text.slice(start, i));
      i++;
      if (text[i] === ']') {
        i++;
        while (i < text.length && text.charCodeAt(i) !== 7 && !(text.charCodeAt(i) === 27 && text[i + 1] === '\\')) i++;
        i += text.charCodeAt(i) === 7 ? 1 : i < text.length ? 2 : 0;
      } else if (text[i] === '[') {
        i++;
        while (i < text.length && text.charCodeAt(i) >= 0x30 && text.charCodeAt(i) <= 0x3f) i++;
        while (i < text.length && text.charCodeAt(i) >= 0x20 && text.charCodeAt(i) <= 0x2f) i++;
        if (i < text.length && text.charCodeAt(i) >= 0x40 && text.charCodeAt(i) <= 0x7e) i++;
      }
      start = i;
    } else if (c <= 8 || c >= 11 && c <= 31 || c === 127 || c >= 0x202a && c <= 0x202e || c >= 0x2066 && c <= 0x2069) {
      if (i > start) parts.push(text.slice(start, i));
      start = ++i;
    } else i++;
  }
  if (start < text.length) parts.push(text.slice(start));
  return parts.join('');
}
export function safeChatLink(value: string | undefined): string | null {
  if (!value || /[\x00-\x20\x7f]/.test(value) || !/^https?:\/\//i.test(value)) return null;
  try { const url = new URL(value); return url.username || url.password ? null : url.href; } catch { return null; }
}
export function chatPage(value: unknown): ChatPage {
  const page = value as ChatPage;
  if (!page || !['ready', 'empty', 'unknown', 'unsupported', 'offline', 'error', 'gap', 'busy'].includes(page.state)
    || typeof page.session_id !== 'string' || !(page.run_id === null || Number.isSafeInteger(page.run_id))
    || !Array.isArray(page.items) || page.items.length > 200 || typeof page.cursor !== 'string' || page.cursor.length > 4096
    || !(page.before_cursor === null || (typeof page.before_cursor === 'string' && page.before_cursor.length <= 4096))
    || typeof page.reset !== 'boolean' || !Number.isSafeInteger(page.omitted) || page.omitted < 0
    || (page.more !== undefined && typeof page.more !== 'boolean')
    || (page.notices !== undefined && (!Number.isSafeInteger(page.notices.tools) || page.notices.tools < 0 || page.notices.tools > 1_000_000))
    || !page.capabilities || typeof page.capabilities.read_history !== 'boolean' || typeof page.capabilities.follow_messages !== 'boolean'
    || page.items.some(item => !item || !/^[a-f0-9]{64}$/.test(item.id) || !/^[a-f0-9]{64}$/.test(item.revision)
      || !['user', 'assistant'].includes(item.role) || typeof item.text !== 'string' || new TextEncoder().encode(item.text).length > 131072)) throw new Error('Chat returned an unreadable page. Open Terminal or reload Chat.');
  if (new TextEncoder().encode(JSON.stringify(page.items)).length > 262_144) throw new Error('Chat page is too large. Open Terminal.');
  return page;
}
/** Bounded complete SSE records only; callers retain the last consumed cursor. */
export async function* chatEvents(response: Response): AsyncGenerator<ChatPage> {
  if (!response.body || !response.headers.get('Content-Type')?.includes('text/event-stream')) throw new Error('Chat follow is unavailable.');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let pending = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      pending += decoder.decode(value, { stream: true });
      if (pending.length > 1_048_576) throw new Error('Chat event is too large. Reload Chat.');
      let match: RegExpExecArray | null;
      while ((match = /\r?\n\r?\n/.exec(pending))) {
        const frame = pending.slice(0, match.index); pending = pending.slice(match.index + match[0].length);
        const lines = frame.split(/\r?\n/);
        if (lines.some(line => line === 'event: chat')) {
          const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
          yield chatPage(JSON.parse(data));
        }
      }
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
