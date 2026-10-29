import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api, post } from '../lib/api';
import { useOffline, useRole } from '../lib/queries';
import { useConfirm } from './confirm';
import { Button, ErrorNote, Field, Input, QueryState, Select } from './ui';

type Binding = { id: string; node_id: string; host: string; person: string; account: string; status: string; proof_verified_at: string | null };
type Placement = { id: number; person: string; host: string; account: string; trust: string };

function MachineProof({ row, busy, submit }: { row: Binding; busy: boolean; submit: (code: string) => void }) {
  const [code, setCode] = useState('');
  return <form className="w-full space-y-2" onSubmit={event => { event.preventDefault(); submit(code); setCode(''); }}>
    <p className="text-sm text-muted">On {row.host}, open a terminal as {row.account} and run the command below. Replace BROKER_RUN_DIR with that account’s broker run directory (ask your machine administrator). Enter the code shown there within five minutes.</p>
    <pre className="whitespace-pre-wrap break-all text-sm">{`rynhult-node group-proof --request ${row.id} --node-id ${row.node_id} --run-dir BROKER_RUN_DIR`}</pre>
    <Field label={`Confirmation code for ${row.host} · ${row.account}`}><Input required autoComplete="one-time-code" spellCheck={false} value={code} maxLength={10} pattern="[A-Z0-9]{10}" onChange={event => setCode(event.target.value.toUpperCase())} /></Field>
    <Button type="submit" disabled={busy || code.length !== 10}>Verify on the machine</Button>
  </form>;
}

export function GroupMachines({ group, projectId }: { group: { id: string; revision: number; role: string }; projectId: number }) {
  const { owner, username } = useRole();
  const offline = useOffline();
  const confirm = useConfirm();
  const cache = useQueryClient();
  const [node, setNode] = useState('');
  const [account, setAccount] = useState('');
  const [binding, setBinding] = useState('');
  const base = `/api/groups/${group.id}/projects/${projectId}`;
  const manage = owner || group.role === 'maintainer';
  const bindings = useQuery({ queryKey: ['group-machines'], queryFn: () => api<Binding[]>('/api/group-machines') });
  const reviews = useQuery({ queryKey: ['group-machine-reviews', group.id, projectId], queryFn: () => api<Placement[]>(base + '/remote-placements'), enabled: manage });
  const action = useMutation({ mutationFn: async ({ url, data, danger }: { url: string; data?: object; danger?: boolean }) => {
    if (danger && !await confirm({ title: 'Revoke own-machine access', body: 'Affected group sessions will stop. A disconnected machine keeps cleanup pending until it reconnects. Copied code and keys cannot be recalled.', confirm: 'Revoke access', danger: true })) return;
    return post(url, data ? { ...data, revision: group.revision } : undefined);
  }, onSuccess: () => {
    cache.invalidateQueries({ queryKey: ['group-machines'] });
    cache.invalidateQueries({ queryKey: ['group-machine-reviews'] });
    cache.invalidateQueries({ queryKey: ['group-placements'] });
    cache.invalidateQueries({ queryKey: ['groups'] });
    cache.invalidateQueries({ queryKey: ['group-board'] });
  } });
  const busy = offline || action.isPending;
  return <section aria-label="Own-machine collaboration" className="space-y-3 border-t border-line pt-3">
    <h3 className="font-medium">Your own machine</h3>
    <p className="text-sm text-muted">Request your enrolled machine and account, prove control with the confirmation code shown on that machine, then ask an instance owner to approve them. A group maintainer approves each project placement. Provider sign-ins stay on that machine. Only your own keys permitted on other machines can be delivered.</p>
    <form className="space-y-3" onSubmit={e => { e.preventDefault(); action.mutate({ url: '/api/group-machines', data: { node_id: node, account } }); }}>
      <Field label="Enrolled machine ID"><Input required autoComplete="off" value={node} onChange={e => setNode(e.target.value)} pattern="[a-f0-9]{16,32}" /></Field>
      <Field label="Your broker account"><Input required autoComplete="off" value={account} onChange={e => setAccount(e.target.value)} pattern="[a-z_][a-z0-9_-]{0,31}" /></Field>
      <Button type="submit" disabled={busy}>Request machine account</Button>
    </form>
    <QueryState query={bindings}>{rows => <>
      {rows.length === 0 && <p>No machine account requests yet.</p>}
      {rows.map(row => <div key={row.id} className="flex flex-wrap items-center gap-2 border-t border-line py-2">
        <span className="min-w-0 flex-1 break-words">{row.person} · {row.host} · {row.account} · {row.status} · {row.proof_verified_at ? "Verified on the machine" : "Unverified"}</span>
        {owner && row.status === 'pending' && <Button disabled={busy || !row.proof_verified_at} onClick={() => action.mutate({ url: `/api/group-machines/${row.id}/approve` })}>Approve machine account</Button>}
        {['pending', 'approved'].includes(row.status) && !row.proof_verified_at && row.person === username && <MachineProof row={row} busy={busy} submit={code => action.mutate({ url: `/api/group-machines/${row.id}/verify`, data: { code } })} />}
        {row.status !== 'revoked' && (owner || row.person === username) && <Button disabled={busy} onClick={() => action.mutate({ url: `/api/group-machines/${row.id}/revoke`, danger: true })}>Revoke machine account</Button>}
      </div>)}
      <form className="space-y-3" onSubmit={e => { e.preventDefault(); action.mutate({ url: base + '/remote-placements', data: { binding_id: binding } }); }}>
        <Field label="Your approved machine account"><Select required value={binding} onChange={e => setBinding(e.target.value)}><option value="">Choose machine account…</option>{rows.filter(row => row.person === username && row.status === 'approved').map(row => <option key={row.id} value={row.id}>{row.host} · {row.account}</option>)}</Select></Field>
        <Button type="submit" disabled={busy || !binding}>Request project placement</Button>
      </form>
    </>}</QueryState>
    {manage && <QueryState query={reviews}>{rows => <>
      <h4 className="font-medium">Project machine approvals</h4>
      {rows.length === 0 && <p>No project machine requests yet.</p>}
      {rows.map(row => <div key={row.id} className="flex flex-wrap items-center gap-2 border-t border-line py-2">
        <span className="min-w-0 flex-1 break-words">{row.person} · {row.host} · {row.account} · {row.trust}</span>
        {row.trust === 'pending' && <Button disabled={busy} onClick={() => action.mutate({ url: base + `/remote-placements/${row.id}/approve`, data: {} })}>Approve project placement</Button>}
        {row.trust !== 'revoked' && <Button disabled={busy} onClick={() => action.mutate({ url: base + `/remote-placements/${row.id}/revoke`, data: {}, danger: true })}>Revoke project placement</Button>}
      </div>)}
    </>}</QueryState>}
    <ErrorNote error={action.error} />
    {action.isSuccess && <p role="status">Request updated. Refresh the approved checkout list before launching.</p>}
  </section>;
}
