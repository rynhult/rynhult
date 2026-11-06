import {useState} from 'react';
import {Link,useLocation} from 'react-router-dom';
import {useInstance,useProjects,useRole} from '../../lib/queries';
import {PERSONAL,INSTANCE} from '../../lib/settingsSections';
import {securityLink} from '../../lib/security';
import {desktopBuild} from '../../lib/desktop';
import {QueryState,Select,buttonClass} from '../ui';

export type SettingsScope='account'|'project'|'installation';
export const settingAnchor=(name:string)=>name.toLowerCase().replaceAll(' ','-');
const FLAGS:Record<string,string>={'Remote access':'remote_access',Publishing:'publishing','Issue sources':'issue_sources','Company sign-in':'organizations','Selection allow-lists':'orchestrator',Notifications:'notifications','Notification devices':'notifications','Risk labels':'risk_labels'};
type Availability={features?:Record<string,boolean>;private_ca?:boolean;organizations?:boolean;orchestrator?:boolean};
/** Mirror the existing sections' role/availability gates; navigation grants nothing. */
export function availableSettings(owner:boolean,instance:Availability){
  const visible=(name:string)=>(!FLAGS[name]||instance.features?.[FLAGS[name]]!==false)&&(name!=='Certificate'||owner||instance.private_ca)&&(name!=='Company sign-in'||instance.organizations)&&(name!=='Selection allow-lists'||instance.orchestrator);
  return {personal:PERSONAL.filter(visible),installation:owner?INSTANCE.filter(visible):[]};
}
export function settingsScope(section:string,owner:boolean,securityScope:string|null):SettingsScope{
  if(section==='project-settings')return 'project';
  if(section==='security'||section==='guardrails')return securityScope==='account'&&owner?'account':securityScope==='project'||!owner?'project':'installation';
  return owner&&INSTANCE.some(name=>settingAnchor(name)===section)?'installation':'account';
}

/** Links only: every destination retains its existing, singular editor. */
export function SettingsScopeLinks({only,compact=false,close=()=>{}}:{only?:SettingsScope;compact?:boolean;close?:()=>void}){
  const role=useRole();const instance=useInstance();const entries=availableSettings(role.owner,instance);
  const location=useLocation();
  const context=new URLSearchParams(location.search).get('project_id')??location.pathname.match(/^\/projects\/(\d+)/)?.[1]??'';
  // The sidebar marks the open section; the in-page buttons stay plain.
  const here=compact?[location.pathname+location.search+location.hash,location.pathname+(location.pathname==='/settings'&&!location.hash?'#passkeys':location.hash)]:[];
  const link=(to:string,label:string)=><Link key={to} className={compact?'shell-link':`${buttonClass({size:'sm'})} min-h-11 h-auto whitespace-normal text-left`} aria-current={here.includes(to)?'page':undefined} to={to} onClick={close}>{label}</Link>;
  const heading=(name:string)=>compact?<h3 className="shell-caption">{name}</h3>:<h2 className="font-semibold">{name}</h2>;
  return <div className={compact?'space-y-4':'space-y-4 rounded-lg border border-line bg-panel p-4'}>
    {(!only||only==='account')&&<section aria-label="Account settings" className="min-w-0 space-y-2">{heading('Account')}
      <p className="text-xs text-muted">Your sign-in, devices, keys and personal experience.</p>
      <div className={compact?'':'flex flex-wrap gap-2'}>{entries.personal.filter(name=>name!=='Security'&&name!=='Guardrails').map(name=>link(`/settings#${settingAnchor(name)}`,name==='Sign-in security'?'Security':name))}
        {link('/providers','Provider sign-ins')}
        {role.owner&&role.account&&link(securityLink({kind:'account',machine:'local',account:role.account}),'Agent account Security')}
      </div>
    </section>}
    {(!only||only==='project')&&<section id={compact?undefined:'project-settings'} tabIndex={compact?undefined:-1} aria-label="Project settings" className="min-w-0 space-y-2 scroll-mt-4">{heading('Project')}
      {compact && link('/settings#project-settings','Project settings')}
      <p className="text-xs text-muted">Choose a project, then open its existing settings. Security changes stay owner-managed.</p>
      <ProjectSettingsLinks key={JSON.stringify([role.username,role.role,role.account,context])} context={context} compact={compact} close={close}/>
    </section>}
    {role.owner&&(!only||only==='installation')&&<section aria-label="Installation settings" className="min-w-0 space-y-2">{heading('Installation')}
      <p className="text-xs text-muted">Owner controls for people, policy, integrations and runtime availability.</p>
      <div className={compact?'':'flex flex-wrap gap-2'}>{entries.installation.map(name=>link(`/settings#${settingAnchor(name)}`,name))}
        {link(securityLink({kind:'instance'}),'Security')}{link('/settings/security/folders','Allowed folders')}
      </div>
    </section>}
  </div>;
}
function ProjectSettingsLinks({context,compact,close}:{context:string;compact:boolean;close:()=>void}){
  const projects=useProjects();const {owner}=useRole();const instance=useInstance();
  // The parent keys this picker to authority and context, including browser Back.
  const [chosen,setChosen]=useState<string|null>(null);
  const selected=chosen??context;
  const project=projects.data?.find(p=>String(p.id)===selected);
  const link=(to:string,label:string)=><Link key={to} className={compact?'shell-link':`${buttonClass({size:'sm'})} min-h-11 h-auto whitespace-normal text-left`} to={to} onClick={close}>{label}</Link>;
  return <QueryState query={projects}>{rows=><>
    <label className="block space-y-1 text-xs text-muted">Settings project<Select className="w-full min-h-11" value={project?String(project.id):''} onChange={event=>setChosen(event.target.value)}>
      <option value="">Choose a project</option>{rows.map(p=><option value={p.id} key={p.id}>{p.name}{p.archived?' · Archived':''}</option>)}
    </Select></label>
    {!rows.length&&<p className="text-sm text-muted">No projects available to your account.</p>}
    {project&&<><p className="break-all text-xs text-muted">{project.name} · {project.host} / {project.account??'agent account not reported'}</p>
      <div className={compact?'':'flex flex-wrap gap-2'}>
        {link(`/projects/${project.id}#project-keys`,'Keys')}
        {!project.private_workspace&&link(`/projects/${project.id}#project-repository`,'Issues & PRs')}
        {instance.orchestrator&&link(`/projects/${project.id}#project-brief`,'Project brief')}
        {!desktopBuild&&link(`/projects/${project.id}#project-backup`,'Backup')}
        {!desktopBuild&&project.import_mode==='managed'&&link(`/projects/${project.id}#project-ports`,'Ports')}
        {!desktopBuild&&owner&&project.import_mode==='managed'&&link(`/projects/${project.id}#project-stack`,'Runtime')}
        {link(securityLink({kind:'project',project_id:project.id}),'Security')}
        {owner&&project.account&&link(`/settings/security/folders?${new URLSearchParams({machine:project.host,account:project.account})}`,'Allowed folders')}
      </div>
      {owner&&project.account&&<p className="text-xs text-muted">Allowed folders belongs to this machine / agent account, shared by projects using it. It is not a project-only root list.</p>}
    </>}
  </>}</QueryState>;
}
