import { useSingleMutation as useMutation } from "../lib/mutations";
import clsx from "clsx";
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Globe, ListChecks, Lock } from "lucide-react";
import { useConfirm } from "../components/confirm";
import { PublishActions, PublishSummary } from "../components/PublishRequest";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import type { PublishingAddress } from "../components/PublishingAddresses";
import type { PortEntry } from "../components/Ports";
import type { AuthState, PublishRequest } from "../lib/types";
import { Badge, Button, Card, CardHeader, Empty, ErrorNote, Field, Input, Page, PageHeader, QueryState, Select, Tabs, TabPanel } from "../components/ui";
import { useInstance, useProjects } from "../lib/queries";

const TONE: Record<PublishRequest["status"], "neutral" | "accent" | "ok" | "wait" | "bad"> = {
  pending_approval: "wait", approved: "accent", applying: "accent", applied: "neutral", failed: "bad", rejected: "neutral", removed: "neutral",
};

function PublishForm({ live }: { live: PublishRequest[] }) {
  const queryClient = useQueryClient();
  const { wildcard_zone } = useInstance();
  const zones = useQuery({ queryKey: ["publish-zones"], queryFn: () => api<{ zones: { id: string; name: string }[]; configured?: boolean; public_ip: string; internal_ip: string }>("/api/publish/zones") });
  const auth = useQuery({ queryKey: ["auth"], queryFn: () => api<AuthState>("/api/auth/state") });
  const addresses = useQuery({ queryKey: ["publish-addresses"], queryFn: () => api<PublishingAddress[]>("/api/publish/addresses"), enabled: auth.data?.role === "owner" });
  const projects = useProjects();
  const ports = useQuery({ queryKey: ["ports"], queryFn: () => api<PortEntry[]>("/api/ports") });
  const [project, setProject] = useState("");
  const [target, setTarget] = useState("");
  const [picked, setPicked] = useState("");
  const [name, setName] = useState("");
  const [zone, setZone] = useState("");
  const [port, setPort] = useState("");
  const [access, setAccess] = useState<"internal" | "public">("internal");
  const reservations = (ports.data ?? []).filter(p => String(p.project_id) === project);
  const validPort = Number(port) >= 1 && Number(port) <= 65535
    && (!project || (!ports.isError && reservations.some(p => p.port === Number(port))));
  const selected = addresses.data?.find(r => r.id === target && r.access === access);
  const targetIp = selected ? (selected.address.includes("/") ? picked : selected.address) : undefined;
  useEffect(() => {
    const names = zones.data?.zones.map((z) => z.name) ?? [];
    if (names.length && !names.includes(zone)) setZone(names.includes(wildcard_zone) ? wildcard_zone : names[0]);
  }, [zones.data, zone]);
  const host = name ? `${name.trim().toLowerCase()}.${zone}` : "";
  // The live rule may be on a different Current page. Check this exact name before offering replacement.
  const lookup = useQuery({queryKey:["publish", "current-host", host],
    queryFn:() => api<PublishRequest[]>(`/api/publish?view=current&host=${encodeURIComponent(host)}&limit=100`),
    enabled:!!name.trim() && !!zone, refetchInterval:3000});
  const current = (lookup.data ?? live).find(r => r.host === host && r.status === "applied" && r.action === "publish");
  const submit = useMutation({
    mutationFn: (replace: boolean) => post<PublishRequest>("/api/publish", { host, port: Number(port), access, replace, target_ip: targetIp }),
    onSuccess: () => { setName(""); setPort(""); queryClient.invalidateQueries({ queryKey: ["publish"] }); },
  });
  const confirm = useConfirm();
  const go = async () => {
    if (current && !(await confirm({ title: `Replace the live site ${host}?`, danger: true, confirm: "Replace the live site",
      body: `${host} serves port ${current.port}${current.port_owner?.project ? ` (project ${current.port_owner.project})` : ""} now${current.target_ip ? ` at ${current.target_ip}` : ""}. Publishing points it at port ${port}, DNS target ${targetIp || (access === "public" ? zones.data?.public_ip : zones.data?.internal_ip) || "default address"}.` }))) return;
    if (access === "public" && !(await confirm({ title: `Publish ${host} publicly?`, body: `Anyone on the internet can reach port ${port} through Caddy. Choose "Private networks only" unless that is what you want.`, confirm: "Publish publicly", danger: true }))) return;
    submit.mutate(!!current);
  };
  if (zones.data?.configured === false) return (
    <Card>
      <CardHeader title="Publish a site" icon={Globe} />
      <Empty icon={Globe} action={<Link to="/guide/User-Guide/Publishing#before-you-start" className="inline-flex h-8 pointer-coarse:h-11 items-center rounded-md border border-line-strong px-3 text-[13px] text-fg hover:bg-panel-2">How to set one up</Link>}>
        Publishing needs a Cloudflare zone, and this Rynhult was installed without one. Everything else works without it.
      </Empty>
    </Card>
  );
  return (
    <Card>
      <CardHeader title="Publish a site" icon={Globe} subtitle="A Caddy site plus a DNS-only Cloudflare record" />
      <div className="grid gap-4 px-4 pt-4 sm:grid-cols-2">
        <Field label="Project">
          <Select value={project} onChange={e => { setProject(e.target.value); setPort(""); }}>
            <option value="">Choose a project…</option>
            {(projects.data ?? []).filter(p => !p.archived).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
        </Field>
        <Field label="Reserved port">
          <Select value={project ? port : ""} disabled={!project || ports.isError || ports.isPending} onChange={e => setPort(e.target.value)}>
            <option value="">{ports.isPending ? "Loading ports…" : !project ? "Choose a project first" : reservations.length ? "Choose a reserved port…" : "No ports reserved for this project"}</option>
            {reservations.map(p => <option key={p.port} value={p.port}>{p.port} · {p.range === "dev" ? "Development" : p.range === "prod" ? "Production" : "Other"}</option>)}
          </Select>
        </Field>
        <ErrorNote error={projects.error} title="Couldn't list projects." onRetry={projects.refetch} />
        <ErrorNote error={ports.error} title="Couldn't list reserved ports." onRetry={ports.refetch} />
      </div>
      {project && !ports.isPending && !ports.isError && !reservations.length && <p className="px-4 pt-2 text-xs text-muted">Reserve a port under <Link to={`/projects/${project}`} className="text-accent underline">Project → Ports</Link> first. Reserving a port does not start the app.</p>}
      <div className="grid gap-4 p-4 md:grid-cols-[1.4fr_1fr]">
        <Field label="Hostname">
          <div className="flex gap-2">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="preview" aria-label="Subdomain" />
            <Select value={zone} onChange={(e) => setZone(e.target.value)} aria-label="Zone" className="max-w-[14rem]">
              {(zones.data?.zones?.length ? zones.data.zones : [{ id: "", name: zone || wildcard_zone }]).map((z) => <option key={z.id || z.name} value={z.name}>.{z.name}</option>)}
            </Select>
          </div>
        </Field>
        <Field label="Access">
          <Select value={access} onChange={(e) => setAccess(e.target.value as "internal" | "public")}>
            <option value="internal">Private networks only (LAN, VPN, Tailscale)</option>
            <option value="public">Public internet</option>
          </Select>
        </Field>
      </div>
      <details className="px-4 pb-4 text-xs text-muted">
        <summary className="cursor-pointer py-2">Another service or port</summary>
        <div className="max-w-sm space-y-2 pt-2">
          <p>For a service outside a project. Check that the application is listening on this machine; publishing does not start it.</p>
          <Field label="Local port"><Input inputMode="numeric" value={port} onChange={e => { setProject(""); setPort(e.target.value.replace(/\D/g, "")); }} placeholder="8004" /></Field>
        </div>
      </details>
      {auth.data?.role === "owner" && <div className="grid gap-3 px-4 pb-4 md:grid-cols-2">
        <Field label="DNS address"><Select value={selected?.id ?? ""} onChange={e => { setTarget(e.target.value); setPicked(""); }}>
          <option value="">Default {access} address</option>
          {addresses.data?.filter(r => r.access === access).map(r => <option key={r.id} value={r.id}>{r.label} · {r.address}</option>)}
        </Select></Field>
        {selected?.address.includes("/") && <Field label="Host address from block"><Input value={picked} onChange={e => setPicked(e.target.value)} required spellCheck={false} /></Field>}
        <ErrorNote error={addresses.error} title="Couldn't read DNS addresses." />
      </div>}
      <p role="status" aria-label="Publication preview" className="break-words border-t border-line px-4 py-3 text-[13px] text-muted">
        {host && validPort ? <><span className="font-medium text-fg">https://{host}</span> → port {port} · {access === "public" ? "Public internet: anyone can reach this app." : "Private networks only: LAN, VPN and Tailscale."}</> : "Choose a project and reserved port, then a hostname and audience."}
      </p>
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3">
        <p className="text-xs text-muted">
          {host ? <>DNS <span className="font-mono">{host}</span> → <span className="font-mono">{targetIp || (access === "public" ? zones.data?.public_ip : zones.data?.internal_ip) || "default address"}</span></> : "Ports 8000–8099 (dev) and 8100–8199 (prod)."}
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <ErrorNote error={zones.error} title="Couldn't list the Cloudflare zones." onRetry={zones.refetch} />
          <ErrorNote error={submit.error} />
          <ErrorNote error={lookup.error} title="Couldn’t check the current site." onRetry={lookup.refetch} />
          <Button variant="primary" disabled={!host || !validPort || (selected?.address.includes("/") && !picked) || submit.isPending || lookup.isPending || lookup.isError} onClick={go}>Publish</Button>
        </div>
      </div>
    </Card>
  );
}

function publicationStatus(r: PublishRequest) {
  return r.status === "removed" && r.detail?.startsWith("replaced by ") ? "replaced" : r.status;
}

function PublicationRow({ r, historical, owner }: {r: PublishRequest; historical?: boolean; owner: boolean}) {
  return (
    <article aria-label={`${r.host} ${r.id}`} className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3 px-4 py-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {r.access && (r.access === "public" ? <Globe size={14} className="text-wait" aria-label="Public" /> : <Lock size={14} className="text-muted" aria-label="Private networks only" />)}
          <span className={clsx("break-all font-medium", r.status === "removed" ? "text-muted line-through" : "text-fg")}>{r.host}</span>
          {r.port && <span className="tnum text-xs text-muted">→ :{r.port}</span>}
          <Badge tone={TONE[r.status]}>{r.action === "unpublish" ? `unpublish: ${publicationStatus(r).replaceAll("_", " ")}` : publicationStatus(r).replaceAll("_", " ")}</Badge>
        </div>
        <p className="mt-0.5 break-words text-xs text-muted">
          Requested by {r.requested_by} {ago(r.created_at)}{r.decided_by && `; ${r.status === "rejected" ? "rejected" : "approved"} by ${r.decided_by}`}
        </p>
        {owner && <p className="mt-0.5 break-all text-xs text-muted">Publication ID: {r.id}</p>}
        {r.status === "pending_approval" && r.action === "publish" && (
          <div className="mt-1.5 space-y-1">
            {r.port_owner && <p className="text-xs text-muted">Port {r.port} is reserved for {r.port_owner.project ? `project ${r.port_owner.project}` : r.port_owner.label ?? "an outside service"}.</p>}
            <PublishSummary request={r} facts={false} />
          </div>
        )}
        <p className="mt-1 text-xs text-muted">Access: {r.access === "public" ? "Public internet" : r.access === "internal" ? "Private networks only" : "Not recorded"}</p>
        <p className="mt-1 break-words text-xs text-muted">Project: {r.port_owner?.project ?? (r.project_id ? `#${r.project_id}` : "Outside a project")}</p>
        {owner && <p className="mt-1 break-all font-mono text-xs text-muted">DNS target: {r.target_ip || "Not recorded"}</p>}
        {r.reason && <p className="mt-1 break-words text-[13px] text-muted">{r.reason}</p>}
        {r.detail && <p className={clsx("mt-1 break-words text-[13px]", r.status === "failed" ? "text-bad" : "text-muted")}>{r.detail}</p>}
      </div>
      {!historical && <PublishActions request={r} />}
    </article>
  );
}

export function Publishing() {
  const auth = useQuery({ queryKey: ["auth"], queryFn: () => api<AuthState>("/api/auth/state") });
  const { wildcard_zone } = useInstance();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "audit" ? "audit" : "current";
  const host = params.get("host") ?? "";
  const status = ["failed", "rejected", "removed", "replaced"].includes(params.get("status") ?? "") ? params.get("status")! : "";
  const requestedCurrentPage = Number(params.get("current_page") ?? 1);
  const currentPage = Number.isSafeInteger(requestedCurrentPage) && requestedCurrentPage > 0 && requestedCurrentPage <= 1000000 ? requestedCurrentPage : 1;
  const requestedPage = Number(params.get("page") ?? 1);
  const page = Number.isSafeInteger(requestedPage) && requestedPage > 0 && requestedPage <= 1000000 ? requestedPage : 1;
  const change = (key: string, value: string, replace = false) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    if (key === "host" || key === "status") next.delete("page");
    setParams(next, {replace});
  };
  const requests = useQuery({ queryKey: ["publish", "current", currentPage], queryFn: () => api<PublishRequest[]>(`/api/publish?view=current&limit=26&offset=${(currentPage-1)*25}`), refetchInterval: 3000 });
  const auditParams = new URLSearchParams({view:"audit",limit:"25",offset:String((page-1)*25)});
  if (host) auditParams.set("host",host);
  if (status) auditParams.set("status",status);
  const history = useQuery({queryKey:["publish", "audit", host, status, page],
    queryFn:() => api<{items:PublishRequest[];total:number}>(`/api/publish?${auditParams}`), enabled:tab === "audit", refetchInterval:tab === "audit" ? 10000 : false});
  const visible = (requests.data ?? []).slice(0,25);
  const live = visible.filter(r => r.status === "applied" && r.action === "publish");
  const pending = visible.filter(r => ["pending_approval", "approved", "applying"].includes(r.status));
  const owner = auth.data?.role === "owner";
  return (
    <Page>
      <PageHeader title="Publishing" description={`Put a project's web app on its own hostname. Agents can ask for one; nothing changes until an owner approves.${wildcard_zone ? ` Names under ${wildcard_zone} reuse its wildcard certificate, so they never appear in certificate logs.` : ""}`}
        meta={requests.data && <><span>{live.length} live sites on this page</span>{pending.length > 0 && <Badge tone="wait">{pending.length} pending requests on this page</Badge>}</>} />
      <Tabs id="publishing" label="Publishing views" value={tab} onChange={value => change("tab",value)} tabs={[{id:"current",label:"Current"},{id:"audit",label:"Audit"}]} />
      <TabPanel id="publishing" value={tab} className="space-y-4">
        {tab === "current" ? <>
          <PublishForm live={live} />
          <Card>
            <CardHeader title="Requests and published sites" icon={ListChecks} subtitle="Pending work and the rules in effect now" />
            <QueryState query={requests} isEmpty={d => !d.length} empty={<Empty icon={Globe}>No pending requests or live sites.</Empty>}>
              {() => <>
                <h3 className="border-b border-line px-4 py-3 text-[13px] font-medium">Pending requests</h3>
                {pending.length ? <div className="divide-y divide-line">{pending.map(r => <PublicationRow key={r.id} r={r} owner={owner} />)}</div> : <p className="px-4 py-3 text-[13px] text-muted">No pending requests.</p>}
                <h3 className="border-y border-line px-4 py-3 text-[13px] font-medium">Rules in effect</h3>
                {live.length ? <div className="divide-y divide-line">{live.map(r => <PublicationRow key={r.id} r={r} owner={owner} />)}</div> : <p className="px-4 py-3 text-[13px] text-muted">No live published sites.</p>}
              </>}
            </QueryState>
            <nav aria-label="Current pages" className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3">
              <p role="status" className="text-xs text-muted">Current page {currentPage} · Up to 25 entries per page</p>
              <div className="flex max-w-full flex-wrap gap-2">
                <Button disabled={currentPage === 1 || requests.isFetching} onClick={() => change("current_page",String(currentPage-1))}>Previous current page</Button>
                <Button disabled={!requests.data || requests.data.length <= 25 || requests.isFetching} onClick={() => change("current_page",String(currentPage+1))}>Next current page</Button>
              </div>
            </nav>
          </Card>
        </> : <Card>
          <CardHeader title="Publishing audit" icon={ListChecks} subtitle="Historical requests, newest first. Current rules are on the Current tab." />
          <div className="grid gap-3 border-b border-line p-4 sm:grid-cols-2">
            <Field label="Filter by host"><Input value={host} maxLength={253} spellCheck={false} placeholder="example.com…" onChange={e => change("host", e.target.value, true)} /></Field>
            <Field label="Filter by status"><Select value={status} onChange={e => change("status",e.target.value)}>
              <option value="">All historical statuses</option>
              {["failed", "rejected", "removed", "replaced"].map(value => <option key={value} value={value}>{value[0].toUpperCase()+value.slice(1)}</option>)}
            </Select></Field>
          </div>
          <QueryState query={history} isEmpty={d => !d.items.length} empty={<Empty icon={ListChecks}>No historical entries match these filters.</Empty>}>
            {data => <div className="divide-y divide-line">{data.items.map(r => <PublicationRow key={r.id} r={r} historical owner={owner} />)}</div>}
          </QueryState>
          <nav aria-label="Audit pages" className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3">
            <p role="status" className="text-xs text-muted">Page {page}{history.data ? ` · ${history.data.total} entries` : ""}</p>
            <div className="flex max-w-full flex-wrap gap-2">
              <Button disabled={page === 1 || history.isPending} onClick={() => change("page",String(page-1))}>Previous page</Button>
              <Button disabled={!history.data || page*25 >= history.data.total || history.isFetching} onClick={() => change("page",String(page+1))}>Next page</Button>
            </div>
          </nav>
        </Card>}
      </TabPanel>
    </Page>
  );
}
