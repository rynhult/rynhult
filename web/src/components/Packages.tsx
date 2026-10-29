import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Archive, Download, PackageCheck, RefreshCw, Trash2 } from "lucide-react";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import { useConfirm } from "./confirm";
import { type InstallGuide, InstallSteps } from "./InstallSteps";
import { Badge, Button, Card, CardHeader, ErrorNote, IconButton } from "./ui";

export interface PackageState {
  label?: string; method?: string; note?: string; installed?: string | null; latest?: string | null; checked_at?: number;
  auto_update?: boolean; running?: string; mac_ok?: boolean; last?: { action: string; ok: boolean; detail: string; at: number };
  /** The server's word on that machine: agent software or a backup tool, whether Rynhult can install it there now, and
   * when it can't, the steps to install it by hand. */
  group?: "agent" | "backup"; installable?: boolean; guide?: InstallGuide;
}
/** kind: "local" (this machine's helper), "helper" (another machine's helper), "mac" (a Mac's acc-node itself). */
interface PackagesView { packages: Record<string, PackageState>; pending: { package: string; action: string }[];
  daily_at?: number; helper: boolean; kind?: "local" | "helper" | "mac" | null; reported?: boolean; machine?: string;
  refused?: { id: string; detail: string; at?: number } | null }

const NAMES: Record<string, string> = { claude: "Claude Code", codex: "Codex", copilot: "GitHub Copilot CLI", antigravity: "Google Antigravity CLI", cursor: "Cursor CLI", opencode: "OpenCode", ollama: "Ollama", restic: "Restic" };
const when = (epoch?: number) => (epoch ? ago(new Date(epoch * 1000).toISOString()) : "never");

/** `?machine=…` for another machine; nothing for this one. */
export const onMachine = (machine?: string, joiner = "?") => (machine ? `${joiner}machine=${encodeURIComponent(machine)}` : "");

export const usePackages = (enabled = true, machine?: string, account?: string) =>
  useQuery({ queryKey: ["packages", machine ?? "local", account ?? ""],
    queryFn: () => api<PackagesView>(`/api/packages?${[onMachine(machine, ""), account ? `account=${encodeURIComponent(account)}` : ""].filter(Boolean).join("&")}`), enabled,
    refetchInterval: (q) => ((q.state.data?.pending.length || Object.values(q.state.data?.packages ?? {}).some((p) => p.running)) ? 3000 : machine ? 15000 : 60000) });

export function usePackageAction(machine?: string, onResult?: (error: unknown) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, action, enabled }: { name: string; action: string; enabled?: boolean }) =>
      action === "auto" ? post(`/api/packages/${name}/auto`, { enabled }) : post(`/api/packages/${name}/${action}${onMachine(machine)}`),
    onSuccess: () => {
      onResult?.(null);
      queryClient.invalidateQueries({ queryKey: ["packages"] });
      setTimeout(() => queryClient.invalidateQueries({ queryKey: ["provider-registry"] }), 5000);
    },
    onError: error => onResult?.(error),
  });
}

function subtitle(view: PackagesView | undefined, machine?: string) {
  const daily = `checked daily (last ${when(view?.daily_at)})`;
  if (!machine) return `Installed and kept up to date by Rynhult's root helper; ${daily}. New sessions use the new version; running ones keep theirs.`;
  if (view?.kind === "mac") return `On ${machine} (a Mac), Rynhult installs Codex and Copilot for the signed-in user with Homebrew's npm. The others have step-by-step instructions below; once installed, their versions show here.`;
  return `On ${machine}: installed and kept up to date by that machine's own root helper; ${daily}. Its new sessions use the new version.`;
}

function helperMissing(view: PackagesView, machine?: string) {
  if (!machine) return "The acc-packages helper isn't installed yet: run deploy/install.sh.";
  if (view.kind === "mac") return `npm isn't installed on ${machine}: run brew install node there.`;
  return `The acc-packages helper isn't installed on ${machine} yet: update Rynhult there (deploy/node/install.sh, or the installer).`;
}

/** One entry: its version, what Rynhult can do with it on that machine, and otherwise the steps to install it by hand.
 * `tile`: shown on that provider's sign-in tile (Providers), so without its name and as a block, not a list item. */
