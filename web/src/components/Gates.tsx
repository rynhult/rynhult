import { useSingleMutation as useMutation } from "../lib/mutations";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ListChecks } from "lucide-react";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import type { Project } from "../lib/types";
import { OFFLINE_TITLE, useOffline, useRole } from "../lib/queries";
import { useConfirm } from "./confirm";
import { Badge, Button, Card, CardHeader, Empty, ErrorNote, Field, Input, LoadingRows, Note, QueryState, Select, Textarea } from "./ui";

/** Orchestrator v2 slice 5: the gate queue (docs/ORCHESTRATOR-V2.md). Off until an owner turns the host budget on. */
export interface Budget {
  enabled: boolean; gate_slots: number; full_suites_max: number; worker_tests: string; load_ceiling: number;
  job_minutes: number; log_mib: number; memory_gib: number;  // per gate job: time limit, log cap, memory per process
}
export interface GateJob {
  id: string; branch: string; base: string; suite: "full" | "related"; priority: "merge" | "check"; state: string;
  waiting: string | null; slot: number | null; attempt: number; reason: string | null; requested_by: string;
  results: { counts?: Record<string, number>; failed?: string[]; flaky?: string[]; accepted_flake?: { item: number; by: string } }; created_at: string; finished_at: string | null;
  // The commit the gate tested, and (for a pass) whether the branch has moved since: a pass is for `head` only.
  head?: string | null; current_head?: string | null; stale?: false | string | null; base_sha?: string; commands_digest?: string; runner_version?: string;
}
interface GatesView { budget: Budget; jobs: GateJob[]; running: number; full_running: number }

const STATE: Record<string, { label: string; tone: "neutral" | "accent" | "ok" | "wait" | "bad" }> = {
  queued: { label: "queued", tone: "neutral" }, running: { label: "running", tone: "accent" }, passed: { label: "passed", tone: "ok" },
  "flaky-passed": { label: "legacy retry pass · re-gate required", tone: "wait" },
  "retry-passed": { label: "retry-passed · triage required", tone: "wait" },
  "known-flake": { label: "known-flake", tone: "ok" }, failed: { label: "failed", tone: "bad" },
  "needs-restack": { label: "needs restack", tone: "wait" }, refused: { label: "refused", tone: "bad" }, cancelled: { label: "cancelled", tone: "neutral" },
};

export function ProjectGatesCard({ project }: { project: Pick<Project, "id"> }) {
  const { owner } = useRole();
  const query = useQuery({ queryKey: ["gates", project.id], queryFn: () => api<GatesView>(`/api/gates?project=${project.id}`), refetchInterval: 5000 });
  return (
    <Card>
      <CardHeader title="Gate queue" icon={ListChecks}
        subtitle="Fixed test runs on a clean checkout before merging. Merge jobs go first." />
      <QueryState query={query} loading={<LoadingRows rows={2} />} errorTitle="Couldn't read the gate queue.">
        {(data) => (
          <div className="divide-y divide-line">
            <p className="px-4 py-3 text-[13px] text-muted">
              {data.budget.enabled
                ? <>Slots {data.running}/{data.budget.gate_slots} · full suites {data.full_running}/{data.budget.full_suites_max} · load ceiling {data.budget.load_ceiling}</>
                : <>The gate queue is off. {owner ? "Turn it on below." : "An owner turns it on."}</>}
            </p>
            <Jobs jobs={data.jobs} />
            {data.budget.enabled && <QueueForm projectId={project.id} />}
            {owner && <BudgetForm budget={data.budget} />}
            {owner && <><CommandsForm projectId={project.id} /><VerificationSettings projectId={project.id} /></>}
          </div>
        )}
      </QueryState>
    </Card>
  );
}

function Jobs({ jobs }: { jobs: GateJob[] }) {
  if (!jobs.length) return <Empty icon={ListChecks}>No gate runs yet.</Empty>;
  return (
    <ul aria-label="Gate jobs" className="divide-y divide-line">
      {jobs.map((job) => <JobRow key={job.id} job={job} />)}
    </ul>
  );
}

