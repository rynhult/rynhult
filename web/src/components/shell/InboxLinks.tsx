import { Link, useLocation, useSearchParams } from 'react-router-dom';
import { useAttention } from '../../lib/queries';
import { QueryState } from '../ui';
import { categories, categoryOf, returnPath } from './inbox';

export function InboxLinks({ close }: { close: () => void }) {
  const query = useAttention(); const [params] = useSearchParams(); const route = useLocation();
  const filter = params.get('filter') ?? 'all'; const history = params.get('scope') === 'all';
  const state = { inboxReturn: route.pathname === '/inbox' ? returnPath(route.state?.inboxReturn) : route.pathname + route.search + route.hash };
  return <QueryState query={query}>{items => <>
    <Link className="shell-link" aria-current={route.pathname === '/inbox' && !history && filter === 'all' ? 'page' : undefined} to="/inbox" state={state} onClick={close}>All items · {items.length}</Link>
    {categories.map(category => <Link className="shell-link" key={category.key}
      aria-current={route.pathname === '/inbox' && !history && filter === category.key ? 'page' : undefined}
      to={`/inbox?filter=${category.key}`} state={state} onClick={close}>{category.label} · {items.filter(item => categoryOf(item) === category.key).length}</Link>)}
    <Link className="shell-link" aria-current={route.pathname === '/inbox' && history ? 'page' : undefined} to="/inbox?scope=all" state={state} onClick={close}>History</Link>
    <p className="shell-sidebar-hint">Browsing keeps your work open. Open a category to review its original notices and decisions.</p>
  </>}</QueryState>;
}

