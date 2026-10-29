import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2 } from "lucide-react";
import { api, post } from "../lib/api";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useConfirm } from "./confirm";
import { Button, Card, CardHeader, Dialog, Empty, ErrorNote, Field, Input, QueryState } from "./ui";

interface IssueServer { id: string; base_url: string; addresses?: string[]; custom_ca: boolean }
export const useIssueServers = () => useQuery({ queryKey: ["issue-servers"], queryFn: () => api<IssueServer[]>("/api/issue-servers") });

function ServerDialog({ server, onClose }: { server?: IssueServer; onClose: () => void }) {
  const client = useQueryClient();
  const [base, setBase] = useState(server?.base_url ?? "");
  const [ca, setCa] = useState<string | null>(null);
  const [fileError, setFileError] = useState<Error | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const resolve = useMutation({ mutationFn: () => post<{ base_url: string; addresses: string[] }>("/api/issue-servers/resolve", { base_url: base }),
    onSuccess: () => setConfirmed(false) });
  const save = useMutation({ mutationFn: () => post(server ? `/api/issue-servers/${server.id}/confirm` : "/api/issue-servers", {
    base_url: resolve.data!.base_url, addresses: resolve.data!.addresses, ca_pem: ca,
  }), onSuccess: () => { client.invalidateQueries({ queryKey: ["issue-servers"] }); onClose(); } });
  const busy = resolve.isPending || save.isPending;
  return <Dialog open title={server ? "Re-confirm git server" : "Allow git server"} onClose={onClose} dismissible={false}
    description="Only allow servers you trust. Members can use this server with their own project vault keys. DNS changes stop requests until an owner re-confirms."
    footer={<><Button variant="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
      <Button variant="primary" disabled={busy || !confirmed || !resolve.data || !!fileError} onClick={() => save.mutate()}>
        {save.isPending ? "Saving…" : server ? "Confirm server" : "Allow server"}</Button></>}>
    <div className="space-y-4">
      <Field label="Server HTTPS base URL" hint="Include any installation path prefix.">
        <Input name="server-base" type="url" autoComplete="off" spellCheck={false} value={base} readOnly={!!server} disabled={busy}
          placeholder="https://git.example.lan…" onChange={e => { setBase(e.target.value); resolve.reset(); setConfirmed(false); }} />
      </Field>
      <Field label="Custom CA certificate (PEM, optional)" hint={server?.custom_ca ? "Upload the CA again to retain custom trust, or leave empty to use system trust." : "Trusted only for this server. Uses system trust when empty."}>
        <Input type="file" name="server-ca" accept=".pem,.crt" disabled={busy} onChange={async e => {
          setConfirmed(false); setFileError(null); setCa(null);
          const file = e.target.files?.[0];
          if (!file) return;
          if (file.size > 65536) { setFileError(new Error("CA certificate must be at most 64 KiB.")); return; }
          try { setCa(await file.text()); } catch { setFileError(new Error("Could not read the certificate.")); }
        }} />
      </Field>
      <Button disabled={busy || !base} onClick={() => resolve.mutate()}>{resolve.isPending ? "Resolving…" : "Resolve addresses"}</Button>
      {resolve.data && <div className="space-y-2 text-sm" role="status">
        <p>Expected addresses for {resolve.data.base_url}:</p>
        <ul className="list-inside list-disc font-mono">{resolve.data.addresses.map(ip => <li key={ip}>{ip}</li>)}</ul>
        <label className="flex items-start gap-2"><input type="checkbox" checked={confirmed} disabled={busy}
          onChange={e => setConfirmed(e.target.checked)} />I confirm these addresses belong to this git server</label>
      </div>}
      <ErrorNote error={fileError || resolve.error || save.error} />
    </div>
  </Dialog>;
}

export function IssueServers() {
  const servers = useIssueServers();
  const client = useQueryClient();
  const confirm = useConfirm();
  const [editing, setEditing] = useState<IssueServer | "new" | null>(null);
  const remove = useMutation({ mutationFn: (id: string) => post(`/api/issue-servers/${id}/remove`),
    onSuccess: () => client.invalidateQueries({ queryKey: ["issue-servers"] }) });
  return <Card>
    <CardHeader title="Issue sources" icon={Link2} subtitle="Owner-allowed self-hosted GitLab, Gitea and Forgejo servers"
      action={<Button size="sm" onClick={() => setEditing("new")}>Add git server</Button>} />
    <QueryState query={servers} isEmpty={rows => !rows.length} empty={<Empty>No allowed servers. Public HTTPS sources work without an entry.</Empty>}>
      {rows => <div className="divide-y divide-line">{rows.map(row => <div key={row.id} className="flex flex-wrap items-center gap-2 p-4 text-sm">
        <div className="min-w-0 flex-1 break-words"><p>{row.base_url}</p><p className="text-xs text-muted">{row.addresses?.join(", ")}{row.custom_ca ? " · Custom CA" : " · System CA"}</p></div>
        <Button size="sm" onClick={() => setEditing(row)}>Re-confirm addresses</Button>
        <Button size="sm" variant="ghost" disabled={remove.isPending} onClick={async () => {
          if (await confirm({ title: "Remove allowed server?", body: `Connections using ${row.base_url} will stop working.`, confirm: "Remove", danger: true })) remove.mutate(row.id);
        }}>Remove</Button>
      </div>)}</div>}
    </QueryState>
    <ErrorNote error={remove.error} />
    {editing && <ServerDialog server={editing === "new" ? undefined : editing} onClose={() => setEditing(null)} />}
  </Card>;
}
