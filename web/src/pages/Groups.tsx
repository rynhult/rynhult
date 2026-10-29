import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Users } from 'lucide-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, post } from '../lib/api';
import { useRole } from '../lib/queries';
import { GroupBoard } from '../components/GroupBoard';
import { GroupPublished } from '../components/GroupPublished';
import { useConfirm } from '../components/confirm';
import { Badge, Button, Card, Dialog, Empty, ErrorNote, Field, Input, Page, PageHeader, QueryState, Select } from '../components/ui';

type Group = { id: string; name: string; role: string; revision: number; archived: boolean; paused: boolean; remote_execution_available?: boolean };
type Person = { id: number; username: string; role: string | null; provenance: string | null };
type Project = { id: number; name: string; owners: number[]; workspace: boolean; archived: boolean };

function PersonRow({ person, group, owner, save, pending }: {
  person: Person; group: Group; owner: boolean; save: (id: number, role: string) => void; pending: boolean;
}) {
  const [role, setRole] = useState(person.role ?? 'member');
  return <form aria-label={`Membership for ${person.username}`} className="flex flex-wrap items-center gap-3 border-t border-line py-3" onSubmit={e => { e.preventDefault(); save(person.id, role); }}>
    <span className="min-w-0 flex-1 break-words"><span>{person.username}</span> <span className="text-xs text-muted">{person.provenance === 'identity-provider' ? 'Managed by your identity provider; edit roles there' : person.provenance === 'local' ? 'Added in Rynhult' : 'Not in group'}</span></span>
    <Select className="w-36" aria-label={`Role for ${person.username}`} value={role} onChange={e => setRole(e.target.value)} disabled={group.archived || person.provenance === 'identity-provider'}>
      {(owner || person.role === 'maintainer') && <option value="maintainer">Maintainer</option>}
      <option value="member">Member</option><option value="viewer">Viewer</option>
      {person.role && <option value="remove">Remove</option>}
    </Select>
    <Button disabled={pending || group.archived || person.provenance === 'identity-provider'} type="submit">{person.role ? 'Save role' : 'Add person'}</Button>
  </form>;
}

function GroupPeople({ group, owner, close }: { group: Group; owner: boolean; close: () => void }) {
  const cache = useQueryClient();
  const confirm = useConfirm();
  const people = useQuery({ queryKey: ['group-people', group.id], queryFn: () => api<Person[]>(`/api/groups/${group.id}/people`) });
  const change = useMutation({
    mutationFn: async ({ id, role }: { id: number; role: string }) => {
      if (role === 'remove' || role === 'viewer') {
        if (!await confirm({ title: role === 'remove' ? 'Remove group member' : 'Make group viewer',
          body: 'This revokes development access to this group. Published history stays in the group. Personal projects are unaffected.',
          confirm: role === 'remove' ? 'Remove member' : 'Make viewer', danger: true })) return;
      }
      return post(`/api/groups/${group.id}/members/${id}`, { role, revision: group.revision });
    },
    onSettled: () => { cache.invalidateQueries({ queryKey: ['groups'] }); cache.invalidateQueries({ queryKey: ['group-people', group.id] }); },
  });
  return <Dialog open onClose={close} title={`${group.name} · People`} description="Add existing Rynhult users. Only instance owners appoint maintainers. Keep a maintainer and a named owner for every project.">
    <div aria-live="polite"><ErrorNote error={change.error} /></div>
    <QueryState query={people}>{items => items.map(person => <PersonRow key={`${person.id}-${person.role}`} person={person} group={group} owner={owner}
      pending={change.isPending} save={(id, role) => change.mutate({ id, role })} />)}</QueryState>
  </Dialog>;
}

function GroupProjects({ group, close }: { group: Group; close: () => void }) {
  const cache = useQueryClient();
  const [published, setPublished] = useState<Project | null>(null);
  const manage = group.role === 'owner' || group.role === 'maintainer';
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [owner, setOwner] = useState('');
  const [workspace, setWorkspace] = useState(false);
  const projects = useQuery({ queryKey: ['group-projects', group.id], queryFn: () => api<Project[]>(`/api/groups/${group.id}/projects`) });
  const people = useQuery({ queryKey: ['group-people', group.id], queryFn: () => api<Person[]>(`/api/groups/${group.id}/people`), enabled: manage });
  const create = useMutation({ mutationFn: () => post(`/api/groups/${group.id}/projects`, { name, slug, owners: [Number(owner)], workspace, revision: group.revision }),
    onSuccess: () => { setName(''); setSlug(''); },
    onSettled: () => { cache.invalidateQueries({ queryKey: ['groups'] }); cache.invalidateQueries({ queryKey: ['group-projects', group.id] }); },
  });
  return <Dialog open onClose={close} title={`${group.name} · Projects`} dismissible={false} description="Read explicitly published commits. Named project owners approve integration merges. Use the work board to launch reviewed source in your own private checkout.">
    <QueryState query={projects} isEmpty={items => !items.length} empty={<p className="text-sm text-muted">No group projects yet.</p>}>
      {items => <ul className="space-y-2">{items.map(p => <li className="flex flex-wrap items-center justify-between gap-2 break-words" key={p.id}><span>{p.name}{p.workspace ? ' · Workspace' : ''} <Badge>{p.archived ? 'Archived' : 'Private checkouts'}</Badge></span>{!p.archived && <Button onClick={() => setPublished(p)}>Published Files &amp; Wiki</Button>}</li>)}</ul>}
    </QueryState>
    {manage && !group.archived && <form className="mt-4 space-y-3" onSubmit={e => { e.preventDefault(); create.mutate(); }}>
      <Field label="Project kind"><Select value={workspace ? 'workspace' : 'project'} onChange={e => setWorkspace(e.target.value === 'workspace')}><option value="project">Project</option><option value="workspace">Development workspace</option></Select></Field>
      <Field label="Project name"><Input name="project-name" autoComplete="off" required value={name} onChange={e => setName(e.target.value)} maxLength={200} /></Field>
      <Field label="Project slug"><Input name="project-slug" autoComplete="off" required pattern="[a-z0-9][a-z0-9-]{0,62}" value={slug} onChange={e => setSlug(e.target.value)} /></Field>
      <QueryState query={people}>{items => <Field label="Project owner"><Select value={owner} onChange={e => setOwner(e.target.value)} required>
        <option value="">Choose maintainer…</option>{items.filter(p => p.role === 'maintainer').map(p => <option key={p.id} value={p.id}>{p.username}</option>)}
      </Select></Field>}</QueryState>
      <div aria-live="polite"><ErrorNote error={create.error} /></div>
      <Button type="submit" variant="primary" disabled={create.isPending}>{create.isPending ? 'Creating…' : 'Create group project'}</Button>
    </form>}
    {published && <GroupPublished groupId={group.id} project={published} close={() => setPublished(null)} />}
  </Dialog>;
}

