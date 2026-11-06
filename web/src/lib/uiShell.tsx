import { createContext, useContext } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from './api';
export interface ShellPreference { enabled: boolean; person_id: string | null }
export const ShellChoice = createContext(false);
export const useNewShell = () => useContext(ShellChoice);
export function useShellPreference(username: string | undefined, enabled = true) {
  return useQuery({ queryKey: ['ui-shell', username], queryFn: () => api<ShellPreference>('/api/preferences/ui-shell'), enabled: !!username && enabled, staleTime: Infinity });
}
