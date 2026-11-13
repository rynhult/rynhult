import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from './api';
import { useRole } from './queries';
import { announce } from '../components/ui';
import type { SetupPreference } from './setupPreference';

export function useSaveSetup() {
  const { username } = useRole(); const cache = useQueryClient();
  return useMutation({ mutationFn: (value: Pick<SetupPreference, 'level' | 'dismissed'> & { wizard?: import('./setupWizard').WizardChange }) =>
    api<SetupPreference>('/api/preferences/setup-level', { method: 'PUT', json: value }),
    onError: () => { void cache.invalidateQueries({ queryKey: ['setup-level', username] }); },
    onSuccess: value => { cache.setQueryData(['setup-level', username], value); announce('Setup preference saved. Security settings unchanged.'); } });
}
