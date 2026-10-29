import { useFeatures } from "../components/Feature";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, post } from "../lib/api";
import type { MultiRunGroup, SessionChanges } from "../lib/types";
import { stamp } from "../lib/format";
import { Badge, Button, Card, Empty, ErrorNote, Field, Page, PageHeader, QueryState, Select } from "../components/ui";
import { StateBadge, ProviderBadge } from "../components/session-bits";
import { ComparisonBase, lineClass, splitDiff } from "../components/Changes";
import { useConfirm } from "../components/confirm";

export const useMultiRuns = () => {
  const feature = useFeatures();
  return useQuery({
  enabled: feature("multirun"),
  queryKey: ["multiruns"], queryFn: () => api<MultiRunGroup[]>("/api/multiruns"), refetchInterval: 5000,
});
};

/** A run's launch refusal as the person reads it: the broker's exception class (OpError: …) is not part of the reason. */
const refusal = (error: string) => error.replace(/^(OpError|RequestError|LoginError|ValueError|KeyError): /, "");

function DiffPanel({ data, path, label }: { data: SessionChanges; path: string; label: string }) {
  const file = data.list?.find((f) => f.path === path);
  const lines = splitDiff(data.diff ?? "").find((f) => f.path === path)?.lines;
  return <section aria-label={`${label}: ${path}`} className="min-w-0">
    <div className="border-b border-line bg-panel-2 px-4 py-2 text-xs font-medium">{label}</div>
    {!file ? <p className="p-4 text-xs text-faint">Not changed in this run.</p>
      : file.binary ? <p className="p-4 text-xs text-muted">Binary file changed; inspect it in the session.</p>
      : !lines?.length ? <p className="p-4 text-xs text-muted">No text diff available (empty file, metadata-only change, or truncated output).</p>
      : <pre tabIndex={0} role="region" aria-label={`${label}: ${path}`} className="max-h-[32rem] overflow-auto py-2 font-mono text-xs leading-relaxed">
        {lines.map((line, i) => <div key={i} className={`whitespace-pre px-4 ${lineClass(line)}`}>{line || " "}</div>)}
      </pre>}
  </section>;
}

