import { useCallback, useLayoutEffect, useRef, useState, type Ref } from 'react';
import { stamp } from '../lib/format';
import { ChatMessage } from './ChatSafety';
import { announce, Button, ErrorNote, Skeleton } from './ui';
import type { SessionChatState } from './useSessionChat';

const states = {
  busy: 'Chat source is busy. Waiting to retry; retained messages may be stale.',
  ready: 'Messages from the native conversation.', empty: 'No messages yet. The source was read successfully.',
  unknown: 'The Chat source is not established yet. Open Terminal or retry after the session checks in.',
  unsupported: 'This provider or CLI version does not have a qualified Chat reader. Use Terminal.',
  offline: 'Machine offline. Retained messages may be stale; Chat updates resume after a retry.',
  error: 'The account service could not read Chat. Retained messages may be stale.',
  gap: 'Source reports omitted or discontinuous records.',
};

export function ChatPanel({ chat, provider, active, onTerminal, headingRef }: {
  chat: SessionChatState; provider: string; active: boolean; onTerminal: () => void; headingRef?: Ref<HTMLHeadingElement>;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const anchor = useRef<{ id?: string; offset: number; bottom: boolean }>({ offset: 0, bottom: true });
  const last = useRef<string | undefined>(undefined);
  const [newMessages, setNewMessages] = useState(false);
  const capture = useCallback(() => {
    const el = scroller.current;
    if (!el?.clientHeight) return;
    const top = el.getBoundingClientRect().top;
    const item = [...el.querySelectorAll<HTMLElement>('[data-chat-item]')].find(item => item.getBoundingClientRect().bottom > top + 1);
    anchor.current = { id: item?.dataset.chatItem, offset: item ? item.getBoundingClientRect().top - top : 0, bottom: el.scrollHeight - el.scrollTop - el.clientHeight < 48 };
  }, []);
  const restore = useCallback(() => {
    const el = scroller.current;
    if (!el?.clientHeight) return;
    if (anchor.current.bottom) el.scrollTop = el.scrollHeight;
    else {
      const item = [...el.querySelectorAll<HTMLElement>('[data-chat-item]')].find(item => item.dataset.chatItem === anchor.current.id);
      if (item) el.scrollTop += item.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.current.offset;
    }
    capture();
  }, [capture]);
  useLayoutEffect(() => {
    if (!active) return;
    const latest = chat.items.at(-1)?.id;
    if (last.current && latest && last.current !== latest) {
      if (!anchor.current.bottom) setNewMessages(true);
      announce('New Chat messages available.');
    }
    last.current = latest;
    restore();
  }, [chat.items, active, restore]);
  useLayoutEffect(() => {
    const observer = new ResizeObserver(restore);
    if (scroller.current) observer.observe(scroller.current);
    return () => observer.disconnect();
  }, [restore]);
  const jump = () => { anchor.current = { offset: 0, bottom: true }; restore(); setNewMessages(false); };
  return <section aria-label="Session chat" className="relative flex h-full min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-line bg-panel">
    <header className="shrink-0 space-y-2 border-b border-line px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 ref={headingRef} tabIndex={-1} className="text-sm font-semibold">Chat <span className="font-normal text-muted">· Read-only</span></h2>
        <Button size="sm" onClick={onTerminal}>Open Terminal</Button>
      </div>
      <p className="text-xs text-muted">Source: {provider} · CLI version not reported</p>
      <p className="text-xs text-muted">{chat.loading ? 'Reading Chat…' : chat.page ? states[chat.page.state] : 'Chat source unavailable.'}</p>
      {chat.page?.observed_at && <p className="text-xs text-faint">Source observed {stamp(chat.page.observed_at)}{chat.paused ? ' · Updates paused while hidden' : chat.following ? ' · Following' : ' · Snapshot'}</p>}
      {chat.page?.incomplete && <p role="status" className="text-xs text-wait">This history is incomplete. Some records were omitted or continuity could not be verified.</p>}
      {chat.page?.more && <p className="text-xs text-muted">More history is being checked; this snapshot may be incomplete.</p>}
      {!!chat.toolNotices && <details className="text-xs text-muted"><summary className="cursor-pointer">Tool records · at least {chat.toolNotices}</summary><p className="mt-2">Tool activity was observed. Individual tool details and arguments are unavailable in this text-only reader.</p></details>}
      <details className="text-xs text-muted"><summary className="cursor-pointer">Source details</summary>
        <p className="mt-2">Text messages only. Tool details and attachments are not available in this read-only source. Historical messages have no verified process-run attribution.</p>
        {!!chat.page?.omitted && <p>At least {chat.page.omitted} records omitted. This is not a complete archive.</p>}
      </details>
      <ErrorNote error={chat.error} />
      {chat.limit && <p role="status" className="text-xs text-wait">This view reached its reading limit. Reload Chat for the newest page; retained messages have not been discarded.</p>}
      <div className="flex flex-wrap items-center gap-2">
        {chat.before && <Button size="sm" disabled={chat.older || chat.reset || chat.limit} onClick={() => { capture(); void chat.loadOlder(); }}>{chat.older ? 'Loading earlier…' : 'Load earlier'}</Button>}
        <Button size="sm" onClick={() => { anchor.current = { offset: 0, bottom: true }; chat.reload(); }}>Reload Chat</Button>
      </div>
    </header>
    <div ref={scroller} role="region" aria-label="Chat messages" tabIndex={0} onScroll={() => { capture(); if (anchor.current.bottom) setNewMessages(false); }}
      className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain px-4 py-3 [overflow-anchor:none]">
      {chat.loading && !chat.items.length && <Skeleton className="h-24" />}
      <ol className="mx-auto min-w-0 max-w-3xl space-y-4">
        {chat.items.map((item, index) => <ChatMessage key={item.id} item={item} rich={index >= chat.items.length - 40} />)}
      </ol>
    </div>
    {newMessages && <Button size="sm" onClick={jump} className="absolute bottom-3 right-3 z-10 max-w-[calc(100%-1.5rem)] shadow-pop">New messages · Jump to latest</Button>}
  </section>;
}
