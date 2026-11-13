import { createContext, useContext } from 'react';
import type { DocumentRegistry } from './documentRegistry';
import type { TerminalOwner } from '../components/terminalOwner';
/** The renderer's route is independent of the address bar while a resource is retained. */
export type WorkingViewState = {
  key: string; href: string; visible: boolean; focused: boolean;
  /** This view was requested through navigation, rather than only restored. */
  explicit: boolean;
  intent?: string;
  navigationState?: unknown;
  /** Navigation requests focus in the newly mounted tab strip. */
  focusTab?: string;
  documents: DocumentRegistry;
  terminals: TerminalOwner;
  person: string;
  focusExisting: (key: string) => void;
  openBeside: (href: string) => void;
  setAvailable: (available: boolean) => void;
  setWork: (work: { dirty: boolean; uncertain: boolean; composing: boolean }) => void;
};
export const WorkingView = createContext<WorkingViewState | null>(null);
export const useWorkingView = () => useContext(WorkingView);

export const workingId = (id: string, view: WorkingViewState | null) => view ? `${id}-${encodeURIComponent(view.key)}` : id;
