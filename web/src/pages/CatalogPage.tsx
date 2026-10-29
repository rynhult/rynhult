import { useQuery } from "@tanstack/react-query";
import { BookOpen, ExternalLink, Pencil, Plus, Server, Sparkles } from "lucide-react";
import { useState } from "react";
import { AddMcpDialog, AddSkillDialog, AssignDialog, ProfileDialog } from "../components/CatalogDialogs";
import { CatalogRestoreDialog } from "../components/CatalogRestoreDialog";
import { Badge, Button, Card, Empty, Page, PageHeader, QueryState, SearchInput, TabPanel, Tabs, Toolbar } from "../components/ui";
import { api } from "../lib/api";
import type { Catalog, CatalogItem, CatalogProfile } from "../lib/types";
import { providerShort, useProviderRegistry, useRole } from "../lib/queries";
import { settingWords } from "../lib/words";

type Tab = "profiles" | "skills" | "agents" | "mcps" | "deferred";
const match = (q: string, ...parts: (string | undefined)[]) => !q || parts.join(" ").toLowerCase().includes(q.toLowerCase());

function Items({ items, catalog, onAssign }: { items: CatalogItem[]; catalog: Catalog; onAssign?: (item: CatalogItem) => void }) {
  const registry = useProviderRegistry();
  if (!items.length) return <Empty icon={BookOpen}>Nothing matches.</Empty>;
  return (
    <div className="divide-y divide-line">
      {items.map((item) => {
        const key = `${item.kind}s` as "skills" | "agents" | "mcps";
        const usedBy = catalog.profiles.filter((p) => p[key].includes(item.id));
        return (
          <div key={item.id} className="grid gap-x-6 gap-y-2 px-4 py-3.5 md:grid-cols-[15rem_minmax(0,1fr)_auto] md:items-start">
            <div className="min-w-0">
              <div className="truncate font-medium text-fg">{item.name ?? item.id}</div>
              <div className="truncate font-mono text-[11.5px] text-faint">{item.id}</div>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {item.providers.map((p) => <Badge key={p}>{providerShort(p, registry.byId(p))}</Badge>)}
                <Badge>{item.license}</Badge>
                {item.origin === "restored" && <Badge>restored built-in</Badge>}
                {item.origin === "local" && <Badge tone="accent">added in Rynhult</Badge>}
              </div>
            </div>
            <div className="min-w-0 text-[13px] text-muted">
              {item.notes && <p className="text-fg/90">{item.notes}</p>}
              {item.kind === "mcp" && (
                <p className="mt-1 break-all font-mono text-xs text-muted">
                  {item.url ?? [item.command, ...(item.args ?? [])].join(" ")}
                  {!!item.env_keys?.length && <span className="text-faint"> · env: {item.env_keys.join(", ")}</span>}
                </p>
              )}
              {item.source.startsWith("http")
                ? <a className="mt-1 block truncate text-xs text-muted underline underline-offset-2 hover:text-fg" href={item.source.split(" ")[0]} target="_blank" rel="noreferrer" title={item.source}>{item.source}<ExternalLink size={12} className="ml-1 inline" aria-hidden="true" /></a>
                : <span className="mt-1 block truncate text-xs text-faint">{item.source}</span>}
              <div className="mt-2 flex flex-wrap items-center gap-1">
                {usedBy.length ? usedBy.map((p) => <Badge key={p.id} tone="neutral">{p.name}</Badge>) : <span className="text-xs text-faint">Not in any profile</span>}
              </div>
            </div>
            {onAssign && <Button size="sm" variant="ghost" onClick={() => onAssign(item)} aria-label={`Profiles for ${item.id}`}>
              <Pencil size={13} aria-hidden="true" /> Profiles
            </Button>}
          </div>
        );
      })}
    </div>
  );
}

function ProfileCard({ profile, onEdit }: { profile: CatalogProfile; onEdit?: () => void }) {
  const registry = useProviderRegistry();
  return (
    <Card className="flex flex-col">
      <div className="flex items-start gap-2 px-4 pb-2 pt-3.5">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[14px] font-semibold text-fg">{profile.name}</span>
            {profile.providers && <Badge>{profile.providers.join(", ")} only</Badge>}
            {profile.standalone && <Badge tone="info" title="Can run in a local workspace, without a project">local workspace</Badge>}
            {profile.origin === "override" && <Badge tone="wait" title="Changed from the repository version; reset it from Edit">edited in Rynhult</Badge>}
            {profile.origin === "local" && <Badge tone="accent">added in Rynhult</Badge>}
          </div>
          <p className="mt-1 text-[13px] text-muted">{profile.description}</p>
        </div>
        {onEdit && <Button size="sm" variant="ghost" onClick={onEdit} aria-label={`Edit ${profile.name}`}><Pencil size={13} aria-hidden="true" /> Edit</Button>}
      </div>
      <div className="mt-auto space-y-2 px-4 pb-3.5 pt-1 text-xs">
        <div className="flex flex-wrap gap-1">{profile.skills.map((s) => <Badge key={s}>{s}</Badge>)}</div>
        {(profile.agents.length > 0 || profile.mcps.length > 0) && (
          <div className="flex flex-wrap gap-1">
            {profile.agents.map((a) => <Badge key={a} tone="info">{a}</Badge>)}
            {profile.mcps.map((m) => <Badge key={m} tone="accent">mcp · {m}</Badge>)}
          </div>
        )}
        <p className="text-faint">{registry.list.filter((x) => profile[x.id]).map((x) => {
          const settings = Object.entries(profile[x.id] as Record<string, string>);
          return <span key={x.id} className="mr-3 inline-block" title={settings.map(([k, v]) => `${k}: ${v}`).join(", ")}>
            {x.short}: {settings.map(([k, v]) => settingWords(k, v)).join(", ")}</span>;
        })}</p>
      </div>
    </Card>
  );
}

