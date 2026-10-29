import { Fragment, useState, type ReactNode } from 'react';
import { useRole } from '../lib/queries';
import { Segmented } from './ui';

type View = 'cards' | 'list';
/** Browser-local preferences are isolated by signed-in viewer and collection. */
export function useCollectionView(collection: 'projects' | 'workspaces') {
  const { username } = useRole();
  const key = username ? `acc-view:${encodeURIComponent(username)}:${collection}` : null;
  const [chosen, setChosen] = useState<{ key: string; view: View } | null>(null);
  let view: View = 'cards';
  try { if (key && localStorage.getItem(key) === 'list') view = 'list'; } catch { /* Storage may be unavailable. */ }
  if (chosen?.key === key) view = chosen.view;
  return { view, toggle: <Segmented label="View" value={view} options={[{ id: 'cards', label: 'Cards' }, { id: 'list', label: 'List' }]}
    onChange={next => { if (key) { setChosen({ key, view: next }); try { localStorage.setItem(key, next); } catch { /* Keep the in-page choice. */ } } }} /> };
}

export function CollectionView({ view, children }: { view: View; children: ReactNode }) {
  return <div data-view={view} className={view === 'cards' ? 'grid gap-4 md:grid-cols-2 xl:grid-cols-3' :
    'grid gap-3 [&>section]:md:grid [&>section]:md:grid-cols-2 [&>section>div:last-child]:md:col-span-2'}>{children}</div>;
}

export interface CollectionColumn<T> {
  key: string;
  label: string;
  render?: (item: T) => ReactNode;
  sortValue?: (item: T) => string | number;
}

/** A native table with a keyboard-scrollable viewport; wide rows never widen the page. */
export function CollectionTable<T>({ label, items, columns, itemKey, renderRow }: {
  label: string; items: T[]; columns: CollectionColumn<T>[]; itemKey: (item: T) => string | number;
  renderRow?: (item: T) => ReactNode;
}) {
  const [sort, setSort] = useState({ key: columns[0].key, descending: false });
  const column = columns.find(column => column.key === sort.key);
  const ordered = [...items].sort((a, b) => {
    if (!column?.sortValue) return 0;
    const left = column.sortValue(a), right = column.sortValue(b);
    const result = typeof left === 'number' && typeof right === 'number' ? left - right
      : String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: 'base' });
    return sort.descending ? -result : result;
  });
  return <><p className="mb-2 text-xs text-faint sm:hidden">Scroll the table sideways for more columns.</p><div data-view="list" role="region" aria-label={`${label} table, scroll horizontally`} tabIndex={0}
    className="min-w-0 max-w-full overflow-x-auto rounded-lg border border-line bg-panel">
    <table aria-label={label} className="w-full min-w-[64rem] text-left text-[13px] [&_td]:max-w-xs [&_td]:px-4 [&_td]:py-3 [&_td]:align-top [&_th]:px-4 [&_th]:py-3 [&_th]:align-top">
      <caption className="sr-only">{label}. Activate a column heading to sort. Scroll horizontally to reach all columns on a small screen.</caption>
      <thead className="border-b border-line bg-panel-2 text-xs text-muted">
        <tr>{columns.map(column => <th key={column.key} scope="col"
          aria-sort={column.sortValue ? (sort.key === column.key ? sort.descending ? 'descending' : 'ascending' : 'none') : undefined}>
          {column.sortValue ? <button type="button" aria-label={`Sort by ${column.label}`}
            className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center gap-1 rounded hover:text-fg"
            onClick={() => setSort(old => ({ key: column.key, descending: old.key === column.key && !old.descending }))}>
            {column.label}<span aria-hidden="true">{sort.key === column.key ? sort.descending ? '↓' : '↑' : '↕'}</span>
          </button> : column.label}
        </th>)}</tr>
      </thead>
      <tbody className="divide-y divide-line text-muted">{ordered.map(item => renderRow
        ? <Fragment key={itemKey(item)}>{renderRow(item)}</Fragment>
        : <tr key={itemKey(item)}>{columns.map((column, index) => index === 0
          ? <th key={column.key} scope="row" className="font-normal">{column.render?.(item)}</th>
          : <td key={column.key}>{column.render?.(item)}</td>)}</tr>)}</tbody>
    </table>
  </div></>;
}
