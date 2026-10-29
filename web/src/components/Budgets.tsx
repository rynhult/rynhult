import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Wallet } from "lucide-react";
import { useState } from "react";
import { api, post } from "../lib/api";
import { until } from "../lib/format";
import { Button, Card, CardHeader, ErrorNote, Input, Meter, Select, Skeleton } from "./ui";

export type BudgetRule = { mode: "cap" | "pace"; limit: number; action: "notify" | "pause"; extra?: number; extra_until?: number };
export type BudgetState = {
  windows: { id: string; provider: string; label: string; used: number; resets_at: number; length: number; stale?: boolean }[];
  rules: Record<string, BudgetRule>;
  status: Record<string, { allowed: number; used: number; over: boolean; action: string; stale?: boolean }>;
  extra: number;
};

export const useBudgets = (enabled = true) =>
  useQuery({ queryKey: ["budgets"], queryFn: () => api<BudgetState>("/api/budgets"), enabled, refetchInterval: 60000 });

/** Your own limits on plan usage. None until you set one; nothing is applied for you otherwise. */
export function Budgets() {
  const queryClient = useQueryClient();
  const query = useBudgets();
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<BudgetRule>({ mode: "cap", limit: 80, action: "notify" });
  // The API replaces the whole rules document, so each change is merged into the latest snapshot when it's sent (not
  // the one a row rendered with), and only one budget change runs at a time: every action waits while one is pending.
  const update = useMutation({
    mutationFn: ({ window, rule }: { window: string; rule: BudgetRule | null }) => {
      const { [window]: _old, ...rules } = queryClient.getQueryData<BudgetState>(["budgets"])?.rules ?? {};
      return api<BudgetState>("/api/budgets", { method: "PUT", json: { rules: rule ? { ...rules, [window]: rule } : rules } });
    },
    onSuccess: (data) => { queryClient.setQueryData(["budgets"], data); setEditing(null); queryClient.invalidateQueries({ queryKey: ["attention"] }); },
  });
  const extra = useMutation({
    mutationFn: (window: string) => post<BudgetState>("/api/budgets/extra", { window }),
    onSuccess: (data) => { queryClient.setQueryData(["budgets"], data); queryClient.invalidateQueries({ queryKey: ["attention"] }); },
  });
  const data = query.data;
  const busy = update.isPending || extra.isPending;
  return (
    <Card>
      <CardHeader title="Budgets" icon={Wallet}
        subtitle="Optional limits on your own plan usage. None are set unless you set them." />
      {!data ? <div className="p-4">{query.error ? <ErrorNote error={query.error} /> : <Skeleton className="h-24" />}</div>
        : !data.windows.length ? <p className="p-4 text-[13px] text-muted">No plan usage readings yet. Usage is read from the owner's agent account, so budgets are for owners for now.</p>
        : (
          <div className="divide-y divide-line">
            <p className="px-4 py-3 text-xs text-muted">At a limit Rynhult tells you, or also pauses new launches of that agent (you can still confirm one). Running sessions are never stopped.</p>
            {data.windows.map((w) => {
              const rule = data.rules[w.id];
              const status = data.status[w.id];
              return (
                <div key={w.id} className="space-y-2 px-4 py-3 text-[13px]">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="font-medium">{w.label}</span>
                    <span className="tnum text-xs text-muted">{w.stale ? "Reset: waiting for a new reading" : <>{Math.round(w.used)}% used · resets {until(w.resets_at)}</>}</span>
                  </div>
                  {!w.stale && <Meter value={w.used} label={`${w.label} used`} />}
                  {editing === w.id ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <Select aria-label="Kind" value={draft.mode} onChange={(e) => setDraft({ ...draft, mode: e.target.value as BudgetRule["mode"] })}>
                        <option value="cap">Cap at</option><option value="pace">Pace</option>
                      </Select>
                      <Input aria-label="Percent of the window" type="number" min={1} max={100} className="w-20" value={draft.limit}
                        onChange={(e) => setDraft({ ...draft, limit: Number(e.target.value) })} />
                      <span className="text-xs text-muted">{draft.mode === "pace" ? "% spread evenly over the window" : "% of the window"}</span>
                      <Select aria-label="At the limit" value={draft.action} onChange={(e) => setDraft({ ...draft, action: e.target.value as BudgetRule["action"] })}>
                        <option value="notify">Notify me</option><option value="pause">Notify and pause new launches</option>
                      </Select>
                      <Button size="sm" variant="primary" disabled={busy} onClick={() => update.mutate({ window: w.id, rule: { ...rule, ...draft } })}>Save</Button>
                      <Button size="sm" onClick={() => setEditing(null)}>Cancel</Button>
                    </div>
                  ) : rule ? (
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className={clsx(status?.over ? "text-wait" : "text-muted")}>
                        {rule.mode === "pace" ? `Pace: ${rule.limit}% over the window, ${status?.allowed ?? "–"}% by now` : `Cap: ${rule.limit}%`}
                        {rule.extra_until && rule.extra_until * 1000 > Date.now() ? ` (+${rule.extra}% until it resets)` : ""}
                        {" · "}{rule.action === "pause" ? "notify and pause new launches" : "notify"}
                        {status?.over && " · over"}
                      </span>
                      {status?.over && <Button size="sm" disabled={busy} onClick={() => extra.mutate(w.id)}>+{data.extra}% until it resets</Button>}
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => { setDraft(rule); setEditing(w.id); }}>Edit</Button>
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => update.mutate({ window: w.id, rule: null })}>Remove</Button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 text-xs text-faint">No budget
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => { setDraft({ mode: "cap", limit: 80, action: "notify" }); setEditing(w.id); }}>Set a budget</Button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      {(update.error || extra.error) && <div className="px-4 pb-4"><ErrorNote error={update.error ?? extra.error} /></div>}
    </Card>
  );
}
