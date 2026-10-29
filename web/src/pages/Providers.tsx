import { useRovingFocus } from "../components/keyboard";
import { Feature } from "../components/Feature";
import { desktopBuild } from "../lib/desktop";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { CheckCircle2, ExternalLink, GitBranch, KeyRound, RefreshCw, XCircle } from "lucide-react";
import { api } from "../lib/api";
import { ago, until } from "../lib/format";
import type { AccountHealth, ProviderHealth, ProviderInfo } from "../lib/types";
import { Badge, Button, Card, CardHeader, ErrorNote, Meta, Meter, Page, PageHeader, Segmented } from "../components/ui";
import { useConfirm } from "../components/confirm";
import { EndpointsCard } from "../components/Endpoints";
import { InstallSteps } from "../components/InstallSteps";
import { onMachine, OtherSoftwareCard, type PackageState, ProviderSoftware, SoftwareStatus, usePackageAction, usePackages } from "../components/Packages";
import { MachinePicker, SystemBadge, useNodes } from "../components/Machines";
import { type Provider, ReloginDialog } from "./Relogin";
import { useInstance, useProviderRegistry, useRole } from "../lib/queries";
import { ProviderMark } from "../components/session-bits";
import { ToolsCard } from "../components/Tools";
import { type ModelRow, ModelGroups } from "../components/ModelGroups";

interface Obs { value: any; source: string; observed_at: number }
interface ProvidersView { observations: Record<string, Obs>; links: Record<string, string>; not_exposed: string[] }
type ModelCatalog = Record<string, unknown> & { claude?: ModelRow[]; codex?: ModelRow[]; fetched_at?: number };

const age = (o?: Obs) => (o ? ago(new Date(o.observed_at * 1000).toISOString()) : "never");
const days = (epoch?: number) => (epoch ? (epoch * 1000 - Date.now()) / 86400000 : null);
const windowLabel = (minutes?: number) => (minutes ? (minutes >= 1440 ? `${Math.round(minutes / 1440)}-day window` : `${Math.round(minutes / 60)}-hour window`) : "Window");

function ProviderSection({ id, title, scope, children }: { id: string; title: string; scope: string; children: React.ReactNode }) {
  return <section id={`provider-${id}`} tabIndex={-1} className="min-w-0 scroll-mt-4 space-y-4">
    <div><h2 className="text-sm font-semibold text-fg">{title}</h2><p className="mt-1 text-xs text-muted">{scope}</p></div>
    {children}
  </section>;
}

function Usage({ label, percent, resets, source }: { label: string; percent?: number | null; resets?: number | string | null; source: string }) {
  if (percent === undefined || percent === null) return <p className="text-xs text-faint">{label}: not reported yet</p>;
  const epoch = typeof resets === "string" ? Date.parse(resets) / 1000 : resets;
  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-x-2 text-xs">
        <span className="text-muted">{label}</span>
        <span className="tnum text-fg">{Math.round(percent)}%<span className="text-faint">{epoch ? ` · resets ${until(epoch)}` : ""}</span></span>
      </div>
      <Meter value={percent} label={`${label} used`} />
      <p className="mt-1 text-[11px] text-faint">{source}</p>
    </div>
  );
}

function AccountRow({ label, title, status, plan, action, details, children }: {
  label: string; title: React.ReactNode; status: React.ReactNode; plan?: React.ReactNode;
  action?: React.ReactNode; details?: React.ReactNode; children?: React.ReactNode;
}) {
  return <article aria-label={`${label} account`} className="min-w-0 space-y-3 p-4">
    <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
      <h3 className="flex min-w-0 flex-wrap items-center gap-2 font-medium">{title}</h3>
      <div className="text-xs">{status}</div>
      {plan && <div className="min-w-0 text-xs">{plan}</div>}
      <div className="ml-auto">{action}</div>
    </div>
    {children}
    {details && <details className="group">
      <summary className="cursor-pointer rounded-md py-1 text-xs font-medium text-accent hover:underline">
        Account details<span className="sr-only"> for {label}</span>
      </summary>
      <div className="mt-2 space-y-3">{details}</div>
    </details>}
  </article>;
}