function PackageRow({ name, p, view, machine, onResult, tile }: { name: string; p: PackageState; view: PackagesView; machine?: string; onResult: (error: unknown) => void; tile?: boolean }) {
  const confirm = useConfirm();
  const act = usePackageAction(machine, onResult);
  const queryClient = useQueryClient();
  const busy = view.pending.some((q) => q.package === name) || !!p.running;
  const manual = p.installable === false;  // that machine would refuse: offer no action, only the steps
  const behind = p.installed && p.latest && !String(p.installed).includes(p.latest);
  const label = p.label ?? NAMES[name] ?? name;
  // Reading a version is always possible where something answers: a Mac's acc-node, or a machine's helper.
  const checkable = view.kind === "mac" || view.helper;
  const check = () => {
    if (checkable) act.mutate({ name, action: "check" });
    queryClient.invalidateQueries({ queryKey: ["provider-registry"] });
    queryClient.invalidateQueries({ queryKey: ["accounts"] });
  };
  const Row = tile ? "div" : "li";
  return (
    <Row className={tile ? "text-[13px]" : "px-4 py-3 text-[13px]"} {...(tile ? { role: "group", "aria-label": `${label} software` } : {})}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">{tile ? <span className="text-xs text-muted">Software</span> : <span className="font-medium text-fg">{label}</span>}
            {p.installed ? <Badge tone={behind && !manual ? "wait" : "ok"}>{p.installed}{behind && !manual ? ` → ${p.latest}` : ""}</Badge> : <Badge>not installed</Badge>}
            {manual && p.installed && <Badge title={`Rynhult can't install or update ${label} on ${machine ?? "this machine"}`}>installed by hand</Badge>}
            {busy && <Badge tone="info">{p.running ?? "queued"}…</Badge>}</div>
          <p className="mt-0.5 text-xs text-faint">
            {p.last ? <span className={p.last.ok ? undefined : "text-bad"}>{p.last.action}: {p.last.detail} ({when(p.last.at)})</span> : p.note}
          </p>
        </div>
        {!manual && p.installed && !machine && (
          <label className="flex items-center gap-1.5 text-xs text-muted" title="Update automatically in the daily check">
            <input type="checkbox" checked={p.auto_update !== false} disabled={busy}
              onChange={(e) => act.mutate({ name, action: "auto", enabled: e.target.checked })} className="size-3.5 accent-[var(--accent)]" />
            Auto-update
          </label>
        )}
        {manual ? (p.installed && checkable ? <IconButton label={`Check ${label} for its installed version`} icon={RefreshCw} disabled={busy} onClick={check} /> : null)
          : !p.installed
          ? <Button size="sm" disabled={busy} onClick={() => act.mutate({ name, action: "install" })}><Download size={13} aria-hidden="true" /> Install</Button>
          : <>
              <IconButton label={`Check ${label} for updates`} icon={RefreshCw} onClick={() => act.mutate({ name, action: "check" })} />
              {behind && <Button size="sm" disabled={busy} onClick={() => act.mutate({ name, action: "update" })}>Update</Button>}
              {p.method !== "script" && <IconButton label={`Remove ${label}`} icon={Trash2} onClick={async () => {
                if (await confirm({ title: `Remove ${label}${machine ? ` from ${machine}` : ""}?`, body: "Sessions that use it can't start or resume until it's installed again. Logins stay in each agent account.", confirm: "Remove", danger: true }))
                  act.mutate({ name, action: "remove" });
              }} />}
            </>}
      </div>
      {manual && !p.installed && p.guide && (
        <details className="group mt-2 rounded-md border border-line">
          <summary className="cursor-pointer rounded-md px-3 py-2 text-xs font-medium text-accent hover:bg-panel-2">
            How to install {label} on {machine ?? "this machine"}</summary>
          <div className="border-t border-line p-3"><InstallSteps guide={p.guide} machine={machine} onCheck={check} checking={busy} /></div>
        </details>
      )}
    </Row>
  );
}

/** Providers: a provider CLI's software controls on its own sign-in tile (UX follow-up to W11): version, update, check,
 * remove and auto-update, with that action's error beside them. Owners only; not-installed tiles use NotInstalled. */
