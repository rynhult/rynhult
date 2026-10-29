import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { OFFLINE_TITLE, useOffline } from "../lib/queries";
import { api, post } from "../lib/api";
import { useConfirm } from "./confirm";
import { announce, Button, Card, CardHeader, ErrorNote, Field, Input, QueryState, Select } from "./ui";

export interface PublishingAddress {
  id: string; label: string; address: string; access: "public" | "internal"; default: boolean; source: string;
}
interface Detection { suggestions: { address: string; source: string; access: "public" | "internal" }[]; agreement: boolean; failures: string[] }

export function PublishingAddresses() {
  const client = useQueryClient();
  const offline = useOffline();
  const confirm = useConfirm();
  const entries = useQuery({ queryKey: ["publish-addresses"], queryFn: () => api<PublishingAddress[]>("/api/publish/addresses") });
  const [address, setAddress] = useState("");
  const [label, setLabel] = useState("");
  const [access, setAccess] = useState<"public" | "internal">("public");
  useEffect(() => {
    if (!address && !label) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [address, label]);
  const edit = useMutation({ mutationFn: ({ path, body }: { path: string; body?: unknown }) => post(path, body),
    onSuccess: (_data, variables) => { if (variables.body) { setAddress(""); setLabel(""); } announce("Publishing addresses updated."); client.invalidateQueries({ queryKey: ["publish-addresses"] }); client.invalidateQueries({ queryKey: ["publish-zones"] }); } });
  const detect = useMutation({ mutationFn: () => post<Detection>("/api/publish/addresses/detect"), onSuccess: () => announce("Address detection finished. Review the suggestions; nothing has been saved.") });
  return <section aria-label="Publishing addresses"><Card>
    <CardHeader title="Publishing" subtitle="DNS targets for published sites. Configure firewall routing separately." />
    <div className="space-y-4 p-4 text-[13px]">
      <QueryState query={entries} errorTitle="Couldn't read publishing addresses.">{(rows) => <>
        {(["public", "internal"] as const).map(kind => <div key={kind} className="space-y-2">
          <h3 className="font-medium">{kind === "public" ? "Public addresses" : "Internal addresses"}</h3>
          {!rows.some(r => r.access === kind) && <p className="text-muted">No {kind} addresses yet.</p>}
          {rows.filter(r => r.access === kind).map(row => <div key={row.id} className="flex flex-wrap items-center gap-2 rounded-md border border-line p-3 [content-visibility:auto]">
            <div className="min-w-0 flex-1 break-words"><b>{row.label}</b> · <span className="font-mono">{row.address}</span>
              <p className="text-xs text-muted">{row.default ? "Default · " : ""}{row.source === "environment" ? "From environment (bootstrap)" : "Added by owner"}</p></div>
            {!row.default && <Button disabled={edit.isPending || offline} title={offline ? OFFLINE_TITLE : undefined} onClick={() => edit.mutate({ path: `/api/publish/addresses/${row.id}/default` })}>Make default</Button>}
            <Button disabled={edit.isPending || offline} title={offline ? OFFLINE_TITLE : undefined} aria-label={`Remove ${row.label}`} onClick={async () => {
              if (await confirm({ title: "Remove address?", body: `Remove ${row.label} (${row.address})? Move any published sites first.`, confirm: "Remove address", danger: true }))
                edit.mutate({ path: `/api/publish/addresses/${row.id}/remove` });
            }}>Remove</Button>
          </div>)}
        </div>)}
      </>}</QueryState>
      <Button disabled={detect.isPending || offline} title={offline ? OFFLINE_TITLE : undefined} onClick={() => detect.mutate()}>{detect.isPending ? "Detecting…" : "Detect"}</Button>
      <div className="space-y-2">
        {detect.data && <><p className="text-muted">{detect.data.agreement ? "Public-IP services agree." : "Services disagree or could not all be reached. Review each suggestion."} Nothing is saved until you add an address.</p>
          {detect.data.suggestions.map((s, i) => <div key={i} className="flex flex-wrap items-center gap-2"><span className="break-all">{s.address} · {s.source}</span><Button onClick={() => { setAddress(s.address); setAccess(s.access); }}>Use {s.address}</Button></div>)}
          {!!detect.data.failures.length && <p className="text-muted">Unavailable: {detect.data.failures.join(", ")}</p>}</>}
      </div>
      <form className="space-y-3" onSubmit={e => { e.preventDefault(); if (offline) return; edit.mutate({ path: "/api/publish/addresses", body: { address, label, access } }); }}>
        <div className="grid gap-3 md:grid-cols-3">
          <Field label="Address label"><Input name="address-label" autoComplete="off" required maxLength={80} value={label} onChange={e => setLabel(e.target.value)} /></Field>
          <Field label="Address or CIDR"><Input name="address" autoComplete="off" spellCheck={false} required value={address} onChange={e => setAddress(e.target.value)} /></Field>
          <Field label="Address access"><Select value={access} onChange={e => setAccess(e.target.value as typeof access)}><option value="public">Public</option><option value="internal">Internal</option></Select></Field>
        </div>
        <p className="text-xs text-muted">IPv4 blocks: /16 or smaller. IPv6 blocks: /48 or smaller. Publishing picks one host address from a block.</p>
        <Button type="submit" disabled={edit.isPending || offline} title={offline ? OFFLINE_TITLE : undefined}>Add address</Button>
      </form>
      <ErrorNote error={edit.error ?? detect.error} />
    </div>
  </Card></section>;
}
