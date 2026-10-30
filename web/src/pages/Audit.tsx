import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { api } from "../lib/api";
import { stamp } from "../lib/format";
import { useSessions } from "../lib/queries";
import { auditWords, foldAudit } from "../lib/words";
import { Button, Card, Empty, Field, Input, Page, PageHeader, QueryState, Select, Toolbar } from "../components/ui";

interface AuditRow { id: number; ts: string; actor: string; action: string; target: string | null; detail: Record<string, unknown> }

/** Kinds of entry, by action prefix (the API filters on it). */
const TYPES: [string, string][] = [
  ["", "Everything"], ["session.", "Sessions"], ["terminal.", "Terminals"], ["project.", "Projects"], ["publish.", "Publishing"],
  ["catalog.", "Catalog"], ["auth.", "Sign-in"], ["user.", "People"], ["vault.", "Keys"], ["node.", "Machines"],
  ["guardrails.", "Guardrails"], ["update.", "Updates"], ["team.", "Orchestrator teams"], ["multirun.", "Several agents"],
  ["pr.", "Pull requests"], ["tool.", "Tools"], ["port.", "Ports"], ["push.", "Notifications"], ["git.", "Git identity"],
];

function SignInFailure({ detail }: { detail: Record<string, unknown> }) {
  const text = (key: string) => typeof detail[key] === "string" ? detail[key] : "";
  const code = text("error_code"), description = text("error_text");
  const returned = text("returned_account"), invited = text("invited_account");
  return <span className="block space-y-0.5 text-muted [overflow-wrap:anywhere]">
    {text("reason") && <span className="block">Reason: {text("reason")}</span>}
    {text("detail") && <span className="block">{text("detail")}</span>}
    {code && <span className="block">Provider code: {code}</span>}
    {description && <span className="block">{description}</span>}
    {detail.reason === "invite_mismatch" && invited && <span className="block">
      {returned ? `Signed in as ${returned}; invitation is for ${invited}.`
        : `The provider did not return an eligible directory account; invitation is for ${invited}.`}
    </span>}
  </span>;
}

/** Every change made through Rynhult as a sentence (UX review AU-1). Attaches repeated by just viewing a session fold into
    one row per person, session and day; the raw action and details stay in the row's tooltip. */
export function Audit() {
  const [type, setType] = useState("");
  const [actor, setActor] = useState("");
  const [since, setSince] = useState("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [cursors, setCursors] = useState<number[]>([]);
  const before = cursors.at(-1);
  useEffect(() => {
    const next = search.trim();
    // An unchanged initial search is not a filter change. Its delayed callback
    // must not reset a cursor the person selected while the page was loading.
    if (next === query) return;
    const timer = setTimeout(() => { setQuery(next); setCursors([]); }, 300);
    return () => clearTimeout(timer);
  }, [search, query]);
  const params = new URLSearchParams({ limit: "100", page: "true", ...(before !== undefined && { before_id: String(before) }), ...(query && { q: query }), ...(type && { action: type }), ...(actor && { actor }), ...(since && { since }) });
  const rows = useQuery({ queryKey: ["audit", type, actor, since, query, before], queryFn: () => api<{ items: AuditRow[]; has_more: boolean; next_before: number | null }>(`/api/audit?${params}`) });
  const people = useQuery({ queryKey: ["people"], queryFn: () => api<{ users: { username: string }[] }>("/api/users") });
  const sessions = useSessions();
  const sessionName = (target: string) => sessions.data?.find((s) => s.id === target)?.name ?? target;
  const filtered = type || actor || since || query;
  return (
    <Page>
      <PageHeader title="Audit" description="Every change made through Rynhult: who, what and when. Newest first." />
      <Toolbar className="flex-wrap items-end gap-3">
        <div className="w-44"><Field label="Type"><Select value={type} onChange={(e) => { setType(e.target.value); setCursors([]); }}>
          {TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </Select></Field></div>
        <div className="w-44"><Field label="Person"><Select value={actor} onChange={(e) => { setActor(e.target.value); setCursors([]); }}>
          <option value="">Everyone</option>
          {people.data?.users.map((u) => <option key={u.username} value={u.username}>{u.username}</option>)}
          <option value="supervisor">Rynhult (automatic)</option>
        </Select></Field></div>
        <div className="w-44"><Field label="Since"><Input type="date" value={since} onChange={(e) => { setSince(e.target.value); setCursors([]); }} /></Field></div>
        <div className="w-64 max-w-full"><Field label="Search audit log"><Input type="search" name="audit-search" autoComplete="off" maxLength={256} value={search} onChange={e => setSearch(e.target.value)} placeholder="Person, action, target or details" /></Field></div>
      </Toolbar>
      {rows.data && <div className="flex flex-wrap items-center gap-2 text-sm text-muted"><p role="status">{rows.data.items.length} {rows.data.items.length === 1 ? "entry" : "entries"} on this page{rows.data.has_more ? " · More available" : ""}</p>
        <Button disabled={!cursors.length || rows.isFetching} onClick={() => setCursors(previous => previous.slice(0, -1))}>Previous</Button>
        <Button disabled={!rows.data.has_more || rows.data.next_before === null || rows.isFetching} onClick={() => { if (rows.data?.next_before != null) setCursors(previous => [...previous, rows.data.next_before!]); }}>Next</Button></div>}
      <Card>
        <QueryState query={rows} errorTitle="Couldn’t read audit history. If the search reached its limit, narrow Type, Person or Since." isEmpty={(d) => !d.items.length} empty={<Empty icon={ScrollText}>{filtered ? "No entries match." : "No entries yet."}</Empty>}>
          {(data) => (
            <div className="divide-y divide-line text-[13px]">
              {foldAudit(data.items).map((r) => {
                const { sentence, known } = auditWords(r, sessionName);
                const detail = Object.keys(r.detail ?? {}).length ? JSON.stringify(r.detail) : "";
                return (
                  <div key={r.id} className="grid gap-x-4 gap-y-0.5 px-4 py-2.5 md:grid-cols-[10rem_minmax(0,1fr)]" title={`${r.action}${r.target ? ` · ${r.target}` : ""}${detail ? ` · ${detail}` : ""}`}>
                    <span className="tnum font-mono text-[11.5px] text-muted">{stamp(r.ts)}</span>
                    <span className="min-w-0 break-words text-fg">
                      {r.action === "auth.sso_failed" ? `Company sign-in failed${r.target ? ` from ${r.target}` : ""}.` : sentence}
                      {r.times > 1 && <span className="text-muted"> · {r.times} times that day</span>}
                      {r.action === "auth.sso_failed" ? <SignInFailure detail={r.detail ?? {}} />
                        : !known && detail && <span className="text-faint"> · {detail.slice(0, 140)}</span>}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </QueryState>
      </Card>
    </Page>
  );
}