export function CatalogPage() {
  const { owner } = useRole();  // members use the catalog; owners change it
  const catalog = useQuery({ queryKey: ["catalog"], queryFn: () => api<Catalog>("/api/catalog") });
  const [tab, setTab] = useState<Tab>("profiles");
  const [query, setQuery] = useState("");
  const [restoring, setRestoring] = useState(false);
  const [adding, setAdding] = useState<"skill" | "mcp" | null>(null);
  const [assigning, setAssigning] = useState<CatalogItem | null>(null);
  const [editing, setEditing] = useState<{ profile: CatalogProfile | null } | null>(null);
  const c = catalog.data;
  const items = (list: CatalogItem[]) => list.filter((i) => match(query, i.id, i.name, i.notes, i.source));
  return (
    <Page wide>
      <PageHeader title="Catalog"
        description="Skills, subagents and MCP servers, grouped into profiles for New session. Every item is pinned and reviewed before use."
        meta={c && <span title={`Catalog revision ${c.revision}`}>{c.profiles.length} profiles · {c.skills.length + c.agents.length + c.mcps.length} items</span>}
        actions={owner && <>
          <Button onClick={() => setRestoring(true)}>Restore built-ins</Button>
          <Button onClick={() => setAdding("skill")}><Sparkles size={14} aria-hidden="true" /> Add skill or agent</Button>
          <Button onClick={() => setAdding("mcp")}><Server size={14} aria-hidden="true" /> Add MCP server</Button>
          <Button onClick={() => setEditing({ profile: null })}><Plus size={14} aria-hidden="true" /> New profile</Button>
        </>} />
      {restoring && <CatalogRestoreDialog onClose={() => setRestoring(false)} />}
      <QueryState query={catalog}>
        {(c) => (
          <>
            <div className="flex flex-wrap items-end justify-between gap-3">
              <Tabs id="catalog-tabs" className="flex-1" value={tab} onChange={setTab} tabs={[
                { id: "profiles", label: "Profiles", count: c.profiles.length },
                { id: "skills", label: "Skills", count: c.skills.length },
                { id: "agents", label: "Subagents", count: c.agents.length },
                { id: "mcps", label: "MCP servers", count: c.mcps.length },
                { id: "deferred", label: "Not loaded by default", count: c.deferred.length },
              ]} />
              <Toolbar className="pb-1.5"><SearchInput value={query} onChange={setQuery} placeholder="Filter" className="w-56" /></Toolbar>
            </div>
            <TabPanel id="catalog-tabs" value={tab}>
            {tab === "profiles" && (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {c.profiles.filter((p) => match(query, p.id, p.name, p.description, ...p.skills, ...p.mcps)).map((p) =>
                  <ProfileCard key={p.id} profile={p} onEdit={owner ? () => setEditing({ profile: p }) : undefined} />)}
              </div>
            )}
            {tab === "skills" && <Card><Items items={items(c.skills)} catalog={c} onAssign={owner ? setAssigning : undefined} /></Card>}
            {tab === "agents" && <Card><Items items={items(c.agents)} catalog={c} onAssign={owner ? setAssigning : undefined} /></Card>}
            {tab === "mcps" && <Card><Items items={items(c.mcps)} catalog={c} onAssign={owner ? setAssigning : undefined} /></Card>}
            {tab === "deferred" && (
              <Card>
                <div className="divide-y divide-line">
                  {c.deferred.filter((d) => match(query, d.id, d.reason)).map((d) => (
                    <div key={d.id} className="grid gap-1 px-4 py-3 text-[13px] md:grid-cols-[15rem_1fr]">
                      <span className="font-medium text-fg">{d.id}</span><span className="text-muted">{d.reason}</span>
                    </div>
                  ))}
                </div>
              </Card>
            )}
            </TabPanel>
            <AddSkillDialog open={adding === "skill"} onClose={() => setAdding(null)} catalog={c} />
            <AddMcpDialog open={adding === "mcp"} onClose={() => setAdding(null)} catalog={c} />
            <AssignDialog key={assigning?.id} item={assigning} onClose={() => setAssigning(null)} catalog={c} />
            {editing && <ProfileDialog key={editing.profile?.id ?? "new"} profile={editing.profile} creating={!editing.profile}
              onClose={() => setEditing(null)} catalog={c} />}
          </>
        )}
      </QueryState>
    </Page>
  );
}
