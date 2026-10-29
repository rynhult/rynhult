import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { api, post } from '../lib/api';
import { ago } from '../lib/format';
import { Button, Dialog, ErrorNote, Field, Input, Select } from './ui';
import type { NodeRow } from './Machines';
import { OFFLINE_TITLE, useInstance, useOffline } from '../lib/queries';

export function RoleChoice({ value, onChange, auto = false }: { value: string; onChange: (value: string) => void; auto?: boolean }) {
  return <div className="space-y-2">
    <Field label="Machine role"><Select aria-label="Machine role" value={value} onChange={e => onChange(e.target.value)}>
      {auto && <option value="auto">Use OS default at enrollment</option>}
      <option value="always_on">Always-on</option><option value="workstation">Workstation</option>
    </Select></Field>
    <p className="text-xs text-muted">Always-on: expected to stay online; alerts after its offline threshold.</p>
    <p className="text-xs text-muted">Workstation: often off; no offline alerts or background retries.</p>
    {auto && <p className="text-xs text-muted">Default: Linux is Always-on; a Mac or a Windows PC (WSL) is a Workstation. You can change this anytime.</p>}
  </div>;
}

export function MachinePolicy({ node, onClose }: { node: NodeRow; onClose: () => void }) {
  const client = useQueryClient();
  const offline = useOffline();
  const [revision] = useState(node.policy_revision);
  const [publicGitBackup, setPublicGitBackup] = useState(node.public_git_backup ?? false);
  const [role, setRole] = useState(node.role ?? 'always_on');
  const [minutes, setMinutes] = useState(String((node.offline_threshold_seconds ?? 600) / 60));
  const [snooze, setSnooze] = useState('keep');
  const [downtimeZone, setDowntimeZone] = useState(node.downtime_policy?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [windows, setWindows] = useState(node.downtime_policy?.windows ?? []);
  const [untilReconnect, setUntilReconnect] = useState(node.downtime_until_reconnect ?? false);
  const updateWindow = (index: number, change: Partial<typeof windows[number]>) => setWindows(windows.map((window, i) => i === index ? { ...window, ...change } : window));
  const [sleepEnabled, setSleepEnabled] = useState(node.idle_sleep_minutes != null);
  const [sleepMinutes, setSleepMinutes] = useState(String(node.idle_sleep_minutes ?? 30));
  const save = useMutation({ mutationFn: () => post(`/api/nodes/${node.name}/policy`, {
    expected_revision: revision, public_git_backup: publicGitBackup,
    downtime_policy: { timezone: downtimeZone, windows: windows.map(window => ({ days: window.days, start: window.start,
      ...(window.until_reconnect ? { until_reconnect: true } : { end: window.end ?? '09:00' }) })) }, downtime_until_reconnect: untilReconnect,
    role, offline_threshold_seconds: Number(minutes) * 60,
    ...(role === 'workstation' ? { idle_sleep_minutes: sleepEnabled ? Number(sleepMinutes) : null } : {}),
    ...(snooze === 'keep' ? {} : { snooze_minutes: Number(snooze) }),
  }), onSuccess: () => { client.invalidateQueries({ queryKey: ['nodes'] }); client.invalidateQueries({ queryKey: ['attention'] }); onClose(); } });
  return <Dialog open onClose={onClose} title={`Machine settings: ${node.name}`} dismissible={false}
    footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" title={offline ? OFFLINE_TITLE : undefined} disabled={offline || save.isPending || !Number.isInteger(Number(minutes)) || Number(minutes) < 1 || Number(minutes) > 10080 || (role === 'workstation' && sleepEnabled && (!Number.isInteger(Number(sleepMinutes)) || Number(sleepMinutes) < 1 || Number(sleepMinutes) > 10080))} onClick={() => save.mutate()}>Save</Button></>}>
    <div className="space-y-4"><RoleChoice value={role} onChange={setRole} />
      {role === 'always_on' && <>
        <Field label="Offline alert after (minutes)"><Input aria-label="Offline alert after (minutes)" type="number" min={1} max={10080} value={minutes} onChange={e => setMinutes(e.target.value)} /></Field>
        <Field label="Planned maintenance"><Select aria-label="Planned maintenance" value={snooze} onChange={e => setSnooze(e.target.value)}>
          <option value="keep">Keep current snooze</option><option value="0">End snooze</option><option value="60">Snooze for 1 hour</option><option value="240">Snooze for 4 hours</option><option value="1440">Snooze for 24 hours</option>
        </Select></Field>
        {node.snoozed_until && <p className="text-xs text-muted">Snoozed until {new Date(node.snoozed_until).toLocaleString()}</p>}
      </>}
      <fieldset className="space-y-2 border-t border-line pt-3">
        <legend className="text-sm font-medium">Git backup transport</legend>
        <label className="flex items-start gap-2 text-[13px]">
          <input type="checkbox" name="public-git-backup" aria-describedby="public-git-backup-help" className="mt-1" checked={publicGitBackup} onChange={event => setPublicGitBackup(event.target.checked)} />
          Allow git backups over the public address
        </label>
        <p id="public-git-backup-help" className="text-xs text-muted">Off by default for each machine. LAN or Tailscale stays first. Public fallback requires verified remote access, this machine’s identity and a signed node link established privately. Turning this off ends public git transfers; private backups keep working.</p>
      </fieldset>
      <fieldset className="space-y-3 border-t border-line pt-3">
        <legend className="text-sm font-medium">Planned Downtime</legend>
        <p className="text-xs text-muted">Offline and backup alerts pause during these windows. Backup age stays visible. A reconnect queues one catch-up capture when hourly backups were missed.</p>
        <Field label="Downtime timezone"><Input aria-label="Downtime timezone" name="downtime-timezone" autoComplete="off" spellCheck={false} value={downtimeZone} onChange={e => setDowntimeZone(e.target.value)} /></Field>
        {windows.map((window, index) => <fieldset key={index} className="space-y-2 rounded border border-line p-3">
          <legend className="text-xs">Window {index + 1}</legend>
          <div className="flex flex-wrap gap-3">{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((day, d) => <label key={day} className="flex items-center gap-1 text-xs">
            <input type="checkbox" name={`downtime-${index}-${day}`} checked={window.days.includes(d)} onChange={e => updateWindow(index, { days: e.target.checked ? [...window.days, d].sort() : window.days.filter(value => value !== d) })} />{day}
          </label>)}</div>
          <label className="flex items-center gap-2 text-[13px]"><input type="checkbox" name={`downtime-${index}-until-reconnect`} aria-label={`Window ${index + 1} until it reconnects`} checked={window.until_reconnect ?? false} onChange={e => updateWindow(index, { until_reconnect: e.target.checked })} />Until it reconnects</label>
          <div className="flex flex-wrap gap-3">
            <Field label="Starts"><Input aria-label={`Window ${index + 1} start`} name={`downtime-${index}-start`} autoComplete="off" type="time" value={window.start} onChange={e => updateWindow(index, { start: e.target.value })} /></Field>
            {!window.until_reconnect && <Field label="Ends"><Input aria-label={`Window ${index + 1} end`} name={`downtime-${index}-end`} autoComplete="off" type="time" value={window.end ?? '09:00'} onChange={e => updateWindow(index, { end: e.target.value })} /></Field>}
            <Button onClick={() => setWindows(windows.filter((_, i) => i !== index))} aria-label={`Remove downtime window ${index + 1}`}>Remove</Button>
          </div>
          {window.until_reconnect && <p className="text-xs text-muted">Planned downtime from {window.start} until it reconnects</p>}
        </fieldset>)}
        <Button disabled={windows.length >= 28} onClick={() => setWindows([...windows, { days: [0, 1, 2, 3, 4, 5, 6], start: '01:30', end: '09:00' }])}>Add Downtime Window</Button>
        <p className="text-xs text-muted">Days mark when a window starts; an end before the start crosses midnight, in the selected timezone. A window ending on reconnect re-arms at its next start.</p>
        <label className="flex items-center gap-2 text-[13px]"><input type="checkbox" name="downtime-until-reconnect" checked={untilReconnect} onChange={e => setUntilReconnect(e.target.checked)} />Until it reconnects</label>
        <p className="text-xs text-muted">Starts now for a known shutdown with no fixed return time. Clears after the next observed disconnect and reconnect, or clear it here.</p>
      </fieldset>
      {role === 'workstation' && <div className="space-y-3">
        <label className="flex items-start gap-2 text-[13px]">
          <input type="checkbox" name="idle-sleep-enabled" className="mt-1" checked={sleepEnabled} onChange={e => setSleepEnabled(e.target.checked)} />
          Let this machine sleep when its sessions have been idle for N minutes
        </label>
        {sleepEnabled && <Field label="Session idle time (minutes)"><Input aria-label="Session idle time (minutes)" name="idle-sleep-minutes" autoComplete="off" type="number" min={1} max={10080} value={sleepMinutes} onChange={e => setSleepMinutes(e.target.value)} /></Field>}
        <p className="text-xs text-muted">Rynhult stops idle sessions and keeps their conversations for Resume. Working sessions and sessions that need your answer are never paused. This does not change the machine’s power settings.</p>
      </div>}
      <ErrorNote error={save.error} />
    </div>
  </Dialog>;
}

export function WorkstationSleep({ node, settings = false }: { node: NodeRow; settings?: boolean }) {
  const client = useQueryClient();
  const offline = useOffline();
  const [editing, setEditing] = useState(false);
  const resume = useMutation({ mutationFn: (id: string) => post(`/api/sessions/${id}/resume`),
    onSuccess: () => { client.invalidateQueries({ queryKey: ['nodes'] }); client.invalidateQueries({ queryKey: ['sessions'] }); } });
  if (node.role !== 'workstation' || !node.sleep) return null;
  const sleep = node.sleep;
  return <div className="min-w-0 space-y-2 text-[13px]">
    <div className="flex flex-wrap items-center gap-2">
      <p>{node.connected ? 'Keeping this machine awake' : 'Sessions last reported running'}: {sleep.running_sessions} sessions</p>
      {settings && <Button size="sm" onClick={() => setEditing(true)}>Machine settings</Button>}
    </div>
    <p className="text-xs text-muted">{node.idle_sleep_minutes == null ? 'Automatic sleep pause is off' : `Sleep after ${node.idle_sleep_minutes} idle minutes`} · Session state from Rynhult; not an OS sleep assertion reading.</p>
    {sleep.idle_sessions.length > 0 && <p className="text-xs text-muted">Idle: {sleep.idle_sessions.map((session, i) => <span key={session.id}>{i > 0 && ', '}<Link className="text-accent underline" to={`/sessions/${session.id}`}>{session.name}</Link></span>)}</p>}
    {sleep.paused_sessions.map(session => <div key={session.id} className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <p className="min-w-0 break-words"><Link className="text-accent underline" to={`/sessions/${session.id}`}>{session.name}</Link> · {session.stop_pending ? 'Pause requested; stop not confirmed. Rynhult will retry.' : 'paused so the machine can sleep'}</p>
      <Button size="sm" aria-label={`Resume ${session.name}`} disabled={resume.isPending || offline || !node.connected || session.stop_pending} onClick={() => resume.mutate(session.id)}>Resume</Button>
    </div>)}
    <ErrorNote error={resume.error} />
    {editing && <MachinePolicy node={node} onClose={() => setEditing(false)} />}
  </div>;
}

type CheckRow = { name: string; state: 'offline' | 'responding' | 'unresponsive'; latency_ms?: number; last_seen?: string; offline_since?: string; info?: { version?: string }; snapshot_available?: boolean };
type Checks = { machines: CheckRow[]; checked_at?: string };
export function MachineChecks() {
  const { host } = useInstance();
  const client = useQueryClient();
  const [until, setUntil] = useState(0);
  // Announce a check the person asked for, not the page loading or a background refresh of the list.
  const [said, setSaid] = useState('');
  const result = useQuery({ queryKey: ['machine-check'], queryFn: () => api<Checks>('/api/nodes/check') });
  const check = useMutation({ mutationFn: () => post<Checks>('/api/nodes/check'),
    onMutate: () => { setSaid(''); setUntil(Date.now() + 30000); window.setTimeout(() => setUntil(0), 30000); },
    onSuccess: data => {
      const count = (state: CheckRow['state']) => data.machines.filter(row => row.state === state).length;
      const [responding, unresponsive, offline] = [count('responding'), count('unresponsive'), count('offline')];
      setSaid(`Checked ${data.machines.length} machine${data.machines.length === 1 ? '' : 's'}: ${responding} responding`
        + (unresponsive ? `, ${unresponsive} not responding` : '') + (offline ? `, ${offline} offline` : '') + '.');
      client.setQueryData(['machine-check'], data); client.invalidateQueries({ queryKey: ['nodes'] }); client.invalidateQueries({ queryKey: ['host'] }); } });
  return <div className="space-y-2">
    <Button size="sm" disabled={check.isPending || until > Date.now()} onClick={() => check.mutate()}>{check.isPending ? 'Checking machines…' : 'Check machines now'}</Button>
    <p className="text-xs text-muted">Nodes connect outward to Rynhult. Rynhult cannot dial offline machines; checks report their last seen time. Checks are limited to once every 30 seconds.</p>
    <ErrorNote error={check.error || result.error} />
    <p role="status" className="sr-only" data-testid="machine-check-announcer">{said}</p>
    <div className="space-y-1 text-xs text-muted">
      {result.data?.machines.map(row => <p key={row.name} className="break-words"><b>{row.name === 'local' ? `${host} · main` : row.name}</b>: {row.state === 'responding'
        ? `Connected and responding · ${row.latency_ms} ms${row.info?.version ? ` · Rynhult ${row.info.version}` : ''}${row.snapshot_available === false ? ' · snapshot unavailable; update Rynhult there' : ''}`
        : row.state === 'unresponsive' ? 'Connected but not responding' : `Offline${row.offline_since ? ` since ${new Date(row.offline_since).toLocaleString()}` : ''} · last seen ${row.last_seen ? ago(row.last_seen) : 'never'}`}</p>)}
      {result.data?.checked_at && <p>Checked {ago(result.data.checked_at)}</p>}
    </div>
  </div>;
}

export function MachineState({ node }: { node: NodeRow }) {
  return <span className={`text-xs ${node.offline_alert ? 'text-wait' : 'text-muted'}`}>
    {node.planned_downtime?.active ? 'Planned downtime' : node.connected ? 'Connected' : node.role === 'workstation' ? 'Offline: workstation' : node.offline_alert ? 'Always-on machine offline' : 'Offline: always-on'}
    {!node.connected && ` · last seen ${node.last_seen ? ago(node.last_seen) : 'never'}`}
    {node.snoozed_until && new Date(node.snoozed_until).getTime() > Date.now() && ' · maintenance snoozed'}
  </span>;
}