export function ProviderSoftware({ name, machine, account, tileOffersInstall }: { name: string; machine?: string; account?: string;
  /** The tile already shows "Not installed" with its Install button or steps (NotInstalled). */
  tileOffersInstall?: boolean }) {
  const view = usePackages(true, machine, account);
  const [error, setError] = useState<unknown>(null);
  const p = view.data?.reported !== false ? view.data?.packages[name] : undefined;
  if (!view.data || !p) return error ? <ErrorNote error={error} /> : null;
  if (!p.installed && tileOffersInstall) {  // the tile's own Install (or the steps) is the action; show only what's happening or failed
    const busy = view.data.pending.some((q) => q.package === name) || !!p.running;
    return busy || p.last?.ok === false || p.note || error ? <div className="space-y-1 text-xs">
      {busy && <Badge tone="info">{p.running ?? "queued"}…</Badge>}
      {p.last?.ok === false && <p className="text-bad">{p.last.action}: {p.last.detail} ({when(p.last.at)})</p>}
      {p.note && <p className="text-wait">{p.note}</p>}
      <ErrorNote error={error} />
    </div> : null;
  }
  return <div className="space-y-2 rounded-md border border-line px-3 py-2">
    <PackageRow name={name} p={p} view={view.data} machine={machine} onResult={setError} tile />
    <ErrorNote error={error} />
  </div>;
}

/** Providers: what the software list's header used to say, kept visible above the tiles: how updates work there, and
 * a helper that's missing, a machine that hasn't reported, or a request it refused. */
export function SoftwareStatus({ machine, account }: { machine?: string; account?: string }) {
  const view = usePackages(true, machine, account);
  const refused = view.data?.refused;
  return <div className="space-y-1">
    <p className="flex items-center gap-1.5 text-xs text-muted"><PackageCheck size={13} aria-hidden="true" />Agent software: {subtitle(view.data, machine)}</p>
    {view.data && view.data.reported === false && <p className="text-xs text-wait">{machine} hasn't reported its agent software: update Rynhult there to manage it from here.</p>}
    {view.data && view.data.reported !== false && !view.data.helper && <p className="text-xs text-wait">{helperMissing(view.data, machine)}</p>}
    {refused && refused.at && Date.now() / 1000 - refused.at < 600 && <p role="alert" className="text-xs text-bad">{machine} refused a request: {refused.detail}</p>}
    <ErrorNote error={view.error} />
  </div>;
}

/** Providers: software without a provider tile (the model server, backup tools), so nothing becomes unreachable once
 * the provider CLIs moved onto their tiles. */
export function OtherSoftwareCard({ machine, account, providers }: { machine?: string; account?: string; providers: string[] }) {
  const view = usePackages(true, machine, account);
  const [errors, setErrors] = useState<Record<string, unknown>>({});
  const entries = view.data?.reported !== false ? Object.entries(view.data?.packages ?? {}).filter(([name]) => !providers.includes(name)) : [];
  if (!view.data || !entries.length) return null;
  const backup = entries.filter(([, p]) => p.group === "backup");
  const other = entries.filter(([, p]) => p.group !== "backup");
  const row = ([name, p]: [string, PackageState]) => <PackageRow key={name} name={name} p={p} view={view.data!} machine={machine}
    onResult={error => setErrors(previous => ({ ...previous, [name]: error }))} />;
  return <Card>
    <CardHeader title={machine ? `Other software on ${machine}` : "Other software"} icon={PackageCheck}
      subtitle="Not a provider sign-in: the local model server, and what Rynhult's backups use." />
    {Object.entries(errors).filter(([, error]) => error).map(([name, error]) => <div key={name} className="space-y-1 px-4 py-2">
      <p className="text-xs text-muted">{view.data?.packages[name]?.label ?? NAMES[name] ?? name}</p><ErrorNote error={error} /></div>)}
    {other.length > 0 && <ul className="divide-y divide-line" aria-label="Other software">{other.map(row)}</ul>}
    {backup.length > 0 && <>
      <h3 className="flex items-center gap-2 border-t border-line px-4 pb-1 pt-3 text-xs font-medium text-muted"><Archive size={13} aria-hidden="true" />Backup tools</h3>
      <ul className="divide-y divide-line" aria-label="Backup tools">{backup.map(row)}</ul>
    </>}
  </Card>;
}
