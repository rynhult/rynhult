import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useRole } from '../lib/queries';
import { Card } from './ui';

export const teamPanelSections: Record<string, 'overview' | 'settings'> = {
  brief: 'overview', plan: 'overview', 'run-mode': 'settings', 'agent-tree': 'settings',
  'gate-queue': 'settings', reservations: 'settings', 'team-limits': 'settings',
  'team-members': 'settings', 'decision-log': 'settings', 'selection-reasons': 'settings',
};

/** Keep editors mounted when collapsed, so polling and disclosure never discard a draft. */
export function TeamSection({ teamId, panel, title, summary, children }: {
  teamId: string; panel: string; title: string; summary: string; children: ReactNode;
}) {
  const { username } = useRole();
  const location = useLocation();
  const contentId = useId();
  const summaryId = useId();
  const control = useRef<HTMLButtonElement>(null);
  const key = username ? `acc-team-panel:${encodeURIComponent(username)}:${teamId}:${panel}` : null;
  const [chosen, setChosen] = useState<{ key: string | null; open: boolean } | null>(null);
  let stored = false;
  try { stored = !!key && localStorage.getItem(key) === 'open'; } catch { /* Browser storage is optional. */ }
  const open = chosen?.key === key ? chosen.open : stored;
  const choose = (next: boolean) => {
    setChosen({ key, open: next });
    try { if (key) localStorage.setItem(key, next ? 'open' : 'closed'); } catch { /* Keep the in-page choice. */ }
  };
  const anchor = location.hash.slice(1);
  const target = !location.state?.teamTabNavigation && (anchor === panel || anchor === teamPanelSections[panel]);
  useEffect(() => {
    if (!target) return;
    setChosen({ key, open: true });
    try { if (key) localStorage.setItem(key, 'open'); } catch { /* Keep the in-page choice. */ }
    if (anchor === panel) {
      control.current?.scrollIntoView({ block: 'nearest' });
      control.current?.focus({ preventScroll: true });
    }
  }, [location.key, key, target, anchor, panel]);
  return <div data-team-panel={panel} className="min-w-0">
    <Card>
      <button id={panel} ref={control} type="button" aria-label={title} aria-expanded={open} aria-controls={contentId}
        aria-describedby={summaryId} onClick={() => choose(!open)}
        className="flex min-h-11 w-full scroll-mt-4 items-start gap-2 rounded-[10px] px-4 py-3 text-left hover:bg-panel-2">
        {open ? <ChevronDown size={16} className="mt-0.5 shrink-0" aria-hidden="true" /> : <ChevronRight size={16} className="mt-0.5 shrink-0" aria-hidden="true" />}
        <span className="min-w-0 break-words font-medium">{title}</span>
      </button>
      <p id={summaryId} className="break-words px-4 pb-3 text-xs text-muted">{summary}</p>
      <div id={contentId} hidden={!open} className="border-t border-line [&>section]:border-0 [&>section]:shadow-none">
        {children}
      </div>
    </Card>
  </div>;
}