/** Where a sign-in check stands (UX review PV-2): never an open-ended "Checking…" for a CLI Rynhult hasn't looked at. */
export type CheckState = "loading" | "failed" | "done";
function LoginState({ ok, ready, check }: { ok?: boolean; ready?: boolean; check: CheckState }) {
  if (ok === undefined) {
    if (check === "loading") return <span className="text-muted">Checking…</span>;
    if (check === "failed") return <span className="text-wait" title="The sign-in check didn't answer; select Check now to try again">Couldn't check</span>;
    return <span className="text-faint" title="Rynhult hasn't checked this sign-in yet; select Check now">Not checked yet</span>;
  }
  // `ready`: a CLI with no sign-in of its own (OpenCode): it either answers or it doesn't
  return ok ? <span className="inline-flex items-center gap-1.5 text-fg"><CheckCircle2 size={14} className="text-ok" aria-hidden="true" /> {ready ? "Ready" : "Logged in"}</span>
    : <span className="inline-flex items-center gap-1.5 font-medium text-bad"><XCircle size={14} aria-hidden="true" /> {ready ? "Not responding" : "Logged out"}</span>;
}

/** A CLI missing on the chosen machine: owners install it there, or, where that machine can't (a Mac, a machine
 * without its helper), get the provider's own steps, Check now and sign in from this card. Never a button the machine
 * would refuse. */
