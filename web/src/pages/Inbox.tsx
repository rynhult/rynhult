import { useSingleMutation as useMutation } from "../lib/mutations";
import clsx from "clsx";
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, CheckCheck, Info, KeyRound, MoreHorizontal, OctagonAlert, TerminalSquare } from "lucide-react";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import type { AttentionItem } from "../lib/types";
import { Badge, Button, Card, Empty, ErrorNote, Page, PageHeader, Popover, QueryState, Segmented } from "../components/ui";
import { PublishActions, PublishSummary } from "../components/PublishRequest";
import { OFFLINE_TITLE, providerLabel, useOffline, useProviderRegistry, useRole } from "../lib/queries";
import { useConfirm } from "../components/confirm";
import { sourceWords } from "../lib/words";
import { ToolActions } from "../components/Tools";
import { RiskLabel } from "../components/RiskLabel";
import { groupInbox } from "../lib/order";

const ICON = { info: Info, warn: AlertTriangle, critical: OctagonAlert };
const TONE = { info: "text-accent", warn: "text-wait", critical: "text-bad" };

export { useAttention } from "../lib/queries";

/** A session waiting on a prompt: the answer is in its terminal, never in the Inbox (owner decision 0008, Q2). */
const PROMPTS = new Set(["needs_input"]);

/** A fixed server-generated action; agent text never supplies URLs or request fields. */
function CheckImageAccess({ action }: { action: NonNullable<AttentionItem['image_check']> }) {
  const queryClient = useQueryClient();
  const offline = useOffline();
  const check = useMutation({
    mutationFn: () => post<{ image_input: boolean | null; evidence: { outcome: string } }>('/api/providers/image-check', action),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['attention'] });
      void queryClient.invalidateQueries({ queryKey: ['models'] });
    },
  });
  return <div className="flex min-w-0 flex-col items-start gap-1">
    <Button size="sm" disabled={check.isPending || offline} onClick={() => check.mutate()}>
      {check.isPending ? 'Checking image access…' : 'Check image access'}
    </Button>
    <ErrorNote error={check.error} />
    {check.data && <p role="status" className="text-xs text-muted">
      {check.data.image_input === true ? 'Verified' : check.data.image_input === false ? 'Unavailable' : 'Unknown'} · {check.data.evidence.outcome}
    </p>}
  </div>;
}

/** Replacement recovery: a restored session the old main left running on a machine. Only an owner, verified again
 * within five minutes, stops that one pane; the session stays archived and nothing stops by itself. */
function StopHeld({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const offline = useOffline();
  const confirm = useConfirm();
  const stop = useMutation({
    mutationFn: () => post(`/api/sessions/${sessionId}/stop-held`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["attention"] }),
  });
  const ask = async () => {
    if (await confirm({ title: "Stop it on that machine?", danger: true, confirm: "Stop on that machine",
      body: "Rynhult sends one stop for this session to the machine it runs on. Anything the agent hasn't saved is lost. "
        + "The session stays archived; inspect its read-only terminal first. You need to have verified your sign-in in "
        + "the last five minutes (Settings → Passkeys)." })) stop.mutate();
  };
  return (
    <div className="flex basis-full flex-col items-start gap-1 sm:basis-auto">
      <Button size="sm" variant="danger" onClick={() => void ask()} disabled={stop.isPending || offline}
        title={offline ? OFFLINE_TITLE : undefined}>Stop on that machine</Button>
      <ErrorNote error={stop.error} />
    </div>
  );
}

