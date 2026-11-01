import {securityLink} from '../lib/security';
import { Feature } from "./Feature";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Download, Laptop, Plus, Trash2 } from "lucide-react";
import { Link } from "react-router-dom";
import { useState } from "react";
import { api, post } from "../lib/api";
import { MachineChecks, MachinePolicy, MachineState, RoleChoice, WorkstationSleep } from "./MachinePolicy";
import { providerShort, useInstance, useProviderRegistry } from "../lib/queries";
import { useConfirm } from "./confirm";
import { CopyBlock } from "./InstallSteps";
import { ago } from "../lib/format";
import { announce, Badge, Button, Card, CardHeader, Dialog, Dot, Empty, ErrorNote, Field, IconButton, Input, Note, Segmented, Select, TabPanel, Tabs } from "./ui";

export type NodeRow = {
  policy_revision?: string;
  public_git_backup?: boolean;
  downtime_policy?: { timezone: string; windows: { days: number[]; start: string; end?: string; until_reconnect?: boolean }[] } | null;
  downtime_until_reconnect?: boolean;
  planned_downtime?: { active: boolean; until_reconnect?: boolean; ends_at?: string | null };
  idle_sleep_minutes?: number | null;
  sleep?: { running_sessions: number; idle_sessions: { id: string; name: string }[]; paused_sessions: { id: string; name: string; stop_pending?: boolean }[] } | null;
  role?: string; offline_threshold_seconds: number; offline_since?: string | null; snoozed_until?: string | null; last_downtime_seconds?: number | null; offline_alert?: boolean;
  id: string; name: string; status: string; connected: boolean; last_seen: string | null;
  info: { os?: string; arch?: string; hostname?: string; version?: string; build?: string; wsl?: string; windows?: string }; installer?: string;
  wsl_isolation?: { interop_enabled: boolean | null; windows_drive_readable: boolean | null;
    accounts: Record<string, boolean | null>; warning: boolean; dismissed: boolean; dismissal_outdated?: boolean; revision: string } | null;
  system?: MachineSystem; update_available?: boolean; follow?: boolean; update?: { running?: boolean; ok?: boolean; installer_required?: boolean; detail?: string; recovery?: { command?: string; root_command?: string; detail: string } }; accounts: string[];
  software?: Record<string, { label?: string | null; installed?: string | null }> | null;  // as it last reported (connected only)
  logins?: Record<string, Record<string, boolean | number | null>>;  // per account: provider → logged in, checked_at
  machine_identity?: MachineIdentityState | null;
};
/** Its certificate for the public address (Remote access): identities on or off, and its newest usable leaf. */
export interface MachineIdentityState { enabled: boolean; issued: boolean; fingerprint?: string; expires_at?: string; rotating?: boolean; last_presented_at?: string | null; previous_authority?: boolean }
/** How its system reads (the server's describe_system): a Mac, plain Linux, Linux in WSL with its Windows, or unknown
 *  (a Linux node from before it said whether it is WSL). */
export interface MachineSystem { kind: "macos" | "linux" | "wsl" | "unknown"; label: string; wsl?: string; windows?: string | null }

/** A small badge that tells a WSL machine from a plain Linux one at a glance, with the full label beside it. */
export function SystemBadge({ system, arch, compact }: { system?: MachineSystem; arch?: string; compact?: boolean }) {
  if (!system) return null;
  const short = system.kind === "wsl" ? `WSL${system.wsl ?? ""}` : system.kind === "macos" ? "macOS" : system.kind === "linux" ? "Linux" : "unknown";
  const title = system.kind === "unknown" ? "This machine runs an older Rynhult that doesn't say whether it is WSL: update it" : system.label;
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <Badge tone={system.kind === "wsl" ? "info" : "neutral"} title={title}>{short}</Badge>
      {!compact && <span className="text-xs text-muted">{system.label}{arch ? ` · ${arch}` : ""}</span>}
    </span>
  );
}

