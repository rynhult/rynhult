"""Slice 3: deterministic choices inside immutable grants and owner allow-lists.

No model calls, credentials, task text, or agent-supplied authority enter the ledger.
Price-class estimates are heuristics in plan-usage percentage points, not dollars.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column

from ... import providers
from .. import budgets
from ..image_capabilities import runtime_scope
from ..machine_presence import connected
from ..models import AuditLog, Base, Event, Session, Setting, Team, UTCDateTime, utcnow
from ..supervisor_common import LaunchRefused, now
from . import quota as quota_usage
from .switch import enabled as orchestrator_enabled

TASK_TYPES = ('implementation', 'design', 'security_review', 'docs', 'small_fix', 'review', 'discovery')
POLICY_KEY = 'orchestrator.selection_rules'
ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._:/+-]{0,159}\Z')


class Selection(Base):
    __tablename__ = 'selection'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey('team.id', ondelete='CASCADE'), index=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey('session.id', ondelete='CASCADE'))
    request_id: Mapped[str] = mapped_column(String(32))
    need: Mapped[dict] = mapped_column(JSON)
    assignment_id: Mapped[str | None] = mapped_column(ForeignKey('orch_assignment.id'), index=True)
    catalog_revision: Mapped[str | None] = mapped_column(String(64))
    profile_digest: Mapped[str | None] = mapped_column(String(64))
    classification: Mapped[dict] = mapped_column(JSON, default=dict)
    classification_reason: Mapped[str] = mapped_column(String(1000), default='')
    task_digest: Mapped[str] = mapped_column(String(64))
    candidates: Mapped[list] = mapped_column(JSON)
    chosen: Mapped[int | None]
    reason: Mapped[str] = mapped_column(String(1024))
    budget: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16))
    override_by: Mapped[str | None] = mapped_column(String(64))
    override_reason: Mapped[str | None] = mapped_column(String(1000))
    session_id: Mapped[str | None] = mapped_column(ForeignKey('session.id', ondelete='SET NULL'))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


def enabled():
    return orchestrator_enabled()  # selection has no switch of its own: it is on with Orchestrator v2


def seed_preferences():
    habits = {'implementation': ['codex', 'claude'], 'design': ['claude'],
              'security_review': ['claude'], 'docs': ['copilot', 'claude'],
              'small_fix': ['copilot', 'claude'], 'review': ['claude'], 'discovery': ['claude', 'codex', 'copilot']}
    return {'version': 1, 'rules': [{'profile': '*', 'task_type': task,
            'choices': [{'agent': agent, 'models': ['*']} for agent in agents]} for task, agents in habits.items()]}


def validate_preferences(value):
    if (not isinstance(value, dict) or set(value) - {'version', 'rules', 'quota'} or not {'version', 'rules'} <= set(value) or type(value['version']) is not int
            or value['version'] != 1 or not isinstance(value['rules'], list) or len(value['rules']) > 100):
        raise ValueError('invalid selection allow-lists')
    if 'quota' in value:
        q = value['quota']
        if (not isinstance(q, dict) or set(q) != set(quota_usage.DEFAULTS) or type(q.get('enabled')) is not bool
                or type(q.get('max_used_percent')) not in (int, float)
                or not math.isfinite(q['max_used_percent']) or not 1 <= q['max_used_percent'] <= 100
                or type(q.get('max_age_minutes')) is not int or not 1 <= q['max_age_minutes'] <= 60):
            raise ValueError('quota needs enabled, a 1-100 percent threshold and 1-60 minute freshness')
    seen = set()
    for rule in value['rules']:
        if (not isinstance(rule, dict) or set(rule) != {'profile', 'task_type', 'choices'}
                or not isinstance(rule['profile'], str) or not re.fullmatch(r'\*|[a-z0-9][a-z0-9-]{0,62}', rule['profile'])
                or rule['task_type'] not in TASK_TYPES or not isinstance(rule['choices'], list) or len(rule['choices']) > 20):
            raise ValueError('invalid profile/task allow-list')
        key = rule['profile'], rule['task_type']
        if key in seen:
            raise ValueError('duplicate profile/task allow-list')
        seen.add(key)
        agents = set()
        for choice in rule['choices']:
            if (not isinstance(choice, dict) or set(choice) != {'agent', 'models'}
                    or choice['agent'] not in providers.ids() or choice['agent'] in agents
                    or not isinstance(choice['models'], list) or not 1 <= len(choice['models']) <= 40
                    or any(not isinstance(m, str) or not (m == '*' or ID.fullmatch(m)) for m in choice['models'])
                    or len(set(choice['models'])) != len(choice['models'])
                    or '*' in choice['models'] and choice['models'] != ['*']):
                raise ValueError('choose unique providers and explicit models, or * for known models')
            agents.add(choice['agent'])
    return value


def preferences(db):
    row = db.get(Setting, POLICY_KEY)
    return validate_preferences(row.value if row else seed_preferences())


def policy_choice(policy, profile, task_type, agent, model):
    rules = {(r['profile'], r['task_type']): r['choices'] for r in policy['rules']}
    choices = rules.get((profile or 'none', task_type), rules.get(('*', task_type), []))
    for order, choice in enumerate(choices):
        if choice['agent'] == agent and (choice['models'] == ['*'] or model in choice['models']):
            return order, 0 if choice['models'] == ['*'] else choice['models'].index(model)
    return None


def grant_digest(choice):
    value={key:choice.get(key) for key in ('agent','profile','inputs')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def profile_digest(catalog, choice):
    snapshot = {'profile': catalog.profiles.get(choice.get('profile'), {}),
                'inputs': choice['inputs'], 'catalog_revision': catalog.revision}
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


def permission(choice):
    """The frozen permission a launch really has. Read-only needs the provider's read-only setting AND none of its
    write-everything options or extra arguments: a plan mode next to --allow-all, --force or skip-permissions still
    writes (review C-M1). Anything unrecognised counts as implement."""
    data, agent = choice['inputs'], choice['agent']
    options = data.get('options') or {}
    if data.get('extra_args'):
        return 'implement'
    read_only = (data.get('permission_mode') == 'plan' if agent == 'claude' else
                 data.get('sandbox') == 'read-only' if agent == 'codex' else
                 options.get('mode') == 'plan' and options.get('permissions', 'ask') in (None, 'ask') if agent == 'copilot' else
                 options.get('mode') in ('plan', 'ask') and options.get('force') is not True if agent == 'cursor' else
                 options.get('mode') == 'plan' and options.get('skip_permissions') is not True if agent == 'antigravity' else
                 False)
    return 'read-only' if read_only else 'implement'


def estimate(model, effort):
    price = model.get('price_class', 'standard')
    if not isinstance(price,str):
        price='unknown'
    cost = {'cheap': .25, 'standard': 1., 'expensive': 2.}.get(price, 2.)
    return cost * (4 if effort == 'large' else 2 if effort == 'medium' else 1)


def rank(need, allowed, machines, inventory, *, preferences=None, paused=None, remaining=None, load=None, failures=None, quota=None, pin=None, image_scope='native'):
    policy = validate_preferences(preferences if preferences is not None else seed_preferences())
    paused, remaining, load, failures = paused or {}, remaining or {}, load or {}, failures or {}
    quota_rules = policy.get('quota', quota_usage.DEFAULTS)
    quota = (quota or {}) if quota_rules['enabled'] else {}
    candidates = []
    if len(allowed) * len(machines) > 1200:
        raise LaunchRefused('selection has too many granted combinations')
    task_type = need.get('task_type', 'implementation')
    for grant_index, choice in enumerate(allowed):
        for machine in machines:
            agent = choice['agent']
            ready = inventory.get((machine, agent))
            rows = (ready or {}).get('models', [])
            models = {row['id']: row for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)
                      and ID.fullmatch(row['id'])}
            pinned = choice['inputs'].get('model')
            if pinned:
                models = {pinned: models[pinned]} if pinned in models else {}
            # Record missing/offline/catalog failure even when there is no model.
            for model in sorted(models)[:100] or [None]:
                rejected = []
                pref = policy_choice(policy, choice.get('profile'), task_type, agent, model)
                role = need.get('role', 'general')
                match = (role == 'general' if not choice.get('profile') else
                         role == choice.get('profile') or role in choice.get('roles', []))
                if not match:
                    rejected.append(f'profile does not match required role {role}')
                if pin and any(pin.get(k) is not None and pin[k] != v for k, v in
                               [('agent', agent), ('model', model), ('machine', machine)]):
                    rejected.append('does not match requested pin')
                for requirement in need.get('requires', []):
                    qualified = (models.get(model, {}).get('image_input') is True and not choice['inputs'].get('endpoint') and image_scope=='native' if requirement == 'image_input' else
                                 requirement[5:] in choice['inputs'].get('mcps', []) if requirement.startswith('tool:') else
                                 permission(choice) == 'read-only' if requirement == 'read_only' else
                                 permission(choice) == 'implement' if requirement == 'write' else
                                 (ready or {}).get('network') == 'none' if requirement == 'network:none' else False)
                    if not qualified:
                        rejected.append(f'requirement {requirement} is not qualified')
                if pref is None: rejected.append('profile/task allow-list excludes this provider/model')
                if ready is None: rejected.append('machine offline or provider unavailable')
                elif not ready.get('logged_in'): rejected.append('provider not signed in for this account')
                if model is None: rejected.append('no known granted model')
                if paused.get(agent): rejected.append('budget pauses this provider')
                required = 'read-only' if need.get('effort') == 'plan' or 'read_only' in need.get('requires', []) else 'implement'
                if permission(choice) != required: rejected.append('frozen permission does not match task access')
                cost = estimate(models.get(model, {}), need.get('effort'))
                left = remaining.get(agent)
                if left is not None and (not math.isfinite(left) or cost > left):
                    rejected.append('budget estimate exceeds remaining window')
                q = quota.get((machine, agent), {'status': 'unknown'})
                if quota_rules['enabled'] and q['status'] == 'fresh' and q['used_percent'] >= quota_rules['max_used_percent']:
                    rejected.append('fresh provider quota reaches owner threshold')
                overlap = len(set(need.get('skills_wanted', [])) & set(choice['inputs'].get('skills', [])))
                price_penalty = min(20, round(cost * 10))
                score = 40 * match + min(20, overlap * 5) + (30 if pref is not None else 0) - price_penalty
                score -= 5 * load.get((machine, agent), 0) + round(10 * failures.get(agent, 0))
                reasons = ([f'{choice.get("profile") or "No profile"} profile for a {role} task'] if match else [])
                reasons.append('Risk is recorded only, not used for ranking')
                if overlap: reasons.append(f'{overlap} requested skills already in the frozen profile')
                if pref is not None: reasons.append(f'Owner allow-list preference {pref[0]+1} for {task_type}')
                reasons.append('Cheaper model for a small task' if cost <= 1 else 'Conservative model price-class estimate')
                if load.get((machine, agent)): reasons.append('Load penalty for active helpers on this provider/machine')
                if failures.get(agent): reasons.append('Recent failure penalty, decaying per hour')
                if quota_rules['enabled']:
                    if q['status'] == 'fresh':
                        bonus = math.floor(q['headroom_percent']/5)
                        score += bonus
                        reasons.insert(0, f'Provider quota: {q["used_percent"]:.1f}% used, {q["headroom_percent"]:.1f}% headroom; +{bonus} score (fresh)')
                    else:
                        reasons.insert(0, 'Provider quota: stale or unknown; existing selection rules apply')
                candidates.append({**({'quota': q} if quota_rules['enabled'] else {}), 'agent': agent, 'profile': choice.get('profile'), 'model': model, 'machine': machine,
                    'permission': permission(choice), 'grant_index': grant_index, 'roles':choice.get('roles', []), 'grant_digest':grant_digest(choice), 'score': score, 'reasons': reasons,
                    'est_cost': {'value': cost, 'unit': 'usage_percent', 'source': 'price-class heuristic'},
                    'eligible': not rejected, 'rejections': rejected, 'preference_rank': list(pref) if pref else [999,999]})
                if len(candidates) > 12000:
                    raise LaunchRefused('selection catalogue exceeds its bounded search')
    # Owner decision: allow-list order is an ordered fallback, before heuristic scores.
    candidates.sort(key=lambda c: (not c['eligible'], c['preference_rank'], -c['score']))
    candidates = candidates[:12]
    chosen = 0 if candidates and candidates[0]['eligible'] else None
    context = ' and fresh provider quota' if quota_rules['enabled'] else ''
    available_profiles = ', '.join(sorted({c.get('profile') or 'general' for c in allowed}))
    missing_role = not any('profile does not match' not in ' '.join(c['rejections']) for c in candidates)
    role_hint = f'; closest granted profiles: {available_profiles}; ask the person to add {need.get("role", "general")} to the grant' if missing_role else ''
    return {'candidates': candidates, 'chosen': chosen, 'state': 'proposed' if chosen is not None else 'refused',
            'reason': ('The first choice on the owner\'s allow-list for this kind of task that is within the team\'s limits and budget'+context if chosen is not None
                       else 'Nothing on the owner\'s allow-list for this kind of task is signed in, allowed and within the team\'s limits and budget'+context+role_hint)}




def validate_need(value):
    if not isinstance(value, dict) or set(value) - {'role', 'skills_wanted', 'requires', 'classification_reason', 'effort', 'risk', 'machine', 'task_type', 'author_session'}:
        raise LaunchRefused('invalid structured selection need')
    result = {'role': '', 'skills_wanted': [], 'requires': [], 'classification_reason': '', 'effort': 'implement', 'risk': 'low', **value}
    if result.get('task_type') not in TASK_TYPES or result['effort'] not in ('plan','implement','small','medium','large') or result['risk'] not in ('low','medium','high'):
        raise LaunchRefused('choose a task type, effort and risk')
    if not isinstance(result['role'], str) or len(result['role']) > 63 or not result['role'] or not re.fullmatch('[a-z0-9-]+', result['role']):
        raise LaunchRefused('role is a profile role identifier')
    for key in ('skills_wanted',):
        if not isinstance(result[key], list) or len(result[key]) > 40 or any(not isinstance(v,str) or not re.fullmatch('[a-zA-Z0-9+#.-]{1,63}',v) for v in result[key]):
            raise LaunchRefused('selection skills are bounded identifiers')
    requires = result['requires']
    if (not isinstance(requires, list) or len(requires) > 40 or any(not isinstance(v, str) or
            not (v in ('image_input', 'read_only', 'write', 'network:none') or re.fullmatch(r'tool:[a-z0-9][a-z0-9-]{0,62}', v))
            for v in requires) or len(set(requires)) != len(requires)):
        raise LaunchRefused('requires must contain known bounded hard requirements')
    reason = result['classification_reason']
    if not isinstance(reason, str) or len(reason) > 1000:
        raise LaunchRefused('classification reason must be at most 1000 characters')
    from ..nodes import node_lines
    result['classification_reason'] = node_lines(reason, 1000)
    for key in ('machine','author_session'):
        if key in result and (not isinstance(result[key],str) or not re.fullmatch('[a-z0-9-]{1,40}',result[key])):
            raise LaunchRefused('invalid selection machine or author')
    if result['task_type'] in ('review','security_review') and not result.get('author_session'):
        raise LaunchRefused('a review must identify its author session; it starts a distinct helper')
    return result


def task_digest(task):
    if not isinstance(task,str) or not task.strip() or len(task)>20000:
        raise LaunchRefused('selection task must be 1-20000 characters')
    try:
        data=task.encode('utf-8')
    except UnicodeError:
        raise LaunchRefused('selection task must be UTF-8') from None
    if len(data)>60000:
        raise LaunchRefused('selection task exceeds its byte budget')
    return hashlib.sha256(data).hexdigest()


def authority(db, supervisor, caller_id):
    if not enabled():
        raise LaunchRefused('Orchestrator selection is off')
    caller=db.get(Session,caller_id)
    team=db.get(Team,caller.team_id) if caller and caller.team_id else None
    if not team or team.stopped or now()>=team.expires_at or caller.desired_state!='running':
        raise LaunchRefused('team is stopped, expired, or caller is not running')
    return caller,team


def snapshot_budget(db, owner, agents):
    state=budgets.status(db,owner)
    windows={w['id']:w for w in budgets.windows(db)} if state else {}
    result={}
    for agent in agents:
        rows=[{'id':key,'remaining':max(0.,entry['allowed']-entry['used']),
               'resets_at':windows.get(key,{}).get('resets_at')} for key,entry in state.items()
              if key.startswith(agent+'.') and not entry['stale']]
        result[agent]={'remaining':min((r['remaining'] for r in rows),default=None), 'windows':rows,
                       'paused':bool(budgets.paused(db,owner,agent)), 'unit':'usage_percent'}
    return result


async def readiness(supervisor, account, machines, agents):
    import asyncio
    result={}
    if len(machines)>16:
        raise LaunchRefused('selection supports at most 16 granted machines')
    for machine in machines:
        if not connected(supervisor.broker,machine):
            continue
        try:
            health=await asyncio.wait_for(supervisor.broker.call('auth_health',account=account,host=machine),10)
            catalog=await asyncio.wait_for(supervisor.broker.call('model_catalog',account=account,host=machine),10)
            from ..image_capabilities import decorate
            with supervisor.db() as db:
                catalog = decorate(db, catalog, account, machine)
            for agent in agents:
                if agent in health and isinstance(catalog.get(agent),list):
                    result[machine,agent]={'logged_in':health[agent].get('logged_in') is True,
                                           'models':catalog[agent][:100]}
        except Exception:
            continue  # fixed refusal reasons, never provider diagnostics or sign-in data
    return result


def record_event(db, row, actor, action):
    detail={'selection':row.id,'chosen':row.chosen,'state':row.state,'reason':row.reason}
    if row.override_by:
        detail.update(override_by=row.override_by,override_reason=row.override_reason)
    db.add(AuditLog(actor=actor,action='selection.'+action,target=row.team_id,detail=detail))
    db.add(Event(session_id=row.requested_by,source='orchestrator',kind='team.selection',payload=detail))


def output(row):
    return {key:getattr(row,key) for key in ('id','team_id','requested_by','request_id','need','task_digest','candidates',
                'assignment_id','catalog_revision','profile_digest','classification','classification_reason','chosen','reason','budget','state','override_by','override_reason','session_id','created_at')}


def agent_output(row):
    """What a team's agents see of a selection (security review 2026-10-25, OV-8; threat review S3-7): for the
    providers in the team's own grant, the remaining budget rounded to whole percentage points and whether it is
    paused; never the person's windows, account, owner or policy. The person's API (output) keeps the full record."""
    budget=row.budget or {}
    providers={agent:{'remaining':None if v.get('remaining') is None else round(v['remaining']),'paused':bool(v.get('paused'))}
               for agent,v in (budget.get('providers') or {}).items()}
    result=output(row)
    result['candidates']=[dict(c) for c in result['candidates']]
    for c in result['candidates']:
        if 'quota' in c:
            q=c['quota']
            c['quota']={'status':q['status'],**({'headroom_percent':round(q['headroom_percent'])} if q['status']=='fresh' else {})}
            c['reasons']=['Provider quota headroom considered' if r.startswith('Provider quota:') else r for r in c['reasons']]
    return {**result,'budget':{'providers':providers,'unit':'usage_percent','expected_spend':budget.get('expected_spend')}}


def failure_weight(member, at):
    if member.observed_state != 'failed':
        return 0
    age = max(0, (at - (member.ended_at or member.created_at)).total_seconds() / 3600)
    return max(0, 1 - age)


async def select_action(supervisor,caller_id,payload):
    from copy import deepcopy

    from sqlalchemy import func
    assignment_id = payload.get('assignment')
    if not assignment_id:
        raise LaunchRefused('select requires an assignment')
    from .assignments import assignment_for
    with supervisor.db() as db:
        caller, _ = authority(db, supervisor, caller_id)
        assignment = assignment_for(db, caller, assignment_id)
        need = validate_need(assignment.need)
        digest = task_digest(assignment.goal)
    pin = payload.get('pin')
    if pin is not None and (not isinstance(pin, dict) or not pin.get('agent') or set(pin) - {'agent','model','machine'}
                            or any(not isinstance(v, str) or not ID.fullmatch(v) for v in pin.values())):
        raise LaunchRefused('pin must name a provider and optional known model/machine')
    with supervisor.db() as db:
        caller,team=authority(db,supervisor,caller_id)
        if need.get('author_session'):
            author=db.get(Session,need['author_session'])
            if author is None or author.team_id!=team.id:
                raise LaunchRefused('review author is outside the team')
        limits=deepcopy(team.limits)
        machines=limits['machines']
        if need.get('machine'):
            if need['machine'] not in machines:
                raise LaunchRefused('machine is not granted')
            machines=[need['machine']]
        account,owner=caller.run_as,caller.owner
    agents={c['agent'] for c in limits['agents']}
    ready=await readiness(supervisor,account,machines,agents)
    with supervisor.db() as db:
        caller,team=authority(db,supervisor,caller_id)
        if team.limits!=limits or caller.run_as!=account or caller.owner!=owner:
            raise LaunchRefused('selection authority changed')
        if db.scalar(select(func.count()).select_from(Selection).where(Selection.team_id==team.id))>=2000:
            raise LaunchRefused('team selection ledger limit reached')
        budget=snapshot_budget(db,owner,agents)
        policy=preferences(db)
        allowed=deepcopy(limits['agents'])
        for choice in allowed:
            choice['roles']=supervisor.catalog.profiles.get(choice.get('profile'),{}).get('roles',[])
        members=list(db.scalars(select(Session).where(Session.team_id==team.id,Session.parent_session_id.is_not(None))))
        load={}
        failures={}
        for member in members:
            if member.desired_state=='running' or member.team_stop_pending:
                key=member.host,member.provider
                load[key]=load.get(key,0)+1
            failures[member.provider] = failures.get(member.provider, 0) + failure_weight(member, now())
        quota=quota_usage.snapshot(db,account,machines,agents,supervisor.broker.default_account,policy,now().timestamp(),owner=owner)
        result=rank(need,allowed,machines,ready,preferences=policy,quota=quota,pin=pin,image_scope=runtime_scope(caller),
                    paused={p:v['paused'] for p,v in budget.items()}, remaining={p:v['remaining'] for p,v in budget.items()},
                    load=load,failures=failures)
        redactor=supervisor.vault.redactor(db,caller.vault_keys)
        safe_need=redactor.deep(need)
        result['reason']=redactor.text(result['reason'])
        for candidate in result['candidates']:
            candidate['reasons']=[redactor.text(text) for text in candidate['reasons']]
            candidate['rejections']=[redactor.text(text) for text in candidate['rejections']]
            candidate['catalog_revision'] = supervisor.catalog.revision
            candidate['profile_digest'] = profile_digest(supervisor.catalog, limits['agents'][candidate['grant_index']])
        row=Selection(id=uuid.uuid4().hex,team_id=team.id,requested_by=caller.id,
            request_id=payload.get('id') or uuid.uuid4().hex,need=safe_need,task_digest=digest,
            assignment_id=assignment_id, catalog_revision=supervisor.catalog.revision,
            profile_digest=profile_digest(supervisor.catalog, allowed[result['candidates'][result['chosen']]['grant_index']]) if result['chosen'] is not None else None,
            classification={'task_type':safe_need['task_type'], 'role':safe_need['role']}, classification_reason=safe_need['classification_reason'],
            budget={'providers':budget,'account':account,'owner':owner,'policy_hash':hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest(),
                    'expected_spend':result['candidates'][result['chosen']]['est_cost'] if result['chosen'] is not None else None,
                    'estimated_at':now().isoformat()},**result)
        db.add(row)
        record_event(db,row,'session:'+caller.id,'proposed' if row.chosen is not None else 'refused')
        db.commit()
        return agent_output(row)


async def status_action(supervisor,caller_id,payload):
    from ..teams import descendants
    if not isinstance(payload.get('selection'),str) or not re.fullmatch('[0-9a-f]{32}',payload['selection']):
        raise LaunchRefused('invalid selection identifier')
    with supervisor.db() as db:
        caller,team=authority(db,supervisor,caller_id)
        row=db.get(Selection,payload.get('selection'))
        visible={caller.id}|{s.id for s in descendants(db,caller)}
        if row is None or row.team_id!=team.id or row.requested_by not in visible:
            raise LaunchRefused('selection is outside caller subtree')
        return agent_output(row)


async def prepare_start(supervisor,caller_id,payload):
    """Fresh readiness before claiming. Called by M17, under its existing team lock."""
    if not isinstance(payload.get('selection'),str) or not re.fullmatch('[0-9a-f]{32}',payload['selection']):
        raise LaunchRefused('invalid selection identifier')
    with supervisor.db() as db:
        caller,team=authority(db,supervisor,caller_id)
        row=db.get(Selection,payload.get('selection'))
        if (row is None or row.requested_by!=caller.id or row.team_id!=team.id
                or row.state not in ('proposed','overridden') or row.chosen is None):
            raise LaunchRefused('selection is unavailable, consumed or outside caller authority')
        if row.task_digest!=task_digest(payload.get('task')):
            raise LaunchRefused('task does not match selection digest')
        candidate=dict(row.candidates[row.chosen])
        account=caller.run_as
        choice = team.limits['agents'][candidate['grant_index']]
        if row.profile_digest and profile_digest(supervisor.catalog, choice) != row.profile_digest:
            row.state = 'refused'
            row.reason = 'Selection profile/catalog changed; request a fresh selection'
            record_event(db, row, 'session:' + caller.id, 'refused')
            db.commit()
            raise LaunchRefused('selection profile/catalog changed; request a fresh selection')
    ready=await readiness(supervisor,account,[candidate['machine']],{candidate['agent']})
    return candidate,ready


def check_candidate(db,caller,team,row,candidate,ready,*,default_account=None,catalog=None):
    if row.budget.get('account')!=caller.run_as or row.budget.get('owner')!=caller.owner:
        raise LaunchRefused('selection account or person changed')
    index=candidate.get('grant_index')
    if type(index) is not int or not 0<=index<len(team.limits['agents']):
        raise LaunchRefused('selection grant changed')
    choice=team.limits['agents'][index]
    choice = {**choice, 'roles':candidate.get('roles', [])}
    if catalog is not None:
        if (candidate.get('catalog_revision') != catalog.revision or
                candidate.get('profile_digest') != profile_digest(catalog, choice)):
            raise LaunchRefused('candidate profile/catalog changed; request a fresh selection')
        choice['roles'] = catalog.profiles.get(choice.get('profile'), {}).get('roles', [])
    if candidate.get('grant_digest')!=grant_digest(choice):
        raise LaunchRefused('frozen selection inputs changed')
    if (candidate['agent']!=choice['agent'] or candidate['profile']!=choice.get('profile')
            or candidate['machine'] not in team.limits['machines'] or candidate['permission']!=permission(choice)):
        raise LaunchRefused('selection exceeds frozen grant')
    if row.need.get('author_session'):
        author=db.get(Session,row.need['author_session'])
        if author is None or author.team_id!=team.id:
            raise LaunchRefused('review author is no longer in the team')
    budget=snapshot_budget(db,caller.owner,{candidate['agent']})
    policy=preferences(db)
    quota=quota_usage.snapshot(db,caller.run_as,[candidate['machine']],{candidate['agent']},
                               default_account,policy,now().timestamp(),owner=caller.owner)
    result=rank(row.need,[choice],[candidate['machine']],ready,preferences=policy,quota=quota,image_scope=runtime_scope(caller),
                paused={p:v['paused'] for p,v in budget.items()},remaining={p:v['remaining'] for p,v in budget.items()})
    eligible=next((c for c in result['candidates'] if c['model']==candidate['model'] and c['eligible']),None)
    if eligible is None:
        raise LaunchRefused('selection no longer fits the allow-list, sign-in, permission, budget or fresh provider quota')
    return choice


def check_plain_start(db,supervisor,caller,choice,machine):
    """A start without a selection keeps to the owner's allow-lists and quota threshold too (review 2026-10-26, L-3).
    It names no task type, so its agent and model must be on the allow-list for the grant's profile for some task."""
    policy=preferences(db)
    agent,model,profile=choice['agent'],choice['inputs'].get('model'),choice.get('profile')
    if not any(policy_choice(policy,profile,task,agent,model) is not None for task in TASK_TYPES):
        raise LaunchRefused(f"the owner's selection allow-lists don't include {agent}"+(f" ({model})" if model else '')
                            +f" for the {profile or 'no'} profile: use select, or ask the owner to add it in "
                            "Settings → Selection allow-lists")
    rules=policy.get('quota',quota_usage.DEFAULTS)
    reading=quota_usage.snapshot(db,caller.run_as,[machine],{agent},supervisor.broker.default_account,policy,
                                 now().timestamp(),owner=caller.owner).get((machine,agent),{'status':'unknown'})
    if rules['enabled'] and reading['status']=='fresh' and reading['used_percent']>=rules['max_used_percent']:
        raise LaunchRefused(f"{agent} on {machine} has used {reading['used_percent']:g}% of its plan, at or above the "
                            f"owner's {rules['max_used_percent']:g}% threshold: wait for it to reset, or use select")


