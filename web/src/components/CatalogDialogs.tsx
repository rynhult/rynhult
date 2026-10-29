import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { AlertTriangle, ExternalLink, FileText, Info, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { ApiError, post } from "../lib/api";
import type { Catalog, CatalogItem, CatalogProfile, ImportPreview, Provider } from "../lib/types";
import { Badge, Button, Dialog, ErrorNote, Field, Input, Select, TabPanel, Tabs, Textarea, ToggleChipGroup } from "./ui";
import { useConfirm } from "./confirm";
import { useProviderRegistry } from "../lib/queries";

/** Provider ids from the registry (Claude and Codex until it loads), and those that run subagents. */
function useProviderIds() {
  const registry = useProviderRegistry();
  const all: Provider[] = registry.list.length ? registry.list.map((p) => p.id) : ["claude", "codex"];
  const withAgents: Provider[] = registry.list.length ? registry.list.filter((p) => p.supports.includes("agents")).map((p) => p.id) : ["claude"];
  return { all, withAgents, registry };
}

const profileOptions = (catalog: Catalog) => catalog.profiles.map((p) => ({ id: p.id, label: p.name, title: p.description }));

function useCatalogMutation<T>(fn: (body: T) => Promise<Catalog>, onDone: () => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (catalog) => {
      queryClient.setQueryData(["catalog"], catalog);
      onDone();
    },
  });
}

// ------------------------------------------------------------------ add skill or agent

const flagIcon = { bad: ShieldAlert, warn: AlertTriangle, info: Info };
const flagTone = { bad: "text-bad", warn: "text-wait", info: "text-muted" };

