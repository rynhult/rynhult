/** Orchestrator v2, slice 1: the team's working list and NOW (src/acc/server/orchestrator/worklist.py). Mounted on the
 *  Team tab; renders nothing while the server has orchestrator v2 off (the list answers 404). The lead's text is shown
 *  as plain text, never as HTML. Only exact local brief-review paths become additional navigation links. */
import { Link } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, MoreHorizontal } from 'lucide-react';
import { api, ApiError, isNotFound } from '../lib/api';
import { clearDraft, loadDraft, saveDraft } from '../lib/drafts';
import { useRole } from '../lib/queries';
import { stamp } from '../lib/format';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { Badge, Button, Card, CardHeader, ErrorNote, IconButton, Input, Note, Popover, Select, Textarea } from './ui';

type Kind = 'need' | 'decision' | 'question' | 'task' | 'note';
type State = 'open' | 'answered' | 'ingested' | 'done' | 'dismissed';
interface Item {
  id: number; position: number; kind: Kind; title: string; steps: string; state: State; revision: number;
  options: { id: string; label: string; consequence: string }[] | null;
  recommendation: { option: string | null; reason: string } | null;
  /** What the lead goes ahead with until the person answers (slice 6). */
  default: { option?: string; text?: string } | null;
  blocks?: string[];
  in_effect: { from: 'answer' | 'default'; value: string } | null;
  answer: string | null; answered_by: string | null; answered_at: string | null; note: string | null;
  source: string; owner_edited: boolean; links: { kind: string; id: string }[]; done_at: string | null;
}
interface List { project_id?: number; now: string; now_updated_at: string | null; now_updated_by: string | null; now_revision: number; revision: number; idle_minutes: number; items: Item[] }
type ItemPatch = Partial<Pick<Item, 'answer' | 'blocks' | 'position' | 'state'>> & { answer_text?: string };
interface ItemConflict { before: Item; current: Item; body: ItemPatch; reason: string }

function cardChanges(before: Item, after: Item) {
  return (['title', 'steps', 'state', 'options', 'recommendation', 'blocks', 'answer', 'note'] as const)
    .filter(field => JSON.stringify(before[field]) !== JSON.stringify(after[field]))
    .map(field => `${field}: was ${typeof before[field] === 'string' ? before[field] : JSON.stringify(before[field])}; now ${typeof after[field] === 'string' ? after[field] : JSON.stringify(after[field])}`);
}

const KIND_WORDS: Record<Kind, string> = { need: 'Needs you', decision: 'Decision', question: 'Question', task: 'Lead task', note: 'Note' };
const STATE_WORDS: Record<State, string> = { open: 'Open', answered: 'Answered', ingested: 'Read by the lead', done: 'Done', dismissed: 'Dismissed' };
const FOR_PERSON: Kind[] = ['need', 'decision', 'question'];

