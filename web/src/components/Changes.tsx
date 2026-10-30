import { useFeatures } from "./Feature";
import { useSingleMutation as useMutation } from "../lib/mutations";
import clsx from "clsx";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, FileDiff, MessageSquarePlus, Send, X } from "lucide-react";
import { api, post } from "../lib/api";
import { diffHeaderPath } from "../lib/gitpath";
import type { SessionChanges, SessionDetail } from "../lib/types";
import { Button, Card, Empty, ErrorNote, IconButton, Skeleton, Textarea } from "./ui";
import { isLive } from "./session-bits";
import { Explanation, diffFileId, diffLineId, type DiffReference } from "./Explanation";
import { ChangeCount } from "./ChangeCount";
import { type Comment, feedbackMessage } from "../lib/feedback";

type FileDiff = { path: string; lines: string[] };

export function splitDiff(diff: string): FileDiff[] {
  const files: FileDiff[] = [];
  let inHunk = false;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git ")) {
      files.push({ path: diffHeaderPath(line), lines: [] });  // the real name, as in the file list
      inHunk = false;
    } else if (files.length && (inHunk || !/^(index |--- |\+\+\+ |new file mode|deleted file mode|similarity|rename )/.test(line))) {
      if (line.startsWith("@@ ")) inHunk = true;
      files[files.length - 1].lines.push(line);
    }
  }
  return files;
}

/** Each diff line's number in the file: the new file's for added and context lines, the old one's for removed lines. */
function lineNumbers(lines: string[], side?: "old" | "new"): (number | null)[] {
  let old = 0, next = 0;
  return lines.map((line) => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line);
    if (hunk) { old = Number(hunk[1]); next = Number(hunk[2]); return null; }
    if (line.startsWith("-")) { const n = old++; return side === "new" ? null : n; }
    if (line.startsWith("+")) { const n = next++; return side === "old" ? null : n; }
    if (line.startsWith(" ")) { const n = old++; const m = next++; return side === "old" ? n : m; }
    return null;  // "\ No newline at end of file"
  });
}

const MAX_MESSAGE = 4000;

/** The review comments so far, and the button that sends them to the agent as one message. */
function Feedback({ session, comments, setComments }: { session: SessionDetail; comments: Comment[]; setComments: (c: Comment[]) => void }) {
  const message = feedbackMessage(comments);
  const [receipt, setReceipt] = useState<string | null>(null);
  const status = useQuery({ queryKey: ['feedback-delivery', receipt], enabled: !!receipt,
    queryFn: () => api<{ delivery: { state: string } }>(`/api/deliveries/${receipt}`), refetchInterval: 2000 });
  useEffect(() => {
    if (status.data?.delivery.state === 'acknowledged') { setComments([]); setReceipt(null); }
  }, [status.data, setComments]);
  const send = useMutation({
    mutationFn: () => post<{ delivery?: { id: string; state: string } }>(`/api/sessions/${session.id}/message`, { text: message }),
    onSuccess: result => {
      if (!result?.delivery || result.delivery.state === 'acknowledged') setComments([]);
      else setReceipt(result.delivery.id);
    },
  });
  const live = isLive(session);
  return (
    <Card className="space-y-2 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[13px] font-medium">{comments.length} comment{comments.length === 1 ? "" : "s"} for the agent</span>
        {live && (
          <Button variant="primary" size="sm" disabled={send.isPending || !!receipt || message.length > MAX_MESSAGE} onClick={() => send.mutate()}>
            <Send size={13} aria-hidden="true" /> {send.isPending ? "Sending…" : "Send to agent"}
          </Button>
        )}
      </div>
      {receipt && <p role="status" className="text-xs text-muted">Message queued; comments are kept until delivery is confirmed. See Details in the delivery indicator or open Activity for status and retry.</p>}
      <ErrorNote error={status.error} />
      <ul className="space-y-1 text-[12.5px]">
        {comments.map((c, i) => (
          <li key={i} className="flex items-start gap-2">
            <span className="min-w-0 flex-1"><span className="font-mono text-muted">{c.path}:{c.line}</span> {c.text}</span>
            <IconButton icon={X} label="Remove comment" className="size-6" onClick={() => setComments(comments.filter((_, j) => j !== i))} />
          </li>
        ))}
      </ul>
      {message.length > MAX_MESSAGE && <p className="text-xs text-wait">Too long to send in one message ({message.length} of {MAX_MESSAGE} characters); remove some comments.</p>}
      {!live && <p className="text-xs text-faint">The session isn't running; resume it to send these.</p>}
      <ErrorNote error={send.error} />
    </Card>
  );
}

function CommentBox({ onAdd, onCancel }: { onAdd: (text: string) => void; onCancel: () => void }) {
  const [text, setText] = useState("");
  return (
    <div className="space-y-2 whitespace-normal border-y border-line bg-panel px-4 py-2 font-sans" onClick={(e) => e.stopPropagation()}>
      <Textarea value={text} rows={2} maxLength={1000} autoFocus placeholder="What should the agent change here?" className="w-full text-[13px]"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) onAdd(text); if (e.key === "Escape") onCancel(); }} />
      <div className="flex gap-2">
        <Button size="sm" variant="primary" disabled={!text.trim()} onClick={() => onAdd(text)}>Add comment</Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

