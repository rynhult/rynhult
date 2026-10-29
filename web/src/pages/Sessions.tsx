import { Feature } from "../components/Feature";
import clsx from "clsx";
import { withTeamAncestors } from "../lib/format";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Plus, TerminalSquare, X } from "lucide-react";
import { byRecent, groupSessions, type GroupKey, SESSION_GROUPS } from "../lib/order";
import { useProjects, useProviderRegistry, useSessions } from "../lib/queries";
import type { SessionSummary } from "../lib/types";
import { Badge, Button, Card, Empty, Page, PageHeader, QueryState, SearchInput, Segmented, Select, Toolbar } from "../components/ui";
import { useMultiRuns } from "./MultiRunPage";
import { isLive } from "../components/session-bits";
import { SessionList } from "../components/SessionList";
import { useNewSession } from "../components/newSession";

type Scope = "live" | "stopped" | "all";
const inScope = (s: SessionSummary, scope: Scope) =>
  s.desired_state !== "archived" && (scope === "all" || (scope === "live") === (isLive(s) || s.observed_state === "recovery_needed"));

export function Sessions() {
  const sessions = useSessions();
  const newSession = useNewSession();
  const groups = useMultiRuns();
  const registry = useProviderRegistry();
  const projects = useProjects();
  const [search, setSearch] = useSearchParams();
  const group = SESSION_GROUPS.find((g) => g.key === search.get("group"));  // from the sidebar and Overview links
  const [scope, setScope] = useState<Scope>(group?.key === "stopped" ? "stopped" : "live");
  const [sort, setSort] = useState<"state" | "recent">("state");
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState("");
  const [project, setProject] = useState("");
  const all = sessions.data ?? [];
  const count = (sc: Scope) => all.filter((s) => inScope(s, sc)).length;
  const shown = useMemo(() => withTeamAncestors(all, all.filter((s) => inScope(s, scope)
    && (!provider || s.provider === provider) && (!project || String(s.project_id) === project)
    && (!query || `${s.name} ${s.project} ${s.tmux_name} ${s.native_id ?? ""}`.toLowerCase().includes(query.toLowerCase())))),
  [all, scope, provider, project, query]);
  const stateGroups = groupSessions(shown, group ? [group.key as GroupKey] : undefined);
  const filtered = !!(query || provider || project || group);
  return (
    <Page wide>
      <PageHeader title="Sessions" description="All your agent sessions, what needs you first. Stopping keeps the conversation; Resume picks it up again." />
      <Toolbar>
        <Segmented label="Show" value={scope} onChange={setScope} options={[
          { id: "live", label: `Live ${count("live")}` }, { id: "stopped", label: `Stopped ${count("stopped")}` }, { id: "all", label: "All" }]} />
        <SearchInput value={query} onChange={setQuery} placeholder="Filter by name, project or id" className="w-64" />
        <Select aria-label="Agent" value={provider} onChange={(e) => setProvider(e.target.value)} className="w-36">
          <option value="">All agents</option>
          {(registry.list.length ? registry.list : [{ id: "claude", label: "Claude Code" }, { id: "codex", label: "Codex" }]).map((p) =>
            <option key={p.id} value={p.id}>{p.label}</option>)}
        </Select>
        <Segmented label="Order" value={sort} onChange={setSort} options={[{ id: "state", label: "Needs you first" }, { id: "recent", label: "Recent" }]} />
        {group && (
          <Button size="sm" variant="ghost" onClick={() => setSearch({}, { replace: true })} aria-label={`Showing ${group.label.toLowerCase()} only; show all`}>
            {group.label} only <X size={13} aria-hidden="true" />
          </Button>
        )}
        <Select aria-label="Project" value={project} onChange={(e) => setProject(e.target.value)} className="w-48">
          <option value="">All projects</option>
          {(projects.data ?? []).filter((p) => !p.archived).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Select>
      </Toolbar>
      <Feature name="multirun"><QueryState query={groups}>{(data) => <div className="space-y-2">
        {data.filter((g) => (!project || String(g.project_id) === project)
          && (!provider || g.runs.some((r) => r.choice.provider === provider))
          && (!query || `${g.name} ${g.project} ${g.id}`.toLowerCase().includes(query.toLowerCase()))
          && (scope === "all" || g.runs.some((r) => r.session && inScope(r.session, scope)) || (!g.runs.some((r) => r.session) && scope !== "stopped")))
          .map((g) => <Card key={g.id}>
            <details>
              <summary className="cursor-pointer px-4 py-3 text-sm hover:bg-panel-2">
                <span className="ml-2 inline-flex flex-wrap items-center gap-2">
                  <Badge>Multi-run</Badge>
                  <span className="font-medium text-fg">{g.name}</span>
                  <span className="text-xs text-muted">{g.runs.length} runs · {g.project}{g.winner_session_id ? " · winner selected" : ""}</span>
                </span>
              </summary>
              {/* A link inside <summary> is a nested control (axe nested-interactive); it sits in the opened card instead. */}
              <Link className="mx-4 mb-2 inline-flex min-h-6 pointer-coarse:min-h-11 items-center text-[13px] text-accent hover:underline underline underline-offset-2" to={`/multiruns/${g.id}`}>Open the comparison</Link>
              <SessionList sessions={g.runs.flatMap((r) => r.session ? [r.session] : [])} />
              {g.runs.filter((r) => !r.session).map((r) => <p key={r.position} className="px-4 py-2 text-xs text-wait">
                {r.choice.provider}: {r.error || "Launch not completed; inspect Sessions before retrying."}
              </p>)}
            </details>
          </Card>)}
      </div>}</QueryState></Feature>
      <Card>
        <QueryState query={sessions} isEmpty={() => !stateGroups.length}
          empty={<Empty icon={TerminalSquare} action={filtered ? undefined : <Button onClick={() => newSession()}><Plus size={14} aria-hidden="true" /> Start a session</Button>}>
            {group && !query && !provider && !project ? `No sessions in "${group.label}" right now.` : filtered ? "No sessions match these filters." : scope === "live" ? "No live sessions." : "Nothing here yet."}</Empty>}>
          {() => sort === "recent" ? <SessionList sessions={byRecent(stateGroups.flatMap((g) => g.sessions))} /> : stateGroups.map((g, i) => (
            <section key={g.key} aria-label={g.label} className="border-t border-line first:border-t-0">
              <h2 className={clsx("flex items-center gap-2 border-b border-line bg-panel-2/60 px-4 py-1.5 text-[11px] font-semibold uppercase tracking-[0.08em]",
                g.key === "needs_you" ? "text-wait" : "text-faint")}>{g.label} <span className="tnum font-normal">{g.count}</span></h2>
              <SessionList sessions={g.sessions} header={i === 0} />
            </section>
          ))}
        </QueryState>
      </Card>
    </Page>
  );
}
