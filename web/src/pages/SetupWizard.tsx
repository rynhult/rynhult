import './SetupWizard.css';
import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { useHealth, useInstance, useProjects, useRole } from '../lib/queries';
import { useSetupPreference, type SetupPreference } from '../lib/setupPreference';
import { useSaveSetup } from '../lib/saveSetupPreference';
import { ENTERPRISE_NOTE, STEPS, type WizardChange, type WizardStep } from '../lib/setupWizard';
import { LEVELS, type SetupLevel } from '../lib/setupLevels';
import { SetupInfrastructureStatus } from '../components/SetupInfrastructureStatus';
import type { AccountHealth } from '../lib/types';
import { Surface as Card, ShellPage as Page } from '../components/shell/PagePrimitives';
import { announce, Button, ErrorNote, Field, QueryState, Select } from '../components/ui';
import { RecoverySecurity } from '../components/RecoverySecurity';
import { SetupProtectionReview } from '../components/SetupProtectionReview';
import { SetupPreviewFeatures } from '../components/SetupPreviewFeatures';
import { useNewSession } from '../components/newSession';

export function SetupWizard() {
  const { username } = useRole(); const query = useSetupPreference(username);
  return <Page><QueryState query={query}>{value => <Wizard value={value} />}</QueryState></Page>;
}
function Wizard({ value }: { value: SetupPreference }) {
  const { owner, account } = useRole(); const instance = useInstance(); const health = useHealth(); const navigate = useNavigate(); const launch = useNewSession();
  const progress = value.wizard;
  const first = STEPS.find(([id]) => (progress.steps[id]?.state ?? 'pending') === 'pending')?.[0] ?? STEPS.find(([id]) => progress.steps[id]?.state === 'deferred')?.[0] ?? 'finish';
  const [step, setStep] = useState<WizardStep>(first); const heading = useRef<HTMLHeadingElement>(null);
  const [level, setLevel] = useState<SetupLevel>(value.level ?? 1); const save = useSaveSetup();
  const [binding, setBinding] = useState(progress.binding ? JSON.stringify(progress.binding) : '');
  const [projectId, setProjectId] = useState(progress.project_id ?? 0);
  const projects = useProjects();
  const nodes = useQuery({ queryKey: ['setup-nodes', owner], queryFn: () => api<{ id: string; name: string; connected: boolean; status: string; accounts: string[] }[]>('/api/nodes'), enabled: owner });
  const choices = [ ...(account ? [{ machine: 'local', machine_id: null as string | null, account, ready: health.data?.broker === true }] : []), ...(nodes.data ?? []).flatMap(node => node.accounts.map(a => ({ machine: node.name, machine_id: node.id, account: a, ready: node.connected && node.status === 'active' }))) ];
  // Members discover only their own authorized bindings through projects.
  if (!owner) for (const project of projects.data ?? []) if (project.account && !choices.some(b => b.machine === project.host && b.account === project.account)) choices.push({ machine: project.host, machine_id: null, account: project.account, ready: !project.machine_offline });
  const selected = choices.find(b => JSON.stringify({ machine: b.machine, account: b.account, machine_id: b.machine_id }) === binding);
  const liveBinding = choices.find(b => b.machine === progress.binding?.machine && b.account === progress.binding.account && (!progress.binding.machine_id || b.machine_id === progress.binding.machine_id));
  const scope = new URLSearchParams(progress.binding ? { machine: progress.binding.machine, account: progress.binding.account } : {}).toString();
  const accounts = useQuery({ queryKey: ['setup-accounts', scope, owner, account], queryFn: () => api<AccountHealth>(`/api/accounts?${scope}`), enabled: !!progress.binding && step === 'provider', refetchInterval: 10000 });
  useEffect(() => { heading.current?.focus(); }, [step]);
  useEffect(() => {
    if (step === 'machine' && !progress.binding && !binding && account && health.data?.broker) setBinding(JSON.stringify({ machine: 'local', account, machine_id: null }));
  }, [step, progress.binding, binding, account, health.data?.broker]);
  useEffect(() => { setLevel(value.level ?? 1); setBinding(progress.binding ? JSON.stringify(progress.binding) : ''); setProjectId(progress.project_id ?? 0); }, [value.level, progress.revision, progress.binding, progress.project_id]);
  const index = STEPS.findIndex(([id]) => id === step); const current = STEPS[index];
  const next = async (state: WizardChange['state']) => {
    const change: WizardChange = { revision: progress.revision, step, state };
    if (step === 'machine') change.binding = state === 'completed' && selected ? { machine: selected.machine, account: selected.account, machine_id: selected.machine_id } : null;
    if (step === 'project') change.project_id = state === 'completed' ? projectId : null;
    try {
      await save.mutateAsync({ level: step === 'level' ? level : value.level, dismissed: true, wizard: change });
      announce(state === 'deferred' ? `${current[1]} deferred. ${current[2]}` : `${current[1]} saved.`);
      if (step === 'finish') navigate('/'); else setStep(STEPS[index + 1][0]);
    } catch { /* The inline error remains and the query reloads on conflict. */ }
  };
  const project = projects.data?.find(p => p.id === projectId);
  const canComplete = step !== 'machine' || !!selected?.ready;
  const selectedProjectReady = !!project && !project.archived && !project.machine_offline && (!progress.binding || project.host === progress.binding.machine && project.account === progress.binding.account);
  const projectValid = step !== 'project' || selectedProjectReady;
  const recoverySaved = !progress.recovery_required || progress.steps.recovery?.state === 'completed';
  return <div className="setup-wizard space-y-5 min-w-0 [overflow-wrap:anywhere]">
    <h1 className="text-xl font-semibold">Set up Rynhult</h1>
    <p>Back keeps saved changes. Optional steps can wait; deferred items remain unfinished. Checklist confirmations are not proof of readiness. Launch checks current machine, account, project and effective key again.</p>
    <Link className="inline-flex items-center underline" to="/">Go to Overview</Link>
    <ol aria-label="Setup steps" className="grid gap-1 sm:grid-cols-2 lg:grid-cols-3">{STEPS.map(([id, title], i) => <li key={id}><Button variant="ghost" aria-current={step === id ? 'step' : undefined} disabled={save.isPending || (!recoverySaved && id !== 'recovery')} className="h-auto w-full justify-start whitespace-normal text-left" onClick={() => { save.reset(); setStep(id); }}>{i + 1}. {title} · {progress.steps[id]?.state ?? 'pending'}{step === id ? ' · current' : ''}</Button></li>)}</ol>
    <Card><div className="space-y-4 p-4">
      <h2 ref={heading} tabIndex={-1} className="text-lg font-semibold">{current[1]}</h2>
      <p>{current[2]}</p>
      {step === 'recovery' && <>{!progress.recovery_required && <p>This is an optional recovery review for an existing person. Company-only sign-ins use company recovery; you may finish this step later.</p>}<p>If the original setup response was interrupted, create a fresh set below using the authenticated recovery workflow. This replaces the old codes; it never recreates the owner. Continue only after storing your codes offline.</p><RecoverySecurity /><Link className="inline-flex items-center underline" to="/settings#passkeys">Add an optional passkey</Link></>}
      {step === 'level' && <><p>Levels change guidance and navigation only. Every level recommends all protections On and preserves installation feature defaults. Levels 4–6 use the small-team steps.</p><p>{ENTERPRISE_NOTE}</p><Field label="Setup level"><Select value={level} onChange={e => setLevel(Number(e.target.value) as SetupLevel)}>{LEVELS.map(item => <option key={item.id} value={item.id}>{item.id} · {item.name}{item.id === 1 ? ' (recommended)' : ''}</option>)}</Select></Field><p>{LEVELS[Math.min(level, 3)].description}</p></>}
      {step === 'security' && <><SetupProtectionReview owner={owner} progress={progress} />{value.level === 0 && <p>Expert may separately review provider prompts and sandbox consequences in Security. Unrestricted is never chosen by level.</p>}</>}
      {step === 'machine' && <><p>Select an observed machine/account. A disconnected machine remains unavailable. Adding a node is an explicit installation/link workflow; nothing changes your OS here.</p><ErrorNote error={nodes.error} /><Field label="Machine and account"><Select value={binding} onChange={e => setBinding(e.target.value)}><option value="">Choose a binding</option>{choices.map(b => { const key = JSON.stringify({ machine: b.machine, account: b.account, machine_id: b.machine_id }); return <option key={key} value={key} disabled={!b.ready}>{b.machine} / {b.account} · {b.ready ? 'connected' : 'unavailable'}</option>; })}</Select></Field>{binding && !selected?.ready && <p role="status">Saved binding is unavailable. Select a ready binding or finish later.</p>}<Link className="inline-flex items-center underline" to={owner ? '/host#machines' : '/guide/User-Guide/Install'}>Machine readiness and installation steps</Link></>}
      {step === 'provider' && <><p>A provider subscription and API usage have separate billing. Use your own sign-in; never share credentials. Vault keys require explicit project grants after a project exists. No credentials are stored in this checklist.</p>{progress.binding ? <><p>Selected binding: {progress.binding.machine} / {progress.binding.account}</p><QueryState query={accounts}>{data => <pre className="whitespace-pre-wrap text-sm">{Object.entries(data).filter(([, status]) => status && typeof status === 'object' && 'logged_in' in status).map(([key, status]) => `${key}: ${status && typeof status === 'object' && 'logged_in' in status && status.logged_in ? 'signed in' : 'not signed in / not reported'}`).join('\n') || 'Readiness not reported. Inspect Providers and project key assignment.'}</pre>}</QueryState></> : <p>Select a machine/account first, or finish this later.</p>}<Link className="inline-flex items-center underline" to={`/providers${scope ? `?${scope}` : ''}`}>Open provider sign-in and keys</Link>{progress.project_id && <Link className="inline-flex items-center underline" to={`/projects/${progress.project_id}`}>Review effective project key and grants</Link>}</>}
      {step === 'project' && <><p>Create, clone or register a project through Projects, then select it here. After an interruption, inspect the existing projects before creating another. Trust is a separate reviewed action.</p><Field label="First project"><Select value={projectId || ''} onChange={e => setProjectId(Number(e.target.value))}><option value="">Select an existing project</option>{projects.data?.map(p => <option key={p.id} value={p.id}>{p.name} · {p.host} / {p.account}</option>)}</Select></Field><ErrorNote error={projects.error} />{projectId !== 0 && !projectValid && <p role="status">Project is unavailable, archived or does not match the selected binding. Select again or finish later.</p>}<Link className="inline-flex items-center underline" to="/projects">Create or select project</Link>{(value.level ?? 1) >= 3 && <><p>People and groups are optional. Each person keeps their own sign-ins and grants. No invitations are sent by setup.</p><Link className="inline-flex items-center underline" to={owner ? '/settings#people' : '/groups'}>Review people and groups</Link></>}</>}
      {step === 'backups' && <><SetupInfrastructureStatus kind="backups" owner={owner} /><p>Recommend an off-machine target first. USB is the second option; a folder on the same disk does not protect against disk loss. Installer timer copies on the same disk are separate from off-machine backups.</p><p>Check target configured, successful capture and recovery kit saved separately. Configuration alone is not proof of recovery. Follow the privileged helper, encryption and target checks in Host → Backups; restore is a manual reviewed procedure.</p>{owner ? <Link className="inline-flex items-center underline" to="/host#host-backups">Inspect backups and configure a target</Link> : <p>An owner must configure installation backups.</p>}</>}
      {step === 'updates' && <><SetupInfrastructureStatus kind="updates" owner={owner} /><p>Use signed tagged releases. An untagged development checkout requires manual updates. Saving a level never opts into automatic updates or unsigned releases; setup installs no updates.</p><p>Installed version: {instance.version ?? 'not reported'}. Review the configured repository and eligibility in Updates.</p>{owner ? <Link className="inline-flex items-center underline" to="/settings#updates">Review update repository and eligibility</Link> : <p>An owner manages installation update configuration.</p>}</>}
      {step === 'features' && <SetupPreviewFeatures owner={owner} />}
      {step === 'finish' && <><p>Selected machine/account: {progress.binding ? `${progress.binding.machine} / ${progress.binding.account}` : 'none'} · {liveBinding?.ready ? 'connected' : 'unavailable; select a current binding'}. Project: {project?.name ?? 'none or unavailable'} · {selectedProjectReady ? 'available' : 'unavailable or binding changed; review project selection'}.</p><p>Selected level: {value.level === null ? 'not selected' : `${value.level} · ${LEVELS[value.level].name}`}.</p>{(value.level ?? 0) >= 4 && <p>{ENTERPRISE_NOTE}</p>}<ul className="space-y-2">{STEPS.filter(([id]) => id !== 'finish').map(([id, title, consequence]) => <li key={id}><strong>{title}</strong>: {progress.steps[id]?.state ?? 'pending'}{progress.steps[id]?.state !== 'completed' && ` — ${consequence}`}</li>)}</ul><p>Completed means you confirmed this step. Current resource health, authority and provider readiness still apply. Hand-made choices are preserved; re-runs require fresh unselected diffs and individual confirmation in Security or feature review.</p><Button disabled={!selectedProjectReady || !liveBinding?.ready} onClick={() => launch({ project_id: projectId })}>Start first session</Button></>}
      <div className="flex flex-wrap gap-3">
        {index > 0 && <Button disabled={save.isPending} onClick={() => { save.reset(); setStep(STEPS[index - 1][0]); }}>Back</Button>}
        <Button variant="primary" disabled={save.isPending || !canComplete || !projectValid || (step !== 'recovery' && !recoverySaved)} onClick={() => void next('completed')}>{save.isPending ? 'Saving…' : step === 'recovery' ? 'I saved my recovery codes · Continue' : step === 'finish' ? 'Finish setup' : 'Confirm reviewed · Continue'}</Button>
        {!['level', 'finish', ...(progress.recovery_required ? ['recovery'] : [])].includes(step) && <Button disabled={save.isPending} onClick={() => void next('deferred')}>Finish later</Button>}
      </div>
      <ErrorNote error={save.error} />
      {save.error && <p role="status">The latest progress has been reloaded. Review this step before retrying; no resource action is replayed.</p>}
    </div></Card>
  </div>;
}
