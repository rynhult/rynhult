import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import { Card, CardHeader, ErrorNote, QueryState } from "./ui";

type Preference = { enabled: boolean };

export function RiskLabelsSetting() {
  const client = useQueryClient();
  const [draft, setDraft] = useState<boolean | null>(null);
  const query = useQuery({ queryKey: ["risk-labels"], queryFn: () => api<Preference>("/api/preferences/risk-labels") });
  const save = useMutation({
    mutationFn: (enabled: boolean) => api<Preference>("/api/preferences/risk-labels", { method: "PUT", json: { enabled } }),
    onSuccess: (data) => {
      client.setQueryData(["risk-labels"], data);
      client.invalidateQueries({ queryKey: ["attention"] });
    },
    onSettled: () => setDraft(null),
  });
  return (
    <section aria-label="Advisory risk labels">
      <Card>
        <CardHeader title="Advisory risk labels" subtitle="Static advice, not approval. You still decide what the agent may do." />
        <div className="space-y-2 p-4">
          <QueryState query={query}>{(data) => (
            <label className="flex items-start gap-2.5 text-[13px]">
              <input type="checkbox" name="risk-labels" className="mt-0.5 size-3.5 accent-[var(--accent)]"
                checked={draft ?? data.enabled} disabled={save.isPending}
                onChange={(e) => { setDraft(e.target.checked); save.mutate(e.target.checked); }} />
              <span>Show advisory risk labels
                <span className="block text-xs text-muted">Routine, Review or Risky with a reason in Inbox and Needs you. Applies to your sign-in on every device.</span>
              </span>
            </label>
          )}</QueryState>
          <ErrorNote error={save.error} />
          {save.isSuccess && <p role="status" className="text-xs text-muted">Preference saved.</p>}
        </div>
      </Card>
    </section>
  );
}
