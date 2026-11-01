import clsx from "clsx";
import { ExternalLink, LayoutGrid, Plus, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { SessionSummary } from "../lib/types";
import { useSessions } from "../lib/queries";
import { Button, Dialog, Dot, Empty, ErrorNote, IconButton, LoadingRows, Page, PageHeader, StaleNote, Tabs, useDocumentTitle, useMedia } from "../components/ui";
import { groupLive, isLive, NowDoing, ProviderBadge, sessionState, StateBadge } from "../components/session-bits";
import { Terminal } from "../components/Terminal";
import { expandedGridSession } from "../components/terminalExpansion";

const MAX = 4;
const STORE = "acc-grid";

function remembered(): string[] {
  try { return JSON.parse(localStorage.getItem(STORE) ?? "[]"); } catch { return []; }
}

/** Up to four live terminals side by side: watch several agents and answer whichever needs you. */
export function Grid() {
  useDocumentTitle("Grid");
  const sessions = useSessions();
  const [params, setParams] = useSearchParams();
  const [picking, setPicking] = useState(false);
  const live = useMemo(() => (sessions.data ?? []).filter((s) => s.management !== "external" && isLive(s)), [sessions.data]);
  const byId = useMemo(() => new Map(live.map((s) => [s.id, s])), [live]);
  const fromUrl = (params.get("ids") ?? "").split(",").filter(Boolean);
  // No choice yet: the last one used here, else the sessions that most likely need watching (needs you, then working).
  const fallback = (remembered().filter((id) => byId.has(id)).length ? remembered()
    : groupLive(live).flatMap((g) => g.sessions.map((s) => s.id))).slice(0, MAX);
  const chosen = params.has("ids");  // `?ids=` (empty) is a choice too: nothing, until sessions are picked
  const ids = (chosen ? fromUrl : fallback).filter((id) => byId.has(id)).slice(0, MAX);
  const choose = (next: string[]) => {
    setParams({ ids: next.join(",") }, { replace: true });
    try { localStorage.setItem(STORE, JSON.stringify(next)); } catch { /* private mode: not remembered */ }
  };
  const phone = useMedia("(max-width: 767px)");
  const [focused, setFocused] = useState<string>(expandedGridSession);
  const expansionChanged = useCallback((id: string, expanded: boolean) => {
    // Keep the expanded tile selected when crossing the phone breakpoint.
    if (expanded) setFocused(id);
  }, []);
  // The phone's one terminal: the one picked, else the first (the grid's order puts the one that needs you first).
  const shown = ids.includes(focused) ? focused : ids[0];
  useEffect(() => {  // keep the URL in step once sessions have loaded (shareable, survives reload)
    if (sessions.data && !chosen && ids.length) setParams({ ids: ids.join(",") }, { replace: true });
  }, [sessions.data]);  // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex h-full flex-col gap-3 px-4 pb-4 pt-5 md:px-8 md:pb-6">
      <PageHeader title="Grid" description={phone ? "Up to four live sessions, one at a time: switch between them above the terminal."
        : "Up to four live sessions side by side. Each tile is the real terminal: type into any of them."}
        actions={<Button size="sm" disabled={!sessions.data} onClick={() => setPicking(true)}><LayoutGrid size={14} aria-hidden="true" /> Choose sessions</Button>} />
      {sessions.data && sessions.error ? <StaleNote error={sessions.error} /> : null}
      {!sessions.data ? (  // not loaded yet, or failed: never "no live sessions" until the list has actually arrived
        <Page>{sessions.error ? (
          <div className="flex flex-wrap items-center gap-2"><ErrorNote error={sessions.error} />
            <Button size="sm" disabled={sessions.isFetching} onClick={() => void sessions.refetch()}>{sessions.isFetching ? "Retrying…" : "Retry"}</Button></div>
        ) : <LoadingRows />}</Page>
      ) : !ids.length ? (
        <Page><Empty icon={LayoutGrid} action={live.length ? <Button size="sm" onClick={() => setPicking(true)}><Plus size={14} /> Choose sessions</Button> : undefined}>
          {live.length ? "Pick up to four live sessions to watch together." : "No live sessions. Start one, then come back here."}
        </Empty></Page>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          {phone && <Tabs id="grid-tabs" label="Sessions in the grid" value={shown} onChange={setFocused}
            tabs={ids.map((id) => ({ id, label: <span className="inline-flex items-center gap-1.5"><Dot tone={sessionState(byId.get(id)!).tone} />{byId.get(id)!.name}</span> }))} />}
          {/* The same parent and tile keys on both layouts: no terminal remount on resize/collapse.
              Phones still mount only their selected terminal, as before. */}
          <div role={phone ? "tabpanel" : undefined} id={phone ? "grid-tabs-panel" : undefined}
            aria-labelledby={phone ? `grid-tabs-tab-${shown.replace(/[^A-Za-z0-9_-]+/g, "-")}` : undefined}
            className={clsx("grid min-h-0 flex-1 gap-3", phone || ids.length === 1 ? "grid-cols-1" : "grid-cols-1 lg:grid-cols-2",
              !phone && ids.length > 2 ? "lg:grid-rows-2" : "lg:grid-rows-1")}>
            {(phone ? [shown] : ids).map((id) => <Tile onExpansionChange={expansionChanged} key={id} session={byId.get(id)!} onRemove={() => choose(ids.filter((x) => x !== id))} />)}
          </div>
        </div>
      )}
      <Picker open={picking} onClose={() => setPicking(false)} live={live} chosen={ids} onChoose={(next) => { choose(next); setPicking(false); }} />
    </div>
  );
}

