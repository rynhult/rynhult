import clsx from "clsx";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { ago, size } from "../lib/format";
import { diskMountWords } from "../lib/words";
import { Activity, ChevronRight, Container, Cpu, HardDrive, Laptop, MemoryStick, RefreshCw, ServerCog, Users } from "lucide-react";
import { Badge, Button, Card, CardHeader, Dot, Empty, ErrorNote, Meter, Page, PageHeader, Skeleton, StaleNote, Stat } from "../components/ui";
import type { HostView, RemoteHostView } from "../lib/types";
import { useInstance, useRole } from "../lib/queries";
import { HostBackups } from "../components/HostBackups";
import { MachinePicker, Machines, useMachineUpdate, useNodes } from "../components/Machines";
import { WorkstationSleep } from "../components/MachinePolicy";

const gb = (n: number) => `${(n / 1e9).toFixed(n > 1e11 ? 0 : 1)} GB`;
const one = (x: string) => x.replace(/^[a-z0-9-]+-(\w+)-\d+$/, "$1");

const bytes = (n: number | null | undefined) =>
  n == null ? "–" : n >= 1e12 ? `${(n / 1e12).toFixed(1)} TB` : n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.round(n / 1e6)} MB`;
const uptime = (s: number) => `Up ${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
const NOT_REPORTED = "Not reported by this machine";

type Snapshot = NonNullable<HostView["snapshot"]>;
type Live = { cpu_percent: number | null; cpu_count: number | null; load: (number | null)[] | null; memory: { total: number | null; used: number | null; percent: number | null }; swap_percent: number | null };

/** CPU, load, memory and (owners) services: the first row, for this machine and for another one. */
function HealthStats({ live, services, showServices }: { live: Live | null; services: Snapshot["services"] | null; showServices: boolean }) {
  const units = Object.entries(services?.units ?? {});
  const failed = services?.failed ?? [];
  const unhealthy = units.filter(([, state]) => state !== "active");
  const n = (x: number | null | undefined, digits = 0) => (x == null ? "–" : x.toFixed(digits));
  const load = live?.load?.every((l) => l != null) ? live.load as number[] : null;
  const memory = live?.memory;
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Stat label="CPU" icon={Cpu} value={live?.cpu_percent == null ? "–" : `${n(live.cpu_percent)}%`}
        detail={live?.cpu_count ? `${live.cpu_count} threads` : NOT_REPORTED} meter={live?.cpu_percent ?? undefined} />
      <Stat label="Load (1 / 5 / 15 min)" icon={Activity} value={load ? load[0].toFixed(2) : "–"} detail={load ? load.map((l) => l.toFixed(2)).join(" / ") : NOT_REPORTED} />
      <Stat label="Memory" icon={MemoryStick} value={memory?.percent == null ? "–" : `${n(memory.percent)}%`}
        detail={memory?.used != null && memory.total != null ? `${gb(memory.used)} of ${gb(memory.total)}${live?.swap_percent != null ? ` · swap ${n(live.swap_percent)}%` : ""}` : NOT_REPORTED}
        meter={memory?.percent ?? undefined} />
      {showServices && (
        <Stat label="Services" icon={ServerCog} value={services ? (failed.length || unhealthy.length ? `${failed.length + unhealthy.length} issue(s)` : "Healthy") : "–"}
          tone={services && (failed.length || unhealthy.length) ? "bad" : undefined}
          detail={services ? (failed.length ? `Failed: ${failed.join(", ")}` : `${units.length} watched unit${units.length === 1 ? "" : "s"} active`) : NOT_REPORTED} />
      )}
    </div>
  );
}

function Disks({ disks }: { disks: Snapshot["disks"] }) {
  return (
    <div className="space-y-4 p-4">
      {disks.map((d) => (
        <div key={d.mount}>
          <div className="mb-1.5 flex justify-between gap-3 text-[13px]"><span className="min-w-0 truncate"><span className="font-mono font-medium text-fg" title={d.mount}>{diskMountWords(d.mount)}</span> <span className="text-faint">{d.device}</span></span>
            <span className="tnum shrink-0 text-muted">{d.percent.toFixed(0)}% · {gb(d.free)} free of {gb(d.total)}</span></div>
          <Meter value={d.percent} label={`${diskMountWords(d.mount)} used`} />
        </div>
      ))}
    </div>
  );
}

