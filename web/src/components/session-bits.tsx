import { settingWords } from "../lib/words";
import { useSingleMutation as useMutation } from "../lib/mutations";
import clsx from "clsx";
import { useQueryClient } from "@tanstack/react-query";
import { Archive, CircleStop, ExternalLink, Hand, MoreHorizontal, Play, ShieldAlert } from "lucide-react";
import { ago } from "../lib/format";
import { useConfirm } from "./confirm";
import { post } from "../lib/api";
import type { ObservedState, Provider, SessionSummary } from "../lib/types";
import { Badge, Button, Dot, Popover } from "./ui";
import { OFFLINE_TITLE, providerLabel, providerShort, useInstance, useOffline, useProviderRegistry } from "../lib/queries";
import { groupSessions } from "../lib/order";

type StateTone = "accent" | "ok" | "wait" | "idle" | "neutral" | "bad";
const STATES: Record<ObservedState, { label: string; tone: StateTone; pulse?: boolean; text: string }> = {
  starting: { label: "Starting", tone: "accent", pulse: true, text: "text-accent" },
  active: { label: "Working", tone: "ok", pulse: true, text: "text-ok" },
  waiting_input: { label: "Needs you", tone: "wait", text: "text-wait" },
  idle: { label: "Idle", tone: "idle", text: "text-muted" },
  exited: { label: "Exited", tone: "neutral", text: "text-muted" },
  recovering: { label: "Recovering", tone: "wait", pulse: true, text: "text-wait" },
  recovery_needed: { label: "Recovery needed", tone: "bad", text: "text-bad" },
  stopped: { label: "Stopped", tone: "neutral", text: "text-muted" },
  archived: { label: "Archived", tone: "neutral", text: "text-faint" },
};

export const sessionState = (s: Pick<SessionSummary, "observed_state" | "status_stale_since">) =>
  s.status_stale_since ? { ...STATES.exited, label: `Unknown since ${ago(s.status_stale_since)}` }
    : STATES[s.observed_state] ?? STATES.idle;

export const sessionMachineName = (session: Pick<SessionSummary, "host">, main: string) =>
  session.host && session.host !== "local" ? session.host : main;

export function SessionMachine({ session, inline = false }: { session: Pick<SessionSummary, "host">; inline?: boolean }) {
  const { host } = useInstance();
  const machine = sessionMachineName(session, host);
  return <span className={clsx("inline-flex max-w-[10rem] shrink-0 gap-1 text-muted", !inline && "rounded border border-line px-1 leading-4")}
    title={`Machine: ${machine}`}>{inline && <span className="shrink-0">· on</span>}<span className="truncate">{machine}</span></span>;
}

const SOURCES: Record<string, string> = {
  tmux: "tmux process state",
  supervisor: "Rynhult supervisor",
  api: "your action",
  screen: "approval prompt on screen",
};

export function StateBadge({ session }: { session: Pick<SessionSummary, "observed_state" | "status_source" | "status_at" | "status_stale_since" | "machine_offline" | "sleep_paused_at" | "last_error"> }) {
  if (session.sleep_paused_at && session.observed_state === 'stopped') return <span className="text-xs text-muted" title="Paused so the machine can sleep">{session.last_error?.startsWith('stop not confirmed:') ? 'Pause pending' : 'Paused for sleep'}</span>;
  if (session.machine_offline) return <span className="text-xs text-muted">Machine offline</span>;
  const state = sessionState(session);
  const raw = session.status_source ?? "";
  const source = SOURCES[raw] ?? (raw.endsWith("_hook") ? `${providerShort(raw.slice(0, -5))} hook (verified)` : raw || "unknown");
  return (
    <span className={clsx("inline-flex items-center gap-2 text-[12.5px] font-medium", state.text,
      session.status_stale_since ? "whitespace-normal" : "whitespace-nowrap")}
      title={`Source: ${source}${session.status_at ? ` · ${new Date(session.status_at).toLocaleString()}` : ""}`}>
      <Dot tone={state.tone} pulse={state.pulse} />
      {state.label}
    </span>
  );
}

