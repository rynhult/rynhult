import type { HAHealth } from "./types";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "./api";
import type { AttentionItem, AuthState, Project, ProviderInfo, SessionSummary } from "./types";

/* Shared queries, so every page polls the same keys at the same rates. */
export const useSessions = () =>
  useQuery({ queryKey: ["sessions"], queryFn: () => api<SessionSummary[]>("/api/sessions"), refetchInterval: 3000 });

export const useProjects = () =>
  useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/api/projects"), staleTime: 10000 });

export const useAttention = () =>
  useQuery({ queryKey: ["attention"], queryFn: () => api<AttentionItem[]>("/api/attention"), refetchInterval: 3000 });

// App waits for the initial health result to identify a standby. Descendant subscribers must retain an
// initial error instead of starting another loading cycle just because a new subscriber mounts.
// Periodic polling and window focus still recover the connection.
export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: () => api<{ ok: boolean; broker?: boolean; ha?: HAHealth }>("/api/health"),
    retryOnMount: false, refetchInterval: 5000 });

/** The service that runs agent sessions (the broker) can't be reached. Shown only once it has stayed so for
 *  BROKER_GRACE_MS: right after start-up, or a restart, the first health readings can say so for a moment
 *  (first-run review). */
export const BROKER_GRACE_MS = 8000;
export const BROKER_DOWN_TITLE = "Rynhult can't reach the service that runs agent sessions, so sessions can't start or "
  + "reconnect; running sessions keep going. If this lasts more than a minute, see Troubleshooting: restart the broker.";
export function useBrokerDown() {
  const health = useHealth();
  const down = !!health.data && !health.data.ha && !health.data.broker;
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!down) { setShown(false); return; }
    const timer = setTimeout(() => setShown(true), BROKER_GRACE_MS);
    return () => clearTimeout(timer);
  }, [down]);
  return down && shown;
}

/** Rynhult isn't answering: pages keep the last data, and buttons that change something are disabled (UX review NAV-9). */
export const OFFLINE_TITLE = "Offline: Rynhult isn't answering. This works again once it's back.";
export const useOffline = () => {
  const sessions = useSessions();
  const health = useHealth();
  // A retry without any successful health data temporarily returns to pending. Stay offline until it succeeds.
  return sessions.isError || health.isError || (health.isFetched && !health.data);
};

/** How this installation names itself (host label, agent user, wildcard zone), from the signed-in auth state. */
export const useInstance = () => {
  const auth = useQuery({ queryKey: ["auth"], queryFn: () => api<AuthState>("/api/auth/state", { cache: "no-store" }), staleTime: 5000, refetchInterval: 5000 });
  return auth.data?.instance ?? { host: "this host", agent_user: "the agent user", wildcard_zone: "", version: undefined };
};

/** The signed-in user's role: owners see the host, audit and everyone's work; members their own. */
export const useRole = () => {
  const auth = useQuery({ queryKey: ["auth"], queryFn: () => api<AuthState>("/api/auth/state"), staleTime: 60000 });
  return { owner: auth.data?.role === "owner", role: auth.data?.role, account: auth.data?.account, known: !!auth.data,
    username: auth.data?.username ?? undefined };
};

/** Every agent provider Rynhult knows (label, colour, launch options, capabilities, installed for your agent account). */
export const useProviderRegistry = (projectId?: number, enabled = true) => {
  const query = useQuery({ queryKey: ["provider-registry", projectId ?? "account"], queryFn: () =>
    api<{ providers: ProviderInfo[]; team_agents: { agent: string; profile: string | null; options: Record<string, string | boolean> }[] }>(`/api/providers/registry${projectId ? `?project_id=${projectId}` : ""}`),
    enabled, staleTime: 300000 });
  const list = query.data?.providers ?? [];
  return { ...query, list, byId: (id: string) => list.find((p) => p.id === id) };
};

/** A provider's display name before the registry has loaded (or for an id it no longer knows). */
export const providerLabel = (id: string, info?: ProviderInfo) => info?.label ?? ({ claude: "Claude Code", codex: "Codex" }[id] ?? id);
export const providerShort = (id: string, info?: ProviderInfo) => info?.short ?? ({ claude: "Claude", codex: "Codex" }[id] ?? id);

/** Group administration never grants instance privileges. An absent membership leaves legacy navigation alone. */
export const useGroupAdministration = () => {
  const instance = useInstance();
  const groups = useQuery({ enabled: instance.features?.groups !== false, queryKey: ["groups"], queryFn: () => api<{ role: string }[]>("/api/groups"), refetchInterval: 10000 });
  return groups.data?.some(group => group.role === "maintainer") ?? false;
};