function JobRow({ job }: { job: GateJob }) {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const offline = useOffline();
  const { owner } = useRole();
  const [flakeTask, setFlakeTask] = useState("");
  const accept = useMutation({ mutationFn: () => post(`/api/gates/${job.id}/accept-retry`, { item: Number(flakeTask) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["gates"] }) });
  const passed = job.state === "passed" || job.state === "known-flake" || !!job.results.accepted_flake;
  const outcome = job.state === "retry-passed" && job.results.accepted_flake
    ? { label: "retry-passed · flake task accepted", tone: "wait" as const }
    : STATE[job.state] ?? { label: job.state, tone: "neutral" as const };
  // Security review 2026-10-25 (threat review S5-3, S5-6): a pass never vouches for commits it didn't test.
  const state = passed && job.stale ? { label: `stale: ${job.stale}`, tone: "wait" as const }
    : passed && job.stale !== false ? { label: `${outcome.label}, current commit unconfirmed`, tone: "wait" as const }
    : outcome;
  const cancel = useMutation({
    mutationFn: () => post(`/api/gates/${job.id}/cancel`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["gates"] }),
  });
  const ask = async () => {
    if (await confirm({ title: `Cancel the gate for ${job.branch}?`, danger: true, confirm: "Cancel gate",
      body: job.state === "running" ? "Rynhult stops this test run through the process it started and records it as cancelled." : "The job leaves the queue." }))
      cancel.mutate();
  };
  const counts = job.results.counts;
  return (
    <li className="space-y-1 px-4 py-3 text-[13px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono font-medium text-fg">{job.branch}</span>
        <Badge tone={state.tone}>{state.label}</Badge>
        <Badge>{job.suite}</Badge>
        {job.priority === "merge" && <Badge tone="info">merge</Badge>}
        {job.slot && job.state === "running" && <span className="text-xs text-muted">slot {job.slot}{job.attempt === 2 ? " · rerunning failed tests" : ""}</span>}
        {(job.state === "queued" || job.state === "running") && (
          <Button size="sm" variant="ghost" className="ml-auto" onClick={() => void ask()} disabled={cancel.isPending || offline || job.waiting === "cancel"}
            title={offline ? OFFLINE_TITLE : undefined}>{job.waiting === "cancel" ? "Stopping…" : "Cancel"}</Button>
        )}
      </div>
      <p className="text-xs text-muted">
        on {job.base} · {job.requested_by} · {ago(job.finished_at ?? job.created_at)}
        {job.head && <> · tested <span className="font-mono">{job.head.slice(0, 10)}</span></>}
        {job.state === "queued" && job.waiting && <> · waiting: {job.waiting}</>}
        {counts?.tests !== undefined && <> · {counts.passed ?? 0} passed, {counts.failed ?? 0} failed, {counts.skipped ?? 0} skipped</>}
      </p>
      {job.stale && <p className="break-words text-xs text-wait">stale: {job.stale}. Queue a fresh gate before integrating.</p>}
      <details className="text-xs">
        <summary className="min-h-8 cursor-pointer py-1 pointer-coarse:min-h-11">Verification binding</summary>
        <dl className="mt-2 grid min-w-0 gap-2 [overflow-wrap:anywhere]">
          {([
            ['Tested head', job.head], ['Base SHA', job.base_sha],
            ['Commands digest', job.commands_digest], ['Runner version', job.runner_version],
          ] as const).map(([label, value]) => <div key={label}>
            <dt className="text-muted">{label}</dt><dd className="font-mono">{value || 'Not recorded'}</dd>
          </div>)}
        </dl>
      </details>
      {job.state === "retry-passed" && !job.results.accepted_flake && owner && <form className="flex flex-wrap items-end gap-2" aria-label="Accept flake task" onSubmit={event => { event.preventDefault(); accept.mutate(); }}>
        <Field label="Flake task number" hint="First file the flake as a task on this team's working list."><Input type="number" min={1} value={flakeTask} onChange={event => setFlakeTask(event.target.value)} /></Field>
        <Button type="submit" disabled={offline || accept.isPending || !Number.isSafeInteger(Number(flakeTask)) || Number(flakeTask) < 1}>Accept flake task</Button>
        <ErrorNote error={accept.error} />
      </form>}
      {job.results.accepted_flake && <p className="text-xs text-muted">Flake task {job.results.accepted_flake.item} accepted by {job.results.accepted_flake.by}.</p>}
      {job.reason && <p className="break-words text-xs text-muted">{job.reason}</p>}
      {!!job.results.flaky?.length && <p className="break-words text-xs text-wait">Flaky: {job.results.flaky.join(", ")}</p>}
      {job.state === "failed" && !!job.results.failed?.length && <p className="break-words text-xs text-bad">Failed: {job.results.failed.slice(0, 10).join(", ")}</p>}
      <ErrorNote error={cancel.error} />
    </li>
  );
}

