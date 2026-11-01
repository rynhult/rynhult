import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api, ApiError, post } from '../lib/api';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { useConfirm } from '../components/confirm';
import { FolderAccessSettings } from '../components/FolderAccessSettings';
import { announce, Badge, Button, Card, CardHeader, Dialog, Empty, ErrorNote, Field, Input, Note, Page, PageHeader, QueryState, Select, buttonClass } from '../components/ui';

type Scope = { kind: 'account'; machine: string; machine_id: string; account: string };
type FolderAccount = { scope: Scope; state: string; stored_state: string; retry_available: boolean; allowed_folders: boolean; roots: string[]; revision: number; applied_revision: number;
  detail: { message?: string; operation_id?: string; allowed_folders?: boolean }; projects: { id: number; name: string; path: string }[] };
type State = { accounts: FolderAccount[]; revision: number };
type Preview = { recovery?: {digest:string;roots:string[];allowed_folders:boolean;revision:number}|null; recovery_unavailable?:string; retry_available?:boolean; approved?: boolean; id: string; digest: string; active_sessions: string[]; active_gates: string[]; warning: string; preview: { roots: string[]; legacy_roots: string[] } };
const verification = <Link className={buttonClass({ size: 'sm' })} to='/settings#passkeys'>Verify your sign-in in Passkeys</Link>;

/** Lane B links here; this is the sole account root-list editor. */
export default function AllowedFolders() {
  const query = useQuery({ queryKey: ['security-folders'], queryFn: () => api<State>('/api/security/folders') });
  const [params, setParams] = useSearchParams();
  const chosen = query.data?.accounts.find(row => row.scope.machine === params.get('machine') && row.scope.account === params.get('account')) ?? query.data?.accounts[0];
  return <Page><PageHeader title='Allowed folders' description="Limits the folders Rynhult’s account service will operate on. It does not sandbox native agent processes." />
    <Link to='/settings#security'>Back to Security</Link>
    <QueryState query={query} isEmpty={data => data.accounts.length === 0} empty={<Empty>No provisioned agent accounts. Set up an account in Host first.</Empty>}>
      {data => <>
        <Field label='Machine and agent account'><Select value={chosen ? JSON.stringify(chosen.scope) : ''} onChange={event => {
          const scope = JSON.parse(event.target.value) as Scope;
          setParams({ machine: scope.machine, account: scope.account });
        }}>{data.accounts.map(row => <option key={JSON.stringify(row.scope)} value={JSON.stringify(row.scope)}>{row.scope.machine} · {row.scope.account}</option>)}</Select></Field>
        {chosen && <Editor key={JSON.stringify(chosen.scope)} row={chosen} revision={data.revision} refresh={() => query.refetch()} />}
      </>}
    </QueryState>
  </Page>;
}

