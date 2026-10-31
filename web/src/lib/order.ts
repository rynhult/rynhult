/* What needs you first: the one order and grouping every session list and count uses (UX review SL-1, NAV-1).
   "Needs you" means sessions blocked on a person; the Inbox is everything to review (owner decision 0008, Q1).
   Pure functions, no React, so they can be unit tested. */
import type { AttentionItem, ObservedState } from "./types";

export const SESSION_GROUPS = [
  { key: "needs_you", label: "Needs you", states: ["waiting_input", "recovery_needed"] },
  { key: "working", label: "Working", states: ["starting", "active", "recovering"] },
  { key: "unknown", label: "Unknown", states: [] },
  { key: "idle", label: "Idle", states: ["idle"] },
  { key: "stopped", label: "Stopped", states: ["stopped", "exited", "archived"] },
] as const satisfies readonly { key: string; label: string; states: readonly ObservedState[] }[];
export type GroupKey = (typeof SESSION_GROUPS)[number]["key"];

type Orderable = {
  id: string; observed_state: ObservedState; team_id?: string | null;
  status_stale_since?: string | null;
  activity_at?: string | null; last_output_at?: string | null; status_at?: string | null; created_at?: string | null;
};

const inGroup = (session: Orderable, key: GroupKey) => session.status_stale_since ? key === "unknown"
  : (SESSION_GROUPS.find((g) => g.key === key)!.states as readonly string[]).includes(session.observed_state);

export const sessionCount = (sessions: Orderable[], key: GroupKey) => sessions.filter((s) => inGroup(s, key)).length;

/** The newest sign of life: a tool step, terminal output, a state change, or the launch. */
export const lastActive = (s: Orderable) =>
  Math.max(0, ...[s.activity_at, s.last_output_at, s.status_at, s.created_at].map((t) => (t ? Date.parse(t) || 0 : 0)));

export const byRecent = <T extends Orderable>(sessions: T[]) => [...sessions].sort((a, b) => lastActive(b) - lastActive(a));

/** Sessions blocked on a person: the number the sidebar, the Overview and the Sessions filter all show. */
export const needsYou = <T extends Orderable>(sessions: T[]) => sessions.filter((s) => inGroup(s, "needs_you"));

/** Sessions in groups, Needs you → Working → Unknown → Idle → Stopped, each newest first. A team stays together in the group of
    its most urgent member; `count` is how many sessions are themselves in that state (team context rows aren't counted). */
export function groupSessions<T extends Orderable>(sessions: T[], keys: readonly GroupKey[] = SESSION_GROUPS.map((g) => g.key)) {
  const groupOf = (s: T): GroupKey | undefined => {
    const members = s.team_id ? sessions.filter((x) => x.team_id === s.team_id) : [s];
    return SESSION_GROUPS.find((g) => members.some((x) => inGroup(x, g.key)))?.key;
  };
  const sorted = byRecent(sessions);
  return SESSION_GROUPS.filter((g) => keys.includes(g.key))
    .map((g) => ({ key: g.key, label: g.label, sessions: sorted.filter((s) => groupOf(s) === g.key),
      count: sessionCount(sessions, g.key) }))
    .filter((g) => g.sessions.length > 0);
}

/** Inbox rows by type: sessions waiting on you, then approvals, then alerts, with FYI notes last. */
export const INBOX_GROUPS = [
  { key: "waiting", label: "Waiting sessions" },
  { key: "approvals", label: "Approvals" },
  { key: "alerts", label: "Alerts" },
  { key: "notes", label: "Notes" },
] as const;
export type InboxGroupKey = (typeof INBOX_GROUPS)[number]["key"];

export const inboxGroupOf = (item: Pick<AttentionItem, "kind" | "severity">): InboxGroupKey =>
  item.kind === "needs_input" || item.kind === "recovery_needed" || item.kind === "image_check" ? "waiting"
    : item.kind === "publish_request" || item.kind === "tool_request" ? "approvals"
      : item.severity === "info" ? "notes" : "alerts";

/** Keep the API's order within each group: operational alerts first, otherwise newest first. */
export function groupInbox<T extends Pick<AttentionItem, "kind" | "severity">>(items: T[]) {
  return INBOX_GROUPS.map((g) => ({ ...g, items: items.filter((i) => inboxGroupOf(i) === g.key) })).filter((g) => g.items.length > 0);
}

/** Open Inbox items in group order, also used by the Overview's Inbox stat. */
export const sortInbox = <T extends Pick<AttentionItem, "kind" | "severity">>(items: T[]) => groupInbox(items).flatMap((g) => g.items);