export function Groups() {
  const { owner } = useRole();
  const cache = useQueryClient();
  const confirm = useConfirm();
  const [name, setName] = useState('');
  const [maintainer, setMaintainer] = useState('');
  const [params, setParams] = useSearchParams();
  const selected = params.get('group') ? { id: params.get('group')!, tab: params.get('tab') ?? 'board' } : null;
  const setSelected = (selection: { id: string; tab: string } | null) => setParams(selection ? { group: selection.id, tab: selection.tab } : {});
  const groups = useQuery({ queryKey: ['groups'], queryFn: () => api<Group[]>('/api/groups') });
  const users = useQuery({ queryKey: ['group-candidates'], queryFn: () => api<{ users: Person[] }>('/api/users'), enabled: owner });
  const create = useMutation({ mutationFn: () => post('/api/groups', { name, maintainer_id: Number(maintainer) }),
    onSuccess: () => { setName(''); cache.invalidateQueries({ queryKey: ['groups'] }); },
  });
  const action = useMutation({ mutationFn: async ({ group, command }: { group: Group; command: string }) => {
    if (command === 'archive' && !await confirm({ title: 'Archive group', body: `Archive ${group.name} and refuse further group changes. Personal projects and stored history remain.`, confirm: 'Archive group', danger: true })) return;
    return post(`/api/groups/${group.id}/${command}`, { revision: group.revision });
  }, onSettled: () => { cache.invalidateQueries({ queryKey: ['groups'] }); } });
  const current = groups.data?.find(g => g.id === selected?.id);
  return <Page>
    <PageHeader title="Groups" description="Group projects exchange explicitly published commits. Personal work stays private. Launch reviewed source in your private checkout with an explicit shared task." />
    {owner && <Card className="p-4"><form className="space-y-3" onSubmit={e => { e.preventDefault(); create.mutate(); }}>
      <Field label="Group name"><Input name="group-name" autoComplete="off" value={name} onChange={e => setName(e.target.value)} maxLength={120} required /></Field>
      <QueryState query={users}>{data => <Field label="Initial maintainer"><Select value={maintainer} onChange={e => setMaintainer(e.target.value)} required>
        <option value="">Choose existing user…</option>{data.users.map(u => <option key={u.id} value={u.id}>{u.username}</option>)}
      </Select></Field>}</QueryState>
      <div aria-live="polite"><ErrorNote error={create.error} /></div>
      <Button type="submit" variant="primary" disabled={create.isPending}>{create.isPending ? 'Creating…' : 'Create group'}</Button>
    </form></Card>}
    <div aria-live="polite"><ErrorNote error={action.error} /></div>
    <QueryState query={groups} isEmpty={items => !items.length} empty={<Card><Empty icon={Users}>{owner ? 'No groups yet. Create a group above and choose its first maintainer.' : 'No groups available to you. Ask an instance owner to add you.'}</Empty></Card>}>
      {items => <div className="space-y-3">{items.map(group => <article key={group.id} aria-label={group.name}><Card className="p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="min-w-0 break-words font-medium">{group.name} <Badge>{group.archived ? 'Archived' : 'Active'}</Badge></h2>
          <div className="text-xs text-muted">{group.role}<details><summary className="cursor-pointer">Details</summary>Revision {group.revision}</details></div>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {(owner || group.role === 'maintainer') && <Button onClick={() => setSelected({ id: group.id, tab: 'people' })}>Manage people</Button>}
          <Button onClick={() => setSelected({ id: group.id, tab: 'projects' })}>Projects</Button>
          <Button onClick={() => setSelected({ id: group.id, tab: 'board' })}>Work board</Button>
          {owner && !group.archived && <Button variant="danger" disabled={action.isPending} onClick={() => action.mutate({ group, command: 'archive' })}>Archive group</Button>}
          {(owner || group.role === 'maintainer') && group.paused && !group.archived && <Button disabled={action.isPending} onClick={() => action.mutate({ group, command: 'unpause' })}>Override pause</Button>}
        </div>
      </Card></article>)}</div>}
    </QueryState>
    {current && selected?.tab === 'board' && <GroupBoard group={current} close={() => setSelected(null)} />}
    {current && selected?.tab === 'people' && <GroupPeople group={current} owner={owner} close={() => setSelected(null)} />}
    {current && selected?.tab === 'projects' && <GroupProjects group={current} close={() => setSelected(null)} />}
  </Page>;
}