/** A provider's brand mark: a small square in its colour. Round dots mean status, so a brand colour (Claude's coral)
    is never read as "something is wrong" (UX review PV-1). */
export function ProviderMark({ provider }: { provider: Provider }) {
  const info = useProviderRegistry().byId(provider);
  return <span className="size-2 shrink-0 rounded-[2px]" aria-hidden="true" data-provider-mark=""
    style={{ background: info?.color ?? (provider === "claude" ? "#d97757" : "var(--fg)") }} />;
}

export function ProviderBadge({ provider, model }: { provider: Provider; model?: string | null }) {
  const info = useProviderRegistry().byId(provider);
  return (
    // max-w-full: in a narrow column (a team's session list) a long model name is cut off instead of running into
    // the next column (usability walkthrough 2026-10-27).
    <Badge tone="neutral" className="max-w-full overflow-hidden" title={model ? `${providerLabel(provider, info)} · ${model}` : undefined}>
      <ProviderMark provider={provider} />
      {providerShort(provider, info)}
      {model && <span className="min-w-0 max-w-20 truncate text-faint">· {model}</span>}
    </Badge>
  );
}

/** What a working (or waiting) session is doing now, from its tool hooks; nothing once its turn is over. */
export const nowDoing = (s: Pick<SessionSummary, "activity" | "observed_state" | "status_stale_since">) =>
  !s.status_stale_since && s.activity && ["active", "waiting_input"].includes(s.observed_state) ? s.activity : null;

/** What it's doing now, with a guardrail refusal marked amber with a shield and its age (UX review SL-2, owner
    decision 0008 Q4: a refusal is history with a timestamp, not a session state). */
export function NowDoing({ session, className }: { session: Pick<SessionSummary, "activity" | "activity_at" | "observed_state" | "status_stale_since">; className?: string }) {
  const text = nowDoing(session);
  if (!text) return null;
  const blocked = /^Blocked:\s*/.exec(text);
  if (!blocked) return <span className={className} title={text}>{text}</span>;
  const age = session.activity_at ? ago(session.activity_at) : null;
  return (
    <span className={className} title={`Refused by a guardrail${age ? ` ${age}` : ""}: ${text.slice(blocked[0].length)}`}>
      <ShieldAlert size={12} className="mr-1 inline align-[-2px] text-wait" aria-hidden="true" />
      <span className="font-medium text-wait">Blocked{age ? ` ${age}` : ""}:</span> {text.slice(blocked[0].length)}
    </span>
  );
}

export const isLive = (s: SessionSummary) => ["starting", "active", "waiting_input", "idle", "recovering"].includes(s.observed_state);

/** The shared live groups: needs you, working, unknown, then idle. */
export const groupLive = (sessions: SessionSummary[]) => groupSessions(sessions, ["needs_you", "working", "unknown", "idle"]);