export function AddSkillDialog({ open, onClose, catalog }: { open: boolean; onClose: () => void; catalog: Catalog }) {
  const [mode, setMode] = useState<"github" | "write">("github");
  const [url, setUrl] = useState("");
  const [result, setResult] = useState<ImportPreview | null>(null);
  const [viewing, setViewing] = useState<string | null>(null);
  const [fields, setFields] = useState({ id: "", name: "", notes: "", content: "" });
  const { all: PROVIDERS, withAgents } = useProviderIds();
  const [providers, setProviders] = useState<string[]>(PROVIDERS);
  const [profiles, setProfiles] = useState<string[]>([]);
  const [acceptRisks, setAcceptRisks] = useState(false);
  const reset = () => { setUrl(""); setResult(null); setViewing(null); setFields({ id: "", name: "", notes: "", content: "" }); setProfiles([]); setProviders(PROVIDERS); setAcceptRisks(false); };
  const close = () => { reset(); onClose(); };

  const preview = useMutation({
    mutationFn: (link: string) => post<ImportPreview>("/api/catalog/preview", { url: link }),
    onSuccess: (data) => {
      setResult(data);
      setViewing(data.files?.[0]?.path ?? null);
      setFields((f) => ({ ...f, id: data.suggested_id ?? "", name: data.name ?? "", notes: data.description ?? "" }));
      setProviders(data.kind === "agent" ? withAgents : PROVIDERS);
    },
  });
  const add = useCatalogMutation(
    () => mode === "write"
      ? post<Catalog>("/api/catalog/skills", { id: fields.id, name: fields.name || null, notes: fields.notes, content: fields.content, providers, profiles, accept_risks: acceptRisks })
      : post<Catalog>("/api/catalog/import", { token: result?.token, id: fields.id, name: fields.name || null, notes: fields.notes, providers, profiles, accept_risks: acceptRisks }),
    close,
  );
  const reviewed = mode === "write" || (result && result.kind !== "choose");
  // Red flags (hooks, pre-approved tools, shell on load...) block adding until confirmed; Rynhult strips those settings at
  // launch either way. A pasted skill's flags only show up when the server refuses it.
  const redFlags = !!result?.flags?.some((f) => f.level === "bad") || (add.error instanceof ApiError && add.error.status === 409);
  const shown = result?.files?.find((f) => f.path === viewing);

  return (
    <Dialog open={open} onClose={close} title="Add a skill or agent" wide dismissible={false}>
      <div className="space-y-4">
        <Tabs id="catalog-source" label="Source" value={mode} onChange={(m) => { reset(); setMode(m); }}
          tabs={[{ id: "github", label: "From GitHub" }, { id: "write", label: "Write one" }]} />

        <TabPanel id="catalog-source" value={mode} className="space-y-4">
          {mode === "github" && (
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); preview.mutate(url); }}>
              <Input aria-label="GitHub link" placeholder="https://github.com/owner/repo/tree/<ref>/path/to/skill" value={url}
                onChange={(e) => setUrl(e.target.value)} className="flex-1 font-mono text-[13px]" />
              <Button type="submit" disabled={!url.trim() || preview.isPending}>{preview.isPending ? "Fetching…" : "Preview"}</Button>
            </form>
          )}
          {mode === "github" && !result && (
            <p className="text-xs text-faint">
              Link a folder that holds a SKILL.md, a Claude subagent .md file, or a whole repo to list the skills in it.
              Rynhult pins the commit, fetches text files only and runs nothing. You review the files before adding.
            </p>
          )}
          <ErrorNote error={preview.error} />

          {result?.kind === "choose" && (
            <div className="space-y-2">
              <p className="text-[13px] text-muted">{result.candidates.length} skills in {result.repo} at {result.commit.slice(0, 7)} <ExternalLink size={12} className="inline" aria-hidden="true" />. Pick one:</p>
              <div className="max-h-72 divide-y divide-line overflow-y-auto rounded-md border border-line">
                {result.candidates.map((c) => (
                  <button key={c.path} onClick={() => { setUrl(c.url); preview.mutate(c.url); }}
                    className="block w-full px-3 py-2 text-left font-mono text-[13px] hover:bg-panel-2">{c.path || "(repository root)"}</button>
                ))}
              </div>
            </div>
          )}

          {result && result.kind !== "choose" && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2 text-[13px]">
                <Badge tone="accent">{result.kind}</Badge>
                <a className="font-mono text-accent hover:underline underline underline-offset-2" href={result.source} target="_blank" rel="noreferrer">
                  {result.repo}{result.path ? `/${result.path}` : ""} @ {result.commit.slice(0, 7)} <ExternalLink size={12} className="inline" aria-hidden="true" />
                </a>
                <Badge>{result.license}</Badge>
              </div>
              {!!result.flags?.length && (
                <ul className="space-y-1.5 rounded-md border border-line bg-bg p-3">
                  {result.flags.map((f) => {
                    const Icon = flagIcon[f.level];
                    return (
                      <li key={f.message} className="flex gap-2 text-[13px]">
                        <Icon size={15} className={clsx("mt-0.5 shrink-0", flagTone[f.level])} aria-hidden="true" />
                        <span><span className={flagTone[f.level]}>{f.message}</span>
                          {!!f.files.length && <span className="text-faint">: {f.files.join(", ")}</span>}</span>
                      </li>
                    );
                  })}
                </ul>
              )}
              <div className="grid overflow-hidden rounded-md border border-line md:grid-cols-[13rem_1fr]">
                <div className="max-h-80 overflow-y-auto border-b border-line md:border-b-0 md:border-r">
                  {result.files?.map((f) => (
                    <button key={f.path} onClick={() => setViewing(f.path)}
                      className={clsx("flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left font-mono text-xs",
                        viewing === f.path ? "bg-panel-2 text-fg" : "text-muted hover:text-fg")}>
                      <FileText size={13} className="shrink-0" aria-hidden="true" /><span className="truncate">{f.path}</span>
                    </button>
                  ))}
                </div>
                <pre tabIndex={0} role="region" aria-label="Catalog preview" className="max-h-80 overflow-auto whitespace-pre-wrap break-words bg-bg p-3 font-mono text-xs text-fg">{shown?.text ?? ""}</pre>
              </div>
            </div>
          )}

          {mode === "write" && (
            <Field label="SKILL.md" hint="Frontmatter with name and description, then the instructions.">
              <Textarea rows={12} className="font-mono text-[13px]" value={fields.content}
                placeholder={"---\nname: house-style\ndescription: When to use this skill\n---\n\n# Instructions"}
                onChange={(e) => setFields({ ...fields, content: e.target.value })} />
            </Field>
          )}

          {reviewed && (
            <div className="space-y-3 border-t border-line pt-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="ID" hint="Lowercase letters, digits and dashes">
                  <Input value={fields.id} onChange={(e) => setFields({ ...fields, id: e.target.value })} className="font-mono" />
                </Field>
                <Field label="Display name"><Input value={fields.name} onChange={(e) => setFields({ ...fields, name: e.target.value })} /></Field>
              </div>
              <Field label="Notes"><Input value={fields.notes} onChange={(e) => setFields({ ...fields, notes: e.target.value })} /></Field>
              {result?.kind !== "agent" && (
                <ToggleChipGroup label="Providers" options={PROVIDERS.map((p) => ({ id: p }))} selected={providers} onChange={setProviders} />
              )}
              <ToggleChipGroup label="Add to profiles" options={profileOptions(catalog)} selected={profiles} onChange={setProfiles} />
              <ErrorNote error={add.error} />
              {redFlags && (
                <label className="flex items-start gap-2 rounded-md border border-bad/30 bg-bad/10 p-3 text-xs text-bad">
                  <input type="checkbox" checked={acceptRisks} onChange={(e) => setAcceptRisks(e.target.checked)} className="mt-0.5" />
                  <span>Add it anyway. Sessions never get its hooks, pre-approved tools, MCP servers, permission changes or
                    load-time shell commands: Rynhult removes those when it launches a session.</span>
                </label>
              )}
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={close}>Cancel</Button>
                <Button variant="primary" disabled={!fields.id || !providers.length || add.isPending || (mode === "write" && !fields.content.trim()) || (redFlags && !acceptRisks)}
                  onClick={() => add.mutate(undefined)}>
                  {add.isPending ? "Adding…" : "Add to catalog"}
                </Button>
              </div>
            </div>
          )}
        </TabPanel>
      </div>
    </Dialog>
  );
}

