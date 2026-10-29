import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { ago } from "../lib/format";
import { BookMarked } from "lucide-react";
import { Card, CardHeader, Dialog, Empty, LoadingRows, QueryState } from "./ui";

export interface DocEntry { path: string; bytes: number; modified: number; commit: string | null; committed_at: string | null; subject: string | null }
/** `folder`: for a session, where its docs were read (its worktree, its local workspace, or the project's checkout). */
export interface KnowledgeView { docs: Record<string, DocEntry[]>; repo: string | null; folder?: "worktree" | "workspace" | "project" }

const LABELS: Record<string, string> = { research: "Research", council: "Council reports", decisions: "Decisions", handoffs: "Handoffs", design: "Designs" };

/** Knowledge lives in a project's checkout, or in a local workspace's own folder (session-scoped). */
export type KnowledgeSource = { projectId: number } | { sessionId: string };
const base = (source: KnowledgeSource) => ("sessionId" in source ? `/api/sessions/${source.sessionId}` : `/api/projects/${source.projectId}`);

export function DocViewer({ projectId, source, path, onClose }: { projectId?: number; source?: KnowledgeSource; path: string | null; onClose: () => void }) {
  const from = source ?? { projectId: projectId! };
  const doc = useQuery({ queryKey: ["doc", base(from), path], queryFn: () => api<{ content: string }>(`${base(from)}/knowledge/doc?path=${encodeURIComponent(path!)}`), enabled: !!path });
  return (
    <Dialog open={!!path} onClose={onClose} title={path ?? ""} wide>
      <QueryState query={doc} loading={<LoadingRows rows={4} />} errorTitle="Couldn't open this note.">{(data) => (
        <pre tabIndex={0} aria-label={path ?? undefined} className="max-h-[70vh] overflow-auto whitespace-pre-wrap break-words rounded-md font-mono text-[12.5px] leading-relaxed text-fg">{data.content}</pre>
      )}</QueryState>
    </Dialog>
  );
}

/** A session's knowledge is always read from its own folder (its worktree, if it has one); a project's from its checkout. */
export function useKnowledge(projectId: number | undefined, sessionId?: string) {
  const source: KnowledgeSource | null = sessionId ? { sessionId } : projectId ? { projectId } : null;
  return useQuery({ queryKey: ["knowledge", source ? base(source) : null], queryFn: () => api<KnowledgeView>(`${base(source!)}/knowledge`),
    enabled: !!source, refetchInterval: 30000 });
}

export function DocList({ entries, onOpen }: { entries: DocEntry[]; onOpen: (path: string) => void }) {
  return (
    <div className="divide-y divide-line">
      {entries.map((d) => (
        <button key={d.path} onClick={() => onOpen(d.path)} className="flex w-full items-start justify-between gap-3 px-4 py-2.5 text-left text-[13px] hover:bg-panel-2/70">
          <span className="min-w-0"><span className="block truncate font-medium text-fg">{d.path.split("/").pop()}</span>
            <span className="block truncate text-xs text-muted">{d.commit ? `${d.commit} · ${d.subject}` : "not committed yet"}</span></span>
          <span className="shrink-0 text-xs text-faint">{ago(d.committed_at ?? new Date(d.modified * 1000).toISOString())}</span>
        </button>
      ))}
    </div>
  );
}

export function KnowledgePanel({ projectId, sessionId, title = "Knowledge" }: { projectId: number; sessionId?: string; title?: string }) {
  const knowledge = useKnowledge(projectId, sessionId);
  const source: KnowledgeSource = sessionId ? { sessionId } : { projectId };
  const [open, setOpen] = useState<string | null>(null);
  const docs = knowledge.data?.docs ?? {};
  const kinds = Object.keys(LABELS).filter((k) => docs[k]?.length);
  return (
    <Card>
      <CardHeader title={title} icon={BookMarked} subtitle={sessionId ? "Reports, council findings and notes in this workspace's folder" : "Handoffs, decisions and reports in the repo; Rynhult only indexes them"} />
      <QueryState query={knowledge} loading={<LoadingRows rows={2} />} isEmpty={() => !kinds.length} errorTitle="Couldn't read the notes folder."
        empty={<Empty icon={BookMarked}>No handoffs, decisions, council, research or design notes yet. Sessions write them under docs/ as they work.</Empty>}>
        {() => kinds.map((kind) => (
          <div key={kind} className="border-t border-line first:border-t-0">
            <div className="px-4 pt-3 text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">{LABELS[kind]} · {docs[kind].length}</div>
            <DocList entries={docs[kind].slice(0, 8)} onOpen={setOpen} />
          </div>
        ))}
      </QueryState>
      <DocViewer source={source} path={open} onClose={() => setOpen(null)} />
    </Card>
  );
}