export function SessionActions({ session, compact }: { session: SessionSummary; compact?: boolean }) {
  const queryClient = useQueryClient();
  const action = useMutation({
    mutationFn: (verb: string) => post(`/api/sessions/${session.id}/${verb}`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["sessions"] }),
  });
  const confirm = useConfirm();
  const run = async (verb: string, ask?: { title: string; body: string; confirm: string }) => {
    if (ask && !(await confirm({ ...ask, danger: verb === "archive" }))) return;
    action.mutate(verb);
  };
  const size = compact ? "sm" : "md";
  const live = isLive(session);
  const offline = useOffline();
  const off = offline ? { disabled: true, title: OFFLINE_TITLE } : {};
  if (session.management === "external") {
    return (
      <div className="flex items-center gap-1.5">
        {session.remote_url && (
          <a href={session.remote_url} target="_blank" rel="noreferrer"
            className="inline-flex h-7 pointer-coarse:h-11 items-center gap-1 rounded-md px-2 text-[13px] text-muted hover:bg-panel-2 hover:text-fg underline underline-offset-2"
            title="Open in Claude Remote Control"><ExternalLink size={14} /> {!compact && "Remote"}</a>
        )}
        {!compact && <Badge tone="neutral" className="whitespace-nowrap">{`observe-only · ${session.run_as}`}</Badge>}
      </div>
    );
  }
  const working = session.observed_state === "active" || session.observed_state === "waiting_input";
  const interrupt = () => run("interrupt");
  const stop = () => run("stop", { title: `Stop ${session.name}?`, body: "The process ends; the conversation is kept and Resume brings it back.", confirm: "Stop session" });
  const menuItem = "flex min-h-10 w-full items-center gap-2 rounded-md px-3 text-left text-[13px] hover:bg-panel-2";
  return (
    <div className="flex items-center gap-1.5">
      {session.remote_url && (
        <a href={session.remote_url} target="_blank" rel="noreferrer"
          className="inline-flex h-7 pointer-coarse:h-11 items-center gap-1 rounded-md px-2 text-[13px] text-muted hover:bg-panel-2 hover:text-fg underline underline-offset-2"
          title="Open in Claude Remote Control (claude.ai / mobile app)" aria-label="Open in Remote Control">
          <ExternalLink size={14} /> {!compact && "Remote"}
        </a>
      )}
      {/* With a mouse, Interrupt and Stop sit in the row. On touch they go behind "⋯", so a tap meant for the terminal
          link can't interrupt or stop an agent mid-edit (UX review OV-4). */}
      {working && (
        <Button size={size} variant="ghost" onClick={interrupt} title="Interrupt: send Esc to stop the current turn" aria-label="Interrupt" {...off}
          className={clsx(live && "pointer-coarse:hidden")}>
          <Hand size={14} aria-hidden="true" /> {!compact && "Interrupt"}
        </Button>
      )}
      {live ? (
        <>
          <Button size={size} variant="ghost" onClick={stop} title="Stop (keeps the conversation)" aria-label="Stop" className="pointer-coarse:hidden" {...off}>
            <CircleStop size={14} aria-hidden="true" /> {!compact && "Stop"}
          </Button>
          <Popover menu label={`Actions for ${session.name}`} className="hidden pointer-coarse:block" trigger={<MoreHorizontal size={16} aria-hidden="true" />}>
            {(close) => <>
              {working && <button type="button" role="menuitem" tabIndex={-1} className={clsx(menuItem, "text-fg disabled:opacity-50")} {...off} onClick={() => { close(); interrupt(); }}>
                <Hand size={14} aria-hidden="true" /> Interrupt the current turn</button>}
              <button type="button" role="menuitem" tabIndex={-1} className={clsx(menuItem, "text-bad disabled:opacity-50")} {...off} onClick={() => { close(); void stop(); }}>
                <CircleStop size={14} aria-hidden="true" /> Stop session…</button>
            </>}
          </Popover>
        </>
      ) : session.desired_state !== "archived" && (
        <Button size={size} variant="ghost" onClick={() => run("resume")} title="Resume the same conversation" aria-label="Resume" {...off}>
          <Play size={14} aria-hidden="true" /> {!compact && "Resume"}
        </Button>
      )}
      {!compact && !live && session.desired_state !== "archived" && (
        <Button size={size} variant="ghost" {...off} onClick={() => run("archive", { title: `Archive ${session.name}?`, body: "It leaves the session lists. The transcript stays on disk.", confirm: "Archive" })}>
          <Archive size={14} aria-hidden="true" /> Archive
        </Button>
      )}
    </div>
  );
}

/** Claude's permission modes in words; the raw value stays in a tooltip (UX review SE-6). */
export const permissionWords = (mode: string) => settingWords("permission_mode", mode);