function Editor({ row, revision, refresh }: { row: FolderAccount; revision: number; refresh: () => unknown }) {
  const client = useQueryClient();
  const confirm = useConfirm();
  const [folder, setFolder] = useState('');
  const [draft, setDraft] = useState<string[] | null>(null);
  const [checked, setChecked] = useState<{ state: string; message: string } | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const roots = draft ?? row.roots;
  const coordinates = { machine: row.scope.machine, account: row.scope.account };
  const check = useMutation({ mutationFn: () => post<{ state: string; message: string }>('/api/security/folders/check', { ...coordinates, path: folder }), onSuccess: setChecked });
  const review = useMutation({ mutationFn: () => post<Preview>('/api/security/folders/preview', { ...coordinates, roots, revision }), onSuccess: setPreview });
  const applied = () => {
    announce('Allowed folders applied by the target account service.');
    setDraft(current => current && JSON.stringify(current) !== JSON.stringify(preview?.preview.roots) ? current : null);
    setPreview(null); setChecked(null);
    void client.invalidateQueries({ queryKey: ['security-folders'] });
    void client.invalidateQueries({ queryKey: ['security-effective'] });
  };
  const apply = useMutation({ mutationFn: () => post(`/api/security/folders/${preview!.id}/apply`, { digest: preview!.digest }),
    onSuccess: applied, onError: () => { void client.invalidateQueries({ queryKey: ['security-folders'] }); } });
  const recovered = (result:{state:string}) => {
    if(result.state !== 'pending_policy') { applied(); return; }
    setPreview(null); setDraft(null);
    void refresh();
    void client.invalidateQueries({queryKey:['security-effective']});
    announce('Folder recovery acknowledged. Review folders again or restore the acknowledged switch in Security.');
  };
  const retry = useMutation({
    mutationFn: () => post<{state:string}>(`/api/security/folders/${preview!.id}/retry`, { digest: preview!.digest }),
    onSuccess: recovered,
  });
  const recover = useMutation({
    mutationFn: () => post<{state:string}>(`/api/security/folders/${preview!.id}/recover`, {
      digest: preview!.digest, recovery_digest: preview!.recovery!.digest,
    }), onSuccess: recovered,
    onError: () => { void refresh(); },
  });
  const restoreReview = useMutation({ mutationFn: () => api<Preview>(`/api/security/folders/operations/${row.detail.operation_id!}`), onSuccess: setPreview });
  const approved = !!preview && (preview.approved === true || row.detail.operation_id === preview.id);
  const pending = apply.isPending || retry.isPending || recover.isPending;
  const unverified = [check.error, review.error, apply.error, retry.error, recover.error, restoreReview.error].some(error => error instanceof ApiError && error.status === 403);
  return <Card><CardHeader title={`${row.scope.machine} · ${row.scope.account}`} subtitle={`Machine identity ${row.scope.machine_id} · Desired revision ${row.revision} · Applied revision ${row.applied_revision}`} />
    <div className='space-y-4 p-4'>
      <Badge tone={row.state === 'pending' || row.state === 'pending_policy' || row.state === 'unavailable' ? 'wait' : undefined}>{row.state === 'legacy' ? 'Configured by installer' : row.state === 'pending' || row.state === 'pending_policy' ? 'Pending application' : row.state === 'unavailable' ? 'Unavailable' : 'Applied'}</Badge>
      {!row.allowed_folders && <Note>Allowed folders is Off for this account. OS permissions and mandatory system/service protections remain. A pending change has not reached the target.</Note>}
      {row.detail.message && <p className='text-sm text-muted'>{row.detail.message}</p>}
      {row.stored_state==='pending_policy' && typeof row.detail.allowed_folders==='boolean' && <Note>Last acknowledged folder check: {row.detail.allowed_folders?'On':'Off'}. <Link className='underline' to={`/settings?security_scope=account&machine=${encodeURIComponent(row.scope.machine)}&account=${encodeURIComponent(row.scope.account)}#security`}>Open Security to restore this setting</Link>, or preview folders again to deliver the desired setting.</Note>}
      <p className='text-sm text-muted'>Adding or removing a folder changes account-service policy only. It does not grant OS access, trust a project, move files or change the default workspace folder.</p>
      <ul className='space-y-2'>{roots.map((root, index) => <li key={root} className='flex flex-wrap items-center justify-between gap-2'>
        <span className='min-w-0 break-all font-mono text-xs'>{root}{index === 0 ? ' · Default workspace (unchanged)' : ''}</span>
        {index !== 0 && <Button size='sm' variant='ghost' onClick={() => { setDraft(roots.filter(value => value !== root)); setPreview(null); }}>Remove {root}</Button>}
      </li>)}</ul>
      {!!row.projects.length && <div className='text-sm text-muted'><p>Projects in this account (review before removal):</p><ul>{row.projects.map(project => <li key={project.id}><Link to={`/projects/${project.id}`}>{project.name}</Link> · {roots.some(root => project.path === root || project.path.startsWith(root + '/')) ? "Still covered by a reviewed root" : "Not covered by these roots"}</li>)}</ul></div>}
      <Field label='Folder to add' hint='One exact existing repository folder. Allowing its parent also permits future child repositories.'><Input value={folder} autoComplete='off' spellCheck={false} onChange={event => { setFolder(event.target.value); setChecked(null); }} placeholder='/home/alex/src/demo-api' /></Field>
      <div className='flex flex-wrap gap-2'><Button disabled={!folder || check.isPending} onClick={() => check.mutate()}>Check folder</Button>
        <Button disabled={!checked || !folder || roots.includes(folder)} onClick={() => { setDraft([...roots, folder]); setFolder(''); setChecked(null); }}>Add to review</Button></div>
      {checked && <p role='status' className='text-sm'>{checked.state === 'access_checked' ? 'Access checked. ' : checked.state === 'unavailable' ? 'Unavailable. ' : 'Needs access or local review. '}{checked.message}</p>}
      <FolderAccessSettings machine={row.scope.machine} account={row.scope.account} />
      <ErrorNote error={check.error} /><ErrorNote error={review.error} /><ErrorNote error={restoreReview.error} />{unverified && verification}
      <div className='flex flex-wrap gap-2'><Button variant='primary' disabled={roots.length === 0 || review.isPending || row.state === 'unavailable'} onClick={() => review.mutate()}>Preview folders</Button>
        <Button variant='ghost' onClick={() => { void refresh(); }}>Refresh status</Button>
        {row.retry_available && row.detail.operation_id && <Button disabled={restoreReview.isPending} onClick={() => restoreReview.mutate()}>Review pending application</Button>}
        {draft && <Button variant='ghost' onClick={async () => { if (await confirm({ title: 'Discard folder changes?', body: 'The applied policy and OS permissions stay unchanged.', confirm: 'Discard changes', danger: true })) { setDraft(null); setPreview(null); } }}>Discard changes</Button>}
      </div>
    </div>
    <Dialog title='Review allowed folders' open={!!preview} onClose={() => { if (!pending) setPreview(null); }} dismissible={false} footer={<>
      <Button variant='ghost' disabled={pending} onClick={() => setPreview(null)}>Cancel</Button>
      <Button variant='primary' disabled={pending || approved} onClick={async () => {
        if (await confirm({ title: 'Apply account folder policy?', body: `This changes ${row.scope.machine} · ${row.scope.account}. Removing a root blocks later account-service operations, but OS access and copied data remain. Existing workspaces are never deleted.`, confirm: 'Apply folder policy', danger: true })) apply.mutate();
      }}>Apply folder policy</Button>
      {approved && <Button variant='primary' disabled={pending || preview?.retry_available===false} onClick={() => retry.mutate()}>Retry reviewed application</Button>}
      {approved && preview?.recovery && <Button disabled={pending} onClick={async () => {
        if(await confirm({title:'Recover acknowledged folder policy?', body:`This closes the old delivery and reloads the acknowledged roots with Allowed folders ${preview.recovery!.allowed_folders?'On':'Off'} at a newer revision. This changes later account-service access for running sessions. No OS permissions are changed. Review the acknowledged roots before confirming.`, confirm:'Recover acknowledged policy', danger:true})) recover.mutate();
      }}>Recover acknowledged policy</Button>}</>}>
      <div className='space-y-3'><p>{preview?.warning}</p>{approved && <Note>Any recently verified owner can retry. The target’s durable status is checked first; an unstarted, expired request returns to a fresh folder review or a switch restoration in Security. Unreachable targets remain pending. Refresh status after an interrupted retry.</Note>}<ul>{preview?.preview.roots.map(root => <li key={root} className='break-all font-mono text-xs'>{root}</li>)}</ul>
        {!!preview?.preview.legacy_roots.length && <Note>Installer roots retain their original provenance. They have not passed the new strict grant validator.</Note>}
        {!!(preview && (preview.active_sessions.length || preview.active_gates.length)) && <Note>Running sessions and gates do not prevent this policy reload. Later account-service operations use the new roots and switch; native agents retain OS access and existing files are not deleted. Filesystem grant/undo has separate stop requirements.</Note>}
        {preview?.recovery && <section aria-label='Acknowledged policy for recovery' className='space-y-2'>
          <h3 className='font-medium'>Acknowledged folder check: {preview.recovery.allowed_folders?'On':'Off'}</h3>
          <p>Target revision {preview.recovery.revision}. Recovery reloads these acknowledged roots above the abandoned revision, closes its old delivery, and returns to fresh review. This can widen or restrict running sessions’ later service access.</p>
          <ul>{preview.recovery.roots.map(root=><li key={root} className='break-all font-mono text-xs'>{root}</li>)}</ul>
        </section>}
        {preview?.recovery_unavailable && <Note>{preview.recovery_unavailable}</Note>}
        <ErrorNote error={apply.error} /><ErrorNote error={retry.error} /><ErrorNote error={recover.error} />{unverified && verification}
      </div>
    </Dialog>
  </Card>;
}
