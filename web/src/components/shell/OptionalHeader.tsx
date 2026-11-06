import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, CornerDownRight, FileSearch, MoreHorizontal, Search, SlidersHorizontal } from 'lucide-react';
import { api } from '../../lib/api';
import { announce, Button, Dialog, ErrorNote, IconButton, Popover, useMedia } from '../ui';
import { HeaderReadings } from './HeaderReadings';
import { ProjectFileSearch } from './ProjectFileSearch';

const moduleIds = ['machine_load', 'plan_usage', 'go_to', 'conversation_search', 'file_search'] as const;
type ModuleId = typeof moduleIds[number];
type Preference = { order: ModuleId[]; hidden: ModuleId[] };
const defaults: Preference = { order: [...moduleIds], hidden: [] };
const labels: Record<ModuleId, string> = { machine_load: 'Machine load', plan_usage: 'Plan usage', go_to: 'Go to…', conversation_search: 'Search conversations', file_search: 'Find project files' };
const ownerOnly = (id: ModuleId) => id === 'machine_load' || id === 'plan_usage';

export function OptionalHeader({ username, owner, goTo }: { username: string; owner: boolean; goTo: () => void }) {
  const queryKey = ['ui-header', username];
  const preference = useQuery({ queryKey, queryFn: () => api<Preference>('/api/preferences/ui-header'), staleTime: 30000, retry: false });
  const [editing, setEditing] = useState(false); const phone = useMedia('(max-width: 1199px)');
  const value = preference.data ?? defaults;
  const customize = (close?: () => void) => <Button title="Customize header" disabled={!preference.data || preference.isError} onClick={() => { close?.(); setEditing(true); }}><SlidersHorizontal size={16} aria-hidden="true" /><span className="shell-tool-label">Customize header</span></Button>;
  const failure = preference.isError && <div className="text-wait text-xs"><p>Header preferences unavailable. Using {preference.data ? 'last saved choices' : 'defaults'}.</p><Button size="sm" onClick={() => void preference.refetch()} disabled={preference.isFetching}>Retry header preferences</Button></div>;
  return <HeaderReadings>{readings => <ProjectFileSearch>{showFiles => {
    const modules = (close?: () => void) => {
      const nodes: Record<ModuleId, ReactNode> = {
        ...readings,
        go_to: <Button title="Go to…" onClick={() => { close?.(); goTo(); }} aria-keyshortcuts="Control+K Meta+K"><CornerDownRight size={15} aria-hidden="true" /><span className="shell-tool-label">Go to…</span></Button>,
        conversation_search: <Link className="shell-search" title="Search conversations" to="/search" onClick={() => close?.()}><Search size={16} aria-hidden="true" /><span className="shell-tool-label">Search conversations</span></Link>,
        file_search: <Button onClick={() => { close?.(); showFiles(); }} aria-haspopup="dialog" aria-keyshortcuts="Control+Shift+F Meta+Shift+F" title="Find project files (Ctrl/⌘ Shift F)"><FileSearch size={16} aria-hidden="true" /><span className="shell-tool-label">Find project files</span></Button>,
      };
      return value.order.filter(id => !value.hidden.includes(id) && (owner || !ownerOnly(id))).map(id =>
        <div key={id} data-header-module={id} className={`shell-header-module${ownerOnly(id) ? ' shell-reading' : ''}`}>{nodes[id]}</div>);
    };
    return <>
      <div className="shell-optional">
        {phone ? <Popover label="Header tools" className="shell-header-overflow" align="left" wide trigger={<><MoreHorizontal size={16} aria-hidden="true" /><span>Header tools</span></>}>{close => <div className="shell-overflow">{modules(close)}{customize(close)}{failure}</div>}</Popover>
          : <>{modules()}{customize()}{failure}</>}
      </div>
      {editing && <HeaderEditor initial={value} owner={owner} queryKey={queryKey} close={() => setEditing(false)} />}
    </>;
  }}</ProjectFileSearch>}</HeaderReadings>;
}

function HeaderEditor({ initial, owner, queryKey, close }: { initial: Preference; owner: boolean; queryKey: string[]; close: () => void }) {
  const [draft, setDraft] = useState<Preference>(() => ({ order: [...initial.order], hidden: [...initial.hidden] }));
  const cache = useQueryClient();
  const save = useMutation({
    networkMode: 'always', retry: false,
    mutationFn: () => api<Preference>('/api/preferences/ui-header', { method: 'PUT', json: draft }),
    onSuccess: value => { cache.setQueryData(queryKey, value); announce('Header saved.'); close(); },
  });
  const move = (id: ModuleId, by: number) => {
    const order = [...draft.order]; const from = order.indexOf(id); const to = from + by;
    if (to < 0 || to >= order.length) return;
    [order[from], order[to]] = [order[to], order[from]];
    setDraft({ ...draft, order }); announce(`${labels[id]} moved to position ${to + 1} of ${order.length}.`);
  };
  const dismiss = () => { if (!save.isPending) close(); };
  return <Dialog open onClose={dismiss} dismissible={false} title="Customize header" description="Only optional modules can be changed. Your choices follow your sign-in across browsers and devices."
    footer={<><Button disabled={save.isPending} onClick={() => { setDraft({ order: [...moduleIds], hidden: [] }); announce('Defaults selected. Save header to apply.'); }}>Reset to default</Button><Button disabled={save.isPending} onClick={dismiss}>Cancel</Button><Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : 'Save header'}</Button></>}>
    <div className="space-y-4">
      <p>Use the checkboxes to show or hide modules. Use Move up and Move down to change their order, then Save header.</p>
      <ol aria-label="Optional header modules" className="space-y-2">
        {draft.order.map((id, index) => <li key={id} className="flex min-w-0 items-center gap-2 rounded-md border border-line p-2">
          <div className="min-w-0 flex-1"><label className="flex min-h-11 items-center gap-2"><input type="checkbox" name={id} aria-label={labels[id]} checked={!draft.hidden.includes(id)} disabled={save.isPending || (!owner && ownerOnly(id))} onChange={event => setDraft({ ...draft, hidden: event.target.checked ? draft.hidden.filter(hidden => hidden !== id) : [...draft.hidden, id] })} /><span>{labels[id]}</span></label>{!owner && ownerOnly(id) && <p className="text-muted text-xs">Owners only</p>}</div>
          <IconButton label={`Move ${labels[id]} up`} icon={ArrowUp} disabled={save.isPending || index === 0} onClick={() => move(id, -1)} />
          <IconButton label={`Move ${labels[id]} down`} icon={ArrowDown} disabled={save.isPending || index === draft.order.length - 1} onClick={() => move(id, 1)} />
        </li>)}
      </ol>
      <p className="text-muted">Identity, account, New session, Security, Live/connection state, Inbox status and update notices keep their existing places and conditions. Hiding tools does not change permissions or shortcuts.</p>
      {save.isError && <div><p>Header not saved. Your choices are still here; try Save header again.</p><ErrorNote error={save.error} /></div>}
    </div>
  </Dialog>;
}