export function WorkList({ teamId }: { teamId: string }) {
  const client = useQueryClient();
  const key = ['worklist', teamId];
  const query = useQuery({
    queryKey: key, queryFn: () => api<List>(`/api/teams/${teamId}/worklist`), refetchInterval: 3000,
    retry: (count, error) => !isNotFound(error) && count < 2,
  });
  const [conflict, setConflict] = useState(false);
  const [cardConflicts, setCardConflicts] = useState<Record<number, ItemConflict>>({});
  const refresh = (error?: unknown) => {
    if (error instanceof ApiError && error.status === 409) setConflict(true);
    client.invalidateQueries({ queryKey: key });
    client.invalidateQueries({ queryKey: ['plan', teamId] });
  };
  const change = useMutation({
    mutationFn: ({ item, body }: { item: Item; body: ItemPatch }) =>
      api<Item>(`/api/teams/${teamId}/worklist/items/${item.id}`, { method: 'PATCH', json: { ...body, revision: item.revision } }),
    onSuccess: (saved, { item, body }) => {
      if ('answer' in body || 'answer_text' in body) clearDraft(`answer:${item.id}`);
      setCardConflicts(old => { const next = { ...old }; delete next[item.id]; return next; });
      client.setQueryData<List>(key, old => old && { ...old, items: old.items.map(i => i.id === saved.id ? saved : i) });
      setConflict(false); refresh();
    },
    onError: (error, { item, body }) => {
      if (error instanceof ApiError && error.status === 409) {
        const data = error.data as { item?: Item; worklist?: List } | undefined;
        const current = data?.item ?? data?.worklist?.items.find(i => i.id === item.id);
        if (current) setCardConflicts(old => ({ ...old, [item.id]: { before: item, current, body, reason: error.message } }));
        else setConflict(true);
        if (data?.worklist) client.setQueryData(key, data.worklist);
      }
      refresh();
    },
  });
  if (query.error && isNotFound(query.error)) return null;
  if (!query.data) return query.error ? <ErrorNote error={query.error} title="Couldn't load the working list" onRetry={() => query.refetch()} /> : null;
  const list = query.data;
  const items = list.items.map(i => cardConflicts[i.id]?.current.revision > i.revision ? cardConflicts[i.id].current : i)
    .sort((a, b) => a.position - b.position || a.id - b.id);
  const active = items.filter((i) => i.state !== 'done' && i.state !== 'dismissed');
  const yours = active.filter((i) => FOR_PERSON.includes(i.kind) && i.state === 'open');
  const answered = active.filter((i) => i.state === 'answered' || i.state === 'ingested');
  const rest = active.filter((i) => !yours.includes(i) && !answered.includes(i));
  const closed = items.filter((i) => i.state === 'done' || i.state === 'dismissed');
  const move = (items: Item[], index: number, by: -1 | 1) => {
    const other = items[index + by];
    const after = items[index + by * 2];
    if (!other) return;
    // Land between the neighbour and the one past it, so only one item changes.
    const position = after ? (other.position + after.position) / 2 : other.position + by;
    change.mutate({ item: items[index], body: { position } });
  };
  const section = (title: string, items: Item[], empty: string) => <section aria-label={title} className="space-y-2">
    <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">{title}</h3>
    {items.length ? <ol className="space-y-2">{items.map((item, index) =>
      <ItemRow key={item.id} teamId={teamId} projectId={list.project_id} item={item} busy={change.isPending}
        conflict={cardConflicts[item.id]}
        onChange={(body, base = item) => change.mutate({ item: base, body })}
        up={index > 0 ? () => move(items, index, -1) : undefined}
        down={index < items.length - 1 ? () => move(items, index, 1) : undefined} />)}</ol>
      : <p className="text-xs text-muted">{empty}</p>}
  </section>;
  return <Card>
    <CardHeader title="Working list" subtitle="What the lead is doing, what it needs from you, and what you decided." />
    <div className="space-y-5 p-4">
      {conflict && <Note tone="wait" role="status">Someone changed the list while you were editing. It now shows the latest version; check it and try again.</Note>}
      <NowEditor teamId={teamId} list={list} onSaved={(error) => { if (error) refresh(error); else { setConflict(false); refresh(); } }} />
      {section('What the lead needs from you', yours, 'Nothing is waiting for you.')}
      {section('Team tasks and notes', rest, 'No open tasks.')}
      {answered.length > 0 && section('Answered', answered, '')}
      <AddItem teamId={teamId} onAdded={() => refresh()} />
      {change.error && !(change.error instanceof ApiError && change.error.status === 409) ? <ErrorNote error={change.error} /> : null}
      {closed.length > 0 && <details className="text-sm" open={closed.some(i => !!cardConflicts[i.id]) || undefined}>
        <summary className="cursor-pointer text-xs text-muted">Done and dismissed ({closed.length})</summary>
        <ol className="mt-2 space-y-2">{closed.map((item) => <ItemRow key={item.id} teamId={teamId} projectId={list.project_id} item={item} busy={change.isPending}
          conflict={cardConflicts[item.id]} onChange={(body, base = item) => change.mutate({ item: base, body })} />)}</ol>
      </details>}
    </div>
  </Card>;
}

