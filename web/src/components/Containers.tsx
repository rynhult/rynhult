import { useSingleMutation as useMutation } from "../lib/mutations";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Boxes } from "lucide-react";
import { api, post } from "../lib/api";
import type { Project } from "../lib/types";
import { Badge, Button, Card, CardHeader, Empty, ErrorNote, Input, LoadingRows, Note, QueryState } from "./ui";

export type Limits = { cpus: number | null; memory_mib: number | null; pids: number | null; disk_gib: number | null; sessions: number | null };
export interface ContainersView {
  enabled: boolean;
  /** Containers ship as a Preview: switched on per project, only on a host that passed the qualification run. */
  release: "preview";
  qualification: { passed: boolean; reason: string; report_sha256?: string; recorded_at?: string };
  policy: string;
  member_defaults: Limits;
  accounts: { account: string; limits: Limits; source: "set" | "default"; owner: boolean; people: string[] }[];
}

export const useContainers = (enabled = true) =>
  useQuery({ queryKey: ["containers"], queryFn: () => api<ContainersView>("/api/containers"), enabled });

/** The five limits, as people read them. Memory is edited in GiB and kept in MiB. */
const FIELDS: { key: keyof Limits; label: string; noun: string; unit: string; min: number; step: number }[] = [
  { key: "cpus", label: "CPUs", noun: "CPUs", unit: "", min: 1, step: 1 },
  { key: "memory_mib", label: "Memory", noun: "memory", unit: "GiB", min: 0.25, step: 0.25 },
  { key: "pids", label: "Processes", noun: "processes", unit: "", min: 64, step: 1 },
  { key: "disk_gib", label: "Disk", noun: "disk", unit: "GiB", min: 1, step: 1 },
  { key: "sessions", label: "Sessions at once", noun: "sessions", unit: "", min: 1, step: 1 },
];
const shown = (key: keyof Limits, value: number | null) =>
  value === null ? "" : key === "memory_mib" ? String(+(value / 1024).toFixed(2)) : String(value);
const stored = (key: keyof Limits, text: string): number | null =>
  text.trim() === "" ? null : key === "memory_mib" ? Math.round(Number(text) * 1024) : Math.round(Number(text));
const summary = (l: Limits) => FIELDS.every((f) => l[f.key] === null) ? "No limits"
  : FIELDS.map((f) => l[f.key] === null ? null : `${shown(f.key, l[f.key])}${f.unit ? ` ${f.unit}` : ""} ${f.noun}`).filter(Boolean).join(" · ");

/** The release label every container surface carries (docs/CONTAINERS.md, "Release status"). */
export function PreviewBadge() {
  return <Badge tone="info" title="Containers are a Preview: off by default, on per project, on a qualified host">Preview</Badge>;
}

/** Settings (owners): what each agent account's container sessions may use. Empty means no limit. */
export function ContainerLimits() {
  const query = useContainers();
  return (
    <Card>
      <CardHeader title={<span className="inline-flex items-center gap-2">Container limits <PreviewBadge /></span>} icon={Boxes}
        subtitle="What each agent account's container sessions may use, per session and at once. Empty means no limit." />
      <QueryState query={query} loading={<LoadingRows rows={2} />} errorTitle="Couldn't read the container limits."
        isEmpty={(d) => !d.accounts.length} empty={<Empty icon={Boxes}>No agent accounts on this machine.</Empty>}>
        {(data) => (
          <div className="divide-y divide-line">
            {!data.enabled && (
              <p className="px-4 py-3 text-[13px] text-muted">
                Containers are off on this Rynhult, so these limits aren't used yet. They apply once an owner turns on
                Containers per session in Admin features and switches a project to containers.
              </p>
            )}
            {data.accounts.map((row) => <AccountLimits key={row.account} row={row} defaults={data.member_defaults} />)}
          </div>
        )}
      </QueryState>
    </Card>
  );
}

