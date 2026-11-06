import { createContext, useContext } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from './api';
import type { SetupLevel } from './setupLevels';
export interface SetupPreference { level: SetupLevel | null; dismissed: boolean; version: number }
export const SetupChoice = createContext<SetupLevel | null>(null);
export const useSetupLevel = () => useContext(SetupChoice);
export function useSetupPreference(username: string | undefined, enabled = true) {
  return useQuery({ queryKey: ['setup-level', username], enabled: enabled && !!username,
    queryFn: () => api<SetupPreference>('/api/preferences/setup-level'), staleTime: 10000, refetchOnWindowFocus: true });
}
