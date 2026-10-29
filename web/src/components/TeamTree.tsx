/** Slice 2: a person-only view inside an explicitly granted M17 team. */
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { Card, Button, QueryState, StaleNote, useMedia } from './ui';

interface Member {
  id: string; parent: string | null; depth: number; name: string; agent: string;
  profile: string | null; model: string | null; machine: string | null;
  stop_state?: string | null; observed: string; needs_you: boolean; activity: string | null; archived: boolean;
  merged: boolean; selection_reason: string | null; report_status: string | null;
  deliveries: { id: number; session: string; state: string }[];
}
interface Snapshot { members: Member[]; cursor: number }
interface Change { id: number; session: string; kind: string; payload: Record<string, unknown> }
interface Page { events: Change[]; cursor: number; has_more: boolean; evicted?: boolean }
const states: Record<string, [string, string]> = {
  starting: ['◌', 'Starting'], active: ['●', 'Working'], waiting_input: ['◆', 'Needs you'],
  idle: ['○', 'Idle'], stopped: ['✓', 'Stopped'], failed: ['✕', 'Failed'],
};

export function TeamTree({ teamId, wrapupUntil }: { teamId: string; wrapupUntil?: string | null }) {
  return <LiveTree key={teamId} teamId={teamId} wrapupUntil={wrapupUntil} />;
}

