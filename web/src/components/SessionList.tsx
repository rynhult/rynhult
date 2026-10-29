import clsx from "clsx";
import { useInstance } from "../lib/queries";
import { Link } from "react-router-dom";
import { TerminalSquare } from "lucide-react";
import { ago, teamOrder } from "../lib/format";
import type { SessionSummary } from "../lib/types";
import { nowDoing, NowDoing, ProviderBadge, SessionActions, SessionMachine, sessionMachineName, StateBadge } from "./session-bits";
import { Badge } from "./ui";
import { ChangeCount } from "./ChangeCount";

/** One session row layout everywhere: name/project · status · provider · activity · actions.
    Columns follow the list's own width (container queries), so it works in a full page and in a narrow card. */
export function SessionList({ sessions, showProject = true, header = true, hostOrder, onHostSort }: { sessions: SessionSummary[]; showProject?: boolean;
  /** False for the later groups of a grouped list: the column names show once (screen readers still get them). */ header?: boolean;
  hostOrder?: "asc" | "desc" | null; onHostSort?: () => void }) {
  const { host } = useInstance();
  const ordered = teamOrder(sessions);
  if (hostOrder) ordered.sort((a, b) => sessionMachineName(a, host).localeCompare(sessionMachineName(b, host)) * (hostOrder === "asc" ? 1 : -1));
  return (
    <div role="table" aria-label="Sessions" className="@container text-[13px]">
      <div role="row" className={header ? "hidden grid-cols-[minmax(0,1fr)_5.5rem_7.5rem_5rem_4rem_8.5rem] gap-3 border-b border-line px-4 py-2 text-[11px] font-medium uppercase tracking-[0.06em] text-faint @2xl:grid" : "sr-only"}>
        <span role="columnheader">Session</span>
        <span role="columnheader" aria-sort={hostOrder === "asc" ? "ascending" : hostOrder === "desc" ? "descending" : "none"}>
          {onHostSort ? <button type="button" onClick={onHostSort} className="pointer-coarse:inline-flex pointer-coarse:min-h-11 pointer-coarse:min-w-11 items-center underline underline-offset-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-accent">Host</button> : "Host"}
        </span>
        <span role="columnheader">Status</span><span role="columnheader">Agent</span>
        <span role="columnheader">Activity</span><span role="columnheader" className="text-right">Actions</span>
      </div>
      <div className="divide-y divide-line">
        {ordered.map((s) => (
          <div role="row" key={s.id}
            className={clsx("group grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 px-4 py-2.5 transition-colors hover:bg-panel-2/70",
              "@2xl:grid-cols-[minmax(0,1fr)_5.5rem_7.5rem_5rem_4rem_8.5rem] @2xl:gap-x-3")}>
            <div role="cell" className="min-w-0"><Link to={`/sessions/${s.id}`} className="block min-w-0 pointer-coarse:min-h-11 outline-offset-[-2px]">
              <div className="truncate font-medium text-fg group-hover:text-accent" style={{ paddingInlineStart: `${(s.team_depth ?? 0) * 16}px` }}>{s.parent_session_id ? "↳ " : ""}{s.name}{s.team_id === s.id && <span className="ml-2 text-xs text-muted">Orchestrator</span>}</div>
              <div className="flex min-w-0 items-center gap-2 text-xs text-muted">
                <span className="min-w-0 truncate">
                  {showProject && s.project}
                  {s.team_id && <span className="font-mono"> · {s.worktree_branch}</span>}
                  {s.git?.ahead !== undefined && <span> · {s.git.ahead} commits</span>}
                  {nowDoing(s) && <>{showProject ? " · " : ""}<NowDoing session={s} className="text-fg/80" /></>}
                  {s.changes?.files ? <>{" · "}<ChangeCount changes={s.changes} /></> : null}
                  {s.management === "external" && <span className="text-faint">{showProject ? " · " : ""}observe-only · {s.run_as}</span>}
                  {!s.machine_offline && s.last_error && <span className="text-bad"> · {s.last_error}</span>}
                </span>
                <span className="shrink-0 @2xl:hidden"><SessionMachine session={s} inline /></span>
              </div>
            </Link></div>
            <div role="cell" className="hidden min-w-0 truncate text-xs text-muted @2xl:block" title={sessionMachineName(s, host)}>{sessionMachineName(s, host)}</div>
            <div role="cell" className="justify-self-end @2xl:justify-self-start"><StateBadge session={s} />{s.team_stop_pending && <Badge tone="wait">Stop pending</Badge>}</div>
            <div role="cell" className="hidden min-w-0 @2xl:block"><ProviderBadge provider={s.provider} model={s.model} /></div>
            <div role="cell" className="tnum hidden text-xs text-muted @2xl:block" title="Last terminal output (heuristic)">{ago(s.last_output_at)}</div>
            <div role="cell" className="col-span-2 flex items-center justify-end gap-0.5 @2xl:col-span-1">
              <SessionActions session={s} compact />
              <Link to={`/sessions/${s.id}`} title="Open terminal" aria-label={`Open ${s.name}`}
                className="inline-grid size-7 place-items-center rounded-md text-muted hover:bg-panel-3 hover:text-fg pointer-coarse:size-11">
                <TerminalSquare size={15} aria-hidden="true" />
              </Link>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