function QueueForm({ projectId }: { projectId: number }) {
  const queryClient = useQueryClient();
  const offline = useOffline();
  const [branch, setBranch] = useState("");
  const [suite, setSuite] = useState("related");
  const [priority, setPriority] = useState("check");
  const queue = useMutation({
    mutationFn: () => post("/api/gates", { project: projectId, branch: branch.trim(), suite, priority }),
    onSuccess: () => { setBranch(""); return queryClient.invalidateQueries({ queryKey: ["gates"] }); },
  });
  return (
    <form aria-label="Queue a gate" className="grid gap-3 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end"
      onSubmit={(e) => { e.preventDefault(); if (branch.trim()) queue.mutate(); }}>
      <div className="sm:col-span-3"><Field label="Branch"><Input className="w-full" value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="acc/team-…" spellCheck={false} /></Field></div>
      <Field label="Tests"><Select value={suite} onChange={(e) => setSuite(e.target.value)}><option value="related">Related</option><option value="full">Full suite</option></Select></Field>
      <Field label="Priority"><Select value={priority} onChange={(e) => setPriority(e.target.value)}><option value="check">Check</option><option value="merge">Blocks a merge</option></Select></Field>
      <Button type="submit" disabled={!branch.trim() || queue.isPending || offline} title={offline ? OFFLINE_TITLE : undefined}>Queue gate</Button>
      <div className="sm:col-span-3"><ErrorNote error={queue.error} /></div>
    </form>
  );
}

/** Gates trust branch code (review L-1): the same words as the server's refusal, shown before the queue is turned on. */
export const GATE_TRUST_WARNING = "Gate jobs run the gate commands on a branch's files as the project's account, on this machine, with its network. " +
  "That is outside the helpers' sandbox, permission mode and guardrails: a helper's branch can change what a test run does. " +
  "Turn the gate queue on only for projects whose helpers you trust with that account.";

