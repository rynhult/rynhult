import { SettingsCard as Card, SettingsCardHeader as CardHeader } from './shell/SettingsSurfaces';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useRole } from '../lib/queries';
import { LEVELS, SECURITY_DEFAULTS, type SetupLevel } from '../lib/setupLevels';
import { useSetupPreference, type SetupPreference } from '../lib/setupPreference';
import { useSaveSetup } from '../lib/saveSetupPreference';
import { SECURITY_COPY } from '../lib/security';
import { navFor } from './nav';
import { pathVisible } from '../lib/setupLevels';
import { useGroupAdministration, useInstance } from '../lib/queries';
import { buttonClass, Button, ErrorNote, Field, QueryState, Select } from './ui';

export function SetupLevelSetting({ firstRun = false }: { firstRun?: boolean }) {
  const { username } = useRole(); const query = useSetupPreference(username);
  if (firstRun) return <SetupLevelPrompt value={query.data} />;
  return <Card><CardHeader title="Setup level" subtitle="Your layout and suggested defaults" />
    <div className="space-y-4 p-4"><QueryState query={query}>{value => <SetupForm value={value} />}</QueryState></div>
  </Card>;
}
function SetupLevelPrompt({ value }: { value: SetupPreference | undefined }) {
  const save = useSaveSetup();
  if (!value || value.dismissed || value.level != null) return null;
  return <aside aria-label="Choose what you see" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
    <span>Choose what you see in <Link className="underline" to="/settings#setup-level">Settings → Setup level</Link>.</span>
    <Button disabled={save.isPending} onClick={() => save.mutate({ level: value.level, dismissed: true })} aria-label="Dismiss setup prompt">Dismiss</Button>
    <ErrorNote error={save.error} />
  </aside>;
}
function SetupForm({ value }: { value: SetupPreference }) {
  const { owner } = useRole(); const groupAdmin = useGroupAdministration(); const instance = useInstance();
  const [level, setLevel] = useState<SetupLevel | null>(value.level);
  const save = useSaveSetup();
  useEffect(() => setLevel(value.level), [value.level]);
  const selected = LEVELS.find(x => x.id === level);
  const dirty = level !== value.level;
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  return <div className="space-y-4">
    <p className="text-sm text-muted">Choose what you normally see. Your permissions, running work, sign-ins and Security settings stay as they are. Change this here at any time.</p>
    <Field label="Setup level"><Select name="setup-level" value={level ?? ''} disabled={save.isPending} onChange={e => { setLevel(e.target.value === '' ? null : Number(e.target.value) as SetupLevel); save.reset(); }}>
      <option value="">Current layout · not yet chosen</option>
      {LEVELS.map(x => <option value={x.id} key={x.id}>{x.id} · {x.name}{x.id === 1 ? ' (recommended)' : ''}</option>)}
    </Select></Field>
    {selected && <>
      <p>{selected.description}</p>
      <details><summary className="cursor-pointer py-2 text-sm">Compare levels</summary><ul className="space-y-2 pt-2 text-sm">{LEVELS.map(x => <li key={x.id}><strong>{x.id} · {x.name}</strong> — {x.description}</li>)}</ul></details>
      <p className="text-sm"><strong>Normally visible:</strong> {navFor(owner, groupAdmin, instance.features).flatMap(g => g.items).filter(n => pathVisible(level, n.to)).map(n => n.label).join(', ')}.</p>
      <p className="text-sm text-muted">More tools reveals other authorized areas in the new layout. Saved links and recovery remain available.</p>
      <section aria-label="Recommended team"><h3 className="font-semibold">Suggested team</h3><p className="text-sm">{selected.team}</p>
        <p className="text-sm text-muted">Use these suggestions when choosing an agent in New session. Saving this layout creates no team and starts no work. Reusable personal team profiles are Later.</p>
      </section>
      <section aria-label="Recommended Security"><h3 className="font-semibold">Quiet protections stay on</h3>
        <p className="text-sm">Recommended starting values: all eleven protections On. Existing customized or inherited choices are preserved. Review effective protections and choose a scope separately in Security.</p>
        <details><summary className="cursor-pointer py-2 text-sm">See recommended Security values</summary><ul className="space-y-1 pt-2 text-sm">{Object.keys(SECURITY_DEFAULTS).map(id => <li key={id}>{SECURITY_COPY[id].label}: On</li>)}</ul></details>
        {level === 0 && <div className="space-y-1 pt-2 text-sm"><p className="font-medium">Run Build tasks without provider permission prompts</p><p>Optional, separately reviewed in Security for a qualified provider, machine and account. This may also remove the provider’s sandbox. Keep prompts On and still use Expert. Read-only roles retain their restrictions.</p></div>}
        <Link className={buttonClass()} to="/settings#security">{owner ? 'Review matching security defaults' : 'View effective protections'}</Link>
      </section>
      {level !== null && level <= 2 && <p className="text-sm text-muted">Automatic tool permission not available in this setup. Account-wide tool changes keep their existing approval path; no remembered consent is created here.</p>}
    </>}
    <div className="flex flex-wrap gap-2">
      <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate({ level, dismissed: true })}>{save.isPending ? 'Saving…' : 'Save what I see'}</Button>
    </div><ErrorNote error={save.error} />
  </div>;
}
