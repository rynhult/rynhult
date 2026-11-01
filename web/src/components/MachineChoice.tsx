import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Button, ErrorNote, Skeleton } from "./ui";

export interface LaunchMachine {
  name: string; label: string; connected: boolean; system: string; available: boolean; reason: string | null;
  providers: { id: string; label: string; installed: boolean | null }[];
}

export const useLaunchMachines = (projectId?: number, enabled = true) => useQuery({
  queryKey: ["launch-machines", projectId ?? "account"],
  queryFn: () => api<LaunchMachine[]>(`/api/nodes?launch=1${projectId ? `&project_id=${projectId}` : ""}`),
  enabled, staleTime: 15000, refetchInterval: enabled ? 15000 : false,
});

export const machineDetail = (machine: LaunchMachine) => [
  machine.connected ? "Online" : "Offline", machine.system,
  machine.providers.some(p => p.installed) ? `Installed: ${machine.providers.filter(p => p.installed).map(p => p.label).join(", ")}`
    : machine.providers.some(p => p.installed === false) ? "No agents installed for this account" : "Agents not checked",
].join(" · ");

export function TeamMachines({ projectId, value, onChange }: { projectId?: number; value: string[]; onChange: (value: string[]) => void }) {
  const machines = useLaunchMachines(projectId, !!projectId);
  return <fieldset className="min-w-0 space-y-2">
    <legend className="mb-1 text-xs font-medium text-muted">Machines</legend>
    <p className="text-xs text-muted">Choose where helpers may run, using the project's agent account.</p>
    {machines.isPending && <Skeleton className="h-11" />}
    <ErrorNote error={machines.error} />
    {machines.isError && <Button size="sm" onClick={() => machines.refetch()}>Retry machines</Button>}
    {machines.data?.map(machine => <label key={machine.name} className="flex min-w-0 items-start gap-2.5 rounded-md border border-line p-2.5">
      <input type="checkbox" className="mt-1 shrink-0" checked={value.includes(machine.name)}
        disabled={!machine.available && !value.includes(machine.name)}
        onChange={event => onChange(event.target.checked ? [...value, machine.name] : value.filter(name => name !== machine.name))} />
      <span className="min-w-0 break-words text-sm"><span className="font-medium">{machine.label}</span>
        <span className="block text-xs text-muted">{machineDetail(machine)}</span>
        {machine.reason && <span className="block text-xs text-wait">{machine.reason}</span>}
      </span>
    </label>)}
    {machines.data && !machines.data.length && <p className="text-xs text-muted">No machines are available for this account. Ask an owner to check Host → Machines.</p>}
    {machines.data && value.filter(name => !machines.data.some(machine => machine.name === name)).map(name =>
      <label key={name} className="flex items-start gap-2 text-xs text-wait">
        <input type="checkbox" checked onChange={() => onChange(value.filter(item => item !== name))} />
        <span>{name}: no longer available. Uncheck it and choose an enrolled machine.</span>
      </label>)}
  </fieldset>;
}