function BudgetForm({ budget }: { budget: Budget }) {
  const queryClient = useQueryClient();
  const offline = useOffline();
  const confirm = useConfirm();
  const [draft, setDraft] = useState({ ...budget, load_ceiling: String(budget.load_ceiling), gate_slots: String(budget.gate_slots),
    full_suites_max: String(budget.full_suites_max), job_minutes: String(budget.job_minutes), log_mib: String(budget.log_mib),
    memory_gib: String(budget.memory_gib) });
  const save = useMutation({
    mutationFn: (trusted: boolean) => api("/api/gates/budget", { method: "PUT", json: { enabled: draft.enabled, gate_slots: Number(draft.gate_slots),
      full_suites_max: Number(draft.full_suites_max), load_ceiling: Number(draft.load_ceiling), job_minutes: Number(draft.job_minutes),
      log_mib: Number(draft.log_mib), memory_gib: Number(draft.memory_gib), ...(trusted && { confirm_branch_code: true }) } }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["gates"] }),
  });
  const submit = async () => {
    const turningOn = draft.enabled && !budget.enabled;
    if (turningOn && !(await confirm({ title: "Turn on the gate queue? Gates trust branch code", danger: true,
      confirm: "Turn on the gate queue", body: GATE_TRUST_WARNING }))) return;
    save.mutate(turningOn);
  };
  return (
    <form aria-label="Host test budget" className="space-y-3 px-4 py-3 text-[13px]" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <p className="font-medium text-fg">Host test budget <span className="font-normal text-muted">(owners; this machine, all projects)</span></p>
      <p className="text-xs text-muted">Orchestrator v2 is on by default. Gates stay off until an owner enables this host budget and accepts the branch-code trust warning.</p>
      <label className="flex items-center gap-2">
        <input type="checkbox" className="size-3.5 accent-[var(--accent)]" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
        Run the gate queue
      </label>
      <p className="text-xs text-muted">Gates trust branch code: they run outside the helpers' sandbox, as the project's account. Projects whose helpers run in containers are never gated on this machine.</p>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Gate slots"><Input type="number" min={1} max={8} value={draft.gate_slots} onChange={(e) => setDraft({ ...draft, gate_slots: e.target.value })} /></Field>
        <Field label="Full suites at once"><Input type="number" min={1} max={8} value={draft.full_suites_max} onChange={(e) => setDraft({ ...draft, full_suites_max: e.target.value })} /></Field>
        <Field label="Load ceiling" hint="Full suites wait while the 1-minute load is higher."><Input type="number" min={0.5} step={0.5} value={draft.load_ceiling} onChange={(e) => setDraft({ ...draft, load_ceiling: e.target.value })} /></Field>
        <Field label="Time limit per job (minutes)" hint="A job still running then is stopped and fails."><Input type="number" min={5} max={1440} value={draft.job_minutes} onChange={(e) => setDraft({ ...draft, job_minutes: e.target.value })} /></Field>
        <Field label="Log cap per job (MiB)" hint="Later output isn't kept; the job keeps running."><Input type="number" min={1} max={1024} value={draft.log_mib} onChange={(e) => setDraft({ ...draft, log_mib: e.target.value })} /></Field>
        <Field label="Memory per test process (GiB)"><Input type="number" min={1} max={256} value={draft.memory_gib} onChange={(e) => setDraft({ ...draft, memory_gib: e.target.value })} /></Field>
      </div>
      <Button type="submit" disabled={save.isPending || offline} title={offline ? OFFLINE_TITLE : undefined}>Save budget</Button>
      <ErrorNote error={save.error} />
    </form>
  );
}

const lines = (argv?: string[]) => (argv ?? []).join("\n");
const argv = (text: string) => text.split("\n").map((line) => line.trim()).filter(Boolean);