type Added = NodeRow & {
  code: string; command: string; expires_in: number; trust: "system" | "ca"; verified: boolean; primary: string;
  linux: string | null; version: string; installers: { release?: string; windows?: string; mac?: string };
  prerelease?: boolean;
  from_source?: { windows?: string | null; mac?: string | null; windows_keepalive?: string };
};
type System = "linux" | "windows" | "mac";
const SYSTEMS = [{ id: "linux", label: "Linux" }, { id: "windows", label: "Windows (WSL)" }, { id: "mac", label: "Mac" }] as const;

const PREREQUISITES: Record<System, { guide: string; items: string[] }> = {
  linux: { guide: "/guide/User-Guide/Install#add-a-linux-machine", items: [
    "Debian or Ubuntu with systemd", "Python 3.12 or newer and curl", "Outbound HTTPS to your main Rynhult",
  ] },
  windows: { guide: "/guide/User-Guide/Install-Windows#before-you-start", items: [
    "Windows 11 x64 and an administrator account", "Virtualization enabled in BIOS/UEFI (Intel VT-x or AMD-V/SVM)",
    "At least 20 GB free disk space for WSL, Ubuntu and Rynhult", "WSL will be installed if missing; Windows may need one restart",
    "Rynhult keeps running only while you are signed in and the PC is awake", "Outbound HTTPS to your main Rynhult",
  ] },
  mac: { guide: "/guide/User-Guide/Install-macOS#before-you-start", items: [
    "macOS 13 or newer (Apple silicon or Intel)", "Homebrew installed", "Node.js 22 or newer for agents installed with npm",
    "Outbound HTTPS to your main Rynhult",
  ] },
};

function Installer({ href, file }: { href?: string; file: string }) {
  return href
    ? <a href={href} className="inline-flex items-center gap-1.5 font-medium text-accent underline underline-offset-2"><Download size={14} aria-hidden="true" />{file}</a>
    : <b>{file}</b>;
}

/** No installer for this version (before v1.0): set up WSL by hand on Windows, then one command, run on the new
 *  machine, installs Rynhult from this main's source and enrolls it (from_source, built by the server). */