function StopScopeHelper({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const offline = useOffline();
  const confirm = useConfirm();
  const stop = useMutation({
    mutationFn: () => post(`/api/sessions/${sessionId}/stop`),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["attention"] });
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  return <div className="flex basis-full flex-col items-start gap-1 sm:basis-auto">
    <Button size="sm" variant="danger" disabled={stop.isPending || offline} title={offline ? OFFLINE_TITLE : undefined}
      onClick={async () => {
        if (await confirm({ title: "Stop this helper?", danger: true, confirm: "Stop helper",
          body: "The accepted scope change stays saved. Unsaved work may be lost. Resume the helper to deliver the current scope; integration remains blocked until receipt or a verified restart." })) stop.mutate();
      }}>Stop helper</Button>
    <ErrorNote error={stop.error} />
  </div>;
}

export function AttentionRow({ item }: { item: AttentionItem }) {
  const queryClient = useQueryClient();
  const registry = useProviderRegistry();
  const offline = useOffline();
  const ack = useMutation({
    mutationFn: () => post(`/api/attention/${item.id}/ack`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["attention"] }),
  });
  const snooze = useMutation({
    mutationFn: (minutes: number) => post(`/api/attention/${item.id}/snooze`, { minutes }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["attention"] }),
  });
  const Icon = item.kind === "auth" ? KeyRound : ICON[item.severity];
  const snoozed = !!item.snoozed_until && Date.parse(item.snoozed_until) > Date.now();
  const open = !item.resolved_at && !snoozed && (!!item.persistent || !item.acked_at);
  const prompt = PROMPTS.has(item.kind);
  const source = sourceWords(item.source, (id) => providerLabel(id, registry.byId(id)));
  const publish = item.kind === "publish_request" && item.publish ? { ...item.publish, action: item.publish.action ?? "publish" } : null;
  const dismissible = open && item.kind !== "publish_request" && item.kind !== "tool_request";
  const { owner } = useRole();
  return (
    <div role={item.tool ? "region" : undefined} aria-label={item.tool ? `Tool request: ${item.tool.name}` : undefined}
      className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-3 px-4 py-3 sm:grid-cols-[auto_minmax(0,1fr)_auto]">
      <Icon size={16} className={clsx("mt-0.5", open ? TONE[item.severity] : "text-faint")} aria-label={item.severity} />
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className={clsx("font-medium", open ? "text-fg" : "text-muted")}>{item.title}</span>
          {/* Only the exception: a signal guessed from the screen rather than reported by a hook (UX review IN-2). */}
          {!item.verified && <Badge title="Guessed from what is on screen, not reported by the agent">guessed</Badge>}
          {item.resolved_at && <Badge>resolved</Badge>}
          {item.acked_at && !item.resolved_at && <Badge>{item.persistent ? "acknowledged" : "dismissed"}</Badge>}
          {snoozed && !item.resolved_at && <Badge>snoozed</Badge>}
        </div>
        {publish ? <>
          {publish.reason && <p className="mt-0.5 break-words text-[13px] text-muted">{publish.reason}</p>}
          <div className="mt-1"><PublishSummary request={publish} /></div>
        </> : item.detail && <p className="mt-0.5 break-words text-[13px] text-muted">{item.detail}</p>}
        {item.risk && <div className="mt-1"><RiskLabel risk={item.risk} /></div>}
        <p className="mt-1 text-xs text-faint">
          <span title={`Source: ${item.source}`}>{source}</span> · {ago(item.created_at)}{item.session_name?.includes(" · team ") && ` · ${item.session_name}`}{item.project && ` · ${item.project}`}
        </p>
        <ErrorNote error={ack.error || snooze.error} />
      </div>
      <div className="col-start-2 flex flex-wrap items-center gap-2 sm:col-start-auto">
        {item.session_id && (item.kind === 'team_expired' || item.kind === 'team_lifetime') && <Link to={`/sessions/${item.session_id}?tab=team#overview`} className="inline-flex min-h-11 items-center rounded-md border border-line-strong px-3 text-sm text-accent">
          {item.kind === 'team_expired' ? 'Resume team' : 'Review team time limit'}
        </Link>}
        {item.session_id && (
          <Link to={`/sessions/${item.session_id}`} className={clsx("inline-flex h-7 pointer-coarse:h-11 items-center gap-1 rounded-md px-2.5 text-[12.5px] font-medium",
            prompt && open ? "bg-accent text-accent-ink hover:brightness-110" : "border border-line-strong bg-panel-2 text-fg hover:bg-panel-3")}>
            <TerminalSquare size={14} aria-hidden="true" /> {prompt && open ? "Answer in terminal" : "Open session"}
          </Link>
        )}
        {(item.kind === 'telemetry_report' || item.kind === 'telemetry_consent') && <Link to="/settings#telemetry" className="text-accent underline">Review privacy choices</Link>}
        {item.kind === "machine_offline" && <Link to="/host" className="text-accent underline">Review machine</Link>}
        {item.kind === "backup" && <Link to="/host#host-backups" className="text-accent underline">Review backups</Link>}
        {item.persistent && !item.resolved_at && <Button size="sm" variant="ghost" onClick={() => snooze.mutate(snoozed ? 0 : 60)}
          disabled={snooze.isPending || offline} title={offline ? OFFLINE_TITLE : undefined}>{snoozed ? "End snooze" : "Snooze 1 hour"}</Button>}
        {item.kind === "auth" && (
          <Link to="/providers" className="inline-flex h-7 pointer-coarse:h-11 items-center gap-1 rounded-md px-2 text-[13px] text-muted hover:bg-panel-2 hover:text-fg">
            <KeyRound size={14} aria-hidden="true" /> Providers
          </Link>
        )}
        {item.image_check && open && owner && <CheckImageAccess action={item.image_check} />}
        {item.kind === "publish_request" && item.publish_id && open && (
          <PublishActions request={{ id: item.publish_id, status: "pending_approval", action: publish?.action ?? "publish", host: publish?.host ?? "",
            port: publish?.port ?? null, access: publish?.access ?? null, replaces: publish?.replaces ?? null,
            port_owner: publish?.port_owner, project: publish?.project }} />
        )}
        {item.tool && open && item.tool.status === "pending_approval" && <ToolActions request={item.tool} />}
        {item.kind === "scope_change" && item.session_id && open && <StopScopeHelper sessionId={item.session_id} />}
        {item.kind === "replacement_held" && item.session_id && open && owner && <StopHeld sessionId={item.session_id} />}
        {/* Dismissing hides the notice; it never answers. On a prompt it sits behind ⋯, so it isn't mistaken for one. */}
        {dismissible && prompt && (
          <Popover menu label="More actions" trigger={<MoreHorizontal size={15} aria-hidden="true" />}>
            {(close) => (
              <button type="button" role="menuitem" tabIndex={-1} onClick={() => { close(); ack.mutate(); }}
                className="flex min-h-10 w-full flex-col items-start justify-center rounded-md px-3 text-left text-[13px] text-fg hover:bg-panel-2">
                Dismiss notice<span className="text-xs text-muted">The session keeps waiting for its answer</span>
              </button>
            )}
          </Popover>
        )}
        {dismissible && !prompt && !item.acked_at && <Button size="sm" variant="ghost" onClick={() => ack.mutate()} disabled={ack.isPending || offline} title={offline ? OFFLINE_TITLE : undefined}>{item.persistent ? "Acknowledge" : "Dismiss notice"}</Button>}
      </div>
    </div>
  );
}