function LiveTree({ teamId, wrapupUntil }: { teamId: string; wrapupUntil?: string | null }) {
  const client = useQueryClient();
  const key = ['team-tree', teamId];
  const snapshot = useQuery({ queryKey: key, queryFn: ({ signal }) => api<Snapshot>(`/api/teams/${teamId}/tree`, { signal }), retry: false });
  const [pollError, setPollError] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  const [selected, select] = useState(teamId);
  const [expanded, expand] = useState<Record<string, { open: boolean; finished: boolean }>>({});
  const phone = useMedia('(max-width: 639px)');
  const refs = useRef(new Map<string, HTMLLIElement>());
  const treeFocused = useRef(false);
  const ready = snapshot.data !== undefined;
  const initialCursor = useRef(0);
  initialCursor.current = snapshot.data?.cursor ?? 0;

  useEffect(() => {
    if (!ready) return;
    const abort = new AbortController();
    let cursor = initialCursor.current;
    const refresh = async () => {
      const data = await api<Snapshot>(`/api/teams/${teamId}/tree`, { signal: abort.signal });
      if (!abort.signal.aborted) client.setQueryData(['team-tree', teamId], data);
      cursor = data.cursor;
    };
    setPollError(null);
    void (async () => {
      try {
        while (!abort.signal.aborted) {
          let changes: Page;
          try {
            changes = await api<Page>(`/api/teams/${teamId}/events?after=${cursor}&wait=25`, { signal: abort.signal });
          } catch (error) {
            if (error instanceof ApiError && error.status === 409) { await refresh(); continue; }
            throw error;
          }
          cursor = changes.cursor;
          // Another of this person's views took the waiting slot: pause before asking again, so several open tabs
          // share the four slots instead of evicting each other in a tight loop.
          if (changes.evicted) await new Promise((resolve) => setTimeout(resolve, 5000));
          if (changes.events.length) {
            // A canonical snapshot coalesces events and includes its own cursor;
            // no optimistic patch can double-apply an event or miss a child.
            await refresh();
            void client.invalidateQueries({ queryKey: ['team', teamId] });
            void client.invalidateQueries({ queryKey: ['worklist', teamId] });
          }
        }
      } catch (error) {
        if (!abort.signal.aborted) setPollError(error);
      }
    })();
    return () => abort.abort(); // exactly one poll per mounted tab; no retry timer survives it
  }, [client, teamId, ready, retry]);

  const members = snapshot.data?.members ?? [];
  const children = (id: string) => members.filter(node => node.parent === id);
  const finished = (node: Member) => node.archived || node.observed === 'stopped';
  const isOpen = (node: Member) => expanded[node.id]?.finished === finished(node)
    ? expanded[node.id].open : !finished(node) && (!phone || node.depth === 0);
  const visible: Member[] = [];
  const visit = (node: Member) => { visible.push(node); if (isOpen(node)) children(node.id).forEach(visit); };
  members.filter(node => node.parent === null).forEach(visit);
  const visibleIds = visible.map(node => node.id).join(':');
  useLayoutEffect(() => {
    if (!members.length || visible.some(node => node.id === selected)) return;
    let ancestor = members.find(node => node.id === selected);
    while (ancestor && !visible.some(node => node.id === ancestor?.id)) {
      ancestor = members.find(node => node.id === ancestor?.parent);
    }
    const fallback = ancestor?.id ?? teamId;
    select(fallback);
    // A collapsing finished branch may remove the focused descendant. Move to
    // its nearest visible parent only if focus belonged to the tree, never from
    // another control or tab when an update arrives.
    if (treeFocused.current) refs.current.get(fallback)?.focus();
  }, [selected, visibleIds]);
  const focusId = visible.some(node => node.id === selected) ? selected : teamId;
  const detail = members.find(node => node.id === focusId);
  const focus = (id: string) => { select(id); refs.current.get(id)?.focus(); refs.current.get(id)?.scrollIntoView({ block: 'nearest' }); };
  const toggle = (node: Member) => expand(previous => ({ ...previous, [node.id]: { open: !isOpen(node), finished: finished(node) } }));
  function navigate(event: KeyboardEvent<HTMLLIElement>, node: Member) {
    if (event.target !== event.currentTarget) return; // inner links/buttons keep native keys
    const index = visible.findIndex(row => row.id === node.id);
    switch (event.key) {
      case 'ArrowDown': event.preventDefault(); focus(visible[Math.min(index + 1, visible.length - 1)].id); break;
      case 'ArrowUp': event.preventDefault(); focus(visible[Math.max(index - 1, 0)].id); break;
      case 'Home': event.preventDefault(); focus(visible[0].id); break;
      case 'End': event.preventDefault(); focus(visible[visible.length - 1].id); break;
      case 'ArrowRight':
        event.preventDefault();
        if (children(node.id).length) { if (!isOpen(node)) toggle(node); else focus(children(node.id)[0].id); }
        break;
      case 'ArrowLeft':
        event.preventDefault();
        if (children(node.id).length && isOpen(node)) toggle(node); else if (node.parent) focus(node.parent);
        break;
      case 'Enter': event.preventDefault(); event.currentTarget.querySelector('a')?.click(); break;
      case ' ': event.preventDefault(); select(node.id); if (children(node.id).length) toggle(node); break;
    }
  }
  return <section aria-label="Live agent tree panel"><Card className="space-y-3 p-4">
    <h2 className="font-medium">Live agent tree</h2>
    <p className="text-xs text-muted">From Rynhult · select an agent for details. Use arrow keys to move and expand; Enter opens its session.</p>
    {pollError ? <div className="space-y-2" role="status"><p className="text-xs text-wait">Live updates paused. Showing the last team snapshot.</p>
      <StaleNote error={pollError} /><Button size="sm" onClick={() => setRetry(value => value + 1)}>Retry live updates</Button></div> : null}
    <QueryState query={snapshot} errorTitle="Couldn't read the team tree.">{() => <>
      <p role="status" aria-atomic="true" className="sr-only">{members.length} agents in the team. {members.filter(node => node.needs_you).length} need you.</p>
      <ul role="tree" aria-label="Live agent tree" className="space-y-1"
        onFocusCapture={() => { treeFocused.current = true; }}
        onBlurCapture={event => { treeFocused.current = !!event.relatedTarget && event.currentTarget.contains(event.relatedTarget as Node); }}>
        {visible.map(node => {
          const siblings = members.filter(other => other.parent === node.parent);
          const [symbol, state] = states[node.observed] ?? ['?', 'Unknown'];
          const hasChildren = children(node.id).length > 0;
          return <li key={node.id} role="treeitem" aria-level={node.depth + 1}
            aria-posinset={siblings.findIndex(other => other.id === node.id) + 1} aria-setsize={siblings.length}
            aria-expanded={hasChildren ? isOpen(node) : undefined} aria-selected={node.id === focusId}
            tabIndex={node.id === focusId ? 0 : -1} ref={element => { if (element) refs.current.set(node.id, element); else refs.current.delete(node.id); }}
            onFocus={() => select(node.id)} onKeyDown={event => navigate(event, node)}
            className={`min-w-0 rounded-md border p-2 focus-visible:outline-2 focus-visible:outline-accent ${node.id === focusId ? 'border-line-strong bg-panel-2' : 'border-transparent'} ${finished(node) ? 'text-muted' : ''}`}
            style={{ marginInlineStart: `${Math.min(node.depth, 4) * (phone ? 8 : 16)}px`, contentVisibility: 'auto', containIntrinsicSize: 'auto 68px' }}>
            <div className="flex min-w-0 items-start gap-2">
              {hasChildren ? <button type="button" tabIndex={-1} aria-label={`${isOpen(node) ? 'Collapse' : 'Expand'} ${node.name}`}
                className="grid size-8 shrink-0 place-items-center rounded-md hover:bg-panel-3" onClick={() => toggle(node)}>
                {isOpen(node) ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}</button>
                : <span aria-hidden="true" className="w-8 shrink-0" />}
              <div className="min-w-0 flex-1 space-y-1 [overflow-wrap:anywhere]">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span aria-hidden="true">{symbol}</span>
                  <Link tabIndex={-1} to={`/sessions/${node.id}`} className="font-medium underline decoration-line-strong underline-offset-2">{node.name || 'Unnamed agent'}</Link>
                  <span className={`text-xs ${node.needs_you ? 'text-wait' : 'text-muted'}`}>{node.stop_state ?? (node.id === teamId && wrapupUntil ? 'Team expiry wrap-up' : node.archived ? 'Finished' : state)}</span>
                  {node.merged ? <span className="text-xs text-muted">Merged</span> : null}
                  {node.report_status ? <span className="text-xs text-muted">Reported {node.report_status}</span> : null}</div>
                <p className="text-xs text-muted">{[node.agent, node.profile || 'No profile', node.model, node.machine].filter(Boolean).join(' · ')}</p>
                {node.activity ? <p className="text-xs">{node.activity}</p> : null}
              </div>
            </div>
          </li>;
        })}
      </ul>
      {detail ? <section aria-label="Selected agent" className="space-y-2 border-t border-line pt-3 text-xs [overflow-wrap:anywhere]">
        <h3 className="font-medium">{detail.name}</h3>
        <p>{detail.selection_reason || 'No selection reason recorded.'}</p>
        {detail.deliveries.length ? <ul aria-label="Latest deliveries">{detail.deliveries.map(delivery => <li key={delivery.id}>Message {delivery.id}: {delivery.state}</li>)}</ul> : <p className="text-muted">No delivery recorded.</p>}
        <p>{detail.report_status ? `Report: ${detail.report_status}` : 'No report recorded.'}</p>
        <div className="flex flex-wrap gap-3"><Link to={`/sessions/${detail.id}`} className="underline underline-offset-2">Open session</Link>
          {detail.needs_you ? <Link to="/inbox" className="underline underline-offset-2">Answer in Inbox</Link> : null}</div>
      </section> : null}
    </>}</QueryState>
  </Card></section>;
}
