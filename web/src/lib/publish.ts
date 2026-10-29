/* What an approver must know about a publish request, in one place for Inbox, Overview and Publishing (UX review
   IN-1, PB-1). Pure functions, no React, so they can be unit tested. */
import type { PublishRequest } from "./types";

export type PublishFacts = Pick<PublishRequest, "host" | "port" | "access" | "action" | "replaces"> &
  Partial<Pick<PublishRequest, "port_owner" | "requested_by" | "reason">> & { project?: string | null };

export const where = (access: string | null) => (access === "public" ? "the public internet" : "private networks only");

export interface PublishWarning { tone: "wait" | "bad"; text: string }

/** The exceptions, most serious first. `port_owner` null means nobody reserved the port; undefined means not known. */
export function publishWarnings(r: PublishFacts): PublishWarning[] {
  if (r.action !== "publish") return [];
  const out: PublishWarning[] = [];
  if (r.replaces) {
    out.push({ tone: "bad", text: `Replaces the live site: now port ${r.replaces.port ?? "?"}${r.replaces.project ? ` (project ${r.replaces.project})` : ""}, on ${where(r.replaces.access)}.` });
  }
  if (r.port_owner === null) out.push({ tone: "wait", text: `Port ${r.port ?? "?"} isn't reserved for any project.` });
  else if (r.port_owner && r.project && r.port_owner.project !== r.project) {
    out.push({ tone: "wait", text: `Port ${r.port} is reserved for ${r.port_owner.project ? `project ${r.port_owner.project}` : r.port_owner.label ?? "an outside service"}, not ${r.project}.` });
  }
  if (r.access === "public") out.push({ tone: "wait", text: "Anyone on the internet can reach it." });
  return out;
}

/** The plain facts, one line: who asked, for which project, the port's owner and the audience. */
export function publishFactsLine(r: PublishFacts): string {
  const parts: string[] = [];
  if (r.requested_by) parts.push(`Requested by ${r.requested_by}`);
  if (r.project) parts.push(`project ${r.project}`);
  if (r.action === "publish") {
    parts.push(`port ${r.port ?? "?"}${r.port_owner ? ` (reserved for ${r.port_owner.project ? `project ${r.port_owner.project}` : r.port_owner.label ?? "an outside service"})` : ""}`);
    parts.push(where(r.access));
  }
  return parts.join(" · ");
}