function Comparison({ group }: { group: MultiRunGroup }) {
  const [params, setParams] = useSearchParams();
  const sessions = group.runs.flatMap((r) => r.session ? [r.session] : []);
  const left = params.get("left") ?? sessions[0]?.id ?? "";
  const right = params.get("right") ?? sessions[1]?.id ?? "";
  const valid = left !== right && sessions.some((s) => s.id === left) && sessions.some((s) => s.id === right);
  const query = useQuery({
    queryKey: ["multirun-compare", group.id, left, right],
    queryFn: () => api<{ left: SessionChanges; right: SessionChanges }>(`/api/multiruns/${group.id}/compare?${new URLSearchParams({ left, right })}`),
    enabled: valid, refetchInterval: 10000,
  });
  const label = (id: string) => {
    const session = sessions.find((s) => s.id === id);
    return session ? `${session.provider} / ${session.model || "CLI default"} / ${session.id}` : id;
  };
  return <section className="space-y-3" aria-labelledby="compare-heading">
    <h2 id="compare-heading" className="text-base font-semibold">Compare Changes</h2>
    <p className="text-xs text-muted">Each run's changes against its own branch's comparison base, as on its Changes tab, aligned file by file. Opening this comparison does not launch an agent.</p>
    {sessions.length < 2 ? <Empty>At least two accepted runs are needed to compare. Launch refusals are listed above.</Empty> : <>
      <div className="grid gap-3 sm:grid-cols-2">
        {(["left", "right"] as const).map((side) => <Field key={side} label={side === "left" ? "Left run" : "Right run"}>
          <Select value={side === "left" ? left : right} onChange={(e) =>
            setParams({ ...Object.fromEntries(params), left, right, [side]: e.target.value })}>
            {sessions.map((s) => <option key={s.id} value={s.id}>{label(s.id)}</option>)}
          </Select>
        </Field>)}
      </div>
      {!valid ? <ErrorNote error="Choose two different runs from this group." /> :
        <QueryState query={query}>{(data) => {
          const paths = [...new Set([...(data.left.list ?? []), ...(data.right.list ?? [])].map((f) => f.path))].sort();
          return <div className="space-y-3">
            <div className="grid gap-2 text-xs text-muted sm:grid-cols-2">
              {(["left", "right"] as const).map((side) => <div key={side} className="min-w-0 break-words">
                {label(side === "left" ? left : right)}: <ComparisonBase data={data[side]} />
                {data[side].truncated && <p className="text-wait">Diff truncated. Open this session's Changes or terminal for the rest.</p>}
                {!data[side].git && <p className="text-wait">The worktree is not available as a git repository. Check this session's folder.</p>}
              </div>)}
            </div>
            {!paths.length ? <Empty>No changed files in these runs yet.</Empty> : paths.map((path) =>
              <Card key={path} className="min-w-0 overflow-hidden">
                <h3 className="break-all border-b border-line px-4 py-2 font-mono text-xs font-medium">{path}</h3>
                <div className="grid min-w-0 divide-y divide-line lg:grid-cols-2 lg:divide-x lg:divide-y-0">
                  <DiffPanel data={data.left} path={path} label={label(left)} />
                  <DiffPanel data={data.right} path={path} label={label(right)} />
                </div>
              </Card>)}
          </div>;
        }}</QueryState>}
    </>}
  </section>;
}

function Group({ group }: { group: MultiRunGroup }) {
  const confirm = useConfirm();
  const client = useQueryClient();
  const keep = useMutation({
    mutationFn: ({ session_id, archive_others = false }: { session_id: string; archive_others?: boolean }) =>
      post<MultiRunGroup>(`/api/multiruns/${group.id}/keep`, { session_id, archive_others, confirm: archive_others }),
    onSuccess: (next) => {
      client.setQueryData(["multirun", group.id], next);
      void client.invalidateQueries({ queryKey: ["multiruns"] });
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  const archive = async () => {
    if (group.winner_session_id && await confirm({
      title: "Stop and archive others", confirm: "Stop and archive others", danger: true,
      body: "Stop and archive every other run in this group? Their branches and worktrees are kept for comparison. This does not merge or delete any code.",
    })) keep.mutate({ session_id: group.winner_session_id, archive_others: true });
  };
  return <>
    <PageHeader title={group.name} description="Independent solutions to one task. Review the changes, keep a winner, then decide what to archive."
      meta={<span>{group.project} · {stamp(group.created_at)}</span>}
      actions={group.winner_session_id && <Button variant="danger" disabled={keep.isPending} onClick={() => void archive()}>Stop and archive others</Button>} />
    <ErrorNote error={keep.error} />
    {keep.data?.archive_errors?.map((e) => <ErrorNote key={e.session_id} error={`${e.session_id}: ${e.error}`} />)}
    <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {group.runs.map((r) => {
        const session = r.session;
        const cost = r.usage?.cost?.total_cost_usd;
        const context = r.usage?.context_window;
        return <Card key={r.position} className="min-w-0 space-y-3 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <ProviderBadge provider={r.choice.provider} model={r.choice.model} />
            <span className="text-xs text-muted">Run {r.position + 1}</span>
            {session && group.winner_session_id === session.id && <Badge tone="info">Winner</Badge>}
          </div>
          <p className="text-xs text-muted">{r.choice.profile || "No profile"}</p>
          {r.error && <ErrorNote error={refusal(r.error)} />}
          {!session ? !r.error && <p role="status" className="text-xs text-wait">Launch not completed. Inspect Sessions before starting another run; it will not retry automatically.</p> : <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <StateBadge session={session} />
              <Link className="text-xs text-accent hover:underline underline underline-offset-2" to={`/sessions/${session.id}?tab=changes`}>Open Changes</Link>
            </div>
            <p className="break-all font-mono text-xs text-faint">{session.worktree_branch}</p>
            <ErrorNote error={session.last_error} />
            <div className="space-y-1 text-xs text-muted">
              <p className="tnum">{session.changes?.git
                ? `${session.changes.files ?? 0} file${session.changes.files === 1 ? "" : "s"} · +${session.changes.additions ?? 0} / −${session.changes.deletions ?? 0}`
                : "Changes not measured yet"}</p>
              {session.changes_at && <p className="text-faint">Workspace snapshot · {stamp(session.changes_at)}</p>}
              <p className="tnum">Cost: {typeof cost === "number"
                ? new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(cost) : "unknown"}</p>
              <p className="tnum">Context used: {typeof context?.used_percentage === "number" ? `${context.used_percentage}%` : "unknown"}</p>
              {(typeof context?.total_input_tokens === "number" || typeof context?.total_output_tokens === "number") &&
                <p className="tnum">Tokens: {context?.total_input_tokens?.toLocaleString() ?? "unknown"} in / {context?.total_output_tokens?.toLocaleString() ?? "unknown"} out</p>}
              {r.usage && <p className="text-faint">{r.usage.source} · {stamp(new Date(r.usage.observed_at * 1000).toISOString())}</p>}
              {r.ci?.error ? <ErrorNote error={`CI: ${r.ci.error}`} />
                : r.ci?.run ? <a className="text-accent hover:underline underline underline-offset-2" href={r.ci.run.url} target="_blank" rel="noreferrer">
                  CI: {r.ci.run.conclusion ?? r.ci.run.status}
                </a> : <p>CI: no branch-matched result known</p>}
              {r.ci && <p className="text-faint">{r.ci.source} snapshot · {stamp(new Date(r.ci.observed_at * 1000).toISOString())}</p>}
            </div>
            <Button disabled={keep.isPending || group.winner_session_id === session.id} onClick={() => keep.mutate({ session_id: session.id })}>
              Keep this one
            </Button>
          </>}
        </Card>;
      })}
    </div>
    <Comparison group={group} />
  </>;
}

export function MultiRunPage() {
  const { id } = useParams();
  const query = useQuery({
    queryKey: ["multirun", id], queryFn: () => api<MultiRunGroup>(`/api/multiruns/${id}`), refetchInterval: 5000,
  });
  return <Page wide><QueryState query={query}>{(group) => <Group group={group} />}</QueryState></Page>;
}
