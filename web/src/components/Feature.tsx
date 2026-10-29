import type { ReactNode } from 'react';
import { useInstance } from '../lib/queries';
import { Empty } from './ui';

export function useFeatures() {
  const instance = useInstance();
  return (key: string) => instance.features?.[key] !== false;
}
export function Feature({ name, children, page = false }: { name: string; children: ReactNode; page?: boolean }) {
  const enabled = useFeatures();
  return enabled(name) ? children : page ? <Empty>This feature is turned off. An owner can turn it on in Settings, Admin features.</Empty> : null;
}