function NowEditor({ teamId, list, onSaved }: { teamId: string; list: List; onSaved: (error?: unknown) => void }) {
  // An unsaved NOW is kept for this tab: a refresh or Back brings it back (break-it review 2026-10-26).
  const storeKey = `now:${teamId}`;
  const [restored] = useState(() => loadDraft<{ draft: string; base: number; stale: boolean }>(storeKey));
  const [draft, setDraft] = useState<string | null>(restored?.draft ?? null);
  // The NOW the person started from (break-it review 2026-10-26): a newer NOW from the lead is refused, not silently
  // replaced. After that, the editor shows the latest NOW, and saving again is a deliberate choice.
  const [base, setBase] = useState(restored?.base ?? 0);
  const [stale, setStale] = useState(restored?.stale ?? false);
  useEffect(() => {
    if (draft === null) clearDraft(storeKey); else saveDraft(storeKey, { draft, base, stale });
  }, [storeKey, draft, base, stale]);
  const save = useMutation({
    mutationFn: (text: string) => api(`/api/teams/${teamId}/worklist/now`, { method: 'PUT', json: {
      text, revision: list.revision, now_revision: stale ? list.now_revision : base } }),
    onSuccess: () => { setDraft(null); setStale(false); onSaved(); },
    onError: (error) => { if (error instanceof ApiError && error.status === 409) setStale(true); onSaved(error); },
  });
  return <section aria-label="Now" className="space-y-2 rounded-md border border-line bg-panel-2 p-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">Now</h3>
      {draft === null && <Button size="sm" onClick={() => { setDraft(list.now); setBase(list.now_revision); setStale(false); }}>Edit</Button>}
    </div>
    {draft === null
      ? <>
        <p className="whitespace-pre-wrap break-words text-sm">{list.now || <span className="text-muted">The lead hasn't written a summary yet.</span>}</p>
        {list.now_updated_at && <p className="text-xs text-muted">{list.now_updated_by === 'lead' ? 'Lead' : list.now_updated_by} · {stamp(list.now_updated_at)}</p>}
      </>
      : <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); save.mutate(draft); }}>
        {stale && <p className="whitespace-pre-wrap break-words rounded-md border border-line p-2 text-sm">
          <span className="text-muted">Latest NOW: </span>{list.now || <span className="text-muted">empty</span>}</p>}
        <Textarea aria-label="Now summary" rows={4} maxLength={4096} value={draft} onChange={(e) => setDraft(e.target.value)} className="w-full" />
        <div className="flex gap-2"><Button type="submit" variant="primary" size="sm" disabled={save.isPending}>Save</Button>
          <Button size="sm" onClick={() => setDraft(null)}>Cancel</Button></div>
        {save.error && !(save.error instanceof ApiError && save.error.status === 409) ? <ErrorNote error={save.error} /> : null}
      </form>}
  </section>;
}

