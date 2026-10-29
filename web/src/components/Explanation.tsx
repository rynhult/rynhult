import { useSingleMutation as useMutation } from "../lib/mutations";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { X } from "lucide-react";
import { api, post } from "../lib/api";
import type { SessionDetail } from "../lib/types";
import { Button, Card, ErrorNote, Field, IconButton, Input, Select, Skeleton } from "./ui";

export type DiffReference = { path: string; side: "old" | "new"; start: number; end: number };
type Walkthrough = {
  status: "missing" | "running" | "ready" | "error"; revision: string; helper_id?: string;
  provider?: string; model?: string; created_at?: string; error?: string;
  input_summarised?: boolean; diff_truncated?: boolean;
  steps: { title: string; summary: string; references: DiffReference[] }[];
};

export const diffLineId = (path: string, side: string, line: number) =>
  `diff-line-${encodeURIComponent(path)}-${side}-${line}`;
export const diffFileId = (path: string) => `diff-file-${encodeURIComponent(path)}`;

export function Explanation({ session, revision, onClose, onReference, onRefresh }: {
  session: SessionDetail; revision: string; onClose: () => void;
  onReference: (reference: DiffReference) => void; onRefresh: () => void;
}) {
  const client = useQueryClient();
  const key = ["explanation", session.id, revision];
  const [provider, setProvider] = useState(session.provider as string);
  const [model, setModel] = useState(session.model ?? (session.provider === "claude" ? "sonnet" : ""));
  const [regenerate, setRegenerate] = useState(false);
  const [step, setStep] = useState(0);
  const query = useQuery({
    queryKey: key, queryFn: () => api<Walkthrough>(`/api/sessions/${session.id}/explanation`),
    refetchInterval: q => q.state.data?.status === "running" ? 1000 : false,
  });
  const agents = useQuery({
    queryKey: ["explanation-agents", session.id],
    queryFn: () => api<{ agents: { id: string; label: string }[] }>(`/api/sessions/${session.id}/explanation/agents`),
  });
  const choices = agents.data?.agents ?? [];
  const chosen = choices.some(a => a.id === provider) ? provider : "";
  const launch = useMutation({
    mutationFn: () => post<Walkthrough>(`/api/sessions/${session.id}/explanation`, {
      provider: chosen, model: model.trim(), regenerate, revision,
    }),
    onSuccess: value => { client.setQueryData(key, value); setRegenerate(false); setStep(0); },
  });
  const data = query.data;
  const stale = data && data.revision !== revision;
  const current = data?.steps[Math.min(step, data.steps.length - 1)];
  const changeStep = (index: number) => {
    setStep(index);
    const ref = data?.steps[index]?.references[0];
    if (ref) onReference(ref);
  };
  return (
    <aside aria-label="Change walkthrough" className="min-w-0 lg:sticky lg:top-0 lg:self-start">
      <Card className="space-y-4 p-4">
        <div className="flex items-center justify-between gap-2">
          <h2 className="font-medium">Change walkthrough</h2>
          <IconButton icon={X} label="Close walkthrough" onClick={onClose} />
        </div>
        <div aria-live="polite" className="space-y-3">
          <ErrorNote error={query.error ?? launch.error ?? agents.error} />
          {!data && !query.error && <Skeleton className="h-24" />}
          {stale ? <div className="space-y-2 text-sm text-wait">
            <p>The diff changed. Refresh Changes before using this walkthrough.</p>
            <Button size="sm" onClick={onRefresh}>Refresh Changes</Button>
          </div> : data && <>
            {data.helper_id && <p className="break-words text-xs text-muted">
              {data.provider} · {data.model} · {data.created_at && new Date(data.created_at).toLocaleString()}
              {" · "}<Link className="text-accent underline hover:decoration-2" to={`/sessions/${data.helper_id}`}>Helper session</Link>
            </p>}
            {(data.input_summarised || data.diff_truncated) && <p className="text-xs text-wait">
              {data.input_summarised ? "Large diff: generated from per-file summaries and excerpts. " : ""}
              {data.diff_truncated ? "The source diff was truncated; this walkthrough is incomplete." : ""}
            </p>}
            {data.status === "running" && <p className="text-sm text-muted">Generating walkthrough… The helper stops when done (up to 3 minutes).</p>}
            {data.status === "error" && <ErrorNote error={new Error(data.error ?? "Couldn't write the walkthrough. Select Regenerate to try again.")} />}
            {data.status === "ready" && !regenerate && current && <>
              <p className="text-xs text-faint">generated; check the diff</p>
              <p className="tnum text-xs text-muted">Step {step + 1} of {data.steps.length}</p>
              <h3 className="break-words font-medium">{current.title}</h3>
              <p className="whitespace-pre-wrap break-words text-sm">{current.summary}</p>
              <div className="flex flex-col items-start gap-2">
                {current.references.map((ref, i) => <a key={i}
                  href={`#${encodeURIComponent(diffLineId(ref.path, ref.side, ref.start))}`}
                  onClick={() => onReference(ref)} className="max-w-full break-all text-xs text-accent hover:underline">
                  {ref.path}:{ref.start}–{ref.end} ({ref.side})
                </a>)}
              </div>
              <nav aria-label="Walkthrough steps" className="flex flex-wrap gap-2">
                <Button size="sm" disabled={step === 0} onClick={() => changeStep(step - 1)}>Previous step</Button>
                <Button size="sm" disabled={step >= data.steps.length - 1} onClick={() => changeStep(step + 1)}>Next step</Button>
              </nav>
              <Button size="sm" variant="ghost" onClick={() => { setRegenerate(true); launch.reset(); }}>Regenerate</Button>
            </>}
            {(data.status === "missing" || data.status === "error" || regenerate) && <form
              className="space-y-3" onSubmit={e => { e.preventDefault(); launch.mutate(); }}>
              <p className="text-xs text-muted">Runs one read-only helper as your signed-in agent account. Uses your plan and launch budgets. No edits or network tools.</p>
              <Field label="Explanation agent">
                <Select value={chosen} onChange={e => {
                  setProvider(e.target.value);
                  setModel(e.target.value === session.provider ? session.model ?? (e.target.value === "claude" ? "sonnet" : "")
                    : e.target.value === "claude" ? "sonnet" : "");
                }} disabled={!choices.length || launch.isPending}>
                  {!chosen && <option value="" disabled>{choices.length ? "Choose a signed-in CLI" : "No signed-in CLIs available"}</option>}
                  {choices.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}
                </Select>
              </Field>
              <Field label="Explanation model" hint="Choose a model available to this CLI account.">
                <Input name="explanation-model" autoComplete="off" value={model} maxLength={160}
                  onChange={e => setModel(e.target.value)} placeholder="Model ID…" required />
              </Field>
              <Button type="submit" size="sm" variant="primary" disabled={!chosen || !model.trim() || launch.isPending}>
                {launch.isPending ? "Launching…" : "Generate walkthrough"}
              </Button>
            </form>}
          </>}
        </div>
      </Card>
    </aside>
  );
}