function Tile({ session: s, onRemove, onExpansionChange }: { session: SessionSummary; onRemove: () => void; onExpansionChange: (id: string, expanded: boolean) => void }) {
  const expansionChanged = useCallback((expanded: boolean) => onExpansionChange(s.id, expanded), [s.id, onExpansionChange]);
  return (
    <section data-grid-session={s.id} aria-label={s.name} className="flex min-h-[18rem] min-w-0 flex-col gap-1.5">
      <div className="flex min-w-0 items-center gap-2 text-[13px]">
        <span className="min-w-0 flex-1 truncate font-medium" title={s.name}>{s.name}</span>
        <StateBadge session={s} />
        <span className="max-xl:hidden"><ProviderBadge provider={s.provider} /></span>
        <NowDoing session={s} className="min-w-0 max-w-[30%] truncate text-xs text-muted max-lg:hidden" />
        <span className="ml-auto flex shrink-0 items-center">
          <Link to={`/sessions/${s.id}`} title="Open the session page" aria-label={`Open ${s.name}`}
            className="inline-grid size-7 place-items-center rounded-md text-muted hover:bg-panel-3 hover:text-fg"><ExternalLink size={14} /></Link>
          <IconButton icon={X} label={`Remove ${s.name} from the grid`} onClick={onRemove} />
        </span>
      </div>
      <div className="min-h-0 flex-1">{s.machine_offline ? <p className="p-4 text-xs text-muted">Machine offline</p> : <Terminal onExpansionChange={expansionChanged} sessionId={s.id} live compact name={s.name} tone={sessionState(s).tone} stateLabel={sessionState(s).label} />}</div>
    </section>
  );
}

function Picker({ open, onClose, live, chosen, onChoose }: {
  open: boolean; onClose: () => void; live: SessionSummary[]; chosen: string[]; onChoose: (ids: string[]) => void;
}) {
  const [picked, setPicked] = useState<string[]>(chosen);
  useEffect(() => { if (open) setPicked(chosen); }, [open]);  // eslint-disable-line react-hooks/exhaustive-deps
  const toggle = (id: string) => setPicked(picked.includes(id) ? picked.filter((x) => x !== id) : picked.length < MAX ? [...picked, id] : picked);
  return (
    <Dialog open={open} onClose={onClose} title="Choose sessions" description={`Up to ${MAX}, shown in the order you pick them.`}
      footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" onClick={() => onChoose(picked)}>Show {picked.length || ""}</Button></>}>
      {!live.length ? <Empty>No live sessions.</Empty> : (
        <div className="space-y-3">
          {groupLive(live).map((group) => (
            <div key={group.key}>
              <div className="pb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">{group.label}</div>
              {group.sessions.map((s) => (
                <label key={s.id} className="flex items-center gap-2.5 rounded-md px-1.5 py-1.5 text-[13px] hover:bg-panel-2">
                  <input type="checkbox" className="size-3.5 accent-[var(--accent)]" checked={picked.includes(s.id)}
                    disabled={!picked.includes(s.id) && picked.length >= MAX} onChange={() => toggle(s.id)} />
                  <span className="truncate">{s.name}</span>
                  <span className="truncate text-xs text-muted">{s.project}</span>
                  {picked.includes(s.id) && <span className="ml-auto text-xs text-faint">#{picked.indexOf(s.id) + 1}</span>}
                </label>
              ))}
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}
