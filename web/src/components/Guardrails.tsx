import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Button, Card, CardHeader, ErrorNote, IconButton, Input, Skeleton } from "./ui";

type Custom = { pattern: string; reason: string };
type GuardrailsState = {
  rules: Record<string, boolean>;
  custom: Custom[];
  available: { id: string; label: string; help: string }[];
};

/** Hard limits on agents' tools (bin/acc-guard), whatever their permission mode. Owners edit; everyone can read. */
export function Guardrails({ owner }: { owner: boolean }) {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["guardrails"], queryFn: () => api<GuardrailsState>("/api/guardrails") });
  // The draft and the server snapshot it was started from are kept apart: a refetch (focus, another owner's save)
  // replaces a clean draft, but never one with unsaved edits; those get a notice and a way to reload instead.
  const [base, setBase] = useState<GuardrailsState | null>(null);
  const [rules, setRules] = useState<Record<string, boolean>>({});
  const [custom, setCustom] = useState<Custom[]>([]);
  const kept = (list: Custom[]) => list.filter((c) => c.pattern.trim());
  const dirty = !!base && (JSON.stringify(rules) !== JSON.stringify(base.rules) || JSON.stringify(kept(custom)) !== JSON.stringify(base.custom));
  const adopt = (data: GuardrailsState) => { setBase(data); setRules(data.rules); setCustom(data.custom); };
  const save = useMutation({
    mutationFn: () => api<GuardrailsState>("/api/guardrails", { method: "PUT", json: { rules, custom: kept(custom) } }),
    // editing is frozen while saving, so the draft is exactly what was sent: adopting the response loses nothing
    onSuccess: (data) => { adopt(data); queryClient.setQueryData(["guardrails"], data); },
  });
  useEffect(() => {
    if (query.data && !dirty && !save.isPending) adopt(query.data);
  }, [query.data, dirty, save.isPending]);  // eslint-disable-line react-hooks/exhaustive-deps
  const changedElsewhere = dirty && !!query.data && !!base
    && JSON.stringify({ r: query.data.rules, c: query.data.custom }) !== JSON.stringify({ r: base.rules, c: base.custom });
  const editable = owner && !save.isPending;
  return (
    <Card>
      <CardHeader title="Guardrails" icon={ShieldCheck}
        subtitle="Limits every agent's tools respect, even with permissions skipped." />
      {!query.data || !base ? <div className="p-4">{query.error ? <ErrorNote error={query.error} /> : <Skeleton className="h-32" />}</div> : (
        <div className="space-y-4 p-4">
          <p className="text-xs text-muted">A blocked step is refused with the reason and the session carries on; you get an Inbox note. Changes apply to sessions started afterwards.</p>
          <div className="space-y-2.5">
            {base.available.map((r) => (
              <label key={r.id} className="flex items-start gap-2.5 text-[13px]">
                <input type="checkbox" className="mt-0.5 size-3.5 accent-[var(--accent)]" disabled={!editable}
                  checked={!!rules[r.id]} onChange={(e) => setRules({ ...rules, [r.id]: e.target.checked })} />
                <span><span className="font-medium text-fg">{r.label}</span><span className="block text-xs text-muted">{r.help}</span></span>
              </label>
            ))}
          </div>
          <div>
            <div className="mb-1.5 text-[13px] font-medium">Blocked commands</div>
            <p className="mb-2 text-xs text-muted">Regular expressions matched against every shell command an agent runs, e.g. <code className="font-mono">\bterraform\s+destroy\b</code>.</p>
            <div className="space-y-2">
              {custom.map((c, i) => (
                <div key={i} className="flex flex-wrap items-center gap-2">
                  <Input aria-label="Pattern" placeholder="Pattern" value={c.pattern} disabled={!editable} className="min-w-48 flex-1 font-mono"
                    onChange={(e) => setCustom(custom.map((x, j) => (j === i ? { ...x, pattern: e.target.value } : x)))} />
                  <Input aria-label="Reason shown to the agent" placeholder="Reason shown to the agent" value={c.reason} disabled={!editable} className="min-w-48 flex-1"
                    onChange={(e) => setCustom(custom.map((x, j) => (j === i ? { ...x, reason: e.target.value } : x)))} />
                  {owner && <IconButton icon={Trash2} label="Remove pattern" disabled={!editable} onClick={() => setCustom(custom.filter((_, j) => j !== i))} />}
                </div>
              ))}
              {!custom.length && <p className="text-xs text-faint">None.</p>}
            </div>
          </div>
          {owner && (
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={!editable} onClick={() => setCustom([...custom, { pattern: "", reason: "" }])}><Plus size={14} aria-hidden="true" /> Add pattern</Button>
              <Button size="sm" variant="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save guardrails"}</Button>
              <p role="status" aria-label="Guardrail save status" className={`text-xs ${dirty ? "text-wait" : "text-muted"}`}>
                {save.isPending ? "Saving guardrails…" : dirty ? "Unsaved changes. Select Save guardrails to apply them to new sessions."
                  : save.isSuccess ? "Guardrails saved. New sessions use these rules." : "Changes here require Save guardrails; they do not save automatically."}
              </p>
              {changedElsewhere && (
                <span className="text-xs text-wait" role="status">Changed elsewhere since you started editing.
                  {" "}<button type="button" className="underline" onClick={() => query.data && adopt(query.data)}>Discard yours and reload</button></span>
              )}
              {save.error && <ErrorNote error={save.error} />}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
