import { lazy, Suspense, useLayoutEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import type { ThemePreference } from '../lib/community-theme';
import { lazyImport } from '../lib/lazyImport';
const ThemeAppearance = lazy(() => lazyImport(() => import('./ThemeAppearance')));
import type { AuthState } from '../lib/types';
const preferencePath = '/api/catalog/themes/preference';
let recovery = new URLSearchParams(location.search).get('appearance') === 'builtin';
export function usePersonalTheme() {
  const enrolling = location.pathname === '/setup' || location.pathname.startsWith('/invite/');
  const auth = useQuery({ enabled: !enrolling, queryKey: ['auth'], queryFn: () => api<AuthState>('/api/auth/state'), staleTime: 5000, refetchInterval: 5000 });
  const person = !auth.isError && auth.data?.authenticated && !auth.data.step_up_required ? auth.data.username : null;
  const client = useQueryClient(), previous = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (previous.current && previous.current !== person) {
      client.removeQueries({ queryKey: ['personal-theme', previous.current], exact: true });
      client.removeQueries({ queryKey: ['catalog-themes', previous.current], exact: true });
    }
    previous.current = person;
  }, [person, client]);
  const query = useQuery({ queryKey: ['personal-theme', person], enabled: !!person,
    queryFn: () => api<ThemePreference>(preferencePath, { cache: 'no-store' }), staleTime: 10000, refetchInterval: 30000 });
  return { query, person };
}
export function resumeCustomAppearance() {
  recovery = false;
  const url = new URL(location.href); url.searchParams.delete('appearance');
  history.replaceState(history.state, '', url);
}
/** Mount once beside the Router. No persisted tokens, no terminal/editor remounts. */
export default function PersonalThemeRuntime() {
  const { query, person } = usePersonalTheme();
  // Built-in pages need only the identity-scoped preference query. Load token
  // validation and trusted custom-theme chrome only when actually needed.
  if (!person || !(query.data?.effective || query.data?.unavailable || query.isError || recovery)) return null;
  return <Suspense fallback={null}><ThemeAppearance person={person} preference={query.data}
    failed={query.isError} recovery={recovery} /></Suspense>;
}
