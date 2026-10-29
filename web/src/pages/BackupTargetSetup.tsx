import { useQuery } from '@tanstack/react-query';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, post } from '../lib/api';
import { Button, Card, CardHeader, ErrorNote, Field, Input, Page, PageHeader, QueryState, Select } from '../components/ui';

import { CopyButton } from '../components/InstallSteps';

type Node = {id:string; name:string; status:string; info:{os?:string}; backup_default_max_size_bytes?:number};
export function BackupTargetSetup() {
  const [params] = useSearchParams();
  return <TargetSetup key={params.get('target') ?? `new:${params.get('kind') === 's3' ? 's3' : 'node'}`} />;
}

function TargetSetup() {
  const [params, setParams] = useSearchParams();
  const id = params.get('target');
  const commands = useRef<HTMLPreElement>(null);
  const [kind, setKind] = useState(params.get('kind') === 's3' ? 's3' : 'node');
  const [endpoint, setEndpoint] = useState('');
  const [bucket, setBucket] = useState('');
  const [region, setRegion] = useState('us-east-1');
  const [access, setAccess] = useState('');
  const [secret, setSecret] = useState('');
  const [session, setSession] = useState('');
  const [node, setNode] = useState('');
  const [ceiling, setCeiling] = useState('20');
  const nodes = useQuery({queryKey:['backup-target-nodes'],queryFn:() => api<Node[]>('/api/nodes'),enabled:!id && kind === 'node'});
  const setup = useQuery({queryKey:['backup-target-setup',id],queryFn:() => api<{commands:string; max_size_bytes?:number; kind?:string; endpoint?:string; bucket?:string; region?:string; prefix?:string; capabilities?:{versioning:boolean;mode:string|null;default_days:number}}>(`/api/host/backups/${id}/setup`),enabled:!!id});
  const create = useMutation({mutationFn:() => post<{id:string}>('/api/host/backups/targets',{node_id:node,max_size_bytes:Number(ceiling)*2**30}),onSuccess:t => setParams({target:t.id})});
  const createS3 = useMutation({mutationFn:() => post<{id:string}>('/api/host/backups/s3-targets', {
    endpoint, bucket, region, credentials:{access_key_id:access, secret_access_key:secret, ...(session ? {session_token:session} : {})},
  }), onSuccess:t => {setAccess('');setSecret('');setSession('');setParams({target:t.id});}});
  const replaceCredentials = useMutation({mutationFn:() => post(`/api/host/backups/${id}/credentials`, {
    credentials:{access_key_id:access,secret_access_key:secret,...(session ? {session_token:session} : {})},
  }), onSuccess:() => {setAccess('');setSecret('');setSession('');check.reset();void setup.refetch();}});
  const check = useMutation({mutationFn:() => post<{kind?:string;immutability?:unknown;capabilities?:{versioning:boolean;mode:string|null;default_days:number}}>(`/api/host/backups/${id}/check`,{}),onSuccess:() => {void setup.refetch();}});
  return <Page><PageHeader title="Target setup" description="Choose where Rynhult keeps its encrypted backups: another Linux machine you've added, or cloud storage. A folder on this server is set up by an administrator instead (see the Guide, Backups). Only owners can do this." actions={<Link to="/host" className="inline-flex min-h-8 pointer-coarse:min-h-11 items-center rounded-md border border-line-strong px-3 underline underline-offset-2">Back to Host</Link>} />
    <Card><CardHeader title={id ? setup.data?.kind === 's3' ? 'Check S3 storage' : 'Install the append-only receiver' : 'Choose a backup target'} />
      <div className="space-y-4 p-4 text-sm">
        {!id && <Field label="Target type" hint="Where the backups go. Another Linux machine works best if it's always on; cloud storage means a bucket at AWS S3, Backblaze B2, Wasabi, Cloudflare R2 or similar."><Select aria-label="Target type" name="target_type" value={kind} onChange={e => setKind(e.target.value)}><option value="node">Another Linux machine (receiver)</option><option value="s3">Cloud storage (S3-compatible)</option></Select></Field>}
        {!id && kind === 's3' ? <form className="space-y-4" onSubmit={e => {e.preventDefault();createS3.mutate();}}>
          <p className="text-muted">AWS S3, Backblaze B2, Wasabi, MinIO or R2 through restic’s S3 backend. Use the regional HTTPS endpoint. Provider support for immutable retention varies.</p>
          <Field label="Endpoint"><Input name="s3_endpoint" type="url" required autoComplete="off" spellCheck={false} placeholder="https://s3.example.com…" value={endpoint} onChange={e => setEndpoint(e.target.value)} /></Field>
          <Field label="Bucket"><Input name="s3_bucket" required autoComplete="off" spellCheck={false} value={bucket} onChange={e => setBucket(e.target.value)} /></Field>
          <Field label="Region"><Input name="s3_region" required autoComplete="off" spellCheck={false} value={region} onChange={e => setRegion(e.target.value)} /></Field>
          <Field label="Access key ID"><Input name="s3_access_key" type="password" required autoComplete="new-password" spellCheck={false} value={access} onChange={e => setAccess(e.target.value)} /></Field>
          <Field label="Secret access key"><Input name="s3_secret_key" type="password" required autoComplete="new-password" spellCheck={false} value={secret} onChange={e => setSecret(e.target.value)} /></Field>
          <Field label="Session token (optional)"><Input name="s3_session_token" type="password" autoComplete="new-password" spellCheck={false} value={session} onChange={e => setSession(e.target.value)} /></Field>
          <p className="text-muted">Credentials are sealed in the infrastructure vault, never revealed or given to projects. Use a dedicated writer that can list, read and add objects, and delete only transient locks. Keep prune credentials outside Rynhult.</p>
          <p className="text-muted">For verified protection, enable versioning and default COMPLIANCE Object Lock of at least 30 days. Versioning alone does not prove a retention period. Rynhult checks actual object retention and deletion refusal; unsupported providers remain backed up, not protected.</p>
          <Button type="submit" disabled={createS3.isPending}>{createS3.isPending ? 'Preparing…' : 'Prepare S3 target'}</Button>
          <ErrorNote error={createS3.error} />
        </form> : !id ? <QueryState query={nodes}>{values => <form className="space-y-4" onSubmit={e => {e.preventDefault();create.mutate();}}>
          <Field label="Machine to keep the backups on" hint="A Linux machine you've added under Host → Machines. Rynhult installs a small receiver there that can add backups but never delete them."><Select name="node" value={node} onChange={e => {setNode(e.target.value);setCeiling(String((values.find(n => n.id === e.target.value)?.backup_default_max_size_bytes ?? 20*2**30)/2**30));}}><option value="">Select a machine…</option>{values.filter(n => n.status === 'active' && n.info.os?.toLowerCase() === 'linux').map(n => <option key={n.id} value={n.id}>{n.name}</option>)}</Select></Field>
          <Field label="Maximum backup space (GiB)" hint="The most disk the backups may use on that machine. The suggestion is half its free space (20 GiB if it doesn't report any), which suits most installs; 1 to 10240 GiB. Backups are kept on its disk under /var/lib/acc-backup."><Input name="max_size_gib" type="number" min={1} max={10240} step={1} required value={ceiling} onChange={e => setCeiling(e.target.value)} /></Field>
          <p className="text-muted">Next, Rynhult creates the target switched off, with its own encryption keys (adding a machine never gives it your backups). You then copy a few setup commands to run on that machine, check the setup, and turn hourly backups on.</p>
          <Button type="submit" disabled={!node || create.isPending}>{create.isPending ? 'Preparing…' : 'Prepare target'}</Button>
          <ErrorNote error={create.error} />
        </form>}</QueryState> : <QueryState query={setup}>{value => value.kind === 's3' ? <>
          <p className="text-muted">Configure your provider’s dedicated writer policy for this exact bucket and prefix, then check setup. The check initializes an encrypted restic repository and reads it back.</p>
          <dl className="space-y-2 break-all"><dt>Endpoint</dt><dd>{value.endpoint}</dd><dt>Bucket</dt><dd>{value.bucket}</dd><dt>Region</dt><dd>{value.region}</dd><dt>Repository prefix</dt><dd className="font-mono">{value.prefix}</dd></dl>
          <p className="text-muted">Allow list, read and add within the prefix, read bucket versioning and Object Lock configuration, and read object retention. Deny object deletion and version deletion except DeleteObject on locks/*. Deny policy, lifecycle, retention and bucket configuration changes. Start with a writer scoped to the bucket’s acc/* prefix, then narrow it to the repository prefix above.</p>
          {(check.data?.capabilities ?? value.capabilities) && <p className="text-muted">Observed versioning: {(check.data?.capabilities ?? value.capabilities)?.versioning ? 'Enabled' : 'Unverified or unsupported'}. Object Lock: {(check.data?.capabilities ?? value.capabilities)?.mode ?? 'Unverified or unsupported'}; default {(check.data?.capabilities ?? value.capabilities)?.default_days ?? 0} days.</p>}
          <p className="font-medium">Separate maintenance credentials</p>
          <p className="text-muted">Keep maintenance on an independent trusted host outside this main. No maintenance key is accepted here. The retention-only job can extend exact versions alongside captures; pause captures and disable the writer at the provider before prune or version cleanup. <Link to="/guide/User-Guide/Backups#independent-retention-maintenance">Retention maintenance guide</Link>.</p>
          <p className="text-muted">Enable versioning and default COMPLIANCE Object Lock. Choose a window longer than 30 days so older deduplicated objects retain 30 days of protection. Rynhult inspects every current repository object after each capture. Versioning-only and unsupported providers remain backed up, not protected. Checks leave small opaque retained probes.</p>
          <details><summary className="cursor-pointer">Replace writer credentials</summary>
            <form className="mt-4 space-y-4" onSubmit={e => {e.preventDefault();replaceCredentials.mutate();}}>
              <Field label="New access key ID"><Input name="s3_new_access_key" type="password" autoComplete="new-password" spellCheck={false} required value={access} onChange={e => setAccess(e.target.value)} /></Field>
              <Field label="New secret access key"><Input name="s3_new_secret_key" type="password" autoComplete="new-password" spellCheck={false} required value={secret} onChange={e => setSecret(e.target.value)} /></Field>
              <Field label="New session token (optional)"><Input name="s3_new_session_token" type="password" autoComplete="new-password" spellCheck={false} value={session} onChange={e => setSession(e.target.value)} /></Field>
              <p className="text-muted">Replacing the writer disables hourly capture and clears setup and protection evidence. Check setup again, then re-enable the schedule. Revoke the old key with your provider after checking the replacement.</p>
              <Button type="submit" disabled={replaceCredentials.isPending}>Save writer credentials</Button>
              {replaceCredentials.isSuccess && <p role="status">Writer replaced. Check setup before capturing again.</p>}
              <ErrorNote error={replaceCredentials.error} />
            </form>
          </details>
          <p><Link to="/guide/User-Guide/Backups#s3-compatible-storage">S3 policy and recovery guidance</Link></p>
          <Button disabled={check.isPending} onClick={() => check.mutate()}>{check.isPending ? 'Checking…' : 'Check setup'}</Button>
          {check.isSuccess && <p role="status">{check.data?.immutability ? 'Immutability check passed.' : 'Setup verified; immutability is unverified, so this target is not protected.'} Export a new recovery kit, then run a test backup.</p>}
          <ErrorNote error={check.error} />
        </> : <>
          <p className="text-muted">Review and run these exact commands as root on the selected target. They install pinned, checksum-verified rest-server 0.14.0. They grant no root shell to Rynhult. Requires an updated Linux node, systemd, Python 3, runuser, curl, tar and sha256sum.</p>
          {value.max_size_bytes != null && <p className="text-muted">Maximum backup space: {value.max_size_bytes / 2**30} GiB. Check setup keeps this saved value.</p>}
          <p className="text-muted">Mount dedicated storage before setup. Use a bind-mounted subfolder, never the filesystem root. The copied block runs in a child Bash shell; a failure leaves your SSH login open and explains what stopped.</p>
          <CopyButton text={value.commands} label="Copy target commands" target={commands} />
          <pre ref={commands} role="region" aria-label="Target commands" tabIndex={0} className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-md border border-line bg-panel-2 p-4 font-mono text-xs">{value.commands}</pre>
          <p className="text-muted">These commands stop and block the main-controlled root updater. Update this backup target only after independent owner review. Keep maintenance on the target under your own administrator credentials. Rynhult cannot prune remote history. A compromised main can fill storage or upload bad snapshots; receiver root can delete history. Keep an independent offline copy and recovery kit.</p>
          <Button disabled={check.isPending} onClick={() => check.mutate()}>{check.isPending ? 'Checking…' : 'Check setup'}</Button>
          {check.isSuccess && <p role="status">Setup verified. Export a new recovery kit, then run a test backup.</p>}
          <ErrorNote error={check.error} />
        </>}</QueryState>}
      </div>
    </Card>
  </Page>;
}
