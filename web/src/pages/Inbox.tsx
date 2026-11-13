import { ParentChain } from '../components/ParentChain';
import { visibleText } from '../lib/visibleText';
import { RelayInboxActions } from '../components/session-relay/RelayInboxActions';
import { ApprovalCard } from '../components/ApprovalCard';
import { useSingleMutation as useMutation } from "../lib/mutations";
import clsx from "clsx";
import { lazy, Suspense, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, Info, KeyRound, MoreHorizontal, OctagonAlert, TerminalSquare } from "lucide-react";
import { post } from "../lib/api";
import { ago } from "../lib/format";
import type { AttentionItem } from "../lib/types";
import { Badge, Button, ErrorNote, Popover } from "../components/ui";
import { PublishActions, PublishSummary } from "../components/PublishRequest";
import { OFFLINE_TITLE, providerLabel, useOffline, useProviderRegistry, useRole } from "../lib/queries";
import { useConfirm } from "../components/confirm";
import { sourceWords } from "../lib/words";
import { ToolActions, ToolDecision } from "../components/Tools";
import { RiskLabel } from "../components/RiskLabel";

import { lazyImport } from '../lib/lazyImport';
const ShellInbox = lazy(() => lazyImport(() => import('../components/shell/Inbox')));

const ICON = { info: Info, warn: AlertTriangle, critical: OctagonAlert };
const TONE = { info: "text-accent", warn: "text-wait", critical: "text-bad" };

export { useAttention } from "../lib/queries";

/** Hook-backed Claude permissions can receive an explicit one-shot human decision. */
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

function PermissionActions({ item }: { item: AttentionItem }) {
  const queryClient = useQueryClient();
  const offline = useOffline();
  const reply = useMutation({
    mutationFn: (decision: "allow" | "deny") => post(`/api/attention/${item.id}/permission`, { token: item.permission_id, decision }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["attention"] }),
  });
  const [expired, setExpired] = useState(false);
  useEffect(() => {
    if (!item.permission_expires_at) return;
    const timer = setTimeout(() => setExpired(true), Math.max(0, Date.parse(item.permission_expires_at) - Date.now()));
    return () => clearTimeout(timer);
  }, [item.permission_expires_at]);
  return <div className="flex flex-wrap items-center gap-2">

    {item.permission_reason && <p>{item.permission_reason}</p>}
    {expired ? <p>This request expired. Answer in the terminal.</p> : <>
    {item.permission_can_approve && <Button size="sm" disabled={offline || reply.isPending || reply.isSuccess} onClick={() => reply.mutate("allow")}>Approve</Button>}
    <Button size="sm" variant="danger" disabled={offline || reply.isPending || reply.isSuccess} onClick={() => reply.mutate("deny")}>Deny</Button>
    </>}
    {reply.isSuccess && <span role="status">{reply.variables === "allow" ? "Approved" : "Denied"}</span>}
    <ErrorNote error={reply.error} />
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
      <div className="min-w-0 [overflow-wrap:anywhere]">
        <div className="flex flex-wrap items-center gap-2">
          <span className={clsx("font-medium", open ? "text-fg" : "text-muted")}>{item.title}</span>
          {/* Only the exception: a signal guessed from the screen rather than reported by a hook (UX review IN-2). */}
          {!item.verified && <Badge title="Guessed from what is on screen, not reported by the agent">guessed</Badge>}
          {item.resolved_at && <Badge>resolved</Badge>}
          {item.acked_at && !item.resolved_at && <Badge>{item.persistent ? "acknowledged" : "dismissed"}</Badge>}
          {snoozed && !item.resolved_at && <Badge>snoozed</Badge>}
        </div>
        {!publish && <ParentChain chain={item.parent_chain} owner={item.root_owner} />}
        {publish ? <>
          {publish.reason && <p className="mt-0.5 break-words text-[13px] text-muted">{publish.reason}</p>}
          <div className="mt-1"><PublishSummary request={publish} /></div>
        </> : item.detail && <p className="mt-0.5 break-words text-[13px] text-muted">{item.approval_card ? visibleText(item.detail) : item.detail}</p>}
        {item.source !== 'claude_terminal' && item.approval_card && <ApprovalCard key={item.permission_id ?? item.id} facts={item.approval_card} input={item.permission_input} risk={item.risk} />}
        {item.risk && !item.approval_card && <div className="mt-1"><RiskLabel risk={item.risk} /></div>}
        <p className="mt-1 text-xs text-faint">
          <span title={`Source: ${item.source}`}>{source}</span> · {ago(item.created_at)}{item.session_name?.includes(" · team ") && ` · ${item.session_name}`}{item.project && ` · ${item.project}`}
        </p>
        {item.source !== 'claude_terminal' && item.relay_decision && open && <RelayInboxActions key={item.relay_decision.decisionId} item={item} />}
        {item.source !== 'claude_terminal' && !item.relay_decision && item.permission_id && open && <div className="mt-3">{!item.approval_card && item.permission_input != null && <pre className="whitespace-pre-wrap break-all font-mono text-xs">{visibleText(item.permission_input)}</pre>}<PermissionActions key={item.permission_id} item={item} /></div>}
        <ErrorNote error={ack.error || snooze.error} />
      </div>
      <div className="col-start-2 flex flex-wrap items-center gap-2 sm:col-start-auto">
        {item.session_id && (item.kind === 'team_expired' || item.kind === 'team_lifetime') && <Link to={`/sessions/${item.session_id}?tab=team#overview`} className="inline-flex min-h-11 items-center rounded-md border border-line-strong px-3 text-sm text-accent">
          {item.kind === 'team_expired' ? 'Resume team' : 'Review team time limit'}
        </Link>}

        {item.session_id && (
          <Link to={`/sessions/${item.session_id}${item.source === 'claude_terminal' ? '?tab=terminal' : ''}`} className={clsx("inline-flex h-7 pointer-coarse:h-11 items-center gap-1 rounded-md px-2.5 text-[12.5px] font-medium",
            prompt && open ? "bg-accent text-accent-ink hover:brightness-110" : "border border-line-strong bg-panel-2 text-fg hover:bg-panel-3")}>
            <TerminalSquare size={14} aria-hidden="true" /> {item.source === 'claude_terminal' ? 'Open Terminal' : prompt && open ? "Answer in terminal" : "Open session"}
          </Link>
        )}
        {(item.kind === 'telemetry_report' || item.kind === 'telemetry_consent') && <Link to="/settings#telemetry" className="text-accent underline">Review privacy choices</Link>}
        {item.kind === "phone_setup" && <Link to="/guide/User-Guide/Connect-Your-Phone" className="text-accent underline">Connect your phone</Link>}
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
            port_owner: publish?.port_owner, project: publish?.project,
            parent_chain: publish?.parent_chain ?? item.parent_chain, root_owner: publish?.root_owner ?? item.root_owner }} />
        )}
        {item.tool && open && item.tool.status === "pending_approval" && <ToolActions request={item.tool} />}
        {item.tool && <ToolDecision request={item.tool} />}
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

  return <Suspense fallback={<p role="status">Loading Inbox…</p>}><ShellInbox /></Suspense>;
}