def claim_start(db,supervisor,caller,team,payload,candidate,ready):
    row=db.scalar(select(Selection).where(Selection.id==payload['selection']).with_for_update())
    if (row is None or row.team_id!=team.id or row.requested_by!=caller.id
            or row.state not in ('proposed','overridden') or row.chosen is None
            or row.candidates[row.chosen]!=candidate or row.task_digest!=task_digest(payload.get('task'))):
        raise LaunchRefused('selection changed or has already been consumed')
    if not enabled():
        raise LaunchRefused('Orchestrator selection is off')
    try:
        if row.profile_digest and profile_digest(supervisor.catalog, team.limits['agents'][candidate['grant_index']]) != row.profile_digest:
            raise LaunchRefused('selection profile/catalog changed')
        check_candidate(db,caller,team,row,candidate,ready,default_account=supervisor.broker.default_account,catalog=supervisor.catalog)
    except LaunchRefused:
        row.state='refused'
        row.reason='Selection no longer fits the grant, allow-list, sign-in, budget or fresh provider quota; request a new selection'
        record_event(db,row,'session:'+caller.id,'refused')
        db.commit()
        raise
    row.state='started'  # reserve once before any worktree/session await; no replay after a crash
    record_event(db,row,'session:'+caller.id,'started')
    return row


def refuse_start(db,team,caller,selection_id,reason):
    """A selected start refused before claim_start (teams L-7 re-check): close the selection like check_candidate does."""
    row=db.scalar(select(Selection).where(Selection.id==selection_id).with_for_update())
    if row is None or row.team_id!=team.id or row.requested_by!=caller.id or row.state not in ('proposed','overridden'):
        return
    row.state='refused'
    row.reason=reason
    record_event(db,row,'session:'+caller.id,'refused')


def complete_start(db,selection_id,session_id=None):
    row=db.get(Selection,selection_id)
    if row is None:
        return
    if session_id:
        if row.need.get('author_session')==session_id:
            raise LaunchRefused('a review must use a distinct session')
        row.session_id=session_id
    else:
        row.state='refused'
        row.reason='Helper launch failed after reservation; inspect before retrying with a new selection'
        record_event(db,row,'session:'+row.requested_by,'refused')


TEAM_FIELDS = {'select': {'assignment','pin'}, 'selection_status': {'selection'},
               'start': {'assignment','attempt','selection','name','task','kind','fresh','machine','agent','profile'}}
TEAM_ACTIONS = {'select':select_action,'selection_status':status_action}

ON_STATE: list = []