function FromSource({ added, system }: { added: Added; system: "windows" | "mac" }) {
  const command = added.from_source?.[system];
  // By hand, the code is piped in too (--code -), so it is on no command line and not in sudo's log.
  const enroll = `echo ${added.code} | ${system === "windows" ? "sudo " : ""}${added.command.replace(` --code ${added.code} `, " --code - ")}`;
  const checks = added.trust === "ca" ? " It first checks this Rynhult's private certificate authority, which the command carries, against its fingerprint, and stops if they differ." : "";
  // Release candidates are server builds handed to testers privately: no Windows or Mac installer exists for them.
  const candidate = added.prerelease ? <p className="text-xs text-muted" data-testid="candidate-installer-note">Rynhult {added.version} is
    a release candidate for the server, without a {system === "windows" ? "Windows" : "Mac"} installer: use the steps below.</p> : null;
  if (!command) return (
    <>
      {candidate}
      <p>This build has no {system === "windows" ? "Windows" : "Mac"} installer, and this Rynhult can't build a command for it
        (it needs one plain https:// address). Install the node by hand (Guide → Install, <b>By hand</b>), then run:</p>
      <CopyBlock text={enroll} label="Copy the enroll command" />
    </>
  );
  return system === "windows" ? (
    <>
      {candidate}
      <p>This build has no Windows installer, so set up WSL first, then install from this Rynhult.</p>
      <ol className="list-decimal space-y-2 pl-5">
        <li>On <b>{added.name}</b>, open PowerShell as administrator and run the line below. Restart Windows if it asks.
          <CopyBlock text="wsl --install -d Ubuntu-24.04" label="Copy the WSL command" /></li>
        <li>Open <b>Ubuntu 24.04</b> from the Start menu. The first time, it asks for a new Linux user name and password: that password is the one <code>sudo</code> asks for.</li>
        <li>In the Ubuntu window, run:</li>
      </ol>
      <CopyBlock text={command} label="Copy the Ubuntu command" />
      <p className="text-xs text-muted">It installs Rynhult {added.version} from this Rynhult's source inside Ubuntu and enrolls the PC.{checks}</p>
      {added.from_source?.windows_keepalive && <section aria-label="Keep Rynhult running (optional)" className="space-y-2">
        <h3 className="font-medium">Optional: keep Rynhult running while you're signed in</h3>
        <p className="text-muted">Windows stops WSL, and Rynhult with it, a little while after the last Ubuntu window closes. The installer adds
          a task for this; without it, run the block below once in PowerShell as administrator, while signed in as the
          person whose PC this is. It adds the task <b>Rynhult node (WSL)</b>, which starts Ubuntu 24.04 in the background
          at each sign-in. Skip it to keep the Ubuntu window open instead.</p>
        <CopyBlock text={added.from_source.windows_keepalive} label="Copy the keep-running task" />
        <p className="text-xs text-muted">Do this right after enrolling, before any session runs on this PC. An administrator
          PowerShell also runs your PowerShell profile and modules, and agent sessions can change those through WSL interop.</p>
      </section>}
      <p className="text-xs text-muted">Already enrolled and only out of date? Use the update steps on its row in Host, not Add machine.</p>
    </>
  ) : (
    <>
      {candidate}
      <p>This build has no Mac installer, so install from this Rynhult instead.</p>
      <ol className="list-decimal space-y-2 pl-5">
        <li>On <b>{added.name}</b>, install Homebrew if it isn't there yet (<a href="https://brew.sh" className="text-accent underline underline-offset-2">brew.sh</a>).</li>
        <li>Open <b>Terminal</b> and run, as yourself (not with sudo):</li>
      </ol>
      <CopyBlock text={command} label="Copy the Mac command" />
      <p className="text-xs text-muted">It installs Rynhult {added.version} from this Rynhult's source and enrolls the Mac.{checks}
        {" "}A Mac is an additional machine only, and sessions on it run as the signed-in user.</p>
    </>
  );
}

/** What to run on the new machine, per system: the Linux one-liner, or the installer and the command to paste. */
function Enroll({ added }: { added: Added }) {
  const [system, setSystem] = useState<System>("linux");
  const release = added.installers.release;
  return (
    <div className="space-y-3 text-[13px]">
      <Tabs id="enroll-system" label="System" tabs={SYSTEMS} value={system} onChange={setSystem} />
      <TabPanel id="enroll-system" value={system} className="space-y-3">
      <section aria-label="Before you start" className="space-y-2">
        <h3 className="font-medium">Before you start</h3>
        <ul className="list-disc space-y-1 pl-5 text-muted">
          {PREREQUISITES[system].items.map(item => <li key={item}>{item}</li>)}
        </ul>
        <Link to={PREREQUISITES[system].guide} className="text-accent underline underline-offset-2">Installation guide</Link>
      </section>
      {system === "linux" && (added.linux ? (
        <>
          <p>On <b>{added.name}</b>, in a terminal (Debian or Ubuntu; it asks for your password for sudo):</p>
          <CopyBlock text={added.linux} label="Copy the Linux command" />
          <p className="text-xs text-muted">It installs Rynhult {added.version} from this Rynhult and enrolls the machine.
            {added.trust === "ca" ? " It first checks this Rynhult's private certificate authority against the fingerprint in the command, and stops if they differ." : ""} Needs curl{added.trust === "ca" ? " and openssl" : ""}.</p>
        </>
      ) : (
        <>
          <p>On <b>{added.name}</b>, install the Rynhult node (Guide → Install), then run as root:</p>
          <CopyBlock text={`sudo ${added.command}`} label="Copy the enroll command" />
        </>
      ))}
      {system !== "linux" && !(system === "windows" ? added.installers.windows : added.installers.mac) ? (
        <FromSource added={added} system={system} />
      ) : system !== "linux" && (
        <>
          <ol className="list-decimal space-y-2 pl-5">
            <li>On <b>{added.name}</b>, download and open{" "}
              {system === "windows" ? <Installer href={added.installers.windows} file={`Rynhult-Setup-${added.version}.exe`} />
                : <Installer href={added.installers.mac} file={`Rynhult-${added.version}.pkg`} />}
              {release ? <> (or the <a href={release} className="text-accent underline underline-offset-2">release page</a>)</> : null}.</li>
            <li>{system === "windows" ? <>Choose <b>Additional machine</b> and paste:</> : <>When it asks for the command from Add machine, paste:</>}</li>
          </ol>
          <CopyBlock text={added.command} label="Copy the enroll command" />
          <p className="text-xs text-muted">{system === "windows"
            ? "It sets up WSL2 with Ubuntu (it may restart once and continue at the next sign-in), then installs and enrolls the node inside it."
            : "A Mac is an additional machine only, and sessions on it run as the signed-in user."}</p>
        </>
      )}
      </TabPanel>
      <p className="text-xs text-muted">The code works once and expires in {Math.round(added.expires_in / 60)} minutes.
        {added.trust === "ca" ? " The fingerprint lets the machine check this Rynhult's private certificate authority." : added.verified ? " The machine checks this Rynhult's certificate the normal way." : " This Rynhult's certificate didn't verify from here: see Guide → Install (a private CA needs ACC_NODE_CA_FILE)."}</p>
      <p className="text-xs text-muted">Next, once it's connected: install and sign in its agent CLIs, step by step for {system === "mac" ? "a Mac" : system === "windows" ? "Windows (WSL)" : "Linux"}, in{" "}
        <Link to={`/guide/User-Guide/Connect-an-Agent#${system === "mac" ? "mac" : system === "windows" ? "windows-wsl" : "linux"}`} className="text-accent underline underline-offset-2">Guide → Connect an agent</Link>.</p>
    </div>
  );
}

/** A machine at a glance: its agent CLIs and, per agent account, which sign-ins work (Providers has the details). */
function Glance({ node }: { node: NodeRow }) {
  const registry = useProviderRegistry();
  const installed = Object.entries(node.software ?? {}).filter(([, p]) => p.installed);
  const logins = Object.entries(node.logins ?? {});
  if (!node.connected && !logins.length) return null;
  return (
    <div className="flex basis-full flex-wrap items-center gap-x-4 gap-y-1.5 pl-5 text-xs text-muted">
      {node.software ? (
        <span className="flex flex-wrap items-center gap-1.5"><span className="text-faint">CLIs</span>
          {installed.length ? installed.map(([name, p]) => <Badge key={name}>{p.label ?? name} {p.installed}</Badge>) : <span>none installed</span>}
        </span>
      ) : node.connected ? <span className="text-faint">CLIs: not reported (update Rynhult there)</span> : null}
      {logins.map(([account, providers]) => (
        <span key={account} className="flex flex-wrap items-center gap-1.5"><span className="text-faint">{account}</span>
          {Object.entries(providers).filter(([k]) => k !== "checked_at").map(([provider, ok]) => ok
            ? <span key={provider} className="inline-flex items-center gap-1"><Dot tone="ok" />{provider === "github" ? "GitHub" : providerShort(provider, registry.byId(provider))}</span>
            : <span key={provider} className="text-faint">{provider === "github" ? "GitHub" : providerShort(provider, registry.byId(provider))} not signed in</span>)}
        </span>
      ))}
      <Link to={`/providers?machine=${encodeURIComponent(node.name)}`} className="inline-flex pointer-coarse:min-h-11 items-center gap-1 font-medium text-accent underline underline-offset-2">
        Sign-ins and CLIs <ArrowRight size={12} aria-hidden="true" /><span className="sr-only"> on {node.name}</span></Link>
      {node.connected && <Link to={`/host?machine=${encodeURIComponent(node.name)}`} className="inline-flex pointer-coarse:min-h-11 items-center gap-1 font-medium text-accent underline underline-offset-2">
        Host details <ArrowRight size={12} aria-hidden="true" /><span className="sr-only"> of {node.name}</span></Link>}
    </div>
  );
}

function WslIsolation({ node }: { node: NodeRow }) {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const preference = useMutation({
    // A dismissal names the exposure on screen; the server refuses it if that or the saved choice already changed.
    mutationFn: (choice: {dismissed: boolean; expected_revision: string; accepted?: {interop_enabled: boolean | null; accounts: Record<string, boolean | null>}}) =>
      api<NodeRow>(`/api/nodes/${node.name}/wsl-isolation`, {method:"PUT", json:choice}),
    onSettled: () => queryClient.invalidateQueries({queryKey:["nodes"]}),
    onSuccess: (_data, choice) => {
      announce(choice.dismissed ? `WSL warning dismissed for ${node.name}` : `WSL warning restored for ${node.name}`);
    },
  });
  const state = node.wsl_isolation;
  if (!state) return null;
  const value = (enabled: boolean | null, yes: string, no: string) => enabled === null ? "unknown" : enabled ? yes : no;
  return <section aria-label={`WSL isolation on ${node.name}`} className="min-w-0 basis-full space-y-2 pl-5">
    {state.warning && !state.dismissed && <Note>Agent sessions on this machine can run Windows programs and read your Windows files as you.</Note>}
    <p className="text-xs text-muted">{node.connected ? "Reported" : "Last reported (machine offline)"} Windows interop: {value(state.interop_enabled, "on", "off")}
      {" · "}Windows drive access for agent accounts: {value(state.windows_drive_readable, "readable", "not readable")}.</p>
    {(state.interop_enabled === null || state.windows_drive_readable === null) &&
      <p className="text-xs text-muted">WSL isolation is not fully reported. Update this machine or reconnect; do not assume Windows access is blocked.</p>}
    {state.dismissed && <p className="text-xs text-muted">Windows access warning dismissed by an owner.</p>}
    {state.warning && !state.dismissed && state.dismissal_outdated &&
      <p className="text-xs text-muted">Windows access changed since an owner dismissed this warning, so it is shown again.</p>}
    <div className="flex flex-wrap items-center gap-3">
      <Link className="text-xs text-accent underline underline-offset-2" to="/guide/User-Guide/Install-Windows#isolation-for-dedicated-wsl-nodes">WSL isolation guide</Link>
      {state.warning && !state.dismissed && <Button size="sm" disabled={preference.isPending} aria-label={`Dismiss WSL warning for ${node.name}`} onClick={async () => {
        // Keep the revision and exposure the owner reviewed, even during polling.
        const expected_revision = state.revision, accepted = {interop_enabled:state.interop_enabled, accounts:state.accounts};
        if (await confirm({title:`Accept Windows access on ${node.name}?`,
          body:"This only hides the warning; it does not block Windows access. Accept this risk only for a machine where agents are allowed to use its Windows account and files. The choice is recorded in Audit.",
          confirm:"Accept risk"})) preference.mutate({dismissed:true, expected_revision, accepted});
      }}>Dismiss warning</Button>}
      {state.dismissed && <Button size="sm" disabled={preference.isPending} aria-label={`Show WSL warning for ${node.name}`} onClick={() => preference.mutate({dismissed:false, expected_revision:state.revision})}>Show warning again</Button>}
    </div>
    <ErrorNote error={preference.error} />
  </section>;
}

const SOON_MS = 3 * 24 * 3600 * 1000;

/** Its identity for the public address, read-only: Remote access turns identities on, removing the machine revokes. */
function MachineIdentity({ node }: { node: NodeRow }) {
  const state = node.machine_identity;
  if (!state || (!state.enabled && !state.issued)) return null;
  const expires = state.expires_at ? new Date(state.expires_at) : null;
  const soon = !!expires && !state.rotating && expires.getTime() - Date.now() < SOON_MS;
  const day = (date: Date) => date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  let text: string;
  if (!state.enabled) text = "Public identity off. Its last certificate is no longer admitted.";
  else if (!state.issued) text = node.connected ? "Public identity: being issued over its link." : "Public identity: issued when it next connects.";
  else text = `Public identity valid until ${day(expires!)}${state.rotating ? " · new certificate issued, switching after its next connection"
    : state.previous_authority ? ` · from the previous device authority, replaced ${node.connected ? "over its link" : "when it next connects"}` : ""}`
    + ` · ${state.last_presented_at ? `last used through the public address ${ago(state.last_presented_at)}` : "not yet used through the public address"}.`;
  return <section aria-label={`Public identity of ${node.name}`} className="flex min-w-0 basis-full flex-wrap items-center gap-2 pl-5">
    <p className="text-xs text-muted">{text}</p>
    {state.enabled && soon && <Badge tone="wait">{node.connected ? "Renewal overdue: check Remote access" : "Expires soon: reconnect this machine"}</Badge>}
    <Feature name="remote_access"><Link className="text-xs text-accent underline underline-offset-2" to="/settings/remote-access">Remote access</Link></Feature>
  </section>;
}

/** Update a machine (or "all") from this main Rynhult, after the one confirmation that says what happens. */
export function useMachineUpdate() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const update = useMutation({
    mutationFn: async (name: string) => {
      const result = await post<Record<string, { queued: boolean; error?: string }>>(`/api/updates/machines/${name}`);
      const failures = Object.entries(result).filter(([, value]) => !value.queued);
      if (failures.length) throw new Error(failures.map(([name, value]) => `${name}: ${value.error}`).join('; '));
      return result;
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["nodes"] }),
  });
  const updateConfirmed = async (name: string) => {
    if (await confirm({ title: name === 'all' ? 'Update all machines?' : `Update ${name}?`, body: 'Sync Rynhult from this main Rynhult. Machine links and terminals disconnect briefly; agents keep running in tmux. Macs use the installer link.', confirm: 'Update' })) update.mutate(name);
  };
  return { update, updateConfirmed };
}

