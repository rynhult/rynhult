import { useQuery } from "@tanstack/react-query";
import { Search as SearchIcon } from "lucide-react";
import { Fragment, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { ago } from "../lib/format";
import { useProjects, useProviderRegistry } from "../lib/queries";
import type { SearchResponse } from "../lib/types";
import { Button, Card, Empty, ErrorNote, Field, Input, LoadingRows, Page, PageHeader, SearchInput, Select, useDocumentTitle } from "../components/ui";
import { ProviderBadge } from "../components/session-bits";

/** "…the \x02billing\x03 webhooks…" with the matched words highlighted (no HTML from the server). */
function Snippet({ text }: { text: string }) {
  const parts = text.split(/\x02|\x03/);
  return <>{parts.map((part, i) => (i % 2 ? <mark key={i} className="rounded-sm bg-accent/25 px-0.5 text-fg">{part}</mark> : <Fragment key={i}>{part}</Fragment>))}</>;
}

/** History search: every conversation you can see, archived sessions included. */
export function Search() {
  useDocumentTitle("Search");
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const project = params.get("project_id") ?? "";
  const provider = params.get("provider") ?? "";
  const since = params.get("since") ?? "";
  const projects = useProjects();
  const registry = useProviderRegistry();
  const filtered = !!(project || provider || since);
  const update = (key: string, value: string) => setParams(current => {
    const next = new URLSearchParams(current);
    if (value) next.set(key, value); else next.delete(key);
    return next;
  }, { replace: true });
  const [draft, setDraft] = useState(q);
  useEffect(() => setDraft(q), [q]);
  useEffect(() => {  // search as you type, after a short pause
    const t = setTimeout(() => { if (draft !== q) setParams(current => {
      const next = new URLSearchParams(current);
      if (draft) next.set("q", draft); else next.delete("q");
      return next;
    }, { replace: true }); }, 300);
    return () => clearTimeout(t);
  }, [draft, q, setParams]);
  const query = useQuery({
    queryKey: ["search", q, project, provider, since], enabled: q.trim().length > 1,
    queryFn: () => api<SearchResponse>(`/api/search?${new URLSearchParams({
      q, ...(project ? { project_id: project } : {}), ...(provider ? { provider } : {}), ...(since ? { since } : {}),
    })}`),
  });
  return (
    <Page>
      <PageHeader title="Search" description="Find words in your session conversations, including archived sessions."
        meta="For pages, projects, sessions and commands, use Jump to… in the top bar." />
      <SearchInput value={draft} onChange={setDraft} placeholder="Search conversations, e.g. billing webhook" className="max-w-xl" />
      <div className="grid items-end gap-3 sm:grid-cols-2 lg:grid-cols-[1fr_1fr_1fr_auto]">
        <Field label="Project filter"><Select value={project} onChange={e => update("project_id", e.target.value)}>
          <option value="">All projects</option>
          {project && !projects.data?.some(p => String(p.id) === project) && <option value={project}>Selected project (not available)</option>}
          {(projects.data ?? []).map(p => <option key={p.id} value={p.id}>{p.name}{p.archived ? " (archived)" : ""}</option>)}
        </Select></Field>
        <Field label="Agent filter"><Select value={provider} onChange={e => update("provider", e.target.value)}>
          <option value="">All agents</option>
          {provider && !registry.list.some(p => p.id === provider) && <option value={provider}>{provider}</option>}
          {registry.list.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
        </Select></Field>
        <Field label="Sessions started since (UTC)"><Input type="date" name="since" value={since} onChange={e => update("since", e.target.value)} /></Field>
        <Button disabled={!filtered} onClick={() => setParams(current => {
          const next = new URLSearchParams(current);
          for (const key of ["project_id", "provider", "since"]) next.delete(key);
          return next;
        }, { replace: true })}>Clear filters</Button>
      </div>
      <ErrorNote error={projects.error} title="Couldn't list project filters." onRetry={projects.refetch} />
      <ErrorNote error={registry.error} title="Couldn't list agent filters." onRetry={registry.refetch} />
      {q.trim().length <= 1 ? <Empty icon={SearchIcon}>Type at least two characters. Words are matched in any order; the last one can be the start of a word.</Empty>
        : query.error ? <ErrorNote error={query.error} />
        : !query.data ? <Card><LoadingRows /></Card>
        : !query.data.results.length ? <Empty icon={SearchIcon}>{query.data.indexed_sessions === 0
          ? <><span>{filtered ? "No indexed conversations in these filters yet." : "No conversations indexed yet."}</span><p className="mt-2">Search indexes prompts and replies as sessions report them. Start a session or try broader filters.</p></>
          : <>No conversation mentions “{q}”.{filtered && <p className="mt-2">Try broader filters or different words.</p>}</>}</Empty>
        : (
          <div className="space-y-3">
            {query.data.results.map(({ session: s, matches, hits }) => (
              <Card key={s.id}>
                <Link to={`/sessions/${s.id}`} className="flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-line px-4 py-2.5 hover:bg-panel-2/70">
                  <span className="font-medium text-fg">{s.name}</span>
                  <ProviderBadge provider={s.provider} />
                  <span className="text-xs text-muted">{s.workdir ? s.workdir.split("/").pop() : s.project}</span>
                  {s.desired_state === "archived" && <span className="text-xs text-faint">archived</span>}
                  <span className="ml-auto text-xs text-faint">{matches} match{matches === 1 ? "" : "es"} · started {ago(s.created_at)}</span>
                </Link>
                <div className="divide-y divide-line">
                  {hits.map((h) => (
                    <div key={h.seq} className="grid grid-cols-[4.5rem_1fr] gap-3 px-4 py-2 text-[13px]">
                      <span className={h.role === "user" ? "text-accent" : "text-muted"}>{h.role === "user" ? "You" : "Agent"}</span>
                      <span className="min-w-0 break-words text-muted"><Snippet text={h.snippet} /></span>
                    </div>
                  ))}
                </div>
              </Card>
            ))}
          </div>
        )}
    </Page>
  );
}
