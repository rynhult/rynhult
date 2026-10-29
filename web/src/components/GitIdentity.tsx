import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { api } from "../lib/api";
import { Button, Card, CardHeader, ErrorNote, Field, Input } from "./ui";

type Identity = { supported: boolean; name: string | null; email: string | null };

export function GitIdentityCard({ projectId }: { projectId: number }) {
  const client = useQueryClient();
  const url = `/api/projects/${projectId}/git-identity`;
  const query = useQuery({ queryKey: ["git-identity", projectId], queryFn: () => api<Identity>(url) });
  const [draft, setDraft] = useState<{ name: string; email: string } | null>(null);
  const save = useMutation({ mutationFn: () => api<Identity>(url, { method: "PUT", json: draft }),
    onSuccess: data => { client.setQueryData(["git-identity", projectId], data); setDraft(null); } });
  return <Card>
    <CardHeader title="Git author" subtitle="Used for commits in this repository and its session worktrees. Your account and machine Git settings stay unchanged." />
    <div className="space-y-3 p-4">
      <ErrorNote error={query.error ?? save.error} onRetry={query.refetch} />
      {query.isPending ? <p role="status" className="text-sm text-muted">Reading Git author…</p> : query.data && (
        query.data.supported ? <form className="space-y-3" onSubmit={event => { event.preventDefault(); save.mutate(); }}>
          <Field label="Git author name"><Input autoComplete="off" maxLength={200} required
            value={draft?.name ?? query.data.name ?? ""} onChange={event => setDraft({ name: event.target.value, email: draft?.email ?? query.data?.email ?? "" })} /></Field>
          <Field label="Git author email"><Input type="email" autoComplete="off" maxLength={200} required
            value={draft?.email ?? query.data.email ?? ""} onChange={event => setDraft({ name: draft?.name ?? query.data?.name ?? "", email: event.target.value })} /></Field>
          <p className="text-xs text-muted">If Git has no author configured, Rynhult uses the project owner's displayed username and a generated noreply address. Changing these fields saves an override for this repository.</p>
          <Button type="submit" disabled={!draft || save.isPending}>{save.isPending ? "Saving…" : "Save Git author"}</Button>
          {!draft && save.isSuccess && <p role="status" className="text-sm text-ok">Git author saved.</p>}
        </form> : <p className="text-sm text-muted">This folder does not have a Git repository yet.</p>
      )}
    </div>
  </Card>;
}