/** The next line with a number (one that can take a comment) from `at`, in direction `step`. */
const nextLine = (numbers: (number | null)[], path: string, at: number, step: 1 | -1) => {
  for (let i = at + step; i >= 0 && i < numbers.length; i += step) if (numbers[i] !== null) return { path, index: i };
  return null;
};

/** A unified diff line's colours (also the compose review on Projects). */
export const lineClass = (line: string) =>
  line.startsWith("+") ? "bg-ok/10 text-ok" : line.startsWith("-") ? "bg-bad/10 text-bad"
    : line.startsWith("@@") ? "text-accent" : "text-muted";

/** What a session's changes are compared with; Changes and the multi-run comparison say it the same way. */
export function ComparisonBase({ data }: { data: Pick<SessionChanges, "base" | "since_launch" | "comparison_ref"> }) {
  return data.comparison_ref ? <>current branch merge-base with <span className="font-mono">{data.comparison_ref}</span> (<span className="font-mono">{data.base}</span>)</>
    : !data.since_launch ? <>uncommitted changes</>
    : data.base === "empty" ? <>since the session started (the repository had no commits yet)</>
    : <>since <span className="font-mono">{data.base}</span>, where the session started</>;
}

export function Changes({ session }: { session: SessionDetail }) {
  const working = ["active", "waiting_input"].includes(session.observed_state);
  const [comments, setComments] = useState<Comment[]>([]);
  const [draft, setDraft] = useState<{ path: string; index: number } | null>(null);
  // The one line that shows a comment button: the hovered one, or the one picked with the arrow keys (UX review SE-5).
  // One 24px button at a time keeps the code dense and every target large enough.
  const [cursor, setCursor] = useState<{ path: string; index: number } | null>(null);
  const feature = useFeatures();
  const [explain, setExplain] = useState(false);
  const [highlight, setHighlight] = useState<DiffReference | null>(null);
  const showReference = (reference: DiffReference) => {
    setHighlight(reference);
    const file = document.getElementById(diffFileId(reference.path));
    if (file instanceof HTMLDetailsElement) file.open = true;
    requestAnimationFrame(() => {
      const line = document.getElementById(diffLineId(reference.path, reference.side, reference.start));
      line?.scrollIntoView({ block: "center" });
      line?.focus({ preventScroll: true });
    });
  };
  const query = useQuery({
    queryKey: ["changes", session.id], queryFn: () => api<SessionChanges>(`/api/sessions/${session.id}/changes`),
    refetchInterval: working ? 10000 : 60000,  // idle: slowly, for edits made outside a turn
  });
  // The detail is fetched on its own timer, so a turn that ends (or a summary that moves) between two ticks would leave
  // it stale: refetch once a turn finishes, and whenever the session's polled change summary differs.
  const { refetch } = query;
  const wasWorking = useRef(working);
  useEffect(() => {
    if (wasWorking.current && !working) void refetch();
    wasWorking.current = working;
  }, [working, refetch]);
  const summary = session.changes ? `${session.changes.git}:${session.changes.files ?? 0}:${session.changes.additions ?? 0}:${session.changes.deletions ?? 0}` : "";
  const lastSummary = useRef(summary);
  useEffect(() => {
    if (summary !== lastSummary.current) void refetch();
    lastSummary.current = summary;
  }, [summary, refetch]);
  if (query.error) return <ErrorNote error={query.error} />;
  const data = query.data;
  if (!data) return <Skeleton className="h-40" />;
  if (!data.git) return <Empty icon={FileDiff}>This workspace isn't a git repository, so Rynhult can't show its changes.</Empty>;
  if (!data.files) return <Empty icon={FileDiff}>{data.comparison_ref
    ? `No changes against the current branch's merge-base with ${data.comparison_ref}.`
    : data.since_launch ? "No changes since this session started." : "No uncommitted changes."}</Empty>;
  const diffs = new Map(splitDiff(data.diff).map((f) => [f.path, f.lines]));
  return (
    <div className="space-y-3 overflow-y-auto">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted">
        <ChangeCount changes={data} className="text-[13px]" />
        <span><ComparisonBase data={data} /></span>
        {data.truncated && <span className="text-wait">Diff truncated; open the terminal or the repository for the rest.</span>}
        {!comments.length && <span className="inline-flex items-center gap-1 text-faint"><MessageSquarePlus size={13} aria-hidden="true" /> Select a line (or press Enter on it) to comment on it for the agent.</span>}
        <Button hidden={!feature("explanations")} size="sm" aria-expanded={explain} onClick={() => setExplain(!explain)}>Explain these changes</Button>
      </div>
      {comments.length > 0 && <Feedback session={session} comments={comments} setComments={setComments} />}
      <div className={clsx("grid min-w-0 items-start gap-3", explain && "lg:grid-cols-[minmax(0,1fr)_22rem]")}>
      {feature("explanations") && explain && <div className="min-w-0 lg:col-start-2 lg:row-start-1">
        <Explanation key={data.revision} session={session} revision={data.revision} onClose={() => setExplain(false)}
          onReference={showReference} onRefresh={() => { void refetch(); }} />
      </div>}
      <Card className="min-w-0 divide-y divide-line overflow-hidden lg:col-start-1 lg:row-start-1">
        {data.list.map((f) => {
          const lines = diffs.get(f.path) ?? [];
          const numbers = lineNumbers(lines);
          const oldNumbers = lineNumbers(lines, "old");
          const newNumbers = lineNumbers(lines, "new");
          return (
            <details key={f.path} id={diffFileId(f.path)} open={lines.length > 0 && lines.length <= 400} className="group">
              <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-2 text-[13px] hover:bg-panel-2/70">
                <ChevronRight size={14} className="shrink-0 text-faint transition-transform group-open:rotate-90" aria-hidden="true" />
                <span className={clsx("w-4 shrink-0 font-mono text-[11px]", f.status === "A" ? "text-ok" : f.status === "D" ? "text-bad" : "text-wait")}
                  title={{ A: "added", D: "deleted", M: "modified" }[f.status] ?? f.status}>{f.status}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">{f.path}</span>
                {f.binary ? <span className="text-xs text-faint">binary</span>
                  : <span className="tnum text-xs"><span className="text-ok">+{f.additions}</span> <span className="text-bad">−{f.deletions}</span></span>}
              </summary>
              {lines.length > 0 && (
                <pre tabIndex={0} role="region" aria-label={`Changes in ${f.path}. Up and Down pick a line, Enter comments on it.`}
                  className="max-h-[32rem] overflow-auto border-t border-line bg-panel-2/40 py-2 font-mono text-[12px] leading-[1.45]"
                  onFocus={(e) => { if (e.target === e.currentTarget && cursor?.path !== f.path) setCursor(nextLine(numbers, f.path, -1, 1)); }}
                  onKeyDown={(e) => {
                    if (e.target !== e.currentTarget) return;  // typing in a comment box
                    const at = cursor?.path === f.path ? cursor.index : -1;
                    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                      e.preventDefault();
                      const next = nextLine(numbers, f.path, at, e.key === "ArrowDown" ? 1 : -1);
                      if (!next) return;
                      setCursor(next);
                      e.currentTarget.querySelector(`[data-line="${next.index}"]`)?.scrollIntoView({ block: "nearest" });
                    } else if (e.key === "Enter" && at >= 0) {
                      e.preventDefault();
                      setDraft(draft?.path === f.path && draft.index === at ? null : { path: f.path, index: at });
                    }
                  }}>
                  {lines.map((line, i) => {
                    const number = numbers[i];
                    const open = draft?.path === f.path && draft.index === i;
                    const selectedNumber = highlight?.side === "old" ? oldNumbers[i] : newNumbers[i];
                    const selected = highlight?.path === f.path && selectedNumber !== null
                      && selectedNumber >= highlight.start && selectedNumber <= highlight.end;
                    const add = (text: string) => {
                      setComments([...comments, { path: f.path, line: number!, code: line.slice(1).trim(), text }]);
                      setDraft(null);
                    };
                    return (
                      <div key={i} data-explanation-highlight={selected || undefined}
                        className={clsx(selected && "border-l-2 border-accent bg-accent/10")}>
                        {oldNumbers[i] !== null && <span id={diffLineId(f.path, "old", oldNumbers[i]!)} tabIndex={-1} className="scroll-mt-4" />}
                        {newNumbers[i] !== null && <span id={diffLineId(f.path, "new", newNumbers[i]!)} tabIndex={-1} className="scroll-mt-4" />}
                        {number === null
                          ? <div className={clsx("whitespace-pre pl-9 pr-4", lineClass(line))}>{line || " "}</div>
                          : <div data-line={i} className="relative" onMouseEnter={() => setCursor({ path: f.path, index: i })}>
                              <button type="button" tabIndex={-1} aria-label={`Comment on line ${number}: ${line}`} className={clsx("block min-h-6 w-full text-left cursor-pointer whitespace-pre pl-9 pr-4 hover:bg-accent/10", lineClass(line),
                                cursor?.path === f.path && cursor.index === i && "bg-accent/10")}
                                onClick={() => setDraft(open ? null : { path: f.path, index: i })}>{line || " "}</button>
                            </div>}
                        {open && <CommentBox onAdd={add} onCancel={() => setDraft(null)} />}
                      </div>
                    );
                  })}
                </pre>
              )}
            </details>
          );
        })}
      </Card>
      </div>
    </div>
  );
}