export function Inbox() {
  const queryClient = useQueryClient();
  const offline = useOffline();
  const { username } = useRole();
  const [snapshot, setSnapshot] = useState<AttentionItem[] | null>(null);
  const [undo, setUndo] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  useEffect(() => { setUndo(null); setSnapshot(null); setMessage(""); }, [username]);
  useEffect(() => { if (undo !== null) { const timer = window.setTimeout(() => setUndo(null), 8000); return () => clearTimeout(timer); } }, [undo]);
  const dismiss = useMutation({
    mutationFn: (shown: AttentionItem[]) => post<{ batch: number; dismissed: number; skipped: number }>("/api/attention/dismiss", {
      items: shown.map(({ id, updated_at }) => ({ id, updated_at })),
    }),
    onSuccess: result => {
      setSnapshot(null); setUndo(result.dismissed ? result.batch : null);
      setMessage(`${result.dismissed} dismissed. ${result.skipped} skipped (decisions, active alerts, handled or changed notices).`);
      queryClient.invalidateQueries({ queryKey: ["attention"] });
    },
  });
  const restore = useMutation({
    mutationFn: (batch: number) => post<{ restored: number }>("/api/attention/dismiss/undo", { batch }),
    onSuccess: result => { setUndo(null); setMessage(`${result.restored} restored. Changed notices stay as they are.`); queryClient.invalidateQueries({ queryKey: ["attention"] }); },
  });
  const [scope, setScope] = useState<"open" | "all">("open");
  const items = useQuery({
    queryKey: ["attention", scope],
    queryFn: () => api<AttentionItem[]>(`/api/attention?scope=${scope}`),
    refetchInterval: 3000,
  });
  return (
    <Page>
      <PageHeader title="Inbox" description="Everything to review: sessions waiting on you first, then approvals, alerts and notes. Dismissing a notice never approves a change or answers a prompt."
        actions={<>{items.data?.some(item => item.bulk_dismissible) && <Button disabled={!items.data?.length || !!snapshot || undo !== null || dismiss.isPending || restore.isPending || offline} onClick={() => { dismiss.reset(); restore.reset(); setSnapshot([...(items.data ?? [])]); }}>Dismiss all</Button>}<Segmented label="Show" value={scope} onChange={setScope} options={[{ id: "open", label: "Open" }, { id: "all", label: "History" }]} /></>} />
      {snapshot && <Card className="space-y-3 p-4">
        <p className="font-medium">Dismiss {snapshot.filter(i => i.bulk_dismissible).length} items?</p>
        <p className="text-sm text-muted">{snapshot.filter(i => !i.bulk_dismissible).length} skipped. Decisions, active alerts and handled notices stay in your Inbox.</p>
        <div className="flex flex-wrap gap-2"><Button disabled={dismiss.isPending} onClick={() => setSnapshot(null)}>Cancel</Button>
          <Button disabled={dismiss.isPending || offline || !snapshot.some(i => i.bulk_dismissible)} onClick={() => dismiss.mutate(snapshot)}>Dismiss {snapshot.filter(i => i.bulk_dismissible).length} items</Button></div>
      </Card>}
      <ErrorNote error={dismiss.error || restore.error} />
      {message && <div className="flex flex-wrap items-center gap-2"><p role="status" className="text-sm text-muted">{message}</p>
        {undo !== null && <Button disabled={restore.isPending || offline} onClick={() => restore.mutate(undo)}>Undo</Button>}</div>}
      <Card>
        <QueryState query={items} isEmpty={(d) => !d.length}
          empty={<Empty icon={CheckCheck}>{scope === "open" ? "Nothing needs you right now." : "No history yet."}</Empty>}>
          {(data) => scope === "open"
            ? groupInbox(data).map((g) => (
              <section key={g.key} aria-label={g.label} className="border-t border-line first:border-t-0">
                <h2 className={clsx("flex items-center gap-2 border-b border-line bg-panel-2/60 px-4 py-1.5 text-[11px] font-semibold uppercase tracking-[0.08em]",
                  g.key === "waiting" ? "text-wait" : "text-faint")}>{g.label} <span className="tnum font-normal">{g.items.length}</span></h2>
                <div className="divide-y divide-line">{g.items.map((i) => <AttentionRow key={i.id} item={i} />)}</div>
              </section>
            ))
            : <div className="divide-y divide-line">{data.map((i) => <AttentionRow key={i.id} item={i} />)}</div>}
        </QueryState>
      </Card>
    </Page>
  );
}
