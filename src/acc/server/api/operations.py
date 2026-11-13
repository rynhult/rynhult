"""Host, providers, model catalog and catalog listing (read-only views)."""
from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select

from ... import image_check
from ... import providers as registry
from .. import budgets
from ..broker_client import BrokerError
from ..image_capabilities import capability, decorate, key
from ..models import AuditLog, Node, Observation, Project, Setting
from ..nodes import node_text
from .context import Context, refresh_read


class ImageCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: StrictInt
    provider: str = Field(pattern=r'^[a-z][a-z0-9-]{1,23}$')
    model: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._:/+-]{0,159}$')


async def run_image_check(supervisor, body: ImageCheck, *, actor: str, authorize=None, budget_owner=None, expected_identity=None):
    """Existing bounded probe and evidence validation; optional current delegated admission."""
    db = supervisor.db
    if body.provider not in registry.ids():
        raise HTTPException(422,'Unknown provider')
    with db() as s:
        project=s.get(Project,body.project_id)
        if project is None:
            raise HTTPException(404,'Project unavailable')
        if not project.trusted or project.import_mode!='managed' or project.archived:
            raise HTTPException(409,'Choose a trusted managed project on the execution machine')
        if project.containers:
            raise HTTPException(409,'Choose a native execution project; native checks do not qualify per-session containers')
        account,machine=project.account,project.host or 'local'
        if budgets.paused(s,budget_owner or actor,body.provider,project_id=body.project_id):
            raise HTTPException(409,'Your budget pauses this provider; adjust it before checking image access')
    lock=supervisor.__dict__.setdefault('_image_check_lock',asyncio.Lock())
    cooldown=key(account,machine,body.provider,'*')+'.cooldown'
    async with lock:
        with db() as s:
            previous=s.get(Setting,cooldown)
            if previous and time.time()-previous.value['at']<60:
                raise HTTPException(429,'Wait one minute between image checks for this machine/provider')
            s.merge(Setting(key=cooldown,value={'at':time.time()},updated_by=actor))
            s.add(AuditLog(actor=actor,action='image.check.started',target=machine,
                          detail={'account':account,'provider':body.provider,'model':body.model}))
            s.commit()
    try:
        catalog=await supervisor.broker.call('model_catalog',account=account,host=machine)
        chosen=next((m for m in catalog.get(body.provider,[]) if isinstance(m,dict) and m.get('id')==body.model),None)
        if chosen is None:
            raise HTTPException(409,'Model is not in this execution machine/account catalog; refresh and choose again')
        if expected_identity is not None:
            if (type(chosen.get('uid')) is not int
                    or chosen.get('delegation_protocol') != 'image-check-delegation-v1'
                    or any(chosen.get(k) != v for k, v in expected_identity.items())
                    or not isinstance(chosen.get('adapter_version'), str)
                    or not re.fullmatch('[0-9a-f]{64}', chosen['adapter_version'])
                    or not isinstance(chosen.get('cli_version'), str) or not 1 <= len(chosen['cli_version']) <= 120):
                raise HTTPException(409, 'Image probe account/runtime or delegation protocol changed')
        if authorize is not None:
            from ..db import write_in_thread
            await write_in_thread(authorize, account, machine)
        binding = ({'expected_uid':expected_identity['uid'], 'expected_runtime_id':expected_identity['runtime_id']}
                   if expected_identity is not None else {})
        result=await supervisor.broker.call('image_probe',account=account,host=machine,provider=body.provider,model=body.model,**binding)
        identity_fields = ('adapter_version','cli_version','runtime_id') + (('uid',) if expected_identity is not None else ())
        if any(result.get(k)!=chosen.get(k) for k in identity_fields):
            result={**result,'image_input':None,'evidence':{'outcome':'runtime changed; check again'}}
    except BrokerError:
        result={'image_input':None,'checked_at':time.time(),'evidence':{'outcome':'broker unavailable or reply uncertain'}}
    evidence=result.get('evidence') if isinstance(result.get('evidence'),dict) else {}
    safe_evidence={'outcome':evidence.get('outcome') if evidence.get('outcome') in (
        'pass','pass, 1 character misread','fail','error','unsupported','runtime changed during probe','runtime changed; check again',
        'broker unavailable or reply uncertain') else 'error'}
    for field in ('image_sha256','response_sha256'):
        value=evidence.get(field)
        if isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value):
            safe_evidence[field]=value
    if evidence.get('error_kind') in ('sign-in required','unsupported arguments','CLI error','claude_subscription_automation'):
        safe_evidence['error_kind']=evidence['error_kind']
    if type(evidence.get('exit_code')) is int and -255<=evidence['exit_code']<=255:
        safe_evidence['exit_code']=evidence['exit_code']
    if isinstance(evidence.get('unsupported_argument'),str) and re.fullmatch(r'--[a-zA-Z][a-zA-Z-]{1,60}',evidence['unsupported_argument']):
        safe_evidence['unsupported_argument']=evidence['unsupported_argument']
    for field in ('expected','observed'):
        proof=evidence.get(field)
        if (isinstance(proof,dict) and isinstance(proof.get('code'),str)
                and re.fullmatch('[A-Z0-9]{0,4}',proof['code']) and isinstance(proof.get('shapes'),list)
                and len(proof['shapes'])<=3 and all(v in {
                    color+' '+shape for color in ('red','green','blue') for shape in ('circle','square','triangle')
                } for v in proof['shapes'] if isinstance(v,str))
                and all(isinstance(v,str) for v in proof['shapes'])):
            safe_evidence[field]={'code':proof['code'],'shapes':list(proof['shapes'])}
    if result.get('image_input') is True:
        decision=image_check.assessment(safe_evidence.get('expected'),safe_evidence.get('observed'),0)
        if decision not in image_check.PASS_OUTCOMES:
            result={**result,'image_input':None}
            safe_evidence['outcome']='error'
        else:
            safe_evidence['outcome']=decision
    result={k:result.get(k) for k in ('image_input','adapter_version','cli_version','runtime_id')}
    result={**result,'checked_at':time.time(),'evidence':safe_evidence,
            'account':account,'machine':machine,'provider':body.provider,'model':body.model,'scope':'native'}
    result['image_input']=capability(result,{'id':body.model,**result},account=account,machine=machine,provider=body.provider)
    if result['image_input'] is None and result['evidence']['outcome'] in (*image_check.PASS_OUTCOMES,'fail'):
        result['evidence']['outcome']='error'
    with db() as s:
        s.merge(Setting(key=key(account,machine,body.provider,body.model),value=result,updated_by=actor))
        if result['image_input'] is not None:
            from ..image_capabilities import resolve_check
            resolve_check(s, body.project_id, account, machine, body.provider, body.model)
        s.add(AuditLog(actor=actor,action='image.check.finished',target=machine,detail=result))
        s.commit()
    if result['evidence'].get('error_kind') == 'claude_subscription_automation':
        from ...providers.claude_auth import detail
        raise HTTPException(409, detail())
    return result



