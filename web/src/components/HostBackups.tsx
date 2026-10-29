import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Archive } from 'lucide-react';
import { api, downloadFile, post } from '../lib/api';
import { ago, size } from '../lib/format';
import { useConfirm } from './confirm';
import { Badge, Button, Card, CardHeader, ErrorNote, Field, QueryState, Select } from './ui';

type Target = {
  schedule_seconds?:number; verify_interval_seconds?:number; checked_at?:number; check_kind?:string;
  cost_estimate?:{captures:number;full_verifications:number;upload_bytes:number|null;download_bytes:number|null;s3_policy_requests:number|null;restic_requests?:number|null;basis:string};
  last_successful_off_machine_at?: number; next_capture_expected_at?: number; planned_downtime?: { active: boolean; until_reconnect?: boolean };
  node_name?: string; machine_removed?: boolean; kind?: string; setup_verified_at?: number; id: string; repository: string; enabled: boolean; status: string; stale: boolean;
  immutable_until?: number; immutability_checked_at?: number; captured_at?: number; verified_at?: number; restore_tested_at?: number; backup_id?: string; snapshot_id?: string; bytes?: number;
  retention_runway_days?: number | null; retention_warning?: boolean; retention_lapsed?: boolean | null;
  last_job?: { state: string; error?: string; note?: string; repository_locked?:boolean;
    locks?:{hostname:string;pid:number;age_seconds:number;exclusive:boolean}[] };
};
type State = { targets: Target[]; coverage: string; quarterly_due: boolean; drill: {at?: number} };
const age = (time?: number) => time ? ago(new Date(time * 1000).toISOString()) : 'Never';
type Generation = {backup_id:string;snapshot_id:string;time:string;duplicate:boolean;note?:string};
type History = {items:Generation[];verified_missing:boolean};

function BackupHistory({id, busy, onDrill}:{id:string;busy:boolean;onDrill:(item:Generation) => void}) {
  const [open, setOpen] = useState(false);
  const query = useQuery({queryKey:['backup-history',id],queryFn:() => api<History>(`/api/host/backups/${id}/snapshots`),enabled:open});
  const verify = useMutation({mutationFn:(item:Generation) => post(`/api/host/backups/${id}/verify`,{confirmed:true,backup_id:item.backup_id,snapshot_id:item.snapshot_id})});
  return <div className="space-y-2">
    <Button aria-expanded={open} onClick={() => setOpen(!open)}>{open ? 'Hide backups' : 'List backups'}</Button>
    {open && <QueryState query={query}>{({items, verified_missing}) => <>
      {verified_missing && <p role="alert" className="text-wait">The target no longer lists the last verified backup. Its history may have been rolled back or deleted: check the target before relying on it.</p>}
      {items.some(item => item.duplicate) && <p className="text-wait">Some backup IDs appear more than once. Only the snapshot ID you recorded identifies the genuine backup.</p>}
      <p className="text-muted">Test restore validates a selected generation in private scratch and deletes the scratch afterwards. To recover files on a replacement, follow <Link to="/guide/User-Guide/Backups#test-recovery-every-quarter" className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center rounded-md border border-line-strong px-3 underline underline-offset-2">the offline recovery guide</Link>.</p>
      {!items.length && <p>No complete backup generations.</p>}
      {items.map(item => <div key={item.snapshot_id} className="flex flex-wrap items-center gap-2">
        <code className="break-all">{item.backup_id}</code><span className="text-muted">{new Date(item.time).toLocaleString()}</span>
        <span className="text-muted">snapshot <code title={item.snapshot_id}>{item.snapshot_id.slice(0, 12)}</code></span>
        {item.note && <span className="text-muted">{item.note}</span>}
        {item.duplicate && <Badge tone="wait">Duplicate ID</Badge>}
        <Button disabled={busy || verify.isPending} aria-label={`Test restore of snapshot ${item.snapshot_id.slice(0, 12)}`} onClick={() => verify.mutate(item)}>Test restore</Button>
        <Button aria-label={`Record restore drill of snapshot ${item.snapshot_id.slice(0, 12)}`} onClick={() => onDrill(item)}>Record restore drill</Button>
      </div>)}
    </>}</QueryState>}
    {verify.isSuccess && <p role="status">Restore test queued. Check the last job for its result.</p>}
    <ErrorNote error={verify.error} />
  </div>;
}

