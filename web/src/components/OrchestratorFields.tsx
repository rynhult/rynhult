import { lazy, Suspense, useState } from 'react';
import { lazyImport } from '../lib/lazyImport';
import type { Catalog, ProviderInfo } from '../lib/types';
import { Button, Field, Input, Select, Skeleton } from './ui';
import { ProjectBriefChoice } from './ProjectBriefChoice';

// Only a team grant needs the brief form: keep it out of every page's first load (payload budget).
const ProjectBrief = lazy(() => lazyImport(() => import('./ProjectBrief').then((m) => ({ default: m.ProjectBrief }))));

export interface TeamChoice { agent: string; profile: string | null; options: Record<string, string | boolean> }
export interface TeamGrant { brief_version?: number; run_mode?: 'prepare' | 'build'; at_once: number; total: number; depth: number; hours: number; machines: string[]; agents: TeamChoice[] }
export const defaultGrant = (machine: string, agents: TeamChoice[] =
  ['fullstack', 'frontend', 'backend', 'code-reviewer', 'qa'].map(profile => ({ agent: 'claude', profile, options: {} }))): TeamGrant =>
  ({ run_mode: 'build', at_once: 3, total: 10, depth: 2, hours: 8, machines: [machine], agents });

export function OrchestratorFields({ value, onChange, catalog, providers, projectId, onOpenBrief }: {
  projectId?: number; value: TeamGrant; onChange: (value: TeamGrant) => void; catalog?: Catalog; providers: ProviderInfo[];
  onOpenBrief?: () => void;
}) {
  const [briefMode, setBriefMode] = useState<'prepare' | 'build'>();
  const update = (index: number, change: Partial<TeamChoice>) => onChange({ ...value,
    agents: value.agents.map((x, i) => i === index ? { ...x, ...change } : x) });
  return <fieldset className="space-y-3 rounded-lg border border-line p-3">
    <legend className="px-1 font-medium">Team limits</legend>
    {projectId && <>
      <ProjectBriefChoice projectId={projectId} value={value.brief_version} onOpenBrief={onOpenBrief} onChange={(version, mode) => {
        setBriefMode(mode); onChange({ ...value, brief_version: version, ...(mode === 'prepare' ? { run_mode: 'prepare' as const } : {}) });
      }} />
      <Suspense fallback={<Skeleton className="h-11" />}><ProjectBrief key={projectId} projectId={projectId} compact /></Suspense>
    </>}
    <p className="text-xs text-muted">Shared by every helper, including helpers of helpers. Only you grant these limits.</p>
    <Field label="Team run mode"><Select disabled={briefMode === 'prepare'} value={value.run_mode ?? 'build'} onChange={(e) => onChange({ ...value, run_mode: e.target.value as 'prepare' | 'build' })}>
      <option value="prepare">Clarify and prepare (read-only)</option><option value="build">Build within this scope</option>
    </Select></Field>
    <p className="text-xs text-muted">Prepare launches the lead read-only and permits only read-only discovery helpers. Choose plan/read-only permissions for discovery helper choices. Your helper grant stays fixed. If the project brief has its own run mode, the stricter of the two applies.</p>
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {([['at_once', 'At once', 100], ['total', 'In total', 1000], ['depth', 'Depth', 10], ['hours', 'Hours', 168]] as const).map(([key, label, max]) =>
        <Field key={key} label={label}><Input type="number" min={1} max={max} value={value[key]}
          onChange={(e) => onChange({ ...value, [key]: Number(e.target.value) })} /></Field>)}
    </div>
    <Field label="Machines"><Input value={value.machines.join(', ')} onChange={(e) => onChange({ ...value, machines: e.target.value.split(',').map((x) => x.trim()) })} /></Field>
    <p className="text-xs text-muted">Comma-separated enrolled machine names; local means this Rynhult. Helpers use your project’s agent account.</p>
    <p className="text-xs text-muted">Assignments select the matching role from these allowed profiles. Remove choices you do not want.</p>
    {!value.agents.length && <p role="status" className="text-xs text-wait">No signed-in Claude or Codex was found on this machine for the project’s agent account. Sign in, or add an allowed agent/profile below.</p>}
    {value.agents.map((choice, i) => {
      const provider = providers.find((x) => x.id === choice.agent);
      return <fieldset key={i} className="space-y-3 border-t border-line pt-3">
        <legend className="text-xs text-muted">Allowed helper {i + 1}</legend>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={`Helper agent ${i + 1}`}><Select value={choice.agent} onChange={(e) => update(i, { agent: e.target.value, profile: null, options: {} })}>
            {providers.filter((x) => x.supports.includes('mcp') && x.supports.includes('instructions')).map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
          </Select></Field>
          <Field label={`Helper profile ${i + 1}`}><Select value={choice.profile ?? ''} onChange={(e) => update(i, { profile: e.target.value || null, options: {} })}>
            <option value="">No profile</option>
            {catalog?.profiles.filter((p) => !p.providers || p.providers.includes(choice.agent)).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select></Field>
        </div>
        {provider?.options.filter((o) => o.kind === 'select').map((o) => <Field key={o.key} label={`Helper ${i + 1}: ${o.label}`}>
          <Select value={String(choice.options[o.key] ?? (catalog?.profiles.find((p) => p.id === choice.profile)?.[choice.agent] as Record<string, unknown> | undefined)?.[o.key] ?? o.default ?? '')} onChange={(e) => update(i, { options: { ...choice.options, [o.key]: e.target.value } })}>
            {o.choices.map((c) => <option key={String(c.value)} value={String(c.value)}>{c.label}</option>)}
          </Select>
        </Field>)}
        {provider?.options.filter((o) => o.kind === 'bool').map((o) => <label key={o.key} className="flex items-start gap-2.5 text-xs">
          <input type="checkbox" checked={Boolean(choice.options[o.key] ?? (catalog?.profiles.find((p) => p.id === choice.profile)?.[choice.agent] as Record<string, unknown> | undefined)?.[o.key] ?? o.default)}
            onChange={(e) => update(i, { options: { ...choice.options, [o.key]: e.target.checked } })} />
          <span>Helper {i + 1}: {o.label}</span>
        </label>)}
        {value.agents.length > 1 && <Button variant="ghost" onClick={() => onChange({ ...value, agents: value.agents.filter((_, n) => i !== n) })}>Remove helper choice {i + 1}</Button>}
      </fieldset>;
    })}
    <Button variant="secondary" onClick={() => onChange({ ...value, agents: [...value.agents, { agent: 'claude', profile: null, options: {} }] })}>Add allowed agent/profile</Button>
  </fieldset>;
}