def router(ctx: Context) -> APIRouter:
    db, supervisor, refused = ctx.db, ctx.supervisor, ctx.refused
    api = APIRouter()
    from . import github_token
    api.include_router(github_token.router(ctx))

    @api.get("/api/host")
    async def host_view(machine: str | None = None, who=Depends(ctx.principal)):
        """The machine's health. Everyone sees CPU, memory, disks and drives; host-wide readings (systemd units, every
        container, the other agent accounts and what they use, git backups) are the owners' (prerelease review D12).
        A member gets their own account's line and the containers of their own projects that Rynhult runs.
        `machine`: another connected machine's details, owners only (node_host.py)."""
        if machine and machine != "local":
            if not who.is_owner:
                raise HTTPException(403, "Only Rynhult's owners can see other machines' details.")
            return await node_view(machine)
        view = await asyncio.to_thread(supervisor.host_view)
        if who.is_owner:
            return {**view, "git_backup": await asyncio.to_thread(supervisor.backup_total)}
        snapshot = view.get("snapshot")
        if snapshot is not None:
            with db() as s:
                stacks = set(s.scalars(select(Project.slug).where(Project.owner == who.username,
                                                                  Project.compose_autostart.is_(True))))
            snapshot = {k: v for k, v in snapshot.items() if k not in ("services", "containers", "accounts")} | {
                "accounts": [a for a in snapshot.get("accounts") or [] if a.get("account") == who.account],
                "containers": [c for c in snapshot.get("containers") or [] if c.get("project") in stacks]}
        return {**view, "snapshot": snapshot, "scoped": True}

    async def node_view(name: str) -> dict:
        with db() as s:
            node = s.scalar(select(Node).where(Node.name == name, Node.status == "active"))
            if node is None:
                raise HTTPException(404, "That machine wasn't found. Check Host → Machines.")
            from ..machine_presence import planned_downtime
            downtime = planned_downtime(node)
            info, last_seen = dict(node.info or {}), node.last_seen
            public_backup = s.get(Observation, f'backup.public.{node.id}')
            public_backup_at = public_backup.value.get('at') if public_backup else None
        hub = ctx.broker.hub
        link = hub.links.get(name) if hub else None
        connected = bool(link and not link.closed)
        out = {"last_backup_via_public_address": public_backup_at, "machine": name, "connected": connected, "last_seen": last_seen, "planned_downtime": downtime,
               # Text a node reported, maybe before SR6-11's cleaning (SR7-6).
               "version": node_text((link.info if connected else info).get("version") or info.get("version") or "") or None,
               "os": node_text(info["os"]) if info.get("os") else info.get("os"),
               "supported": bool(connected and link.host_capable),
               "snapshot": None, "live": None, "received_at": None, "light": None}
        if not out["supported"]:
            return out
        snapshot = await link.request_host()
        if snapshot is not None:
            out["live"] = snapshot.get("live")
            out["snapshot"] = {k: v for k, v in snapshot.items() if k != "live"}
            out["received_at"] = link.host_snapshot_at
        out["light"] = link.host_light
        return out

    @api.get("/api/providers")
    async def providers(refresh: int = 0, who=Depends(ctx.principal)):
        """Plan usage is the default account's (owners); members see their logins on the same page."""
        if refresh and who.is_owner:
            with refresh_read():
                await supervisor.refresh_providers()
        with db() as s:
            observations = {o.key: {"value": o.value, "source": o.source, "observed_at": o.observed_at}
                            for o in s.scalars(select(Observation).where(Observation.key.in_(
                                (*(p.usage_observation for p in registry.PROVIDERS.values()), "claude.rate_limits",
                                 "auth.health"))))} \
                if who.is_owner else {}
        return {
            "observations": observations,
            "links": {**{p.id: p.usage_url for p in registry.PROVIDERS.values() if p.usage_url},
                      "anthropic_console": "https://console.anthropic.com/settings/usage",
                      "openai_platform": "https://platform.openai.com/usage"},
            "not_exposed": ["A combined remaining percentage across providers (the plans measure usage differently)",
                            "Claude subscription usage before any session has replied (statusline appears after the first reply)",
                            "Metered API spend (needs admin API keys; Rynhult has none, by design)"],
        }


    def launch_scope(project_id: int | None, who, machine=None) -> tuple[str, str]:
        from ..launch_machines import scope
        return scope(ctx, who, project_id, machine)

    @api.get("/api/providers/registry")
    async def provider_registry(project_id: int | None = None, machine: str | None = None, who=Depends(ctx.principal)):
        """Every agent provider Rynhult knows: label, launch options, capabilities, and whether its CLI is installed for
        the signed-in person's agent account (None when that account's broker can't be asked)."""
        account, host = launch_scope(project_id, who, machine)
        try:
            installed = await ctx.broker.call("installed_providers", account=account, host=host)
        except BrokerError:
            installed = {}
        from ...providers.claude_auth import detail, refusal
        from ..teams import default_team_agents
        try:
            auth = await ctx.broker.call('claude_auth', account=account, host=host)
        except BrokerError:
            auth = {}
        from .. import vault
        if project_id is not None:
            with db() as s:
                ctx.project_for(s, who, project_id)
                try:
                    keys = vault.plan(s, project_id, who.username, host != "local")
                except vault.VaultError:
                    keys = None
                # Names only: never decrypt a key to render a launch notice.
                if keys and any(k.variable == 'ANTHROPIC_API_KEY' for k in keys.keys):
                    auth = {'auth_mode': 'api_key'}
        return {"providers": [{**p.public(installed=bool(installed.get(p.id)) if installed else None),
                              'automation_refusal': detail(project_id) if refusal(p.id, auth.get('auth_mode')) else None}
                              for p in registry.PROVIDERS.values()],
                "team_agents": await default_team_agents(ctx.broker, account, host, installed=installed)}

    @api.get("/api/catalog")
    def catalog(who=Depends(ctx.principal)):
        return supervisor.catalog.public()



    @api.post('/api/providers/image-check')
    async def check_image(body: ImageCheck, who=Depends(ctx.owner)):
        with db() as s:
            ctx.project_for(s, who, body.project_id)
        return await run_image_check(supervisor, body, actor=who.username)

    @api.get("/api/models")
    async def models(refresh: int = 0, project_id: int | None = None, machine: str | None = None, who=Depends(ctx.principal)):
        account, host = launch_scope(project_id, who, machine)
        if project_id is not None or account != ctx.broker.default_account or host != "local":
            try:
                catalog = await ctx.broker.call("model_catalog", account=account, host=host)
                with db() as s:
                    return decorate(s,catalog,account,host)
            except BrokerError as error:
                raise refused(error)
        if refresh:
            try:
                with refresh_read():
                    catalog = await supervisor.refresh_models()
                    with db() as s:
                        return decorate(s,catalog,account,host)
            except BrokerError as error:
                raise refused(error)
        with db() as s:
            catalog = s.get(Observation, "models")
            return decorate(s,catalog.value if catalog else {p: [] for p in registry.ids()},account,host)

    return api
