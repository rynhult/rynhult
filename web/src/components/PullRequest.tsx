import { useSingleMutation as useMutation } from "../lib/mutations";
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, GitMerge, GitPullRequest, ListChecks, MessageSquareWarning, RefreshCw, Send, UserPlus } from "lucide-react";
import { api, post } from "../lib/api";
import type { PullCheck, PullStatus, SessionDetail } from "../lib/types";
import { Badge, Button, Card, CardHeader, Dialog, Empty, ErrorNote, Field, Input, QueryState, Segmented, Skeleton, Textarea } from "./ui";
import { useConfirm } from "./confirm";
import { isLive } from "./session-bits";

type Pull = NonNullable<PullStatus["pull"]>;
type Method = "merge" | "squash" | "rebase";
const METHOD_LABEL: Record<Method, string> = { squash: "Squash and merge", merge: "Create a merge commit", rebase: "Rebase and merge" };
const FAILED = ["failure", "timed_out", "startup_failure", "action_required"];

const checkTone = (c: PullCheck) => c.status !== "completed" ? "accent" : ["success", "neutral", "skipped"].includes(c.conclusion ?? "") ? "ok"
  : FAILED.includes(c.conclusion ?? "") ? "bad" : "neutral";
const stateBadge = (p: Pull) => p.state === "merged" ? <Badge tone="info">merged</Badge> : p.state === "closed" ? <Badge>closed</Badge>
  : p.draft ? <Badge tone="wait">draft</Badge> : <Badge tone="ok">open</Badge>;

