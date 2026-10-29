import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, post } from '../lib/api';
import { useSingleMutation } from '../lib/mutations';
import { Button, Card, CardHeader, ErrorNote, Field, QueryState, Select } from './ui';

type Model = {id:string;label?:string;image_input?:boolean|null;image_qualification?:{status:string;checked_at?:number;evidence?:{outcome?:string}}};
type Result = {image_input:boolean|null;checked_at:number;evidence:{outcome:string;error_kind?:string}};
const providers = ['claude','codex','copilot','antigravity','cursor','opencode'];
const status = (value:boolean|null|undefined) => value===true?'Verified':value===false?'Unavailable':'Unknown';

export function ImageAccess() {
  const client=useQueryClient();
  const [project,setProject]=useState('');
  const [provider,setProvider]=useState('claude');
  const [model,setModel]=useState('');
  const projects=useQuery({queryKey:['image-projects'],queryFn:()=>api<{id:number;name:string;host?:string;trusted:boolean;archived:boolean}[]>('/api/projects')});
  const models=useQuery({queryKey:['image-models',project],enabled:!!project,queryFn:()=>api<Record<string,Model[]>>(`/api/models?project_id=${project}`)});
  const check=useSingleMutation({mutationFn:()=>post<Result>('/api/providers/image-check',{project_id:Number(project),provider,model}),
    onSuccess:()=>{client.invalidateQueries({queryKey:['image-models',project]});client.invalidateQueries({queryKey:['models']});}});
  const rows=Array.isArray(models.data?.[provider])?models.data![provider]:[];
  const current=rows.find(row=>row.id===model);
  const reset=()=>{setModel('');check.reset();};
  return <Card><CardHeader title="Image access" subtitle="Check whether an exact model can read a private image using the native CLI on its execution machine." />
    <div className="p-4"><p className="text-sm text-muted">This starts one short read-only check with a generated image, using the selected project's machine and agent account. It may use provider quota. No project files are sent. Native checks do not qualify containers or custom endpoints. Results expire when the CLI or adapter changes, or after 30 days.</p>
    <QueryState query={projects}>{()=> <form className="mt-4 space-y-3" aria-label="Check image access" onSubmit={event=>{event.preventDefault();check.mutate();}}>
      <Field label="Execution project"><Select value={project} required disabled={check.isPending} onChange={event=>{setProject(event.target.value);reset();}}>
        <option value="">Choose a trusted project</option>{projects.data?.filter(p=>p.trusted&&!p.archived).map(p=><option key={p.id} value={p.id}>{p.name} · {p.host||'local'}</option>)}
      </Select></Field>
      <Field label="Provider"><Select value={provider} disabled={check.isPending} onChange={event=>{setProvider(event.target.value);reset();}}>{providers.map(p=><option key={p}>{p}</option>)}</Select></Field>
      {project&&<QueryState query={models}>{()=> <Field label="Model"><Select value={model} required disabled={check.isPending} onChange={event=>{setModel(event.target.value);check.reset();}}>
        <option value="">Choose a model</option>{rows.map(row=><option key={row.id} value={row.id}>{row.label||row.id}</option>)}
      </Select></Field>}</QueryState>}
      {current&&<p className="text-sm">Image access: {status(current.image_input)}{current.image_qualification?.checked_at&&` · Checked ${new Date(current.image_qualification.checked_at*1000).toLocaleString()}`}</p>}
      <Button type="submit" disabled={!project||!model||check.isPending}>{check.isPending?'Checking image access…':'Check image access'}</Button>
      <ErrorNote error={check.error}/>
      {check.data&&<p role="status" className="break-words text-sm">{status(check.data.image_input)} · {check.data.evidence.outcome}{check.data.evidence.error_kind&&` · ${check.data.evidence.error_kind}`}. A result qualifies only this machine, account, model and adapter/CLI versions.</p>}
    </form>}</QueryState></div>
  </Card>;
}