/** Healthy detail behind one quiet line (UX declutter W8): problems are never inside one. */
function HealthyDetails({ summary, children }: { summary: string; children: ReactNode }) {
  return (
    <details className="group border-t border-line">
      <summary className="flex w-fit cursor-pointer list-none items-center gap-1.5 px-4 py-2.5 text-[13px] text-muted hover:text-fg">
        <ChevronRight size={14} className="transition-transform group-open:rotate-90" aria-hidden="true" />{summary}
      </summary>
      {children}
    </details>
  );
}

/** A drive is healthy only when SMART says so: failing, media errors, wear from 50% and "n/a" stay in sight. */
const driveHealthy = (d: Snapshot["smart"][number]) =>
  d.passed === true && !(d.media_errors ?? 0) && (d.percentage_used ?? 0) < 50;

function Smart({ smart }: { smart: Snapshot["smart"] }) {
  const healthy = smart.filter(driveHealthy);
  const shown = smart.filter((d) => !driveHealthy(d));
  return (
    <>
      {shown.length > 0 && <SmartRows smart={shown} />}
      {healthy.length > 0 && <HealthyDetails summary={`${healthy.length} drive${healthy.length === 1 ? "" : "s"} SMART ok`}><SmartRows smart={healthy} /></HealthyDetails>}
    </>
  );
}