// ------------------------------------------------------------------ add MCP server

const lines = (text: string) => text.split("\n").map((l) => l.trim()).filter(Boolean);

export function AddMcpDialog({ open, onClose, catalog }: { open: boolean; onClose: () => void; catalog: Catalog }) {
  const empty = { id: "", name: "", transport: "stdio", command: "npx", args: "", env: "", url: "", source: "", license: "", notes: "" };
  const [f, setF] = useState(empty);
  const { all: PROVIDERS } = useProviderIds();
  const [providers, setProviders] = useState<string[]>(PROVIDERS);
  const [profiles, setProfiles] = useState<string[]>([]);
  const close = () => { setF(empty); setProviders(PROVIDERS); setProfiles([]); onClose(); };
  const args = lines(f.args);
  const env = Object.fromEntries(lines(f.env).map((l) => [l.split("=")[0].trim(), l.slice(l.indexOf("=") + 1)]));
  const unpinned = f.transport === "stdio" && ["npx", "uvx", "bunx"].includes(f.command) &&
    !args.some((a) => !a.startsWith("-") && /.@\d/.test(a));
  const add = useCatalogMutation(() => post<Catalog>("/api/catalog/mcps", {
    id: f.id, name: f.name || null, notes: f.notes, source: f.source || null, license: f.license || null, providers, profiles,
    ...(f.transport === "stdio" ? { command: f.command, args, env } : { url: f.url }),
  }), close);
  const set = (key: keyof typeof empty) => (e: { target: { value: string } }) => setF({ ...f, [key]: e.target.value });

  return (
    <Dialog open={open} onClose={close} title="Add an MCP server" wide dismissible={false}>
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="ID"><Input value={f.id} onChange={set("id")} className="font-mono" placeholder="shadcn" /></Field>
          <Field label="Display name"><Input value={f.name} onChange={set("name")} /></Field>
          <Field label="Transport">
            <Select value={f.transport} onChange={set("transport")}>
              <option value="stdio">Command (stdio)</option>
              <option value="http">URL (HTTP)</option>
            </Select>
          </Field>
        </div>
        {f.transport === "stdio" ? (
          <>
            <div className="grid gap-3 sm:grid-cols-[10rem_1fr]">
              <Field label="Command"><Input value={f.command} onChange={set("command")} className="font-mono" /></Field>
              <Field label="Arguments" hint="One per line. Pin package versions (pkg@1.2.3).">
                <Textarea rows={4} value={f.args} onChange={set("args")} className="font-mono text-[13px]" placeholder={"-y\nshadcn@4.22.0\nmcp"} />
              </Field>
            </div>
            <Field label="Environment" hint="KEY=value per line. Values are stored for Rynhult only, but each session that uses this server can read them.">
              <Textarea rows={2} value={f.env} onChange={set("env")} className="font-mono text-[13px]" />
            </Field>
            {unpinned && <p className="flex items-center gap-1.5 text-xs text-wait"><AlertTriangle size={13} aria-hidden="true" /> No pinned version in the arguments; the server could change under you.</p>}
          </>
        ) : (
          <Field label="URL"><Input value={f.url} onChange={set("url")} className="font-mono" placeholder="https://mcp.example.com/mcp" /></Field>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Source"><Input value={f.source} onChange={set("source")} placeholder="https://github.com/…" /></Field>
          <Field label="Licence"><Input value={f.license} onChange={set("license")} placeholder="MIT" /></Field>
        </div>
        <Field label="Notes"><Input value={f.notes} onChange={set("notes")} /></Field>
        <ToggleChipGroup label="Providers" options={PROVIDERS.map((p) => ({ id: p }))} selected={providers} onChange={setProviders} />
        <ToggleChipGroup label="Add to profiles" options={profileOptions(catalog)} selected={profiles} onChange={setProfiles} />
        <ErrorNote error={add.error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={close}>Cancel</Button>
          <Button variant="primary" disabled={!f.id || !providers.length || add.isPending || (f.transport === "stdio" ? !f.command : !f.url)}
            onClick={() => add.mutate(undefined)}>{add.isPending ? "Adding…" : "Add to catalog"}</Button>
        </div>
      </div>
    </Dialog>
  );
}

// ------------------------------------------------------------------ assign an item to profiles

export function AssignDialog({ item, onClose, catalog }: { item: CatalogItem | null; onClose: () => void; catalog: Catalog }) {
  const key = item ? (`${item.kind}s` as "skills" | "agents" | "mcps") : "skills";
  const current = item ? catalog.profiles.filter((p) => p[key].includes(item.id)).map((p) => p.id) : [];
  const [picked, setPicked] = useState<string[] | null>(null);
  const close = () => { setPicked(null); onClose(); };
  const save = useCatalogMutation(() => post<Catalog>(`/api/catalog/${item!.kind}/${item!.id}/profiles`, { profiles: picked ?? current }), close);
  const remove = useCatalogMutation(() => post<Catalog>(`/api/catalog/${item!.kind}/${item!.id}/remove`), close);
  const confirm = useConfirm();
  if (!item) return null;
  const options = catalog.profiles.filter((p) => !p.providers || p.providers.some((x) => item.providers.includes(x)))
    .map((p) => ({ id: p.id, label: p.name, title: p.description }));
  return (
    <Dialog open onClose={close} title={`${item.name ?? item.id}: profiles`}>
      <div className="space-y-4">
        <p className="text-[13px] text-muted">Profiles pre-select this {item.kind} in New session. You can still change it per launch.</p>
        <ToggleChipGroup label="Included in" options={options} selected={picked ?? current} onChange={setPicked} />
        <ErrorNote error={save.error ?? remove.error} />
        <div className="flex items-center justify-between gap-2">
          {item.origin === "local"
            ? <Button variant="danger" onClick={async () => (await confirm({ title: `Remove ${item.id}?`, body: "It leaves the catalog and every profile. Running sessions keep their copy.", confirm: "Remove", danger: true })) && remove.mutate(undefined)}>Remove from catalog</Button>
            : <span />}
          <div className="flex gap-2">
            <Button variant="ghost" onClick={close}>Cancel</Button>
            <Button variant="primary" disabled={save.isPending || picked === null} onClick={() => save.mutate(undefined)}>Save</Button>
          </div>
        </div>
      </div>
    </Dialog>
  );
}

// ------------------------------------------------------------------ create or edit a profile


export function ProfileDialog({ profile, creating, onClose, catalog }: {
  profile: CatalogProfile | null; creating: boolean; onClose: () => void; catalog: Catalog;
}) {
  const base: CatalogProfile = profile ?? { id: "", name: "", description: "", skills: ["acc-workflow"], agents: [], mcps: [],
    instructions: "", claude: { permission_mode: "acceptEdits" }, codex: { sandbox: "workspace-write", approval: "on-request" } };
  const [p, setP] = useState<CatalogProfile>(base);
  const save = useCatalogMutation(() => post<Catalog>(`/api/catalog/profiles/${p.id}${creating ? "?create=true" : ""}`, {
    ...p, providers: p.providers?.length ? p.providers : null }), onClose);
  const reset = useCatalogMutation(() => post<Catalog>(`/api/catalog/profiles/${p.id}/reset`), onClose);
  const confirm = useConfirm();
  const options = (items: CatalogItem[]) => items.map((i) => ({ id: i.id, title: `${i.name ?? i.id} · ${i.license}` }));
  const { all: PROVIDERS, registry } = useProviderIds();
  const section = (id: string) => (p[id] as Record<string, string> | undefined) ?? {};
  return (
    <Dialog open onClose={onClose} title={creating ? "New profile" : `Edit ${base.name}`} wide dismissible={false}>
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-[12rem_1fr]">
          <Field label="ID"><Input value={p.id} disabled={!creating} onChange={(e) => setP({ ...p, id: e.target.value })} className="font-mono" /></Field>
          <Field label="Name"><Input value={p.name} onChange={(e) => setP({ ...p, name: e.target.value })} /></Field>
        </div>
        <Field label="Description"><Input value={p.description} onChange={(e) => setP({ ...p, description: e.target.value })} /></Field>
        <Field label="Instructions" hint="Added to the agent's instructions for every session with this profile.">
          <Textarea rows={5} value={p.instructions} onChange={(e) => setP({ ...p, instructions: e.target.value })} />
        </Field>
        <ToggleChipGroup label="Skills" options={options(catalog.skills)} selected={p.skills} onChange={(skills) => setP({ ...p, skills })} />
        <ToggleChipGroup label="Agents (subagents)" options={options(catalog.agents)} selected={p.agents} onChange={(agents) => setP({ ...p, agents })} />
        <ToggleChipGroup label="MCP servers" options={options(catalog.mcps)} selected={p.mcps} onChange={(mcps) => setP({ ...p, mcps })} />
        <label className="flex items-center gap-2 text-[13px] text-fg">
          <input type="checkbox" checked={!!p.standalone} onChange={(e) => setP({ ...p, standalone: e.target.checked })} className="size-3.5 accent-[var(--accent)]" />
          Can run in a local workspace (no project needed)
        </label>
        <ToggleChipGroup label="Only for (none selected = every agent)" options={PROVIDERS.map((x) => ({ id: x }))} selected={p.providers ?? []}
          onChange={(providers) => setP({ ...p, providers: providers as Provider[] })} />
        <div className="grid gap-3 sm:grid-cols-3">
          {registry.list.flatMap((info) => info.options.filter((o) => o.kind === "select").map((o) => (
            <Field key={`${info.id}.${o.key}`} label={`${info.short} ${o.label.toLowerCase()}`}>
              <Select value={section(info.id)[o.key] ?? String(o.default)}
                onChange={(e) => setP({ ...p, [info.id]: { ...section(info.id), [o.key]: e.target.value } })}>
                {o.choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
              </Select>
            </Field>
          )))}
        </div>
        <ErrorNote error={save.error ?? reset.error} />
        <div className="flex items-center justify-between gap-2">
          {profile?.origin === "override" && <Button variant="ghost" onClick={() => reset.mutate(undefined)}>Reset to repository version</Button>}
          {profile?.origin === "local" && <Button variant="danger" onClick={async () => (await confirm({ title: `Delete profile ${p.name}?`, body: "Sessions already running keep their launch settings.", confirm: "Delete", danger: true })) && reset.mutate(undefined)}>Delete profile</Button>}
          {(!profile || profile.origin === "repo") && <span />}
          <div className="flex gap-2">
            <Button variant="ghost" onClick={onClose}>Cancel</Button>
            <Button variant="primary" disabled={!p.id || !p.name || save.isPending} onClick={() => save.mutate(undefined)}>{creating ? "Create" : "Save"}</Button>
          </div>
        </div>
      </div>
    </Dialog>
  );
}