function CommandsForm({ projectId }: { projectId: number }) {
  const queryClient = useQueryClient();
  const offline = useOffline();
  const query = useQuery({ queryKey: ["gate-commands", projectId], queryFn: () => api<{ commands: Record<string, string[]> | null }>(`/api/gates/projects/${projectId}/commands`) });
  const [draft, setDraft] = useState<Record<string, string> | null>(null);
  const current = draft ?? { full: lines(query.data?.commands?.full), related: lines(query.data?.commands?.related), rerun: lines(query.data?.commands?.rerun) };
  const save = useMutation({
    mutationFn: () => api(`/api/gates/projects/${projectId}/commands`, { method: "PUT", json: {
      full: argv(current.full), related: argv(current.related), ...(argv(current.rerun).length ? { rerun: argv(current.rerun) } : {}) } }),
    onSuccess: () => { setDraft(null); return queryClient.invalidateQueries({ queryKey: ["gate-commands", projectId] }); },
  });
  return (
    <form aria-label="Gate commands" className="space-y-3 px-4 py-3 text-[13px]" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <p className="font-medium text-fg">Gate commands <span className="font-normal text-muted">(owners; one argument per line, no shell)</span></p>
      <Note>Each command writes a JUnit report to <span className="font-mono">{"{junit}"}</span>. The rerun command gets the failed tests where a line is exactly <span className="font-mono">{"{tests}"}</span>. Agents can't change these.</Note>
      <div className="grid gap-3 lg:grid-cols-3">
        {(["full", "related", "rerun"] as const).map((suite) => (
          <Field key={suite} label={suite === "full" ? "Full suite" : suite === "related" ? "Related tests" : "Rerun failed tests (optional)"}>
            <Textarea rows={4} spellCheck={false} className="font-mono text-xs" value={current[suite]}
              onChange={(e) => setDraft({ ...current, [suite]: e.target.value })} />
          </Field>
        ))}
      </div>
      <Button type="submit" disabled={save.isPending || offline || !argv(current.full).length || !argv(current.related).length}
        title={offline ? OFFLINE_TITLE : undefined}>Save commands</Button>
      <ErrorNote error={save.error ?? query.error} />
    </form>
  );
}

function VerificationSettings({ projectId }: { projectId: number }) {
  const client = useQueryClient();
  const offline = useOffline();
  const flakes = useQuery({ queryKey: ["gate-flakes", projectId], queryFn: () => api<{tests: string[]}>(`/api/gates/projects/${projectId}/flakes`) });
  const wrapup = useQuery({ queryKey: ["team-wrapup"], queryFn: () => api<{minutes: number}>("/api/gates/wrapup") });
  const [tests, setTests] = useState<string | null>(null);
  const [minutes, setMinutes] = useState<string | null>(null);
  const saveFlakes = useMutation({ mutationFn: () => api(`/api/gates/projects/${projectId}/flakes`, { method: "PUT", json: { tests: argv(tests ?? lines(flakes.data?.tests)) } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["gate-flakes", projectId] }) });
  const saveWrapup = useMutation({ mutationFn: () => api("/api/gates/wrapup", { method: "PUT", json: { minutes: Number(minutes ?? wrapup.data?.minutes) } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["team-wrapup"] }) });
  return <div className="space-y-4 px-4 py-3 text-[13px]">
    <QueryState query={flakes}>{data => <form aria-label="Known flakes" className="space-y-3" onSubmit={e => { e.preventDefault(); saveFlakes.mutate(); }}>
      <Field label="Known flaky test IDs" hint="Owners only. One exact test ID per line. Listed tests count as known flakes only after their targeted rerun passes."><Textarea rows={3} spellCheck={false} value={tests ?? lines(data.tests)} onChange={e => setTests(e.target.value)} /></Field>
      <Button type="submit" disabled={offline || saveFlakes.isPending}>Save known flakes</Button><ErrorNote error={saveFlakes.error} />
    </form>}</QueryState>
    <QueryState query={wrapup}>{data => <form aria-label="Expiry wrap-up" className="space-y-3" onSubmit={e => { e.preventDefault(); saveWrapup.mutate(); }}>
      <Field label="Lead wrap-up (minutes)" hint="Host setting, 0–120. Helpers stop at expiry. The lead may only save reports, update the plan/working list and finish stopped helpers before it stops too."><Input required type="number" min={0} max={120} step={1} value={minutes ?? String(data.minutes)} onChange={e => setMinutes(e.target.value)} /></Field>
      <Button type="submit" disabled={offline || saveWrapup.isPending || (minutes !== null && !minutes.trim()) || !Number.isInteger(Number(minutes ?? data.minutes)) || Number(minutes ?? data.minutes) < 0 || Number(minutes ?? data.minutes) > 120}>Save wrap-up</Button><ErrorNote error={saveWrapup.error} />
    </form>}</QueryState>
  </div>;
}