function BackupSchedule({target, busy, refresh}:{target:Target;busy:boolean;refresh:() => void}) {
  const [capture, setCapture] = useState(target.schedule_seconds ?? 86400);
  const [verify, setVerify] = useState(target.verify_interval_seconds ?? 604800);
  useEffect(() => {setCapture(target.schedule_seconds ?? 86400);setVerify(target.verify_interval_seconds ?? 604800);},
    [target.id,target.schedule_seconds,target.verify_interval_seconds]);
  const save = useMutation({mutationFn:() => post(`/api/host/backups/${target.id}/schedule`,
    {confirmed:true,schedule_seconds:capture,verify_interval_seconds:verify}),onSuccess:refresh});
  const estimate = target.cost_estimate;
  const volume = (n:number|null) => n == null ? 'Unknown until a capture completes' : `${size(n)} / 30 days`;
  return <div className="space-y-2">
    <form aria-label={`Backup schedule for ${target.repository}`} className="flex flex-wrap items-end gap-2" onSubmit={e => {e.preventDefault();if(!save.isPending)save.mutate();}}>
      <Field label="Capture"><Select name="capture" value={capture} disabled={busy||save.isPending} onChange={e => setCapture(Number(e.target.value))}>
        <option value={86400}>Daily</option><option value={3600}>Hourly</option>
      </Select></Field>
      <Field label="Full restore verification"><Select name="verification" value={verify} disabled={busy||save.isPending} onChange={e => setVerify(Number(e.target.value))}>
        <option value={604800}>Weekly</option><option value={86400}>Daily</option><option value={0}>Every capture (high transfer)</option>
      </Select></Field>
      <Button type="submit" disabled={busy||save.isPending}>{save.isPending?'Saving…':'Save schedule'}</Button>
    </form>
    <p className="text-muted">Metadata check after every capture. Full verification also reads all repository data and restores one snapshot. You can test a restore on demand below.</p>
    <p className="text-muted" data-testid="backup-cost-estimate">30-day estimate when enabled: upload {volume(estimate?.upload_bytes??null)} · full verification download {volume(estimate?.download_bytes??null)}.
      {estimate?.restic_requests!=null&&<> Small-repository scenario: about {Math.ceil(estimate.restic_requests).toLocaleString()} native restic requests / 30 days.</>}
      {estimate?.s3_policy_requests!=null&&<> S3 protection checks: at least {Math.ceil(estimate.s3_policy_requests).toLocaleString()} requests, plus restic requests.</>}
      {estimate?.s3_policy_requests==null&&estimate?.restic_requests==null&&' Request volume depends on repository object count; no request measurement is available for this target.'}
    </p>
    {estimate&&<p className="text-muted">{estimate.basis}</p>}
    <p className="text-wait">Cloud providers bill egress and may charge per request. These estimates exclude metadata transfer, initial setup, on-demand tests, retries and storage charges.</p>
    {save.isSuccess&&<p role="status">Backup schedule saved.</p>}
    <ErrorNote error={save.error}/>
  </div>;
}

