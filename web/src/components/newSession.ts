import { createContext, useContext } from "react";
import type { IssueRef } from "../lib/types";

/** Opens the New session dialog from anywhere, optionally preset (e.g. a new session in an existing workspace,
 *  or one in a project's own worktree for a GitHub issue). People still choose the agent and press Launch. */
export interface NewSessionPreset {
  mode?: "project" | "workspace"; workspace?: string; profile?: string;
  project_id?: number; name?: string; worktree?: boolean; initial_prompt?: string;
  issue_source?: IssueRef;
}
export const NewSessionContext = createContext<(preset?: NewSessionPreset) => void>(() => {});
export const useNewSession = () => useContext(NewSessionContext);

/** How a launch runs (one Run choice in Advanced; owner decision 0008, Q5). */
export type RunMode = "once" | "several" | "helpers" | "schedule";
export const RUN_MODES: { id: RunMode; label: string; detail: string }[] = [
  { id: "once", label: "Once", detail: "One agent works on the task." },
  { id: "several", label: "Several agents", detail: "The same task, run independently by 2 to 5 agents; compare and keep the best." },
  { id: "helpers", label: "With helpers", detail: "This session may start a team of helper agents within limits you set." },
  { id: "schedule", label: "On a schedule", detail: "Saved, and started at the times you choose." },
];

/** A session name from the task's first words ("Fix the login form's error" → "fix-the-login-form"), else `fallback`. */
export function suggestedName(task: string, fallback = "session"): string {
  const words = task.toLowerCase().replace(/[^a-z0-9\s-]/g, " ").split(/\s+/).filter(Boolean).slice(0, 4);
  return words.join("-").slice(0, 40).replace(/-+$/, "") || fallback;
}

export interface SummaryInput {
  run: RunMode; runs?: number; schedule?: string; workspace?: boolean;
  model?: string; effort?: string; options?: string[]; worktree?: boolean;
  skills?: number; agents?: number; mcps?: number; goal?: boolean; autoresume?: boolean; name?: string;
}

/** The one line under Advanced: every choice made there, so a closed Advanced still says what will happen. */
export function advancedSummary(x: SummaryInput): string {
  const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;
  const run = x.run === "several" ? `${x.runs ?? 2} agents, each in its own worktree` : x.run === "helpers" ? "with helpers"
    : x.run === "schedule" ? `on a schedule${x.schedule ? ` (${x.schedule})` : ""}` : "once";
  const parts = [x.name ? `${x.name}, ${run}` : run[0].toUpperCase() + run.slice(1)];
  if (x.run !== "several") {
    parts.push(`${x.model || "default model"}${x.effort ? `, ${x.effort} effort` : ""}`);
    parts.push(...(x.options ?? []));
    if (!x.workspace) parts.push(x.worktree || x.run === "helpers" ? "own worktree" : "shared checkout");
    const picks = [x.skills ? plural(x.skills, "skill") : "", x.agents ? plural(x.agents, "agent") : "", x.mcps ? `${x.mcps} MCP` : ""].filter(Boolean);
    parts.push(picks.length ? picks.join(", ") : "no skills");
    if (x.goal) parts.push("goal set");
    if (x.autoresume) parts.push("resumes after limits");
  }
  return parts.join(" · ");
}
