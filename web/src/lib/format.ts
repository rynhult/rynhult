export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function until(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "";
  const seconds = epochSeconds - Date.now() / 1000;
  if (seconds <= 0) return "now";
  if (seconds < 3600) return `in ${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `in ${Math.round(seconds / 3600)}h`;
  return `in ${Math.round(seconds / 86400)}d`;
}

export const stamp = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** The built-in projects that group local workspaces ("local", and "local-<account>" for other people's accounts). */
export const isLocalProject = (p: { slug: string }) => p.slug === "local" || p.slug.startsWith("local-");

// Greedy prefix selects the rightmost date suffix, preserving an eight-digit topic token before it.
export const workspaceTopic = (folder: string) => folder.replace(/^(.*)-\d{8}(?:-\d+)?$/, "$1");

export const workspaceName = (folder: string) => {
  const words = workspaceTopic(folder).replace(/-/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};

/** A size on disk: 512 KB, 3.4 MB, 1.2 GB. */
export function size(n: number | null | undefined): string {
  if (n == null) return "–";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1e3))} KB`;
}

/** Keep each team together while retaining the input order among roots and siblings. */
export function teamOrder<T extends { id: string; parent_session_id?: string | null }>(sessions: T[]): T[] {
  const ids = new Set(sessions.map((s) => s.id));
  const result: T[] = [], seen = new Set<string>();
  const visit = (s: T) => {
    if (seen.has(s.id)) return;
    seen.add(s.id); result.push(s);
    sessions.filter((x) => x.parent_session_id === s.id).forEach(visit);
  };
  sessions.filter((s) => !s.parent_session_id || !ids.has(s.parent_session_id)).forEach(visit);
  sessions.forEach(visit);
  return result;
}

/** Keep the parents of visible team members as navigation context, including a stopped lead. */
export function withTeamAncestors<T extends { id: string; parent_session_id?: string | null }>(all: T[], visible: T[]): T[] {
  const byId = new Map(all.map((s) => [s.id, s]));
  const ids = new Set(visible.map((s) => s.id));
  for (const member of visible) {
    let parent = member.parent_session_id;
    const seen = new Set<string>();
    while (parent && !seen.has(parent)) {
      seen.add(parent); ids.add(parent); parent = byId.get(parent)?.parent_session_id;
    }
  }
  return all.filter((s) => ids.has(s.id));
}

/** Two letters that tell projects apart: "Demo API" → DA, "Demo Web" → DW, "handbook" → HA (UX review PJ-2). */
export const initials = (name: string) => {
  const words = name.split(/[^A-Za-z0-9]+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : (words[0] ?? "").slice(0, 2)).toUpperCase();
};

/** How long since `iso`, in the two largest units: "45s", "12m", "2h 14m", "3d 4h". */
export function duration(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}
