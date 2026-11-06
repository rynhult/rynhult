import { Component, memo, useRef, type ReactNode } from 'react';
import { cleanChatText, type ChatItem } from '../lib/chat';
import { parseChatMarkdown } from '../lib/chat-markdown';
import { CopyButton } from './InstallSteps';

/** Content failures stay inside Chat; never bubble into the session/Terminal boundary. */
export class ChatBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch() { console.warn('Chat rendering failed; isolated fallback shown.'); }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}
function Code({ text }: { text: string }) {
  const ref = useRef<HTMLPreElement>(null);
  return <div className="min-w-0 max-w-full space-y-2"><CopyButton text={text} label="Copy code" target={ref} />
    <pre ref={ref} tabIndex={0} role="group" aria-label="Chat code" className="max-w-full overflow-auto whitespace-pre-wrap">{text}</pre></div>;
}
export const ChatMessage = memo(function ChatMessage({ item, rich }: { item: ChatItem; rich: boolean }) {
  const fallback = <><p className="text-xs text-muted">Formatting unavailable. Showing plain text.</p><pre className="whitespace-pre-wrap [overflow-wrap:anywhere]">{cleanChatText(item.text.slice(0, 131072))}</pre></>;
  return <li data-chat-item={item.id} className="min-w-0 rounded-md border border-line p-3">
    <h3 className="mb-2 text-xs font-semibold text-muted">{item.role === 'assistant' ? 'Agent' : 'Person'}</h3>
    <div className="wiki-prose min-w-0 text-[13px] [overflow-wrap:anywhere]">
      <ChatBoundary key={item.revision} fallback={fallback}><Content text={item.text} rich={rich} /></ChatBoundary>
    </div>
  </li>;
});
function Content({ text, rich }: { text: string; rich: boolean }) {
  const parsed = rich ? parseChatMarkdown(text) : {plain: true, text: cleanChatText(text.slice(0,131072)), blocks: []};
  if (parsed.plain) return <><p className="text-xs text-muted">Message shown as plain text within Chat’s formatting safety limits.</p><pre className="whitespace-pre-wrap [overflow-wrap:anywhere]">{parsed.text}</pre></>;
  return <>{parsed.blocks.map((block, i) => block.kind === 'code' ? <Code key={i} text={block.text} /> :
    <p key={i} className="whitespace-pre-wrap">{block.spans.map((span, j) => span.kind === 'link' ? <a key={j} href={span.url} target="_blank" rel="noopener noreferrer">{span.text} <span className="text-xs">({span.url})</span></a> :
      span.kind === 'strong' ? <strong key={j}>{span.text}</strong> : span.kind === 'em' ? <em key={j}>{span.text}</em> : span.kind === 'code' ? <code key={j}>{span.text}</code> : <span key={j}>{span.text}</span>)}</p>)}</>;
}
