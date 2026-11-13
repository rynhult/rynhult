import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import { SECURITY_DEFAULTS } from '../lib/setupLevels';
import { SECURITY_COPY, scopeName, securityLink, useSecurityPolicy, type SecurityRegistry, type SecurityScope } from '../lib/security';
import type { WizardProgress } from '../lib/setupWizard';
import { Field, QueryState, Select } from './ui';
export function SetupProtectionReview({ owner, progress }: { owner: boolean; progress: WizardProgress }) {
  const [kind, setKind] = useState('instance');
  const scope: SecurityScope | undefined = kind === 'project' && progress.project_id ? { kind: 'project', project_id: progress.project_id } : kind === 'account' && progress.binding ? { kind: 'account', machine: progress.binding.machine, account: progress.binding.account, ...(progress.binding.machine_id ? { machine_id: progress.binding.machine_id } : {}) } : owner ? { kind: 'instance' } : progress.project_id ? { kind: 'project', project_id: progress.project_id } : undefined;
  const effective = useSecurityPolicy(scope);
  const registry = useQuery({ queryKey: ['setup-security-scopes', owner], queryFn: () => api<SecurityRegistry>('/api/security/scopes'), enabled: owner });
  return <div className="space-y-3">
    <p>Guardrails protect repository changes, files and secrets, tool installation, provider prompts and budget checks. All eleven protections are recommended On at every level. Saving a level applies no policy.</p>
    <p><Link className="underline" to="/settings#security">Settings → Security</Link> is where you inspect or change them. The Guardrails link opens the same scoped editor. Each change requires a separate reviewed diff and fresh reauthentication by an owner.</p>
    {owner && <Field label="Compare recommended protections at scope"><Select value={kind} onChange={e => setKind(e.target.value)}><option value="instance">Instance defaults</option><option value="account" disabled={!progress.binding}>Selected machine / account</option><option value="project" disabled={!progress.project_id}>Selected project</option></Select></Field>}
    {scope ? <><p>Affected scope: {scopeName(scope)}. No changes selected. Matching values need no write.</p>
      <QueryState query={effective}>{data => <ul className="space-y-2">{Object.keys(SECURITY_DEFAULTS).map(id => <li key={id}>
        <strong>{SECURITY_COPY[id].label}</strong>: current {data.switches[id] === null ? 'Varies' : data.switches[id] === undefined ? 'Unavailable at this scope' : data.switches[id] ? 'On' : 'Off'} → recommended On.
        {' '}Source: {data.sources?.[id] ? JSON.stringify(data.sources[id]) : 'Not reported'}.{id === 'allowed_folders' && <span> Review only for a concrete machine/account; On grants no filesystem roots.</span>}
      </li>)}</ul>}</QueryState>
      <Link className="underline" to={securityLink(scope)}>Review recommended settings in the scoped Security editor</Link>
    </> : <p>Select an authorized project first to inspect its effective protections. An owner manages installation protections.</p>}
    {owner && <QueryState query={registry}>{data => <details><summary className="min-h-11 cursor-pointer">Existing scoped exceptions ({data.policies.length})</summary><ul>{data.policies.map((policy, i) => <li key={i} className="py-2"><Link className="underline" to={securityLink(policy.scope)}>{scopeName(policy.scope)}</Link>: {Object.entries(policy.overrides).map(([id, value]) => `${SECURITY_COPY[id]?.label ?? id}: ${value ? 'On' : 'Off'}`).join(', ') || 'Inherited'}</li>)}</ul><p>Child overrides and hand-made choices remain. Review each scope separately; never flatten these exceptions.</p></details>}</QueryState>}
  </div>;
}