/** This machine or another one (Host, Providers): enrolled machines, each with its connection dot. */
export function MachinePicker({ machines, value, onChange }: { machines: NodeRow[]; value: string | undefined; onChange: (id: string) => void }) {
  const { host } = useInstance();
  const mainLabel = `${host} · main`;
  const selected = machines.find(n => n.name === value);
  const select = <label className="flex min-w-0 max-w-full flex-wrap items-center gap-2 text-xs text-muted">Machine
    <Select aria-label="Machine" className="min-w-0 max-w-full" value={value ?? 'local'} onChange={e => onChange(e.target.value)}>
      <option value="local">{mainLabel}</option>
      {machines.map(n => <option key={n.id} value={n.name}>{n.name} ({n.planned_downtime?.active ? 'Planned downtime' : n.connected ? 'connected' : 'offline'}){n.system ? ` · ${n.system.label}` : ''}</option>)}
    </Select>
    <span className="basis-full break-all">{selected ? `${selected.name} · ${selected.planned_downtime?.active ? 'Planned downtime' : selected.connected ? 'Connected' : 'Offline'}` : mainLabel}</span>
  </label>;
  if (machines.length > 4) return select;
  return <><div className="md:hidden">{select}</div><div className="hidden md:block">
    <Segmented label="Machine" value={value ?? "local"} onChange={onChange}
      options={[{ id: "local", label: <span className="inline-flex items-center gap-1.5">{host} <Badge>main</Badge></span> }, ...machines.map((n) => ({ id: n.name,
        label: <span className="inline-flex min-w-0 max-w-full flex-wrap items-center gap-1.5 break-all"><Dot tone={n.planned_downtime?.active ? "neutral" : n.connected ? "ok" : "bad"} />{n.name}<span className="sr-only">{n.planned_downtime?.active ? " (Planned downtime)" : n.connected ? " (connected)" : " (offline)"}, </span><SystemBadge system={n.system} compact /></span> }))]} />
  </div></>;

}

