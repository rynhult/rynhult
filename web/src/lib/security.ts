import {useQuery} from '@tanstack/react-query';
import {api} from './api';

export type SecurityScope = {kind:'instance'} | {kind:'account';machine:string;account:string;machine_id?:string} | {kind:'project';project_id:number};
export type SecurityPolicy = {
  revision:number;registry_version:number;scope:SecurityScope;can_confirm_folder_recovery?:boolean;older_sessions?:string[];unconfirmed_stops?:string[];
  switches:Record<string,boolean|null>;varying_switches?:string[];sources?:Record<string,SecurityScope|{kind:'legacy';key:string}|{kind:'default'}|{kind:'varies'}>;
  mode:'default'|'custom'|'unrestricted';policy_mode?:string;enforcement?:{allowed_folders:string};
};
export interface SecurityRegistry {revision:number;registry_version:number;supported_bypass_providers:string[];
  registry:{id:string;label:string;default:boolean;account_only:boolean}[];
  policies:{scope:SecurityScope;overrides:Record<string,boolean>;can_restore:boolean}[]}
export const SECURITY_COPY:Record<string,{label:string;off:string}> = {
  force_push:{label:'Block force pushes',off:'Agents may rewrite remote branch history if their Git credentials allow it.'},
  push_default:{label:'Block pushes to main or master',off:'Agents may push directly to main or master; remote branch protection still applies.'},
  merge_pr:{label:'Block agent pull-request merges',off:"Agents may merge through their own Git credentials. Rynhult's owner-only/team actions remain restricted."},
  rm_outside:{label:'Block recursive deletes outside the workspace',off:'Agents may delete files outside the session folder wherever their OS account can write.'},
  secrets:{label:'Block credential-file and environment reads',off:'Agents may read credential files and environment values accessible to their OS account. Vault grants and server secrets remain protected.'},
  custom_rules:{label:'Apply custom command blocks',off:'Your saved command patterns will not block tools in this scope. The patterns are kept.'},
  allowed_folders:{label:'Limit account-service operations to allowed folders',off:"Rynhult's account service may use other accessible repositories. This grants no filesystem access or root permission."},
  tool_install_check:{label:'Ask before account-wide tool changes',off:'Agents may install or remove account-wide tools with their existing OS rights. System package requests still need an owner.'},
  provider_prompts:{label:'Ask before provider tool actions',off:'Eligible build sessions may run tools without provider permission prompts or sandbox protection. Read-only assignments and Rynhult authority checks still apply.'},
  budget_checks:{label:'Enforce saved usage budgets',off:'Saved usage warning/pause rules will not apply in this scope. Provider charges and quotas still apply.'},
  risk_labels:{label:'Show advisory risk labels',off:'Advisory labels will be hidden here. This changes no permission or approval.'},
};
export function scopeEqual(a:SecurityScope,b:SecurityScope) {
  return a.kind===b.kind && (a.kind==='instance' || a.kind==='project'&&b.kind==='project'&&a.project_id===b.project_id || a.kind==='account'&&b.kind==='account'&&a.machine===b.machine&&a.account===b.account&&(!a.machine_id||!b.machine_id||a.machine_id===b.machine_id));
}
export function scopeName(scope:SecurityScope) {
  return scope.kind==='instance'?'Instance defaults':scope.kind==='project'?`Project ${scope.project_id}`:`${scope.account} on ${scope.machine}`;
}
export function securityUrl(scope:SecurityScope) {
  const p=new URLSearchParams();
  if(scope.kind==='project')p.set('project_id',String(scope.project_id));
  if(scope.kind==='account'){p.set('machine',scope.machine);p.set('account',scope.account);}
  return `/api/security/effective${p.size?'?'+p:''}`;
}
export function securityLink(scope:SecurityScope) {
  const p=new URLSearchParams({security_scope:scope.kind});
  if(scope.kind==='project')p.set('project_id',String(scope.project_id));
  if(scope.kind==='account'){p.set('machine',scope.machine);p.set('account',scope.account);}
  return `/settings?${p}#security`;
}
export function useSecurityPolicy(scope:SecurityScope|undefined) {
  return useQuery({queryKey:['security-effective',scope?securityUrl(scope):null],enabled:!!scope,
    queryFn:()=>api<SecurityPolicy>(securityUrl(scope!)),refetchInterval:10000});
}
/** Read the frozen launch record, never substitute today's policy for an older session. */
export function frozenSecurity(files:Record<string,string>|undefined):SecurityPolicy|undefined {
  try {
    const value=JSON.parse(files?.['guardrails.json']??'{}').security;
    if(!value||!value.switches||typeof value.revision!=='number'||!Object.values(value.switches).every(x=>typeof x==='boolean'))return undefined;
    return value;
  } catch {return undefined;}
}

export function securityAuditLabel(action:string) {
  return ({'security.policy.applied':'Policy saved','security.policy.preview':'Policy reviewed',
    'security.policy.refused':'Request refused','security.policy.conflict':'Policy conflict',
    'security.policy.failed':'Operation failed'} as Record<string,string>)[action]??'Security event';
}