/** The Pull request tab: create, edit, ready/draft, reviewers, checks and review comments for the agent, and a person's merge. */
export function PullRequestPanel({ session }: { session: SessionDetail }) {
  const queryClient = useQueryClient();
  const key = ["pull", session.id];
  const status = useQuery({ queryKey: key, queryFn: () => api<PullStatus>(`/api/sessions/${session.id}/pull`), refetchInterval: 60000 });
  const { hash } = useLocation();
  useEffect(() => {
    if (!hash.startsWith("#pull-")) return;
    const target = document.getElementById(hash.slice(1));
    target?.scrollIntoView({ block: "start" });
    target?.focus({ preventScroll: true });
  }, [hash, status.isSuccess]);
  const refresh = () => queryClient.invalidateQueries({ queryKey: key });
  return (
    <QueryState query={status} loading={<Skeleton className="h-64" />} errorTitle="Couldn't read this session's pull request from GitHub.">{(data) => {
      const signedIn = data.signed_in === "token" ? "Rynhult's GitHub token" : "your agent account's GitHub sign-in";
      return (
        <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
          {data.pull?.state === "open" && <section aria-label="Pull request readiness" className="min-w-0 lg:col-span-2"><Card className="space-y-3 p-4">
            <div className="flex flex-wrap items-center gap-2"><MakeMergeable session={session} status={data} onChange={refresh} /><h2 className="text-sm font-semibold">Pull request readiness</h2>
              <Badge tone={data.merge?.allowed ? "ok" : "wait"}>{data.merge?.allowed ? "Ready to merge" : data.merge ? "Not ready to merge" : "Merge status unavailable"}</Badge></div>
            {!!data.merge?.blockers.length && <ul className="space-y-1 text-xs text-wait" aria-label="Merge readiness blockers">{data.merge.blockers.map(b => <li key={b}>{b}</li>)}</ul>}
            <nav aria-label="Pull request sections" className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
              <a href="#pull-checks" className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center text-accent underline underline-offset-2">Checks · {data.checks.filter(c => c.status === "completed" && FAILED.includes(c.conclusion ?? "")).length} failed · {data.checks.filter(c => c.status !== "completed").length} running</a>
              <a href="#pull-reviews" className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center text-accent underline underline-offset-2">Review comments · {data.threads} unresolved</a>
              {data.merge && <a href="#pull-merge" className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center text-accent underline underline-offset-2">Merge decision</a>}
            </nav>
          </Card></section>}
          <div className="min-w-0 space-y-4">
            {data.branch === data.default_branch
              ? <Card><Empty icon={GitPullRequest}>This session works on {data.default_branch}, the default branch. Pull requests come from a session on its own branch (start it in a worktree).</Empty></Card>
              : data.pull ? <PullDetails session={session} pull={data.pull} repo={data.repo} onChange={refresh} />
              : <CreatePull session={session} status={data} onCreated={refresh} />}
            {data.pull && <div id="pull-checks" tabIndex={-1} className="scroll-mt-4"><Checks session={session} checks={data.checks} /></div>}
            {data.pull && <div id="pull-reviews" tabIndex={-1} className="scroll-mt-4"><Reviews session={session} threads={data.threads} /></div>}
          </div>
          <div className="min-w-0 space-y-4">
            {data.pull?.state === "open" && data.merge && <div id="pull-merge" tabIndex={-1} className="scroll-mt-4"><Merge pull={data.pull} merge={data.merge} sessionId={session.id} onMerged={refresh} /></div>}
            <Card className="space-y-1.5 p-4 text-xs text-muted">
              <p><span className="font-medium text-fg">{data.repo}</span> · branch <span className="font-mono text-fg">{data.branch}</span></p>
              <p>Rynhult uses {signedIn}. Guided failure delivery follows the session’s automation policy. Only you can confirm a merge.</p>
              <Button size="sm" variant="ghost" onClick={refresh} disabled={status.isFetching}><RefreshCw size={13} aria-hidden="true" /> Refresh</Button>
            </Card>
          </div>
        </div>
      );
    }}</QueryState>
  );
}

function CreatePull({ session, status, onCreated }: { session: SessionDetail; status: PullStatus; onCreated: () => void }) {
  const draft = useQuery({ queryKey: ["pull-draft", session.id], queryFn: () => api<{ title: string; body: string; source: string }>(`/api/sessions/${session.id}/pull/draft`), staleTime: Infinity });
  const [form, setForm] = useState<{ title: string; body: string; draft: boolean } | null>(null);
  const values = form ?? { title: draft.data?.title ?? "", body: draft.data?.body ?? "", draft: false };
  const create = useMutation({ mutationFn: () => post(`/api/sessions/${session.id}/pull`, { ...values, base: status.default_branch }), onSuccess: onCreated });
  return (
    <Card>
      <CardHeader icon={GitPullRequest} title="Create a pull request" subtitle={`${status.branch} → ${status.default_branch}`} />
      <div className="space-y-4 p-4">
        {!status.pushed && <p className="rounded-md border border-wait/30 bg-wait/10 p-3 text-xs text-wait">{status.branch} isn't on GitHub yet. Use Push branch in Changes, then return here to create the pull request.</p>}
        {draft.isLoading ? <Skeleton className="h-40" /> : <>
          <Field label="Title"><Input value={values.title} maxLength={256} onChange={(e) => setForm({ ...values, title: e.target.value })} /></Field>
          <Field label="Description" hint={draft.data?.body ? `Prefilled from the session's ${draft.data.source}; edit it as you like.` : "No handoff note or summary yet: describe the change."}>
            <Textarea value={values.body} rows={12} className="w-full font-mono text-[12.5px]" onChange={(e) => setForm({ ...values, body: e.target.value })} />
          </Field>
          <label className="flex items-center gap-2 text-[13px]">
            <input type="checkbox" checked={values.draft} onChange={(e) => setForm({ ...values, draft: e.target.checked })} className="size-3.5 accent-[var(--accent)]" />
            Open as draft
          </label>
        </>}
        <ErrorNote error={draft.error ?? create.error} />
        <Button variant="primary" disabled={!status.pushed || !values.title.trim() || create.isPending} onClick={() => create.mutate()}>
          <GitPullRequest size={14} aria-hidden="true" /> {create.isPending ? "Creating…" : "Create pull request"}
        </Button>
      </div>
    </Card>
  );
}

function PullDetails({ session, pull, repo, onChange }: { session: SessionDetail; pull: Pull; repo: string; onChange: () => void }) {
  const [edit, setEdit] = useState<{ title: string; body: string } | null>(null);
  const [reviewers, setReviewers] = useState("");
  const open = pull.state === "open";
  const save = useMutation({ mutationFn: () => api(`/api/sessions/${session.id}/pull`, { method: "PATCH", json: edit }), onSuccess: () => { setEdit(null); onChange(); } });
  const ready = useMutation({ mutationFn: (value: boolean) => post(`/api/sessions/${session.id}/pull/ready`, { ready: value }), onSuccess: onChange });
  const request = useMutation({
    mutationFn: () => post(`/api/sessions/${session.id}/pull/reviewers`, { reviewers: reviewers.split(/[\s,]+/).filter(Boolean) }),
    onSuccess: () => { setReviewers(""); onChange(); },
  });
  return (
    <Card>
      <CardHeader icon={GitPullRequest} title={<span className="flex items-center gap-2">#{pull.number} {pull.title} {stateBadge(pull)}</span>}
        subtitle={`${repo} · into ${pull.base}`}
        action={<a href={pull.url} target="_blank" rel="noreferrer" className="inline-flex pointer-coarse:min-h-11 items-center gap-1 text-[12.5px] text-muted hover:text-fg underline underline-offset-2">
          GitHub <ExternalLink size={12} aria-hidden="true" /></a>} />
      <div className="space-y-4 p-4">
        {edit ? <>
          <Field label="Title"><Input value={edit.title} maxLength={256} onChange={(e) => setEdit({ ...edit, title: e.target.value })} /></Field>
          <Field label="Description"><Textarea value={edit.body} rows={10} className="w-full font-mono text-[12.5px]" onChange={(e) => setEdit({ ...edit, body: e.target.value })} /></Field>
          <div className="flex gap-2">
            <Button variant="primary" size="sm" disabled={!edit.title.trim() || save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save"}</Button>
            <Button variant="ghost" size="sm" onClick={() => setEdit(null)}>Cancel</Button>
          </div>
        </> : <>
          <div role="region" aria-label="Pull request description" tabIndex={0} className="max-h-48 overflow-y-auto whitespace-pre-wrap text-[13px] text-muted [overflow-wrap:anywhere]">{pull.body || "No description."}</div>
          {open && <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => setEdit({ title: pull.title, body: pull.body })}>Edit title and description</Button>
            {pull.draft
              ? <Button size="sm" disabled={ready.isPending} onClick={() => ready.mutate(true)}>Ready for review</Button>
              : <Button size="sm" variant="ghost" disabled={ready.isPending} onClick={() => ready.mutate(false)}>Back to draft</Button>}
          </div>}
        </>}
        {open && (
          <div className="space-y-2 border-t border-line pt-4">
            <p className="text-xs text-muted">Reviewers: {pull.reviewers.length ? <span className="text-fg">{pull.reviewers.join(", ")}</span> : "none requested"}</p>
            <div className="flex gap-2">
              <Input value={reviewers} onChange={(e) => setReviewers(e.target.value)} placeholder="GitHub logins, comma-separated" aria-label="Reviewers to request" className="min-w-0 flex-1"
                onKeyDown={(e) => e.key === "Enter" && reviewers.trim() && request.mutate()} />
              <Button size="sm" className="h-8" disabled={!reviewers.trim() || request.isPending} onClick={() => request.mutate()}><UserPlus size={13} aria-hidden="true" /> Request</Button>
            </div>
          </div>
        )}
        <ErrorNote error={save.error ?? ready.error ?? request.error} />
      </div>
    </Card>
  );
}

function Checks({ session, checks }: { session: SessionDetail; checks: PullCheck[] }) {
  const failed = checks.filter((c) => c.status === "completed" && FAILED.includes(c.conclusion ?? "")).length;
  return (
    <Card>
      <CardHeader icon={ListChecks} title="Checks" subtitle={checks.length ? `${failed} failed of ${checks.length}` : undefined}
        action={failed > 0 && <SendToAgent session={session} kind="checks" label="Send failed checks to the agent" />} />
      {checks.length ? (
        <ul className="divide-y divide-line">
          {checks.map((c) => (
            <li key={c.name} className="flex min-w-0 items-center gap-2 px-4 py-2 text-[13px]">
              <Badge tone={checkTone(c)}>{c.status === "completed" ? c.conclusion ?? "done" : "running"}</Badge>
              {c.url ? <a href={c.url} target="_blank" rel="noreferrer" className="min-w-0 truncate hover:text-accent underline underline-offset-2">{c.name}</a> : <span className="min-w-0 truncate">{c.name}</span>}
              {c.required && <span className="text-xs text-faint">required</span>}
            </li>
          ))}
        </ul>
      ) : <p className="px-4 py-3 text-[13px] text-muted">No checks have reported on the latest commit.</p>}
    </Card>
  );
}

function Reviews({ session, threads }: { session: SessionDetail; threads: number }) {
  return (
    <Card>
      <CardHeader icon={MessageSquareWarning} title="Review comments"
        subtitle={threads ? `${threads} unresolved thread${threads === 1 ? "" : "s"}` : "No unresolved review threads."}
        action={threads > 0 && <SendToAgent session={session} kind="reviews" label="Send review comments to the agent" />} />
    </Card>
  );
}

/** Preview, then send: what the dialog shows is exactly the text the agent receives. */
function SendToAgent({ session, kind, label }: { session: SessionDetail; kind: "checks" | "reviews" | "conflicts"; label: string }) {
  const [open, setOpen] = useState(false);
  const preview = useMutation({ mutationFn: () => post<{ id: string | null; text: string; items: number; omitted: number }>(`/api/sessions/${session.id}/pull/feedback`, { kind }) });
  const send = useMutation({ mutationFn: () => post(`/api/sessions/${session.id}/pull/feedback/${preview.data!.id}/send`), onSuccess: () => setOpen(false) });
  const live = isLive(session);
  const subscription = session.provider === "claude" && !["api_key", "cloud"].includes(session.auth?.mode ?? "unknown");
  const show = () => { send.reset(); preview.mutate(); setOpen(true); };
  return (
    <>
      <Button size="sm" onClick={show}><Send size={13} aria-hidden="true" /> {label}</Button>
      {send.isSuccess && !open && <span role="status" className="text-xs text-ok">Sent</span>}
      <Dialog open={open} onClose={() => setOpen(false)} wide title={label}
        description="Review the redacted, bounded message. Subscription Claude requires you to copy and paste it into the terminal; other sessions use the existing delivery policy."
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
          {preview.data?.text && <Button onClick={() => navigator.clipboard.writeText(preview.data!.text)}>Copy message</Button>}
          <Button variant="primary" disabled={subscription || !live || !preview.data?.id || send.isPending} onClick={() => send.mutate()}>
            <Send size={13} aria-hidden="true" /> {send.isPending ? "Sending…" : "Send to agent"}</Button>
        </>}>
        {preview.isPending ? <Skeleton className="h-48" /> : preview.data && (preview.data.id ? (
          <div className="space-y-2">
            <p className="text-xs text-muted">{preview.data.items} item{preview.data.items === 1 ? "" : "s"} · {preview.data.text.length} characters{preview.data.omitted ? ` · ${preview.data.omitted} left out to fit` : ""}</p>
            <pre tabIndex={0} role="region" aria-label="Message preview" className="max-h-[50vh] overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-panel-2 p-3 font-mono text-[12px] text-fg [overflow-wrap:anywhere]">{preview.data.text}</pre>
          </div>
        ) : <p className="text-[13px] text-muted">Nothing to send: {kind === "checks" ? "no failed checks" : "no unresolved review threads"} right now.</p>)}
        {!live && <p className="mt-2 text-xs text-wait">The session isn't running; resume it to send this.</p>}
        <ErrorNote error={preview.error ?? send.error} />
      </Dialog>
    </>
  );
}

function Merge({ pull, merge, sessionId, onMerged }: { pull: Pull; merge: NonNullable<PullStatus["merge"]>; sessionId: string; onMerged: () => void }) {
  const confirm = useConfirm();
  const [method, setMethod] = useState<Method>(merge.methods[0] ?? "squash");
  const chosen = merge.methods.includes(method) ? method : merge.methods[0];
  const run = useMutation({ mutationFn: () => post(`/api/sessions/${sessionId}/pull/merge`, { method: chosen, sha: pull.head_sha, confirm: true }), onSuccess: onMerged });
  const ask = async () => {
    if (await confirm({ title: `Merge #${pull.number} into ${pull.base}?`, confirm: METHOD_LABEL[chosen],
      body: <>{METHOD_LABEL[chosen]} on GitHub, at commit <span className="font-mono">{pull.head_sha.slice(0, 7)}</span>. This changes {pull.base} for everyone working on the repository.</> })) run.mutate();
  };
  return (
    <Card>
      <CardHeader icon={GitMerge} title="Merge" subtitle="A person's decision: nothing merges automatically" />
      <div className="space-y-3 p-4">
        {merge.methods.length > 1 && <Segmented label="Merge method" value={chosen} onChange={setMethod}
          options={merge.methods.map((m) => ({ id: m, label: m === "merge" ? "Merge commit" : m === "squash" ? "Squash" : "Rebase" }))} />}
        {merge.blockers.length > 0 && (
          <ul className="space-y-1 text-xs text-wait" aria-label="What blocks merging">
            {merge.blockers.map((b) => <li key={b}>{b}</li>)}
          </ul>
        )}
        {merge.notes.map((n) => <p key={n} className="text-xs text-muted">{n}</p>)}
        <Button variant="primary" disabled={!merge.allowed || !chosen || run.isPending} onClick={ask}>
          <GitMerge size={14} aria-hidden="true" /> {run.isPending ? "Merging…" : chosen ? METHOD_LABEL[chosen] : "Merge"}
        </Button>
        <ErrorNote error={run.error} />
      </div>
    </Card>
  );
}


/** A person advances each step; the existing PR state and merge confirmation stay authoritative. */
function MakeMergeable({ session, status, onChange }: { session: SessionDetail; status: PullStatus; onChange: () => void }) {
  const confirm = useConfirm();
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const readiness = useQuery({ queryKey: ["mergeable", session.id], enabled: open,
    queryFn: () => api<{ behind: number; base: string; automatic: boolean; mode: "merge" | "rebase"; force_push_blocked: boolean; head: string; conflicts: string[]; summary: { before_count: number | null; after_count: number | null; subjects: string[]; omitted: number } }>(`/api/sessions/${session.id}/pull/mergeable`) });
  const path = `/api/sessions/${session.id}/pull/mergeable`;
  const refresh = () => { onChange(); void readiness.refetch(); };
  const base = useMutation({ mutationFn: () => post<{ ok: boolean; mode: string; conflicts: string[]; output: string; push_ticket?: string; finish_instructions?: string }>(`${path}/base`, { mode: readiness.data!.mode }), onSuccess: refresh });
  const push = useMutation({ mutationFn: () => post<{ ok: boolean; output: string }>(`${path}/push/${base.data!.push_ticket}`), onSuccess: refresh });
  const completed = useMutation({ mutationFn: (sha: string) => post<{ ok: boolean; output: string }>(`${path}/completed-rebase`, { sha, confirm: true }), onSuccess: refresh });
  const askCompleted = async () => {
    const reviewed = readiness.data;
    if (!reviewed) return;
    const summary = reviewed.summary;
    if (await confirm({ title: "Push completed rebase?", confirm: "Push with --force-with-lease",
      body: <div className="space-y-2"><p>Push this session’s reviewed commit {reviewed.head.slice(0, 7)} using the hardened transport. Rynhult rechecks the current PR head, base ancestry, branch protection and Security switch.</p>
        <p>Commits before / after: {summary?.before_count ?? "unavailable"} / {summary?.after_count ?? "unavailable"} (relative to the rebase base).</p>
        {!!summary?.subjects.length && <ul aria-label="Rebased commit subjects" className="max-h-40 overflow-auto space-y-1 [overflow-wrap:anywhere]">{summary.subjects.map((subject, index) => <li key={index}>{subject}</li>)}</ul>}
        {!!summary?.omitted && <p>{summary.omitted} more commit subjects omitted.</p>}
      </div> })) completed.mutate(reviewed.head);
  };
  const rerun = useMutation({ mutationFn: () => post<{ runs: number[] }>(`${path}/rerun`), onSuccess: refresh });
  const feedback = useMutation({ mutationFn: () => post<{ id: string | null; text: string; items: number; sent: boolean; refused?: string }>(`${path}/feedback`) });
  const send = useMutation({ mutationFn: () => post(`/api/sessions/${session.id}/pull/feedback/${feedback.data!.id}/send`) });
  const busy = base.isPending || push.isPending || rerun.isPending || feedback.isPending || send.isPending || completed.isPending;
  return <>
    <Button size="sm" onClick={() => setOpen(true)}>Make it mergeable</Button>
    <Dialog open={open} onClose={() => setOpen(false)} wide title="Make it mergeable"
      description="Work through the checklist. Each action records who requested it. Merge always needs your separate confirmation."
      footer={<Button onClick={() => setOpen(false)}>Close</Button>}>
      <ol className="space-y-5 text-sm [overflow-wrap:anywhere]">
        <li className="space-y-2"><h3 className="font-semibold">1. Up to date with the base branch</h3>
          <p className="text-xs text-muted">{readiness.data ? `${readiness.data.behind} commits behind ${readiness.data.base}.` : "Read base status to continue."} Commit changes first. {readiness.data?.force_push_blocked ? "Block force pushes is on: only Merge base in is offered, so history stays pushable." : "The project’s Git pull.rebase=false preference selects Merge base in; otherwise Rebase is used."}</p>
          <Button disabled={busy || !readiness.data} onClick={() => base.mutate()}>{readiness.data?.mode === "merge" ? "Merge base in" : "Rebase"}</Button>
          {base.data && <>
            <p role="status">{base.data.ok ? `${base.data.mode} completed locally. Push the branch before checking CI.` : "Base update stopped. Resolve or abort it before retrying."}</p>
            {!!base.data.conflicts.length && <><ul aria-label="Conflicted files">{base.data.conflicts.map(file => <li key={file} className="font-mono text-xs">{file}</li>)}</ul>
              <SendToAgent session={session} kind="conflicts" label="Ask the agent to resolve" /></>}
            <pre tabIndex={0} aria-label="Base update output" className="max-h-40 overflow-auto whitespace-pre-wrap text-xs">{base.data.output}</pre>
            {base.data.finish_instructions && <p role="note" className="whitespace-pre-wrap text-xs text-muted">{base.data.finish_instructions}</p>}
            {base.data.push_ticket && <Button disabled={busy || push.data?.ok} onClick={() => push.mutate()}>Push rebased branch with --force-with-lease</Button>}
            {base.data.ok && !base.data.push_ticket && <p className="text-xs text-muted">Use Push branch in Changes.</p>}
          </>}
          {!base.data && !!readiness.data?.conflicts.length && <><ul aria-label="Conflicted files">{readiness.data.conflicts.map(file => <li key={file} className="font-mono text-xs">{file}</li>)}</ul><SendToAgent session={session} kind="conflicts" label="Ask the agent to resolve" /></>}
          <div className="flex flex-wrap gap-2">
            <Button disabled={busy} onClick={refresh}>Refresh checklist</Button>
            <Button disabled={busy || !readiness.data || readiness.data.force_push_blocked || !!readiness.data.conflicts.length || completed.data?.ok} onClick={askCompleted}>Push completed rebase</Button>
          </div>
          {completed.data && <pre role="status" className="max-h-32 overflow-auto whitespace-pre-wrap text-xs">{completed.data.ok ? "Pushed." : "Push failed."} {completed.data.output}</pre>}
          {push.data && <pre role="status" className="max-h-32 overflow-auto whitespace-pre-wrap text-xs">{push.data.ok ? "Pushed." : "Push failed."} {push.data.output}</pre>}
        </li>
        <li className="space-y-2"><h3 className="font-semibold">2. Checks</h3>
          <p className="text-xs text-muted">{status.checks.length} checks reported. Rerun failed GitHub Actions jobs on the pushed commit. Other checks keep their provider’s controls.</p>
          <Button disabled={busy} onClick={() => rerun.mutate()}>Rerun failed CI checks</Button>
          {rerun.data && <p role="status">{rerun.data.runs.length ? `Requested ${rerun.data.runs.length} CI reruns. Refresh checks to see the result.` : "No failed GitHub Actions runs are available to rerun."}</p>}
        </li>
        <li className="space-y-2"><h3 className="font-semibold">3. Send failures to the agent</h3>
          <p className="text-xs text-muted">API-key Claude, Codex and OpenCode can receive failures automatically when you run this step. Subscription Claude requires you to copy the preview and paste it into the terminal.</p>
          <Button disabled={busy} onClick={() => { setCopied(false); feedback.mutate(); }}>Send to agent</Button>
          {feedback.data && <>
            <p role="status">{feedback.data.sent || send.isSuccess ? "Sent to the agent." : feedback.data.items ? "Review the bounded, redacted failure message below." : "No failed checks to send."}</p>
            {feedback.data.refused && <p role="status" className="text-xs text-wait">Automatic send refused: {feedback.data.refused} Copy the preview or retry sending after resolving the refusal.</p>}
            {!!feedback.data.items && <pre tabIndex={0} aria-label="Guided failure preview" className="max-h-48 overflow-auto whitespace-pre-wrap rounded border border-line p-3 text-xs">{feedback.data.text}</pre>}
            {!!feedback.data.items && !feedback.data.sent && !send.isSuccess && <div className="flex flex-wrap gap-2">
              <Button onClick={async () => { await navigator.clipboard.writeText(feedback.data!.text); setCopied(true); }}>{copied ? "Copied — paste into terminal" : "Copy message"}</Button>
              {readiness.data?.automatic && <Button disabled={busy} onClick={() => send.mutate()}>Confirm send to agent</Button>}
            </div>}
          </>}
        </li>
        <li className="space-y-2"><h3 className="font-semibold">4. Your merge decision</h3>
          <p className="text-xs text-muted">{status.merge?.allowed ? "GitHub reports this PR ready to merge." : status.merge?.blockers.join(" ") || "Refresh the PR status."} The merge card rechecks branch rules and the commit you reviewed when you confirm. Nothing merges automatically.</p>
          <Button onClick={() => { setOpen(false); document.getElementById("pull-merge")?.focus(); }}>Review merge decision</Button>
        </li>
      </ol>
      <ErrorNote error={readiness.error ?? base.error ?? push.error ?? completed.error ?? rerun.error ?? feedback.error ?? send.error} />
    </Dialog>
  </>;
}