export function HostBackups() {
  const { hash } = useLocation();
  const section = useRef<HTMLElement>(null);
  const client = useQueryClient();
  const confirm = useConfirm();
  const query = useQuery({queryKey:['host-backups'], queryFn:() => api<State>('/api/host/backups'), refetchInterval:15000});
  const refresh = () => client.invalidateQueries({queryKey:['host-backups']});
  const action = useMutation({mutationFn:({id, action}:{id:string;action:string}) =>
    post(`/api/host/backups/${id}/${action}`, {confirmed:true}), onSuccess:refresh});
  const exportKit = useMutation({mutationFn:() => downloadFile('/api/host/backups/kit', 'acc-recovery-kit.json', {confirmed:true})});
  const drill = useMutation({mutationFn:({id, backup, snapshot}:{id:string;backup:string;snapshot?:string}) => post('/api/host/backups/drill',
    {confirmed:true,target_id:id,backup_id:backup,...(snapshot ? {snapshot_id:snapshot} : {})}), onSuccess:refresh});
  // The drill may have used an older generation than the latest: each listed backup can be the one recorded.
  const recordDrill = async (id:string, backup:string, snapshot?:string) => {
    if (await confirm({title:'Record a completed restore drill?',body:`Confirm you used your saved kit to restore backup ${backup}${snapshot ? ` (snapshot ${snapshot.slice(0, 12)})` : ''} on an isolated replacement and checked the startup hold. This records your attestation.`,confirm:'Record restore drill'})) drill.mutate({id, backup, snapshot});
  };
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (hash !== '#host-backups') return;
    setOpen(true);
    section.current?.scrollIntoView({ block: 'start' });
    section.current?.focus({ preventScroll: true });
  }, [hash]);
  const askExport = async () => {
    if (await confirm({title:'Export recovery kit?',body:'This file unlocks all Rynhult secrets in your backups. Keep it in a password manager plus one offline copy. Test a restore every quarter. Never attach it to a session.',confirm:'Export recovery kit'})) exportKit.mutate();
  };
  // One status line on Host (UX review 2, R3): amber only when something needs doing; the details on request.
  const status = (state: State) => {
    const targets = state.targets.filter(t => !t.machine_removed);
    if (!targets.length) return {tone:'text-wait', text:state.targets.length ? 'No active targets · machine removed' : 'Not set up · no backup target yet'};
    const latest = Math.max(...targets.map(t => t.last_successful_off_machine_at ?? 0));
    const localLatest = Math.max(...targets.filter(t => !['node', 's3'].includes(t.kind ?? '')).map(t => t.captured_at ?? 0), 0);
    const planned = targets.some(t => t.planned_downtime?.active);
    const failed = targets.some(t => !t.planned_downtime?.active && t.last_job?.state === 'failed');
    const stale = targets.some(t => t.stale && !t.planned_downtime?.active);
    const off = targets.every(t => !t.enabled);
    const retentionDue = targets.some(t => t.enabled && t.retention_warning);
    const plannedTargets = targets.filter(t => t.planned_downtime?.active && t.enabled);
    const expectedTimes = plannedTargets.flatMap(t => t.next_capture_expected_at ? [t.next_capture_expected_at] : []);
    const expected = !plannedTargets.length ? '' : expectedTimes.length
      ? `next capture expected ${new Date(Math.min(...expectedTimes) * 1000).toLocaleString()} if connected; earlier on reconnect`
      : 'next capture expected on reconnect';
    const text = [latest ? `Last off-machine backup ${age(latest)}` : 'No off-machine backup yet', localLatest ? `Last local backup ${age(localLatest)}` : '', failed ? 'last job failed' : '', stale ? 'stale' : '',
      planned ? 'Planned downtime' : '', expected, retentionDue ? 'retention maintenance due' : '', off ? 'scheduled backups off' : '', state.quarterly_due ? 'restore test due' : ''].filter(Boolean).join(' · ');
    return {tone: failed || stale || retentionDue || !latest || state.quarterly_due ? 'text-wait' : 'text-muted', text};
  };
  return <section id="host-backups" ref={section} tabIndex={-1} className="scroll-mt-4"><Card>
    <CardHeader title="Backups" icon={Archive}
      subtitle={query.data ? <span className={status(query.data).tone} data-testid="backup-status">{status(query.data).text}</span> : query.error ? 'Status unavailable' : 'Checking…'}
      action={<div className="flex flex-wrap gap-2">
        {query.data && !query.data.targets.length && <Link to="/host/backups/setup" className="inline-flex h-7 pointer-coarse:h-11 items-center rounded-md border border-line-strong px-2.5 text-[12.5px] text-fg hover:bg-panel-2">Set up</Link>}
        {query.data && !!query.data.targets.length && <Link to="/host/backups/setup" className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center rounded-md border border-line-strong px-3 underline underline-offset-2">Add target</Link>}
        <Button size="sm" disabled={exportKit.isPending} onClick={askExport}>{exportKit.isPending ? 'Exporting…' : 'Export recovery kit'}</Button>
        <Button size="sm" variant="ghost" aria-expanded={open} aria-controls="host-backups-details" onClick={() => setOpen(!open)}>{open ? 'Hide details' : 'Details'}</Button>
      </div>} />
    {(open || query.error) && <div id="host-backups-details" className="border-t border-line"><QueryState query={query} errorTitle="Couldn't read the backup status.">{state => <div className="space-y-4 p-4 text-[13px]">
      <p className="text-muted">{state.coverage}</p>
      <p className="text-muted">Per-target schedule · stale after twice its capture interval · 48 hourly / 30 daily / 12 monthly retention · 10 MiB/s</p>
      <p className="text-muted">Protection requires an independent immutable or offline copy with a 30-day lock.</p>
      {state.quarterly_due && <p role="status" className="text-wait">Quarterly restore test due</p>}
      <p className="text-muted">Last owner restore drill: {age(state.drill.at)}{!state.drill.at && state.targets.some(t => t.backup_id) ? ' · after restoring a backup on an isolated machine, select Record restore drill below' : ''}</p>
      {!state.targets.length && <p>Choose an enrolled Linux machine or S3-compatible storage with Set up, or provision a local target using the Guide: <Link to="/guide/User-Guide/Backups#before-you-start" className="text-accent">Backups → Before you start</Link>. Rynhult never takes a destination path from the dashboard.</p>}
      {state.targets.map(target => {
        const busy = action.isPending || ['queued','running'].includes(target.last_job?.state ?? '');
        return <div key={target.id} className="space-y-2 border-t border-line pt-4">
          <div className="flex flex-wrap items-center gap-2"><span className="break-all font-mono">{target.machine_removed ? (target.node_name ?? 'Backup target') : target.repository}</span>
            <Badge>{target.planned_downtime?.active ? 'Planned downtime' : target.status}</Badge>{target.stale && <Badge tone="wait">Stale</Badge>}</div>
          {['node', 's3'].includes(target.kind ?? '') && <p className="text-muted">Last successful off-machine backup: <time title={target.last_successful_off_machine_at ? new Date(target.last_successful_off_machine_at * 1000).toLocaleString() : undefined}>{age(target.last_successful_off_machine_at)}</time></p>}
          {target.planned_downtime?.active && <p className="text-muted">{!target.enabled ? 'Scheduled backups paused' : target.next_capture_expected_at ? `Next capture expected at ${new Date(target.next_capture_expected_at * 1000).toLocaleString()}, if the machine is connected; earlier on reconnect` : 'Next capture expected when the machine reconnects'}</p>}
          <p className="text-muted">Captured {age(target.captured_at)} · metadata checked {age(target.checked_at)} · full verified {age(target.verified_at)} · restore-tested {age(target.restore_tested_at)}{target.bytes != null ? ` · ${size(target.bytes)}` : ''}</p>
          {!target.machine_removed&&<BackupSchedule target={target} busy={busy} refresh={refresh}/>}
          {target.kind === 's3' && <p className="text-muted">Retention checked {age(target.immutability_checked_at)} · minimum object retention deadline: {target.immutable_until ? new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short'}).format(new Date(target.immutable_until*1000)) : 'Unverified'}</p>}
          {target.kind === 's3' && <div className="space-y-1">
            <p data-testid="retention-runway" className={target.retention_warning ? 'text-wait' : 'text-muted'}>
              Retention runway: {target.retention_runway_days == null ? 'Unverified' : `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(target.retention_runway_days)} days`}
              {target.retention_lapsed ? ' · 30-day protection window has lapsed' : target.retention_warning ? ' · Maintenance due' : ''}
              {target.retention_runway_days != null && ' · until the last verified inventory falls below 30 days without maintenance'}
            </p>
            <p className="text-muted">Independent maintenance is off by default. Its key stays outside this main. Protection is checked again by captures.</p>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              <Link className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center underline underline-offset-2" to="/guide/User-Guide/Backups#independent-retention-maintenance">Retention maintenance guide</Link>
              <Link className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center underline underline-offset-2" to="/host/backups/setup?kind=s3">Start fresh S3 repository</Link>
            </div>
          </div>}
          {target.backup_id && <p className="break-all text-muted">Backup ID: <code>{target.backup_id}</code>{target.snapshot_id && <> · snapshot ID: <code>{target.snapshot_id}</code></>}</p>}
          <div aria-live="polite" className="break-words">{target.last_job && <p>Last job: {target.last_job.state}{target.last_job.note ? ` · ${target.last_job.state === 'complete' ? target.last_job.note : 'catch-up capture'}` : ''}{target.last_job.error ? ` · ${target.last_job.error}` : ''}</p>}</div>
          {target.last_job?.repository_locked && <div className="space-y-1 text-wait">
            <p>Lock details reported by the last job:</p>
            {(target.last_job.locks ?? []).map((lock, i) => <p className="break-words" key={i}>{lock.hostname} · PID {lock.pid} · age {lock.age_seconds}s · {lock.exclusive ? 'exclusive' : 'non-exclusive'}</p>)}
            {!target.last_job.locks?.length && <p>Lock details unavailable. Active or unknown locks are left in place.</p>}
          </div>}
          {action.isSuccess && action.variables?.id === target.id && action.variables.action === 'unlock' && <p role="status">Stale lock check queued. Check the last job for removed and remaining locks, then retry the backup.</p>}
          <div className="flex flex-wrap gap-2">
            {!target.machine_removed && <>
            {target.last_job?.repository_locked && <Button disabled={busy} onClick={async () => {
              if (await confirm({title:'Remove stale lock?',body:'Runs a stale-only lock check on this backup target. Active locks are never forced. Removes only locks older than 30 minutes (restic refreshes live ones every 5) or left by this server’s own restic runs that have ended, one by one. This is audited; retry the backup afterwards.',confirm:'Remove stale lock'})) action.mutate({id:target.id,action:'unlock'});
            }}>Remove stale lock</Button>}
            {['node','s3'].includes(target.kind ?? '') && <Link to={`/host/backups/setup?target=${target.id}`} className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center rounded-md border border-line-strong px-3 underline underline-offset-2">Target setup</Link>}
            <Button disabled={busy || (['node','s3'].includes(target.kind ?? '') && !target.setup_verified_at)} onClick={() => action.mutate({id:target.id,action:'run'})}>Back up now</Button>
            {target.enabled && <Button disabled={action.isPending} onClick={() => action.mutate({id:target.id,action:'disable'})}>Pause scheduled backups</Button>}
            {!target.enabled && <Button disabled={busy} onClick={async () => {
              if (await confirm({title:'Enable scheduled backups?',body:'Confirm you saved the recovery kit in a password manager plus one offline copy. A full verified test capture is required. Daily capture and weekly full verification are the defaults; use the schedule above to choose hourly.',confirm:'Enable scheduled backups'})) action.mutate({id:target.id,action:'enable'});
            }}>Enable scheduled backups</Button>}
            {target.kind !== 'node' && target.kind !== 's3' && <Button disabled={busy || !target.verified_at} onClick={async () => {
              if (await confirm({title:'Prune retained backups?',body:'Deletes generations outside 48 hourly, 30 daily and 12 monthly retention. Only an owner can request this.',confirm:'Prune backups',danger:true})) action.mutate({id:target.id,action:'prune'});
            }}>Prune backups</Button>}
            </>}
            {(target.machine_removed || target.kind === 's3') && <Button disabled={busy} onClick={async () => {
              if (await confirm({title:'Remove target?',body:target.kind === 's3' ? 'Removes this target from scheduling. Capture history, recovery keys and audit records are kept. Data in the bucket stays until you delete it there. Object Lock may keep it for the retention period.' : 'Removes this target from scheduling. Capture history, recovery keys and audit records are kept. No data is deleted from the removed machine.',confirm:'Remove target',danger:true})) action.mutate({id:target.id,action:'remove'});
            }}>Remove target</Button>}
            {target.backup_id && <Button disabled={drill.isPending} onClick={() => recordDrill(target.id, target.backup_id!, target.snapshot_id)}>Record restore drill</Button>}
          </div>
          {(!['node','s3'].includes(target.kind ?? '') || !!target.setup_verified_at) && <BackupHistory id={target.id} busy={busy || !!target.machine_removed} onDrill={item => recordDrill(target.id, item.backup_id, item.snapshot_id)} />}
        </div>;
      })}
      <ErrorNote error={action.error || drill.error} />
    </div>}</QueryState></div>}
    {exportKit.error && <div className="border-t border-line p-4"><ErrorNote error={exportKit.error} title="Couldn't export the recovery kit." /></div>}
  </Card></section>;
}