export const useNodes = (enabled = true) =>
  useQuery({ queryKey: ["nodes"], queryFn: () => api<NodeRow[]>("/api/nodes"), enabled, refetchInterval: 15000 });

/** Other machines this Rynhult runs sessions on (owners). Each connects out to here; nothing is opened on them. */
export function Machines() {
  const queryClient = useQueryClient();
  const nodes = useNodes();
  const confirm = useConfirm();

  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState("auto");
  const [editing, setEditing] = useState<NodeRow | null>(null);
  const [added, setAdded] = useState<Added | null>(null);
  const add = useMutation({
    mutationFn: () => post<Added>("/api/nodes", { name: name.trim().toLowerCase(), role: role === "auto" ? null : role }),
    onSuccess: (data) => { setAdded(data); queryClient.invalidateQueries({ queryKey: ["nodes"] }); },
  });
  const remove = useMutation({
    mutationFn: (n: string) => post(`/api/nodes/${n}/remove`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["nodes"] }),
  });
  const { update, updateConfirmed } = useMachineUpdate();
  const follow = useMutation({ mutationFn: ({name, enabled}: {name: string; enabled: boolean}) => post(`/api/updates/machines/${name}/follow`, {enabled}),
    onMutate: ({name, enabled}) => {
      const before = queryClient.getQueryData<NodeRow[]>(['nodes']);
      queryClient.setQueryData<NodeRow[]>(['nodes'], rows => rows?.map(row => row.name === name ? {...row, follow:enabled} : row));
      return {before};
    },
    onError: (_error, _variables, context) => { if (context?.before) queryClient.setQueryData(['nodes'], context.before); },
    onSettled: () => queryClient.invalidateQueries({queryKey: ["nodes"]}) });
  const close = () => { setAdding(false); setAdded(null); setName(""); setRole("auto"); add.reset(); };
  return (
    <Card>
      <CardHeader title="Machines" icon={Laptop}
        action={<div className="flex gap-2"><Button size="sm" disabled={update.isPending || !nodes.data?.some(n => n.connected && n.info.os !== 'darwin' && n.update_available)} onClick={() => updateConfirmed('all')}>Update all</Button><Button size="sm" onClick={() => setAdding(true)}><Plus size={14} aria-hidden="true" /> Add machine</Button></div>} />
      <div className="px-4 pb-3"><MachineChecks /></div>
      {!nodes.data ? <div className="p-4"><ErrorNote error={nodes.error} /></div>
        : !nodes.data.length ? <Empty icon={Laptop}>Only this machine. Add another to run sessions there from here (Guide → Install).</Empty>
        : (
          <div className="divide-y divide-line">
            {nodes.data.map((n) => (
              <div key={n.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-[13px]">
                <Dot tone={n.connected ? "ok" : n.offline_alert ? "wait" : "neutral"} />
                <span className="font-medium">{n.name}</span>
                <span className="text-xs text-muted">
                  {n.status === "pending" ? "waiting to be enrolled" : <MachineState node={n} />}
                  {n.info.hostname ? ` · ${n.info.hostname}` : ""}
                </span>
                {n.status === "active" && <SystemBadge system={n.system} arch={n.info.arch} />}
                <Badge>{n.role === 'workstation' ? 'Workstation' : n.role === 'always_on' ? 'Always-on' : 'OS default at enrollment'}</Badge>
                {n.public_git_backup && <Badge>Public git backup allowed</Badge>}
                <Button size="sm" aria-label={`Machine settings for ${n.name}`} onClick={() => setEditing(n)}>Machine settings</Button>
                {n.downtime_policy?.windows.filter(window => window.until_reconnect).map((window, index) => <span key={index} className="text-xs text-muted">
                  Planned downtime from {window.start} until it reconnects · {n.downtime_policy!.timezone} · {window.days.map(day => ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][day]).join(', ')}
                </span>)}
                {n.last_downtime_seconds != null && <span className="text-xs text-muted">Last downtime: {Math.round(n.last_downtime_seconds / 60)} min</span>}
                {n.accounts.length > 0 && <span className="flex flex-wrap gap-1">{n.accounts.map((a) => <Link className="inline-flex min-h-11 min-w-11 items-center justify-center" key={a} to={securityLink({kind:"account",machine:n.name,account:a})} aria-label={`Security for ${a} on ${n.name}`}><Badge tone="neutral">{a}</Badge></Link>)}</span>}
                {n.accounts.length > 0 && <span className="flex flex-wrap gap-1">{n.accounts.map((a) => <Link className="inline-flex min-h-11 min-w-11 items-center" key={a} aria-label={`Allowed folders · ${a} on ${n.name}`} to={`/settings/security/folders?machine=${encodeURIComponent(n.name)}&account=${encodeURIComponent(a)}`}><span className="text-xs underline">Allowed folders · {a}</span></Link>)}</span>}
                {n.status === 'active' && <>
                  <span className="text-xs text-muted">Rynhult {n.info.version || 'unknown version'} · {n.info.build ? `build ${n.info.build.slice(0, 12)}` : 'unknown build'}</span>
                  {n.update_available && n.info.version && <Badge tone="wait">Update available</Badge>}
                  {n.info.os === 'darwin'
                    ? n.installer && <a href={n.installer} className="text-accent underline underline-offset-2">Update this Mac with the new installer</a>
                    : <>
                      {n.update_available && <Button size="sm" aria-label={`Update ${n.name}`} disabled={!n.connected || update.isPending || !!n.update?.running} onClick={() => updateConfirmed(n.name)}>Update</Button>}
                      <label className="flex items-center gap-2 text-xs text-muted"><input type="checkbox" aria-label={`Follow the main Rynhult automatically on ${n.name}`} checked={!!n.follow} disabled={follow.isPending} onChange={e => follow.mutate({name:n.name,enabled:e.target.checked})} />Follow the main Rynhult automatically</label>
                    </>}
                  {n.update && n.info.os !== 'darwin' && <span role="status" className="min-w-0 max-w-full break-words text-xs text-muted">{n.update.installer_required ? n.update.detail : n.update.running ? 'Updating…' : n.update.detail ? (n.update.ok ? n.update.detail : `Update failed: ${n.update.detail}`) : (n.update.ok ? 'Updated' : 'Update failed; check the machine helper log')}</span>}
                </>}
                <IconButton icon={Trash2} label={`Remove ${n.name}`} className="ml-auto"
                  onClick={async () => { if (await confirm({ title: `Remove ${n.name}?`, body: "It disconnects at once and can't reconnect; add it again to use it. Archive its projects first.", confirm: "Remove", danger: true })) remove.mutate(n.name); }} />
                {n.update?.recovery && <div className="min-w-0 basis-full space-y-2 pl-5">
                  <p className="text-xs text-muted">{n.update.recovery.detail}</p>
                  {n.update.recovery.command && <CopyBlock text={n.update.recovery.command} label={`Copy the update command for ${n.name}`} />}
                  {n.update.recovery.root_command && <>
                    <p className="text-xs text-muted">Or, in this node&apos;s Ubuntu/WSL terminal as root (run <code>sudo -i</code> first):</p>
                    <CopyBlock text={n.update.recovery.root_command} label={`Copy the in-Ubuntu root command for ${n.name}`} />
                  </>}
                  <Link className="text-xs text-accent underline underline-offset-2" to="/guide/User-Guide/Nodes#update-an-older-machine">Recovery steps</Link>
                </div>}
                {n.status === "active" && <Glance node={n} />}
                {n.status === "active" && <WslIsolation node={n} />}
                {n.status === "active" && <MachineIdentity node={n} />}
                {n.role === 'workstation' && <div className="min-w-0 basis-full pl-5"><WorkstationSleep node={n} /></div>}
              </div>
            ))}
          </div>
        )}
      <div className="px-4"><ErrorNote error={update.error || follow.error} /></div>
      {remove.error && <div className="px-4 pb-3"><ErrorNote error={remove.error} /></div>}
      {editing && <MachinePolicy node={editing} onClose={() => setEditing(null)} />}
      <Dialog open={adding} onClose={close} title="Add a machine" wide={!!added}
        description={added ? "Run what the tab for its system shows on that machine within 10 minutes." : "Name it, then follow the steps for its system: one command on Linux, a few more on a Windows PC (WSL) or a Mac (Guide → Install)."}
        footer={added ? <Button variant="primary" onClick={close}>Done</Button>
          : <><Button onClick={close}>Cancel</Button><Button variant="primary" disabled={!/^[a-z0-9][a-z0-9-]{0,39}$/.test(name.trim().toLowerCase()) || add.isPending} onClick={() => add.mutate()}>Create enrollment code</Button></>}>
        {!added ? (
          <div className="space-y-3">
            <p className="text-[13px] text-muted">Other computers this Rynhult runs sessions on: a Linux server, a Windows PC (WSL2) or a Mac.</p>
            <Field label="Name" hint="Lowercase letters, digits and dashes, e.g. gaming-pc. Sessions and projects show it.">
              <Input value={name} autoFocus onChange={(e) => setName(e.target.value)} placeholder="gaming-pc" />
            </Field>
            <RoleChoice value={role} onChange={setRole} auto />
            <ErrorNote error={add.error} />
          </div>
        ) : <Enroll added={added} />}
      </Dialog>
    </Card>
  );
}