function AccountLimits({ row, defaults }: { row: ContainersView["accounts"][number]; defaults: Limits }) {
  const queryClient = useQueryClient();
  const initial = () => Object.fromEntries(FIELDS.map((f) => [f.key, shown(f.key, row.limits[f.key])])) as Record<keyof Limits, string>;
  const [draft, setDraft] = useState(initial);
  const value = Object.fromEntries(FIELDS.map((f) => [f.key, stored(f.key, draft[f.key])])) as Limits;
  const dirty = FIELDS.some((f) => value[f.key] !== row.limits[f.key]);
  const invalid = FIELDS.some((f) => draft[f.key].trim() !== "" && !(Number(draft[f.key]) >= f.min));
  const save = useMutation({
    mutationFn: (limits: Limits) => api(`/api/containers/limits/${row.account}`, { method: "PUT", json: limits }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["containers"] }),
  });
  const reset = (limits: Limits) => setDraft(Object.fromEntries(FIELDS.map((f) => [f.key, shown(f.key, limits[f.key])])) as Record<keyof Limits, string>);
  return (
    <form className="space-y-3 px-4 py-3 text-[13px]" aria-label={`Container limits for ${row.account}`}
      onSubmit={(e) => { e.preventDefault(); if (dirty && !invalid) save.mutate(value); }}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono font-medium text-fg">{row.account}</span>
        {row.owner ? <Badge>owners</Badge> : <Badge>member</Badge>}
        {row.source === "default" && <span className="text-xs text-faint">{row.owner ? "owners aren't limited unless you set limits" : "member defaults"}</span>}
        {row.people.length > 0 && <span className="text-xs text-muted">· {row.people.join(", ")}</span>}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {FIELDS.map((f) => (
          <label key={f.key} className="block space-y-1">
            <span className="text-xs font-medium text-muted">{f.label}{f.unit ? ` (${f.unit})` : ""}</span>
            <Input type="number" inputMode="decimal" min={f.min} step={f.step} placeholder="No limit" className="tnum w-full"
              value={draft[f.key]} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })} />
          </label>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" variant="primary" disabled={!dirty || invalid || save.isPending}>{save.isPending ? "Saving…" : "Save"}</Button>
        {dirty && <Button type="button" size="sm" variant="ghost" onClick={() => reset(row.limits)}>Undo changes</Button>}
        {!row.owner && FIELDS.some((f) => value[f.key] !== defaults[f.key]) && (
          <Button type="button" size="sm" variant="ghost" onClick={() => reset(defaults)}>Member defaults</Button>
        )}
        {!dirty && <span className="text-xs text-faint">{summary(row.limits)}</span>}
        {invalid && <span className="text-xs text-bad">Each limit is a number (memory at least 0.25 GiB, processes at least 64), or empty for no limit.</span>}
      </div>
      <ErrorNote error={save.error} title="Couldn't save these limits." />
    </form>
  );
}

/** A project's switch (owners): new sessions run in containers through the root launcher. */
export function ProjectContainersCard({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const containers = useContainers();
  const [chosen, setChosen] = useState<boolean | null>(null);
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => post<Project>(`/api/projects/${project.id}/containers`, { enabled }),
    onSettled: () => { setChosen(null); queryClient.invalidateQueries({ queryKey: ["projects"] }); },
  });
  const enabled = chosen ?? !!project.containers;
  if (!containers.data || (!containers.data.enabled && !project.containers)) return null;
  // Switching on needs a qualified host; a project that is already on can always be switched off.
  const qualified = containers.data.qualification.passed;
  const locked = !qualified && !project.containers;
  return (
    <Card>
      <CardHeader title={<span className="inline-flex items-center gap-2">Containers <PreviewBadge /></span>} icon={Boxes}
        subtitle="New sessions run in a container, in a checkout of their own"
        action={
          <label className="flex items-center gap-2 text-[13px] text-muted">
            <input type="checkbox" className="size-3.5 accent-[var(--accent)]" checked={enabled} disabled={toggle.isPending || locked}
              aria-describedby={locked ? `containers-locked-${project.id}` : undefined}
              onChange={(e) => { setChosen(e.target.checked); toggle.mutate(e.target.checked); }} aria-label="Run this project's new sessions in containers" />
            On
          </label>
        } />
      <div className="space-y-2 px-4 py-3 text-[13px] text-muted">
        {enabled
          ? <p>Each new session works in a checkout of its own, with its account's container limits; its branch comes back into the project when the session is archived. Running sessions keep how they started.</p>
          : <p>Off: sessions run as they always have.</p>}
        <p>Containers are a Preview in this release: off unless you switch a project on, and only on a host that has passed the container qualification.</p>
        {!qualified && <div id={`containers-locked-${project.id}`}><Note tone="wait">
          {project.containers ? "New launches, including resumes, are blocked" : "Containers can't be switched on yet"}: {containers.data.qualification.reason}.
        </Note></div>}
        {!containers.data.enabled && project.containers && (
          <Note tone="bad">Containers are off on this Rynhult, so this project's sessions can't start. Switch it off to run them as before.</Note>
        )}
        <ErrorNote error={toggle.error} title="Couldn't change this." />
      </div>
    </Card>
  );
}
