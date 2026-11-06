import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from './api';
import { useRole } from './queries';
import { announce } from '../components/ui';
import type { SetupPreference } from './setupPreference';

export function useSaveSetup() {
  const { username } = useRole(); const cache = useQueryClient();
  return useMutation({ mutationFn: (value: Pick<SetupPreference, 'level' | 'dismissed'>) =>
    api<SetupPreference>('/api/preferences/setup-level', { method: 'PUT', json: value }),
    onSuccess: value => { cache.setQueryData(['setup-level', username], value); announce('Setup preference saved. Security settings unchanged.'); } });
}