function NotInstalled({ id, machine, owner, entry, helper, kind }: { id: string; machine?: string; owner: boolean; entry?: PackageState; helper?: boolean; kind?: string | null }) {
  const { host } = useInstance();
  const install = usePackageAction(machine);
  const queryClient = useQueryClient();
  if (owner && entry?.installable === false && entry.guide) {
    const check = () => {
      if (kind === "mac" || helper) install.mutate({ name: id, action: "check" });
      queryClient.invalidateQueries({ queryKey: ["provider-registry"] });
      queryClient.invalidateQueries({ queryKey: ["accounts"] });
    };
    return (
      <div className="space-y-3">
        <p className="text-xs text-muted">Not installed on {machine ?? host} yet, and Rynhult can't install it there. Install it by hand:</p>
        <InstallSteps guide={entry.guide} machine={machine} onCheck={check} checking={install.isPending || !!entry.running} onCard />
        <ErrorNote error={install.error} />
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-3">
      <p className="flex-1 text-xs text-muted">Not installed on {machine ?? host} yet{owner ? "" : ": ask an owner to install it"}.</p>
      {owner && <Button size="sm" disabled={install.isPending || install.isSuccess} onClick={() => install.mutate({ name: id, action: "install" })}>
        {install.isSuccess ? "Installing…" : "Install"}</Button>}
      <ErrorNote error={install.error} />
    </div>
  );
}

/** Any provider without a bespoke card: login, account and plan as its CLI reports them, re-login, usage link. */
function ProviderCard({ info, health, account, onRelogin, usage, machine, installedThere, check, notInstalled, software }: { info: ProviderInfo; health?: ProviderHealth; account: string; onRelogin: () => void; usage?: Obs; machine?: string; installedThere?: boolean; software?: React.ReactNode; check: CheckState; notInstalled: React.ReactNode }) {
  const credits = machine ? undefined : usage?.value?.credits;  // usage is this machine's sessions' only
  const orgs = (health?.organizations as string[] | undefined) ?? usage?.value?.organizations;
  const installed = machine ? installedThere !== false : info.installed !== false;
  const billing = info.usage_url ? <a className="inline-flex pointer-coarse:min-h-11 items-center gap-1.5 text-xs text-accent" href={info.usage_url} target="_blank" rel="noreferrer">
    <ExternalLink size={12} aria-hidden="true" /> Usage and billing</a> : null;
  return (
    <AccountRow label={info.label} title={<><ProviderMark provider={info.id} />
        {info.label}{info.preview && <Badge tone="wait">preview</Badge>}</>}
        status={installed ? <LoginState ok={health?.logged_in} ready={!info.login} check={check} /> : <span className="text-faint">Not installed for {account}</span>}
        plan={(health?.plan ?? health?.subscription) ? <span className="flex flex-wrap items-center gap-1.5"><Badge>{String(health?.plan ?? health?.subscription)}</Badge>
          {orgs?.length ? <span className="text-xs text-muted">via {orgs.join(", ")}</span> : null}</span> : undefined}
        action={installed && info.login ? <Button size="sm" disabled={check !== "done"} onClick={onRelogin}><KeyRound size={13} aria-hidden="true" /> {health?.logged_in ? "Re-login" : "Log in"}</Button> : undefined}
        details={installed ? <><Meta items={[
            ["Account", health?.email ?? "—"],
            ...(Array.isArray(health?.providers) ? [["Connected", (health.providers as string[]).length
              ? <span className="flex flex-wrap gap-1.5">{(health.providers as string[]).map((p) => <Badge key={p}>{p}</Badge>)}</span>
              : <span className="text-muted">Free models only</span>] as [string, React.ReactNode]] : []),
          ]} />
          {!info.login && info.login_hint && <p className="text-xs text-muted">{info.login_hint}</p>}
          {billing}
        </> : billing}>
        {health?.error && <p className="text-xs text-wait">{String(health.error)}</p>}
        {software}
        {installed && credits && (
          <div className="mt-4">
            {credits.unlimited ? <p className="text-xs text-muted">Unlimited AI credits this cycle</p> : (
              <Usage label={`AI credits this cycle · ${Number(credits.used ?? 0).toLocaleString()} of ${Number(credits.entitlement ?? 0).toLocaleString()}${credits.overage_permitted ? " (overage allowed)" : ""}`}
                percent={100 - (credits.percent_remaining ?? 100)} resets={usage?.value?.resets_at}
                source={`GitHub Copilot account (internal API) · ${age(usage)}`} />
            )}
          </div>
        )}
        {!installed && notInstalled}
    </AccountRow>
  );
}

export function Providers() {
  const instance = useInstance();
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const { owner, username } = useRole();
  const { hash, key, search } = useLocation();
  const sectionLinks = useRef<HTMLDivElement>(null);
  const sectionKeys = useRovingFocus(sectionLinks, { selector: "a", orientation: "horizontal" });
  const [catalogOpen, setCatalogOpen] = useState(hash === "#provider-models");
  useEffect(() => {
    if (hash === "#provider-models") setCatalogOpen(true);
    // #provider-software (the old Agent software section): its controls are on each provider's tile now.
    const target = document.getElementById(hash === "#provider-software" ? "provider-sign-ins" : hash.slice(1));
    target?.scrollIntoView({ block: "start" });
    target?.focus({ preventScroll: true });
  }, [hash, key, owner]);
  const [relogin, setRelogin] = useState<{ provider: Provider; account: string; machine?: string } | null>(null);
  const [account, setAccount] = useState<string | undefined>(undefined);  // undefined: the default agent account
  const [params, setParams] = useSearchParams();
  const machine = params.get("machine") || undefined;  // undefined: this machine
  const nodes = useNodes(owner);
  const machines = (nodes.data ?? []).filter((n) => n.status === "active");
  const pick = (id: string) => { setAccount(undefined); setParams(id === "local" ? {} : { machine: id }, { replace: true }); };
  const scope = [account ? `account=${encodeURIComponent(account)}` : "", onMachine(machine, "")].filter(Boolean).join("&");
  const view = useQuery({ queryKey: ["providers"], queryFn: () => api<ProvidersView>("/api/providers"), refetchInterval: 60000 });
  const registry = useProviderRegistry();
  const others = desktopBuild ? [] : registry.list.filter((p) => p.id !== "claude" && p.id !== "codex");
  const accounts = useQuery({ queryKey: ["accounts", machine ?? "local", account ?? ""], queryFn: () => api<AccountHealth>(`/api/accounts?${scope}`), refetchInterval: 60000 });
  const there = usePackages(owner && !desktopBuild, machine, accounts.data?.account);  // which CLIs that machine has, and what it can install
  const missing = (id: string) => desktopBuild ? <p className="text-xs text-muted">Use Install or update CLI above, then check this account again.</p> : <NotInstalled id={id} machine={machine} owner={owner} entry={there.data?.packages[id]}
    helper={there.data?.helper} kind={there.data?.kind} />;
  const models = useQuery({ queryKey: ["models"], queryFn: () => api<ModelCatalog>("/api/models") });
  const refresh = useMutation({
    mutationFn: async () => Promise.all([api<ProvidersView>("/api/providers?refresh=1"), api<AccountHealth>(`/api/accounts?refresh=1&${scope}`)]),
    onSuccess: ([p, a]) => { queryClient.setQueryData(["providers"], p); queryClient.setQueryData(["accounts", machine ?? "local", account ?? ""], a);
      queryClient.invalidateQueries({ queryKey: ["packages"] }); },
  });
  const o = view.data?.observations ?? {};
  const statusline = o["claude.rate_limits"];
  const cache = o["claude.usage_cache"]?.value;
  const codexUsage = o["codex.rate_limits"]?.value;
  // The one installed check for every card: on another machine, what its packages report; here, the registry's answer.
  const installedThere = (id: string) => machine && there.data?.reported ? !!there.data.packages[id]?.installed : undefined;
  const isInstalled = (id: string) => machine ? installedThere(id) !== false : registry.list.find((p) => p.id === id)?.installed !== false;
  const claudeInstalled = isInstalled("claude");
  const codexInstalled = isInstalled("codex");
  // Each provider CLI's install, update and auto-update controls sit on its own tile (UX follow-up to W11).
  const software = (id: string) => owner && !desktopBuild
    ? <ProviderSoftware name={id} machine={machine} account={accounts.data?.account} tileOffersInstall={!isInstalled(id)} /> : null;
  const agentAccount = accounts.data?.account ?? instance.agent_user;
  const usageAccount = accounts.data?.default_account ?? instance.agent_user;
  const usageHere = !machine && agentAccount === usageAccount;
  const check: CheckState = accounts.data ? "done" : accounts.error ? "failed" : "loading";
  const claude = accounts.data?.claude;
  const codex = accounts.data?.codex;
  // The app-server's usage reading is the main Rynhult's sessions': on another machine the plan is only what its login says.
  const codexPlan = usageHere ? codexUsage?.plan ?? codex?.plan : codex?.plan;
  const github = accounts.data?.github;
  const left = days(claude?.login_expires_at);
  const tier = claude?.rate_limit_tier?.includes("20x") ? " 20x" : claude?.rate_limit_tier?.includes("5x") ? " 5x" : "";
  const selectedMachine = machine ?? instance.host;
  // Read as a sentence (first-run review): the machine, then the agent account its sessions run as.
  const selectedScope = `On ${selectedMachine}${machine ? "" : " (the main Rynhult)"}, for sessions running as ${agentAccount}`;
  const requestLogin = async (provider: Provider) => {
    const label = provider === "github" ? "GitHub" : registry.byId(provider)?.label ?? provider;
    if (await confirm({ title: `Sign in again as ${agentAccount} on ${selectedMachine}?`,
      body: `${label} will start its login on ${selectedMachine} as ${agentAccount}.${provider === "codex"
        ? " Codex signs this account out immediately, so running Codex sessions lose access until you finish signing in." : ""}`,
      confirm: "Start re-login", danger: provider === "codex" })) setRelogin({ provider, account: agentAccount, machine });
  };
  const sections = [["sign-ins", "Sign-ins and usage"], ...(!desktopBuild ? [
    ["tools", "Tools"],
    ...(owner && !machine ? [["connections", "Model connections"]] : []),
  ] : []), ["models", "Available models"]];
  return (
    <Page>
      <PageHeader title="Providers" description="Sign-ins, plan usage and agent software."
        meta={accounts.data?.checked_at ? <span>Logins checked {ago(new Date(accounts.data.checked_at * 1000).toISOString())} (every 15 minutes)</span> : undefined}
        actions={<Button onClick={() => refresh.mutate()} disabled={refresh.isPending}><RefreshCw size={14} className={refresh.isPending ? "animate-spin" : ""} aria-hidden="true" /> Check now</Button>} />
      <div ref={sectionLinks} role="group" aria-label="Provider sections" onKeyDown={sectionKeys}
        className="inline-flex max-w-full flex-wrap items-center gap-1 rounded-md border border-line bg-panel p-0.5 text-[12.5px]">
        {sections.map(([id, title]) => {
          const active = (hash === "#provider-software" ? "sign-ins" : hash.replace("#provider-", "")) === id || (!hash && id === "sign-ins");
          return <Link key={id} to={{ search, hash: `#provider-${id}` }} aria-current={active ? "location" : undefined}
            className={`inline-flex min-h-8 pointer-coarse:min-h-11 min-w-0 max-w-full items-center rounded px-2.5 py-1 ${active ? "bg-panel-3 font-medium text-fg" : "text-muted hover:text-fg"}`}>{title}</Link>;
        })}
      </div>

      {(machines.length > 0 || machine || (accounts.data?.accounts?.length ?? 0) > 1) && (
        <div className="mb-4 flex flex-wrap items-center gap-3">
          {(machines.length > 0 || machine) && (
            <MachinePicker machines={machines} value={machine} onChange={pick} />
          )}
          {(accounts.data?.accounts?.length ?? 0) > 1 && (
            <Segmented label="Agent account" value={accounts.data?.account ?? ""} onChange={(id) => setAccount(id === accounts.data?.default_account ? undefined : id)}
              options={(accounts.data?.accounts ?? []).map((a) => ({ id: a, label: a }))} />
          )}
        </div>
      )}
      {machine && (() => { const n = machines.find((m) => m.name === machine);
        return n?.system ? <p className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted">{machine}: <SystemBadge system={n.system} arch={n.info.arch} /></p> : null; })()}
      <span role="status" className="sr-only" data-testid="providers-check-announcer">{refresh.isSuccess ? "Sign-ins and usage checked." : ""}</span>
      {refresh.error && <ErrorNote error={refresh.error} title="Couldn't check again. Showing the last known status." onRetry={() => refresh.mutate()} />}
      {view.error && view.data && <ErrorNote error={view.error} title={`Couldn't refresh the providers. Showing data from ${new Date(view.dataUpdatedAt).toLocaleString()}.`} onRetry={() => view.refetch()} />}
      {view.error && !view.data && <ErrorNote error={view.error} onRetry={() => view.refetch()} />}
      {accounts.error && accounts.data && <ErrorNote error={accounts.error} title={`Couldn't refresh the agent accounts. Showing data from ${new Date(accounts.dataUpdatedAt).toLocaleString()}.`} onRetry={() => accounts.refetch()} />}
      {machine && accounts.error ? <div className="mb-4"><ErrorNote error={accounts.error} /></div> : null}
      <ProviderSection id="sign-ins" title="Sign-ins and usage" scope={`Sign-ins: ${selectedScope.charAt(0).toLowerCase()}${selectedScope.slice(1)}. Plan usage: sessions running as ${usageAccount} on ${instance.host}, each reading with its source and age.`}>
      {!desktopBuild && owner && <SoftwareStatus key={`${machine ?? "local"}:${agentAccount}`} machine={machine} account={accounts.data?.account} />}
      <Card><div className="divide-y divide-line">
        <AccountRow label="Claude Code" title={<><ProviderMark provider="claude" />Claude Code</>}
          status={claudeInstalled ? <LoginState ok={claude?.logged_in} check={check} /> : <span className="text-faint">Not installed for {agentAccount}</span>}
          plan={claude?.subscription ? <Badge>{claude.subscription}{tier}</Badge> : undefined}
          action={claudeInstalled ? <Button size="sm" disabled={!accounts.data?.account || !!accounts.error} onClick={() => requestLogin("claude")}><KeyRound size={13} aria-hidden="true" /> Re-login</Button> : undefined}
          details={claudeInstalled ? <><Meta items={[
              ["Account", claude?.email ?? "—"],
              ["Login expires", claude?.login_expires_at
                ? <span className={left !== null && left < 2 ? "text-bad" : left !== null && left < 7 ? "text-wait" : undefined}>
                    {new Date(claude.login_expires_at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" })} · {left?.toFixed(0)} days</span> : "—"],
            ]} />
            {view.data?.links.claude && <a className="inline-flex pointer-coarse:min-h-11 items-center gap-1.5 text-xs text-accent" href={view.data.links.claude} target="_blank" rel="noreferrer">
              <ExternalLink size={12} aria-hidden="true" /> Usage on claude.ai</a>}
          </> : undefined}>
          {claude?.error && <p className="text-xs text-wait">{claude.error}</p>}
          {claudeInstalled && claude?.login_expires_at && left !== null && left < 7 && <p className={`text-xs ${left < 2 ? "text-bad" : "text-wait"}`}>
            Login expires {new Date(claude.login_expires_at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</p>}
          {software("claude")}
          {!claudeInstalled ? missing("claude") : (
            <div className="grid min-w-0 gap-3 sm:grid-cols-2">{!usageHere ? <p className="text-xs text-faint">Plan usage belongs to {usageAccount} on {instance.host}, not this selection.</p> : !owner ? <p className="text-xs text-faint">Plan usage is shown to owners (it is the main account's plan).</p> : <>
              {statusline ? (<>
                <Usage label="5-hour window" percent={statusline.value.five_hour?.used_percentage} resets={statusline.value.five_hour?.resets_at} source={`Session statusline (official) · ${age(statusline)}`} />
                <Usage label="7-day window" percent={statusline.value.seven_day?.used_percentage} resets={statusline.value.seven_day?.resets_at} source={`Session statusline · ${age(statusline)}`} />
              </>) : cache?.five_hour ? (<>
                <Usage label="5-hour window" percent={cache.five_hour.utilization} resets={cache.five_hour.resets_at} source="Claude CLI cache (internal; may be stale)" />
                <Usage label="7-day window" percent={cache.seven_day?.utilization} resets={cache.seven_day?.resets_at} source="Claude CLI cache (internal; may be stale)" />
              </>) : <p className="text-xs text-faint">Usage appears after a Claude session's first reply.</p>}
            </>}</div>)}
        </AccountRow>

        <AccountRow label="Codex" title={<><ProviderMark provider="codex" />Codex</>}
          status={codexInstalled ? <LoginState ok={codex?.logged_in} check={check} /> : <span className="text-faint">Not installed for {agentAccount}</span>}
          plan={codexPlan ? <Badge>{codexPlan}</Badge> : undefined}
          action={codexInstalled ? <Button size="sm" disabled={!accounts.data?.account || !!accounts.error} onClick={() => requestLogin("codex")}><KeyRound size={13} aria-hidden="true" /> Re-login</Button> : undefined}
          details={codexInstalled ? <><Meta items={[
              ["Account", codex?.email ?? "—"],
              ["Tokens refreshed", codex?.last_refresh ? ago(codex.last_refresh) : "—"],
            ]} />
            {view.data?.links.codex && <a className="inline-flex pointer-coarse:min-h-11 items-center gap-1.5 text-xs text-accent" href={view.data.links.codex} target="_blank" rel="noreferrer">
              <ExternalLink size={12} aria-hidden="true" /> Usage on chatgpt.com</a>}
          </> : undefined}>
          {codex?.error && <p className="text-xs text-wait">{codex.error}</p>}
          {software("codex")}
          {!codexInstalled ? missing("codex") : (
            <div className="grid min-w-0 gap-3 sm:grid-cols-2">{!usageHere ? <p className="text-xs text-faint">Plan usage belongs to {usageAccount} on {instance.host}, not this selection.</p> : !owner ? <p className="text-xs text-faint">Plan usage is shown to owners (it is the main account's plan).</p> : <>
              {codexUsage?.error ? <p role="alert" className="text-xs text-bad">{codexUsage.error}</p> : codexUsage ? (<>
                {codexUsage.primary && <Usage label={windowLabel(codexUsage.primary.window_minutes)} percent={codexUsage.primary.used_percent} resets={codexUsage.primary.resets_at} source={`codex app-server (official) · ${age(o["codex.rate_limits"])}`} />}
                {codexUsage.secondary && <Usage label={windowLabel(codexUsage.secondary.window_minutes)} percent={codexUsage.secondary.used_percent} resets={codexUsage.secondary.resets_at} source={`codex app-server · ${age(o["codex.rate_limits"])}`} />}
                {codexUsage.reached && <Badge tone="bad">limit reached: {codexUsage.reached}</Badge>}
              </>) : <p className="text-xs text-faint">{view.isLoading ? "Checking…" : "Not reported yet: Codex reports usage once a session has run."}</p>}
            </>}</div>)}
        </AccountRow>

      {others.length > 0 && (
        <>
          {others.map((p) => <ProviderCard key={p.id} info={p} health={accounts.data?.[p.id] as ProviderHealth | undefined}
            account={agentAccount} onRelogin={() => requestLogin(p.id)}
            usage={usageHere ? o[`${p.id}.usage`] : undefined} machine={machine}
            installedThere={installedThere(p.id)} check={check} notInstalled={missing(p.id)} software={software(p.id)} />)}
        </>
      )}

      <AccountRow label="GitHub" title={<><GitBranch size={14} aria-hidden="true" />GitHub</>}
          status={<LoginState ok={github?.logged_in} check={check} />}
          action={<Button size="sm" disabled={!accounts.data?.account || !!accounts.error} onClick={() => requestLogin("github")}><KeyRound size={13} aria-hidden="true" /> {github?.logged_in ? "Re-login" : "Log in"}</Button>}
          details={<Meta items={[
            ["Account", github?.login ?? "—"],
            ["Token scopes", github?.scopes ?? "—"],
          ]} />}>
        {github?.error && <p className="text-xs text-wait">{github.error}</p>}
      </AccountRow>
      </div></Card>
      {!desktopBuild && owner && <OtherSoftwareCard key={`${machine ?? "local"}:${agentAccount}`} machine={machine} account={accounts.data?.account}
        providers={["claude", "codex", ...others.map((p) => p.id)]} />}
      </ProviderSection>
      {!desktopBuild && <ProviderSection id="tools" title="Tools" scope={selectedScope}><ToolsCard machine={machine} account={accounts.data?.account} /></ProviderSection>}
      {!desktopBuild && owner && !machine && <Feature name="local_models"><ProviderSection id="connections" title="Model connections" scope={`Configured on ${instance.host} (main Rynhult).`}><EndpointsCard /></ProviderSection></Feature>}
      <ProviderSection id="models" title="Available models" scope={`Catalog collected on ${instance.host} (main Rynhult), not a live availability check on other machines.`}>
      <ErrorNote error={models.error} title="Couldn't read the model catalog." onRetry={() => models.refetch()} />
      {models.isLoading && <p className="text-xs text-muted">Checking model catalog…</p>}
      <details open={catalogOpen} onToggle={event => setCatalogOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer rounded-md py-2 text-sm font-medium text-accent hover:underline">Model catalog
        <span className="ml-2 text-xs font-normal text-muted">{models.data?.fetched_at ? `Refreshed daily · last ${ago(new Date(models.data.fetched_at * 1000).toISOString())}` : "Refreshed daily"}</span>
      </summary>
      <Card>
        <CardHeader title="Available models" subtitle={models.data?.fetched_at ? `Refreshed daily · last ${ago(new Date(models.data.fetched_at * 1000).toISOString())}` : "Refreshed daily"} />
        <ModelGroups person={username ?? "you"} groups={(registry.list.length ? registry.list.filter((p) => p.installed !== false) : [{ id: "claude", label: "Claude Code" }, { id: "codex", label: "Codex" }] as ProviderInfo[])
          .filter((p) => Array.isArray(models.data?.[p.id]) && (models.data?.[p.id] as ModelRow[]).length > 0)
          .map((p) => ({ id: p.id, label: p.label, models: models.data?.[p.id] as ModelRow[] }))} />
      </Card>
      </details>
      </ProviderSection>
      <Card>
        <CardHeader title="Not exposed" subtitle="What Rynhult cannot show, and why" />
        <ul className="list-disc space-y-1 py-4 pl-8 pr-4 text-[13px] text-muted">{view.data?.not_exposed.map((x) => <li key={x}>{x}</li>)}</ul>
      </Card>
      <ReloginDialog provider={relogin?.provider ?? null} account={relogin?.account} machine={relogin?.machine} onClose={() => setRelogin(null)} />
    </Page>
  );
}
