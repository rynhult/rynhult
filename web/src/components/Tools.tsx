import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PackageCheck, ShieldCheck } from "lucide-react";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import type { ToolPin, ToolRequest } from "../lib/types";
import { useConfirm } from "./confirm";
import { Badge, Button, Card, CardHeader, Empty, ErrorNote, QueryState } from "./ui";

export function ToolActions({ request }: { request: ToolRequest }) {
  const cache = useQueryClient();
  const decide = useMutation({
    mutationFn: (action: "approve" | "reject") => post(`/api/tools/requests/${request.id}/${action}`),
    onSuccess: () => {
      cache.invalidateQueries({ queryKey: ["attention"] });
      cache.invalidateQueries({ queryKey: ["tools"] });
    },
  });
  return <div className="space-y-2">
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="primary" disabled={decide.isPending} onClick={() => decide.mutate("approve")}>Approve</Button>
      <Button size="sm" disabled={decide.isPending} onClick={() => decide.mutate("reject")}>Reject</Button>
    </div>
    <ErrorNote error={decide.error} />
  </div>;
}

export function ToolsCard({ machine, account }: { machine?: string; account?: string }) {
  const cache = useQueryClient();
  const confirm = useConfirm();
  const query = useQuery({ queryKey: ["tools"], queryFn: () => api<{ tools: ToolPin[]; requests: ToolRequest[] }>("/api/tools"),
    refetchInterval: 3000 });
  const remove = useMutation({
    mutationFn: (id: string) => post(`/api/tools/${id}/remove`),
    onSuccess: () => {
      cache.invalidateQueries({ queryKey: ["tools"] });
      cache.invalidateQueries({ queryKey: ["attention"] });
    },
  });
  const here = (t: ToolPin) => t.host === (machine ?? "local") &&
    (!account || t.destination === "system-wide" || t.account === account);
  return <section aria-label="Tools">
    <Card>
      <CardHeader title="Tools" icon={PackageCheck}
        subtitle="Account and machine tools installed through Rynhult. Versions are pinned from the manager's result; existing tools need no approval to run." />
      <QueryState query={query}>
        {(data) => {
          const tools = data.tools.filter(here);
          const requests = data.requests.filter(here);
          return <>
            {!tools.length && <Empty icon={PackageCheck}>No tools installed through Rynhult on this machine and account.</Empty>}
            <ul className="divide-y divide-line">
              {tools.map((t) => {
                const pending = requests.some((r) => r.name === t.name && r.manager === t.manager &&
                  (!t.account || r.account === t.account) && ["pending_approval", "approved", "applying"].includes(r.status));
                return <li key={t.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                  <div className="min-w-0 flex-1 break-words">
                    <span className="font-medium">{t.name}</span> <Badge>{t.version}</Badge>
                    <p className="mt-1 text-xs text-muted">{t.manager} · {t.host} · {t.destination}</p>
                  </div>
                  <Button size="sm" aria-label={pending ? `Change waiting for ${t.name}` : `Remove ${t.name}`} disabled={remove.isPending || pending} onClick={async () => {
                    if (await confirm({ title: `Remove ${t.name}?`, body: `Request removal from ${t.host}, ${t.destination}. Nothing changes until the Inbox request is approved.`,
                      confirm: "Request removal", danger: true })) remove.mutate(t.id);
                  }}>{pending ? "Change waiting" : "Remove"}</Button>
                </li>;
              })}
            </ul>
            {requests.length > 0 && <details className="border-t border-line px-4 py-3">
              <summary className="cursor-pointer text-xs text-muted">Request history ({requests.length})</summary>
              <ul className="mt-2 space-y-2">
                {requests.map((r) => <li key={r.id} className="break-words text-xs text-muted">
                  {r.operation} {r.name} <Badge tone={r.status === "failed" ? "bad" : "neutral"}>{r.status.replaceAll("_", " ")}</Badge>
                  {" "}{r.resolved_version ?? r.version ?? "latest"} · {ago(r.created_at)}
                  {r.detail && <p>{r.detail}</p>}
                </li>)}
              </ul>
            </details>}
          </>;
        }}
      </QueryState>
      <div className="px-4 pb-3" aria-live="polite"><ErrorNote error={remove.error} /></div>
    </Card>
  </section>;
}

type ScopedPolicy = { target: string; label: string; required: boolean };
export function ToolPolicies() {
  const cache = useQueryClient();
  const confirm = useConfirm();
  const query = useQuery({ queryKey: ["tool-policy"], queryFn: () =>
    api<{ people: ScopedPolicy[]; projects: ScopedPolicy[] }>("/api/tools/policy") });
  const change = useMutation({
    mutationFn: (value: { scope: string; target: string; required: boolean }) =>
      api("/api/tools/policy", { method: "PUT", json: value }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["tool-policy"] }),
  });
  return <Card>
    <CardHeader title="Tool approvals" icon={ShieldCheck}
      subtitle="On by default. A person or project exemption lets tool installs run directly in new sessions; other guardrails stay on." />
    <div className="space-y-3 p-4">
      <QueryState query={query}>
        {(data) => (["people", "projects"] as const).map((kind) => <fieldset key={kind}>
          <legend className="mb-2 text-xs font-medium">{kind === "people" ? "People" : "Projects"}</legend>
          <div className="space-y-2">{data[kind].map((p) => <label key={p.target} className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={p.required} disabled={change.isPending} name={`${kind}-${p.target}`}
              onChange={async (e) => {
                const required = e.target.checked;
                if (required || await confirm({ title: `Turn off tool approval for ${p.label}?`,
                  body: "New sessions can install and change tools directly. Existing sessions keep their launch policy. This does not grant system privileges.",
                  confirm: "Turn off approval", danger: true }))
                  change.mutate({ scope: kind === "people" ? "person" : "project", target: p.target, required });
              }} />
            Require tool approval for {p.label}
          </label>)}</div>
        </fieldset>)}
      </QueryState>
      <div aria-live="polite"><ErrorNote error={change.error} /></div>
    </div>
  </Card>;
}