function ItemRow({ teamId, projectId, item, busy, conflict, onChange, up, down }: {
  teamId: string; projectId?: number; item: Item; busy: boolean; conflict?: ItemConflict; onChange: (body: ItemPatch, base?: Item) => void; up?: () => void; down?: () => void;
}) {
  // A typed answer is kept for this tab until the card is answered: a refresh or Back brings it back.
  const { owner } = useRole();
  const replyKey = `answer:${item.id}`;  // item ids are unique across teams
  const [changing, setChanging] = useState(!!conflict?.body.answer_text);
  const [discussing, setDiscussing] = useState(false);
  const [reply, setReply] = useState(() => typeof conflict?.body.answer_text === 'string' ? conflict.body.answer_text : item.kind !== 'decision' && typeof conflict?.body.answer === 'string' ? conflict.body.answer : loadDraft<string>(replyKey) ?? '');
  const [blockEditing, setBlockEditing] = useState(!!conflict?.body.blocks);
  const [blockDraft, setBlockDraft] = useState((conflict?.body.blocks ?? item.blocks ?? []).join(', '));
  const [blockBase, setBlockBase] = useState(item);
  useEffect(() => {
    if (!conflict && blockEditing && item.revision > blockBase.revision && (item.blocks ?? []).join(', ') === blockDraft) setBlockEditing(false);
  }, [conflict, blockEditing, item.revision, item.blocks, blockBase, blockDraft]);
  // The card as it was when the person chose or typed (break-it review 2026-10-26). If the lead rewrites it meanwhile,
  // the answer isn't sent with a meaning the person didn't see: the card says so, and answering again is deliberate.
  const [seen, setSeen] = useState<Item | null>(conflict?.before ?? null);
  const changedSince = seen !== null && seen.revision !== item.revision;
  const answer = (value: string, custom = false) => {
    onChange(custom ? { answer_text: value } : { answer: value }, conflict || changedSince ? item : seen ?? item);
  };
  const open = item.state === 'open';
  const closed = item.state === 'done' || item.state === 'dismissed';
  const answerable = (open || conflict?.body.answer !== undefined || conflict?.body.answer_text !== undefined) && FOR_PERSON.includes(item.kind);
  useEffect(() => {
    if (reply) saveDraft(replyKey, reply); else clearDraft(replyKey);
  }, [replyKey, reply]);
  const retry = (!!conflict && conflict.current.revision !== conflict.before.revision) || changedSince;
  const differences = conflict || changedSince ? cardChanges(conflict?.before ?? seen!, item) : [];
  const unansweredDecision = (item.kind === 'decision' || item.kind === 'need') && item.answer === null;
  const labelOf = (id: string | null) => item.options?.find((o) => o.id === id)?.label ?? id;
  const pendingChoice = item.kind === 'decision' ? conflict?.body.answer : undefined;
  const canEditBlocks = (!closed || conflict?.body.blocks !== undefined) && (item.kind === 'decision' || item.kind === 'question');
  const editBlocks = () => { setBlockDraft((item.blocks ?? []).join(', ')); setBlockBase(item); setBlockEditing(true); };
  return <li className="space-y-2 rounded-md border border-line p-3" aria-label={item.title}>
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={item.kind === 'need' || item.kind === 'decision' ? 'wait' : 'neutral'}>{KIND_WORDS[item.kind]}</Badge>
          {item.state !== 'open' && <Badge tone={item.state === 'done' ? 'ok' : 'neutral'}>{STATE_WORDS[item.state]}</Badge>}
          {/* Who wrote it: agent text never reads as Rynhult's or the person's (security review 2026-10-25). */}
          {item.source === 'system' && <Badge tone="info">From Rynhult</Badge>}
          {item.source === 'lead' && !item.owner_edited && <Badge>Written by the team lead (an agent)</Badge>}
          {item.source.startsWith('session:') && <Badge>Relayed from a helper's report (an agent)</Badge>}
          {item.owner_edited && <Badge>Edited by a person</Badge>}
        </div>
        <h4 className="break-words text-sm font-medium">{item.title}</h4>
      </div>
      {(conflict || changedSince) && <Note tone="wait" role="alert">
        <p>{retry ? 'This card changed.' : 'Your edit was not saved.'} Your unsaved answer or edit is kept.</p>
        {conflict && <p>{conflict.reason}</p>}
        {differences.length > 0 && <ul className="list-disc space-y-1 pl-4">{differences.map(line => <li className="whitespace-pre-wrap break-words" key={line}>{line}</li>)}</ul>}
        {retry && <p>Review the current card and resend your edit.</p>}
      </Note>}
      {(up || down || canEditBlocks && !blockEditing) && <Popover className="sm:hidden" label={`More actions for "${item.title}"`}
        trigger={<MoreHorizontal size={16} aria-hidden="true" />} menu>{close => <>
        {(up || down) && <>
          <Button role="menuitem" size="sm" variant="ghost" className="w-full justify-start" disabled={!up || busy} onClick={() => { close(); up?.(); }}>Move up</Button>
          <Button role="menuitem" size="sm" variant="ghost" className="w-full justify-start" disabled={!down || busy} onClick={() => { close(); down?.(); }}>Move down</Button>
        </>}
        {canEditBlocks && !blockEditing && <Button role="menuitem" size="sm" variant="ghost" className="w-full justify-start" disabled={busy}
          onClick={() => { close(); editBlocks(); }}>Edit blocking links</Button>}
      </>}</Popover>}
      {(up || down) && <div className="hidden gap-1 sm:flex">
        <IconButton label={`Move "${item.title}" up`} icon={ArrowUp} disabled={!up || busy} onClick={up} />
        <IconButton label={`Move "${item.title}" down`} icon={ArrowDown} disabled={!down || busy} onClick={down} />
      </div>}
    </div>
    {!!item.blocks?.length && <p className="text-xs text-wait">Blocks: {item.blocks.join(', ')}{item.state === 'open' ? ' · waiting for your answer' : ''}</p>}
    {canEditBlocks && (blockEditing
      ? <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => {
        e.preventDefault(); onChange({ blocks: blockDraft.split(',').map((k) => k.trim()).filter(Boolean) }, conflict ? item : blockBase);
      }}>
        <Input aria-label={`Blocking links for "${item.title}"`} value={blockDraft} maxLength={4000} className="min-w-0 flex-1" disabled={busy}
          onChange={(e) => setBlockDraft(e.target.value)} placeholder="Phase keys or R-n, separated by commas" />
        <Button size="sm" type="submit" disabled={busy}>{conflict?.body.blocks ? 'Resend blocking links' : 'Save blocking links'}</Button>
        <Button size="sm" onClick={() => setBlockEditing(false)}>Cancel</Button>
      </form>
      : <div className="hidden sm:block"><Button size="sm" variant="ghost" disabled={busy} onClick={editBlocks}>Edit blocking links</Button></div>)}
    {item.steps && <p className="whitespace-pre-wrap break-words text-sm text-muted">{item.steps}</p>}
    {[...new Set(item.steps.match(/\/projects\/[1-9][0-9]*\?brief_review=[1-9][0-9]*#project-brief/g) ?? [])].slice(0, 20).map(path =>
      <Link key={path} to={path} className="inline-flex min-h-11 items-center text-sm text-accent underline">Review proposed brief</Link>)}
    {item.options && <ul className="space-y-2 text-sm">{item.options.map((o) => <li key={o.id} className="space-y-1">
      {answerable && item.kind === 'decision'
        ? <Button size="sm" variant={o.id === item.recommendation?.option ? 'primary' : 'secondary'}
          className="h-auto! min-h-7 max-w-full py-1.5 text-left pointer-coarse:min-h-11" disabled={busy}
          aria-describedby={o.consequence ? `option-${item.id}-${o.id}` : undefined}
          onClick={() => { setChanging(false); onChange({ answer: o.id }, item); }}>
          <span className="min-w-0 whitespace-normal break-words">{o.label}</span>
          {o.id === item.recommendation?.option && <span className="text-[11px] font-normal">Recommended</span>}
        </Button>
        : <><span className="font-medium">{o.label}</span>{o.id === item.recommendation?.option && <Badge tone="accent" className="ml-1.5">Recommended</Badge>}</>}
      {o.consequence && <p id={`option-${item.id}-${o.id}`} className="break-words text-xs text-muted">{o.consequence}</p>}
    </li>)}</ul>}
    {item.recommendation && <p className="whitespace-pre-wrap break-words text-xs text-muted">Lead's reasoning: {item.recommendation.reason}</p>}
    {item.in_effect?.from === 'default' && <p className="flex flex-wrap items-baseline gap-1.5 rounded-md border border-line bg-panel-2 px-2.5 py-1.5 text-sm">
      <Badge tone="info">Assumed default</Badge><span className="text-xs text-muted">Default until you answer</span>
      <span className="break-words">The lead goes ahead with <span className="font-medium">{item.kind === 'decision' ? labelOf(item.in_effect.value) : item.in_effect.value}</span>. Your answer replaces it.</span>
    </p>}
    {item.answer !== null && <p className="whitespace-pre-wrap break-words text-sm">
      <span className="text-muted">Answer from {item.answered_by}{item.answered_at ? ` · ${stamp(item.answered_at)}` : ''}: </span>
      {item.kind === 'decision' ? labelOf(item.answer) : item.answer}</p>}
    {item.note && <p className="whitespace-pre-wrap break-words text-xs text-muted">Lead's note: {item.note}</p>}
    {answerable && (item.kind === 'decision'
      ? <div className="space-y-2">
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={busy} onClick={() => setChanging(!changing)}>Change</Button>
          <Button size="sm" disabled={busy} onClick={() => setDiscussing(!discussing)}>Discuss</Button>
        </div>
        {pendingChoice && !changing && <div className="space-y-2">
          <p className="break-words text-sm">Your previous choice: {conflict?.before.options?.find(o => o.id === pendingChoice)?.label ?? pendingChoice}.
            {!item.options?.some(o => o.id === pendingChoice) && ' This option is no longer available. Choose a current option or write a different answer.'}</p>
          <Button size="sm" disabled={busy || !item.options?.some(o => o.id === pendingChoice)} onClick={() => onChange({ answer: pendingChoice }, item)}>Resend decision</Button>
        </div>}
        {changing && <form className="space-y-2" onSubmit={e => { e.preventDefault(); if (reply.trim()) answer(reply, true); }}>
          <Textarea aria-label="Different answer" rows={2} maxLength={200} value={reply}
            onChange={e => { setReply(e.target.value); if (seen === null) setSeen(item); }} placeholder="Write a different answer" />
          <div className="flex gap-2"><Button type="submit" size="sm" variant="primary" disabled={busy || !reply.trim()}>{retry ? 'Resend decision' : 'Save answer'}</Button>
            <Button size="sm" onClick={() => setChanging(false)}>Cancel</Button></div>
        </form>}
      </div>
      : <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); if (reply.trim()) answer(reply); }}>
        <Input aria-label={`Answer "${item.title}"`} className="min-w-0 flex-1" maxLength={8192} value={reply} disabled={busy}
          onChange={(e) => { setReply(e.target.value); if (seen === null) setSeen(item); }}
          placeholder={item.kind === 'need' ? 'Done, or what happened' : 'Your answer'} />
        <Button type="submit" variant="primary" size="sm" disabled={!reply.trim() || busy}>{retry ? 'Resend answer' : 'Answer'}</Button>
      </form>)}
    {answerable && item.kind === 'need' && <div className="flex flex-wrap items-center gap-2">
      {/image|poster|screenshot|picture|drawing|photo|reference/i.test(item.title + ' ' + item.steps) && <>
        <p className="w-full text-xs text-muted">Unreadable reference? Check image access or upload a clearer file. Skipping does not change requirements or constraints.</p>
        {owner && <Link className="inline-flex min-h-11 items-center text-sm text-accent underline" to="/settings#image-access">Check image access</Link>}
        {projectId && <Link className="inline-flex min-h-11 items-center text-sm text-accent underline" to={`/projects/${projectId}#project-brief`}>Upload a reference</Link>}
      </>}
      <Button size="sm" disabled={busy} onClick={() => answer('Skip this input; do not change requirements or constraints without asking.')}>Skip this input</Button>
    </div>}
    {item.kind === 'decision' && <CardDiscussion teamId={teamId} item={item} editing={discussing} onClose={() => setDiscussing(false)} />}
    <div className="flex flex-wrap gap-2">
      {/* A decision is closed by answering it (owner decision 2026-10-26): no Mark done or Dismiss until then. */}
      {!closed && !unansweredDecision && <Button size="sm" variant="ghost" disabled={busy} onClick={() => onChange({ state: 'done' })}>Mark done</Button>}
      {!closed && !unansweredDecision && <Button size="sm" variant="ghost" disabled={busy} onClick={() => onChange({ state: 'dismissed' })}>Dismiss</Button>}
      {(closed || item.state === 'answered' || item.state === 'ingested') && <Button size="sm" variant="ghost" disabled={busy} onClick={() => onChange({ state: 'open' })}>Reopen</Button>}
    </div>
  </li>;
}

function CardDiscussion({ teamId, item, editing, onClose }: { teamId: string; item: Item; editing: boolean; onClose: () => void }) {
  const client = useQueryClient();
  const draftKey = `discuss:${item.id}`;
  const [draft] = useState(() => loadDraft<{ text: string; revision: number }>(draftKey));
  const [text, setText] = useState(draft?.text ?? '');
  const [base, setBase] = useState<number | null>(draft?.revision ?? null);
  const link = item.links.filter(row => row.kind === 'delivery').at(-1);
  const receipt = useQuery({ queryKey: ['card-delivery', link?.id], enabled: !!link,
    queryFn: () => api<{ delivery: { state: string; notice: string | null; session_id: string } }>(`/api/deliveries/${encodeURIComponent(link!.id)}`),
    refetchInterval: query => ['acknowledged', 'failed', 'expired'].includes(query.state.data?.delivery.state ?? '') ? false : 2000 });
  useEffect(() => { if (text) saveDraft(draftKey, { text, revision: base ?? item.revision }); else clearDraft(draftKey); }, [draftKey, text, base, item.revision]);
  const send = useMutation({ mutationFn: () => api(`/api/teams/${teamId}/worklist/items/${item.id}/discuss`, {
    json: { revision: base ?? item.revision, text } }),
    onSuccess: () => { setText(''); setBase(null); onClose(); client.invalidateQueries({ queryKey: ['worklist', teamId] }); },
    onError: () => { setBase(null); client.invalidateQueries({ queryKey: ['worklist', teamId] }); } });
  return <div className="space-y-2 text-xs">
    {editing && <form className="space-y-2" onSubmit={e => { e.preventDefault(); send.mutate(); }}>
      <Textarea aria-label="Message to lead" rows={3} maxLength={6000} value={text}
        onChange={e => { setText(e.target.value); if (base === null) setBase(item.revision); }} />
      <div className="flex gap-2"><Button type="submit" size="sm" disabled={!text.trim() || send.isPending}>Send to lead</Button>
        <Button size="sm" onClick={onClose}>Cancel</Button></div>
    </form>}
    {link && <><Link className="inline-flex min-h-8 items-center underline" to={`/sessions/${teamId}`}>Open lead conversation</Link>
      <p role="status">{receipt.data?.delivery.session_id === teamId
        ? receipt.data.delivery.notice ?? (receipt.data.delivery.state === 'acknowledged' ? 'Discussion delivered. The card stays open for your decision.' : 'Discussion queued; not delivered yet.')
        : 'Checking discussion delivery…'}</p></>}
    <ErrorNote error={send.error ?? receipt.error} />
  </div>;
}

function AddItem({ teamId, onAdded }: { teamId: string; onAdded: () => void }) {
  const [kind, setKind] = useState<'task' | 'note'>('task');
  const [title, setTitle] = useState('');
  const add = useMutation({
    mutationFn: () => api(`/api/teams/${teamId}/worklist/items`, { json: { kind, title } }),
    onSuccess: () => { setTitle(''); onAdded(); },
  });
  return <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); if (title.trim()) add.mutate(); }}>
    <Select aria-label="New item kind" value={kind} onChange={(e) => setKind(e.target.value as 'task' | 'note')}>
      <option value="task">Task for the lead</option><option value="note">Note</option>
    </Select>
    <Input aria-label="New item title" className="min-w-0 flex-1" maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Add to the list" />
    <Button type="submit" size="sm" disabled={!title.trim() || add.isPending}>Add</Button>
    {add.error && <div className="basis-full"><ErrorNote error={add.error} /></div>}
  </form>;
}