function SmartRows({ smart }: { smart: Snapshot["smart"] }) {
  return (
    <div className="divide-y divide-line border-t border-line">
      {smart.map((d) => {
        const wear = d.percentage_used;
        return (
          <div key={d.device} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-[13px]">
            <span className="flex min-w-0 items-center gap-2.5">
              <Dot tone={d.passed === false || (d.media_errors ?? 0) > 0 ? "bad" : wear !== null && wear >= 50 ? "wait" : "ok"} />
              <span className="font-mono font-medium text-fg">{d.device}</span> <span className="truncate text-muted">{d.model} · {d.size}</span>
            </span>
            <span className="tnum flex flex-wrap items-center gap-x-3 text-xs text-muted">
              <span className={d.passed === false ? "text-bad" : undefined}>{d.passed === false ? "SMART failing" : d.passed ? "SMART ok" : "SMART n/a"}</span>
              {(d.media_errors ?? 0) > 0 && <span className="text-bad">{d.media_errors} media error{d.media_errors === 1 ? "" : "s"}</span>}
              {d.temperature !== null && <span>{d.temperature}°C</span>}
              {wear !== null && <span className={wear >= 80 ? "text-bad" : wear >= 50 ? "text-wait" : undefined}>wear {wear}%</span>}
              {d.power_on_hours !== null && <span className="text-faint">{Math.round(d.power_on_hours / 24)} days on</span>}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** A part another machine can't report: said once, in plain words, instead of an empty list or a zero. */
function Unreported({ children }: { children: string }) {
  return <p className="border-t border-line px-4 py-2.5 text-[13px] text-faint">{children}</p>;
}

function ServicesCard({ services, gpu, tailscale, remote }: { services: Snapshot["services"] | null; gpu: Snapshot["gpu"] | null; tailscale: Snapshot["tailscale"] | null; remote?: boolean }) {
  const units = Object.entries(services?.units ?? {});
  const failed = services?.failed ?? [];
  // A unit that isn't active, or one systemd lists as failed, stays in sight; active ones go under Details.
  const active = units.filter(([unit, state]) => state === "active" && !failed.includes(unit));
  const problems = [...units.filter(([unit, state]) => state !== "active" || failed.includes(unit)),
    ...failed.filter((unit) => !(unit in (services?.units ?? {}))).map((unit) => [unit, "failed"] as [string, string])];
  const gpus = gpu?.length ? gpu : null;
  return (
    <Card>
      <CardHeader title="Services" icon={ServerCog} subtitle={!services ? NOT_REPORTED : failed.length ? `${failed.length} failed unit(s)` : "No failed systemd units"} />
      {services ? problems.length > 0 && <UnitRows units={problems} />
        : <p className="px-4 py-3 text-[13px] text-faint">This machine has no systemd for Rynhult to ask (a Mac, or WSL without systemd).</p>}
      {(active.length > 0 || gpus) && (
        <HealthyDetails summary={[active.length ? `${active.length} watched unit${active.length === 1 ? "" : "s"} active` : "", gpus ? "GPU" : ""].filter(Boolean).join(" · ")}>
          {active.length > 0 && <UnitRows units={active} />}
          {gpus && <div className="min-w-0 space-y-2 px-4 pb-3 text-[13px] [overflow-wrap:anywhere]">
            {gpus.map((g) => (
              <div key={g.name} className="flex flex-wrap justify-between gap-2"><span className="font-medium text-fg">{g.name}</span>
                <span className="tnum text-muted">{g.utilization ?? "–"}% · {g.memory_used_mb ?? "–"}/{g.memory_total_mb ?? "–"} MB · {g.temperature ?? "?"}°C</span></div>
            ))}
          </div>}
        </HealthyDetails>
      )}
      <div className="min-w-0 space-y-2 border-t border-line p-4 text-[13px] [overflow-wrap:anywhere]">
        {!gpus && remote && <div className="flex flex-wrap justify-between gap-2"><span className="font-medium text-fg">GPU</span><span className="text-faint">No NVIDIA GPU reported</span></div>}
        <div className="flex flex-wrap justify-between gap-2"><span className="font-medium text-fg">Tailscale</span>
          <span className={tailscale ? "text-muted" : "text-faint"}>{tailscale ? `${tailscale.state}${tailscale.ips?.length ? ` · ${tailscale.ips[0]}` : ""}` : "Not installed or not reported"}</span></div>
      </div>
    </Card>
  );
}

function UnitRows({ units }: { units: [string, string][] }) {
  return (
    <div className="grid grid-cols-1 gap-x-6 px-4 py-2 sm:grid-cols-2">
      {units.map(([unit, state]) => (
        <div key={unit} className="flex items-center justify-between gap-3 border-b border-line py-2 text-[13px] last:border-0 sm:[&:nth-last-child(2)]:border-0">
          <span className="flex min-w-0 items-center gap-2.5"><Dot tone={state === "active" ? "ok" : state === "inactive" ? "neutral" : "bad"} /><span className="truncate font-mono text-xs text-fg">{unit}</span></span>
          <span className={state === "active" ? "text-xs text-faint" : "text-xs font-medium text-bad"}>{state}</span>
        </div>
      ))}
    </div>
  );
}

/** Another machine's details, from its acc-node over its link (owners). What it can't read without root is named. */
function RemoteHost({ name, picker }: { name: string; picker: ReactNode }) {
  const view = useQuery({ queryKey: ["host", name], queryFn: () => api<RemoteHostView>(`/api/host?machine=${encodeURIComponent(name)}`), refetchInterval: 30000 });
  const { update, updateConfirmed } = useMachineUpdate();
  const v = view.data;
  const s = v?.snapshot;
  const header = (meta?: ReactNode) => (
    <PageHeader title="Host" description={s ? `${s.hostname === name ? name : `${name} · ${s.hostname}`} · ${s.kernel}` : name} meta={meta}
      actions={v?.supported && <Button onClick={() => view.refetch()} disabled={view.isFetching}><RefreshCw size={14} className={view.isFetching ? "animate-spin" : ""} aria-hidden="true" /> Refresh</Button>} />
  );
  const top = (meta?: ReactNode) => <>{header(meta)}{picker}
    {v?.last_backup_via_public_address && <p className="text-xs text-muted">
      Last backup via public address <time dateTime={v.last_backup_via_public_address} title={v.last_backup_via_public_address}>
        {ago(v.last_backup_via_public_address)}</time>.
    </p>}
  </>;
  if (!v) return <>{top()}{view.error ? <ErrorNote error={view.error} onRetry={() => view.refetch()} /> : <Skeleton className="h-64" />}</>;
  if (!v.connected) return <>{top()}<Card><Empty icon={Laptop}>{name}{v.planned_downtime?.active ? ": Planned downtime" : " is offline"}{v.last_seen ? ` (last seen ${ago(v.last_seen)})` : ""}. Its details appear once it connects again.</Empty></Card></>;
  if (!v.supported) {
    const mac = v.os === "darwin";
    return (
      <>{top()}
        <Card><Empty icon={Laptop} action={mac
          ? <a href="/guide/User-Guide/Updating" className="text-[13px] font-medium text-accent underline underline-offset-2">How to update a Mac</a>
          : <Button onClick={() => updateConfirmed(name)} disabled={update.isPending}>Update this machine</Button>}>
          {name} runs Rynhult {v.version ?? "(unknown version)"}, which doesn't report host details. Update this machine to see them here.
        </Empty>{update.error ? <div className="px-4 pb-3"><ErrorNote error={update.error} /></div> : null}</Card>
      </>
    );
  }
  if (!s) return <>{top()}<Card><Empty icon={Laptop}>{name} hasn't sent its details yet. They usually arrive within a few seconds.</Empty></Card></>;
  const collected = new Date(s.collected_at * 1000);
  const validTime = Number.isFinite(collected.getTime()) && s.collected_at <= Date.now() / 1000 + 300;
  const stale = !validTime || Date.now() / 1000 - s.collected_at > 300;
  return (
    <>
      {top(<>
        {s.uptime_seconds != null && <span>{uptime(s.uptime_seconds)}</span>}
        <span className={stale ? "text-wait" : undefined}>Reported by {name} {validTime ? ago(collected.toISOString()) : "· Collection time unavailable"}</span>
        {s.reboot_required && <Badge tone="wait">reboot required</Badge>}
      </>)}
      {view.error ? <StaleNote error={view.error} /> : null}
      <div className="space-y-6">
        <HealthStats live={v.live} services={s.services} showServices />
        <div className="grid gap-6 xl:grid-cols-2">
          <Card>
            <CardHeader title="Storage" icon={HardDrive} subtitle="Usage per mount" />
            {s.disks ? <Disks disks={s.disks} /> : <p className="p-4 text-[13px] text-faint">{NOT_REPORTED}</p>}
            <Unreported>SMART drive health needs the root collector, which runs on the main machine only.</Unreported>
          </Card>
          <ServicesCard services={s.services} gpu={s.gpu} tailscale={s.tailscale} remote />
        </div>
        <p className="text-xs text-faint">Not reported by other machines: containers and agent account limits (they need root there). Git backups of
          {" "}{name}'s projects are kept on the main machine; select its named main entry to see Backups.</p>
      </div>
    </>
  );
}

export function Host() {
  const { owner } = useRole();
  const [params, setParams] = useSearchParams();
  const nodes = useNodes(owner);
  const machines = (nodes.data ?? []).filter((n) => n.status === "active");
  const machine = owner ? params.get("machine") || undefined : undefined;  // members: this machine, as before
  const pick = (id: string) => setParams(id === "local" ? {} : { machine: id }, { replace: true });
  const selected = machines.find(n => n.name === machine);
  const picker = (machines.length > 0 || machine) && (
    <div className="space-y-4"><MachinePicker machines={machines} value={machine} onChange={pick} />
      {selected?.role === 'workstation' && <Card className="p-4"><WorkstationSleep node={selected} settings /></Card>}
    </div>
  );
  return (
    <Page wide className={machine ? "[overflow-wrap:anywhere]" : undefined}>
      {machine ? <RemoteHost name={machine} picker={picker} /> : <LocalHost picker={picker} owner={owner} />}
    </Page>
  );
}

function LocalHost({ picker, owner }: { picker: ReactNode; owner: boolean }) {
  const instance = useInstance();
  const view = useQuery({ queryKey: ["host"], queryFn: () => api<HostView>("/api/host"), refetchInterval: 15000 });
  const v = view.data;
  if (!v) return <>{view.error ? <ErrorNote error={view.error} /> : <Skeleton className="h-64" />}{picker}</>;
  const s = v.snapshot;
  const byProject = new Map<string, NonNullable<typeof s>["containers"]>();
  for (const c of s?.containers ?? []) byProject.set(c.project ?? "other", [...(byProject.get(c.project ?? "other") ?? []), c]);
  const stale = s && Date.now() / 1000 - s.collected_at > 300;
  return (
    <>
      <PageHeader title="Host" description={s ? `${s.hostname} · ${s.kernel}` : instance.host}
        meta={s && <>
          <span>{uptime(s.uptime_seconds)}</span>
          <span className={stale ? "text-wait" : undefined}>Snapshot {ago(new Date(s.collected_at * 1000).toISOString())}; live numbers from psutil</span>
          {s.reboot_required && <Badge tone="wait">reboot required</Badge>}
        </>} />
      {picker}
      {view.error ? <StaleNote error={view.error} /> : null}
      {/* This computer's health comes first (UX review 2, HO-1); backups as one status line, then other machines. */}
      <div className="space-y-6">
      <HealthStats live={v.live} services={s?.services ?? null} showServices={!s || !!s.services /* members don't get the host's systemd units */} />
      {!s ? <Card><Empty icon={ServerCog}>No host snapshot yet. acc-hoststate.timer writes one every minute.</Empty></Card> : (
          <div className="grid gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader title="Storage" icon={HardDrive} subtitle="Usage per mount and SMART health per drive" />
              <Disks disks={s.disks} />
              <Smart smart={s.smart} />
              {v.git_backup && (
                <p className="border-t border-line px-4 py-2.5 text-[13px] text-muted">
                  <span className="font-medium text-fg">Git backups</span> · {v.git_backup.repos} project{v.git_backup.repos === 1 ? "" : "s"},{" "}
                  <span className="tnum">{size(v.git_backup.bytes)}</span> in <span className="font-mono">/srv/acc/git</span>
                  {!v.git_backup.enabled && " · off (no https address)"}
                </p>
              )}
            </Card>
            {s.services && <ServicesCard services={s.services} gpu={s.gpu} tailscale={s.tailscale} />}
          </div>
      )}
      {owner && <HostBackups />}
      {owner && <Machines />}
      {s && <>
          {!!s.accounts?.length && (
            <Card>
              <CardHeader title={owner ? "Agent accounts" : "Your agent account"} icon={Users} subtitle={owner ? "What each account's sessions use, against its limits (acc-agent-add --cpu/--memory/--tasks)" : "What your sessions use, against its limits"} />
              <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="Containers (scrolls sideways)">
                <table className="w-full text-[13px]">
                  <thead><tr className="text-left text-xs text-muted">
                    {["Account", "CPU", "Memory", "Processes", "Disk"].map((h) => <th key={h} className="px-4 py-2 font-normal">{h}</th>)}
                  </tr></thead>
                  <tbody className="divide-y divide-line">
                    {s.accounts.map((a) => (
                      <tr key={a.account}>
                        <td className="px-4 py-2"><span className="font-mono">{a.account}</span>
                          {a.role === "services" && <Badge className="ml-2">Docker only</Badge>}
                          {a.active !== "active" && <Badge tone="bad" className="ml-2">{a.active}</Badge>}</td>
                        <td className="tnum px-4 py-2">{a.cpu_percent ?? "–"}%<span className="text-faint"> / {a.cpu_quota_percent ? `${a.cpu_quota_percent}%` : "no limit"}</span></td>
                        <td className="tnum px-4 py-2">{bytes(a.memory_bytes)}<span className="text-faint"> / {a.memory_max ? bytes(a.memory_max) : "no limit"}</span></td>
                        <td className="tnum px-4 py-2">{a.tasks ?? "–"}<span className="text-faint"> / {a.tasks_max && a.tasks_max < 100000 ? a.tasks_max : "no limit"}</span></td>
                        <td className="tnum px-4 py-2" title={a.workspace_root ?? undefined}>{a.role === "agents" ? bytes(a.disk_bytes) : "–"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
          <Card>
            <CardHeader title="Containers" icon={Container} subtitle={owner ? "The project stacks' rootless Docker (the services account), by compose project" : "Your projects' stacks that Rynhult runs (Autostart), by compose project"} />
            {byProject.size ? (
              <div className="divide-y divide-line">
                {[...byProject.entries()].map(([project, items]) => (
                  <div key={project} className="grid gap-2 px-4 py-3 md:grid-cols-[11rem_1fr]">
                    <span className="font-medium text-fg">{project}</span>
                    <span className="flex flex-wrap gap-x-5 gap-y-1.5">
                      {items.map((c) => {
                        const done = c.state === "exited" && c.status.startsWith("Exited (0)");
                        return (
                          <span key={c.name} className="inline-flex items-center gap-2 text-[13px]" title={c.status}>
                            <Dot tone={c.state === "running" ? "ok" : done ? "neutral" : "bad"} />
                            <span className="text-fg">{one(c.name)}</span>
                            <span className={clsx("text-xs", c.state === "running" || done ? "text-faint" : "text-bad")}>{c.status.replace(/^Up /, "")}</span>
                          </span>
                        );
                      })}
                    </span>
                  </div>
                ))}
              </div>
            ) : <Empty icon={Container}>No containers.</Empty>}
          </Card>
      </>}
      </div>
    </>
  );
}
