import { Surface as Card, ShellPage as Page, Workbar as PageHeader } from './PagePrimitives';
import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import { CheckCheck } from 'lucide-react';
import { api } from '../../lib/api';
import { sourceWords } from '../../lib/words';
import { providerLabel, useProviderRegistry } from '../../lib/queries';
import type { AttentionItem } from '../../lib/types';
import { Button, Empty, QueryState, Segmented } from '../ui';
import { AttentionRow } from '../../pages/Inbox';
import { categories, categoryOf, relatedWork, returnPath } from './inbox';

export function InboxContext({ item }: { item: AttentionItem }) {
  const registry = useProviderRegistry();
  const fields = [
    ['Project', item.project ?? item.publish?.project], ['Session', item.session_name],
    ['Source', sourceWords(item.source, id => providerLabel(id, registry.byId(id)))],
    ['Host', item.host ?? item.tool?.host],
  ];
  return <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 px-4 py-3 text-sm">
    {fields.filter(([, value]) => value).map(([label, value]) => <div key={label} className="contents"><dt className="text-muted">{label}</dt><dd className="min-w-0 break-words">{value}</dd></div>)}
  </dl>;
}

export default function ShellInbox() {
  const [params, setParams] = useSearchParams(); const route = useLocation();
  const detail = useRef<HTMLHeadingElement>(null); const picked = params.get('item');
  useEffect(() => { if (picked) detail.current?.focus(); }, [picked]);
  const filter = categories.some(c => c.key === params.get('filter')) ? params.get('filter')! : 'all';
  const scope = params.get('scope') === 'all' ? 'all' : 'open';
  const rawPage = Number(params.get('page') ?? 1);
  const page = Number.isSafeInteger(rawPage) && rawPage > 0 ? rawPage : 1;
  const query = useQuery({ queryKey: scope === 'open' ? ['attention'] : ['attention', 'all'],
    queryFn: () => api<AttentionItem[]>(`/api/attention?scope=${scope}`), refetchInterval: 3000 });
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params); next.set(key, value);
    if (key === 'filter' || key === 'scope') { next.delete('item'); next.delete('page'); }
    setParams(next, { state: route.state });
  };
  const back = returnPath(route.state?.inboxReturn);
  return <Page>
    <PageHeader title="Inbox" description="Decisions, approvals, blocked work, failures and information. Dismissing or snoozing never answers or approves."
      actions={<>{back && <Link className="shell-link text-accent" to={back}>Return to work</Link>}
        <Segmented label="Inbox scope" value={scope} onChange={value => update('scope', value)} options={[{ id: 'open', label: 'Open' }, { id: 'all', label: 'History' }]} />
        <Button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh Inbox</Button></>} />
    <QueryState query={query} isEmpty={items => !items.length}
      empty={<Card><Empty icon={CheckCheck}>{scope === 'open' ? 'Nothing needs you right now.' : 'No history yet.'}</Empty></Card>}>
      {items => {
        const filtered = items.filter(item => filter === 'all' || categoryOf(item) === filter);
        const pages = Math.max(1, Math.ceil(filtered.length / 20)); const current = Math.min(page, pages);
        const shown = filtered.slice((current - 1) * 20, current * 20);
        const selected = params.has('item') ? filtered.find(i => String(i.id) === params.get('item')) : shown[0];
        const destination = selected && relatedWork(selected);
        const heading = categories.find(c => c.key === filter)?.label ?? 'All items';
        return <div className="shell-inbox-grid">
          <Card className="min-w-0 self-start">
            <h2 className="break-words border-b border-line px-4 py-3 font-semibold">{heading}</h2>
            <p className="px-4 pt-2 text-xs text-muted">{filtered.length} items shown · {scope === 'all' ? 'history included' : 'open'} (up to 200 per read)</p>
            {shown.length ? <ul className="shell-inbox-list divide-y divide-line" aria-label="Inbox items">{shown.map(item => <li key={item.id}>
              <button type="button" aria-pressed={selected?.id === item.id} className="shell-link shell-inbox-item w-full text-left" onClick={() => update('item', String(item.id))}>
                <span className="block text-xs text-muted">{categories.find(c => c.key === categoryOf(item))?.label}</span>
                <span className="block break-words font-medium">{item.title}</span>
                <span className="mt-1 block break-words text-xs text-muted">{[item.project ?? item.publish?.project, item.session_name, item.host ?? item.tool?.host].filter(Boolean).join(' · ')}</span>
                {(item.resolved_at || item.acked_at || item.snoozed_until) && <span className="block text-xs text-muted">{item.resolved_at ? 'Resolved' : item.snoozed_until && Date.parse(item.snoozed_until) > Date.now() ? 'Snoozed' : item.acked_at ? item.persistent ? 'Acknowledged' : 'Dismissed' : ''}</span>}
              </button>
            </li>)}</ul> : <p className="p-4 text-sm text-muted">No items in this category.</p>}
            {pages > 1 && <nav aria-label="Inbox pages" className="flex flex-wrap items-center gap-2 p-3">
              <Button disabled={current === 1} onClick={() => update('page', String(current - 1))}>Previous items</Button>
              <span className="text-sm text-muted">{current} / {pages}</span>
              <Button disabled={current === pages} onClick={() => update('page', String(current + 1))}>Next items</Button>
            </nav>}
          </Card>
          <section aria-label="Inbox item details" className="min-w-0"><Card>
            {selected ? <>
              <h2 ref={detail} tabIndex={-1} className="break-words border-b border-line px-4 py-3 font-semibold">{selected.title}</h2>
              <InboxContext item={selected} />
              <AttentionRow key={selected.id} item={selected} />
              {destination && <div className="px-4 pb-4"><Link className="shell-link inline-flex text-accent" to={destination}>Open related work</Link></div>}
              <p className="px-4 pb-4 text-xs text-muted">This is the original notice and operation. Review its current outcome before deciding.</p>
            </> : <p role="status" className="p-4 text-sm text-muted">Choose an item. A previously selected item may have been handled or is no longer visible.</p>}
          </Card></section>
        </div>;
      }}
    </QueryState>
  </Page>;
}
