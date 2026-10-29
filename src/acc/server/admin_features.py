"""Installation feature availability. Core authority and qualification are never switches.

The DB stores availability, not per-project choices or consent. The main owns one
snapshot; publication follows commit, so workers never acquire a second DB lock
from inside a session transaction. Requests/background tasks share that snapshot.
"""
from contextvars import ContextVar
from dataclasses import dataclass

from acc.env import environment

from .config import Settings
from .models import Setting


@dataclass(frozen=True)
class Feature:
    label: str
    description: str
    default: bool = True
    preview: bool = False
    sensitive: bool = False
    config: str = ''
    env: str = ''


FEATURES = {
    'multirun': Feature('Multi-run', 'Compare several agent runs for the same task.'),
    'schedules': Feature('Scheduled sessions', 'Start saved sessions at chosen times; Off pauses dispatch.'),
    'automation': Feature('Auto-resume and goals', 'Continue goals and recover sessions automatically.'),
    'live_preview': Feature('Live preview', 'Open running project sites inside Rynhult.'),
    'publishing': Feature('Publishing', 'Manage site publishing; Off leaves existing sites live.'),
    'issue_sources': Feature('Issue sources', 'Connect project issues from Linear and GitLab.'),
    'organizations': Feature('Company sign-in', 'Offer configured company sign-in alongside local sign-in.', False,
                             preview=True, sensitive=True, config='organizations_enabled', env='ACC_ORGANIZATIONS'),
    'groups': Feature('Groups', 'Share projects and coordinate work in groups.'),
    'groups_remote': Feature('Own-machine groups', 'Allow group placements on members’ machines.', False,
                            preview=True, sensitive=True, config='groups_remote_enabled', env='ACC_GROUPS_REMOTE'),
    'teams': Feature('Agent teams', 'Let a lead delegate work to helpers; Off stops teams safely.'),
    'containers': Feature('Containers per session', 'Use qualified container isolation for opted-in projects.', False,
                          True, True, 'containers_enabled', 'ACC_CONTAINERS'),
    'orchestrator': Feature('Orchestrator v2', 'Plan and coordinate agent teams. Gates need a separate owner opt-in.',
                            sensitive=True, env='ACC_ORCHESTRATOR'),
    'remote_access': Feature('Direct public remote access', 'Show the public-access settings and device pairing; public access stays off until reviewed and applied. Off closes public streams.',
                             preview=True, sensitive=True),
    'telemetry': Feature('Telemetry opt-in', 'Offer usage and report sharing; separate consent is still required.'),
    'mobile': Feature('Mobile enrollment', 'Allow native device enrollment and credential lifecycle APIs.', False,
                      True, True, config='mobile_enabled', env='ACC_MOBILE'),
    'local_models': Feature('Local models', 'Configure model endpoints for agent sessions.'),
    'notifications': Feature('Web notifications', 'Manage browser push subscriptions and notification delivery.'),
    'explanations': Feature('Change explanations', 'Ask a helper to explain session changes.'),
    'risk_labels': Feature('Risk labels', 'Show advisory change-risk labels.'),
}
CONFIG_FEATURES = {feature.config: key for key, feature in FEATURES.items() if feature.config}
KEY = 'admin_features'
CURRENT = ContextVar('admin_features', default=None)


class Features:
    def __init__(self, settings, choices=None):
        self.settings = settings
        self.choices = dict(choices or {})
        self.revision = 0

    def configured(self, key):
        feature = FEATURES[key]
        # Generic overrides also let an operator pin an otherwise DB-controlled feature.
        for variable in (f'ACC_FEATURE_{key.upper()}', feature.env):
            if variable and variable in environment:
                value = environment[variable]
                if variable == 'ACC_ORCHESTRATOR':
                    return value == '1'
                return (value if variable == feature.env else value.lower()) in ('1', 'true', 'yes')
        explicit = getattr(self.settings, 'feature_overrides', {})
        if key in explicit:
            return explicit[key]
        legacy = getattr(self.settings, '_feature_config', {})
        if feature.config and (key in legacy or getattr(self.settings, feature.config) != feature.default):
            return bool(getattr(self.settings, feature.config))
        return None

    def locked(self, key):
        return self.configured(key) is not None

    def enabled(self, key):
        configured = self.configured(key)
        return configured if configured is not None else self.choices.get(key, FEATURES[key].default)

    def load(self, db):
        row = db.get(Setting, KEY)
        value = row.value if row else {}
        # Missing keys inherit this release's defaults; only saved owner choices
        # override them. Do not backfill defaults: explicit False must survive upgrades.
        self.choices = {k: v for k, v in value.get('choices', {}).items() if k in FEATURES and type(v) is bool}
        self.revision = value.get('revision', 0)

    def rows(self):
        return [dict(id=key, label=f.label, description=f.description, enabled=self.enabled(key),
                     preview=f.preview, sensitive=f.sensitive, locked=self.locked(key)) for key, f in FEATURES.items()]


class RuntimeSettings(Settings):
    """Settings-compatible view of availability; qualification remains on the original.

    Keep dataclasses.replace working for isolated callers, without mutating an
    application's shared snapshot when a caller derives different settings.
    """
    def __init__(self, original=None, features=None, **values):
        if original is None:
            previous = features
            original = Settings(**values)
            if previous is not None:
                explicit = dict(previous.settings._feature_config)
                for key, feature in FEATURES.items():
                    if not feature.config:
                        continue
                    if values[feature.config] != previous.enabled(key):
                        explicit[key] = values[feature.config]
                    else:
                        object.__setattr__(original, feature.config, getattr(previous.settings, feature.config))
                object.__setattr__(original, '_feature_config', explicit)
                object.__setattr__(original, '_feature_previous', {key: bool(getattr(original, f.config)) for key, f in FEATURES.items() if f.config})
                features = Features(original, previous.choices)
                features.revision = previous.revision
            else:
                features = Features(original)
        object.__setattr__(self, 'original', original)
        object.__setattr__(self, 'features', features)

    def __getattribute__(self, name):
        if name in ('original', 'features') or name.startswith('__'):
            return object.__getattribute__(self, name)
        # A value set directly on this view (object.__setattr__, as on a frozen Settings) wins, as it did before the view
        # existed; nothing in accd does this at runtime.
        own = object.__getattribute__(self, '__dict__')
        if name in own:
            return own[name]
        original = object.__getattribute__(self, 'original')
        features = object.__getattribute__(self, 'features')
        if key := CONFIG_FEATURES.get(name):
            return features.enabled(key)
        # Properties and methods see this view, so they read availability and any value set on it.
        from inspect import getattr_static, isfunction
        defined = getattr_static(type(original), name, None)
        if isinstance(defined, property):
            return defined.fget(self)
        if isfunction(defined):
            return defined.__get__(self)
        return getattr(original, name)


def enabled(settings, key):
    features = getattr(settings, 'features', None)
    return features.enabled(key) if features else Features(settings).enabled(key)


def current(key, fallback=True):
    features = CURRENT.get()
    return features.enabled(key) if features else fallback


def require(settings, key):
    from fastapi import HTTPException
    if not enabled(settings, key):
        raise HTTPException(409, f'{FEATURES[key].label} is turned off in Admin features.')


def launch_refusal(settings, *, automation=None, orchestrator=None, schedule=False, multirun=False, group=False):
    checks = [('automation', bool(automation and (automation.get('autoresume') or automation.get('goal')))),
              ('teams', bool(orchestrator)), ('schedules', schedule), ('multirun', multirun), ('groups', group)]
    for key, used in checks:
        if used and not enabled(settings, key):
            return f'{FEATURES[key].label} is turned off in Admin features.'
    return None


def path_feature(path):
    """All optional transports, including alternate resource routes, share this boundary."""
    import re
    patterns = {
        'multirun': r'/api/multiruns(?:/|$)',
        'schedules': r'/api/(?:schedules(?:/|$)|projects/[^/]+/schedules(?:/|$))',
        'teams': r'/api/sessions/[^/]+/team$',
        'automation': r'/api/sessions/[^/]+/automation$',
        'live_preview': r'/api/(?:projects/[^/]+/preview(?:/|$)|preview(?:/|$)|sessions/[^/]+/preview-element(?:/|$))',
        'publishing': r'/api/publish(?:/|$)',
        'issue_sources': r'/api/(?:issue-servers|projects/[^/]+/issue-sources)(?:/|$)',
        # Sign-in links (/api/auth/oidc) keep their own redirect to the login page saying company sign-in is off.
        'organizations': r'/api/organizations(?:/|$)',
        # Off still lets people revoke what groups granted (key grants, machines, placements).
        'groups': r'/api/(?:groups|group-machines)(?:/|$)(?!.*/revoke$)',
        'orchestrator': r'/api/(?:teams|gates|deliveries|settings/orchestrator|project-brief-references|projects/[^/]+/brief|providers/image-check)(?:/|$)',
        'remote_access': r'/api/(?:remote-access|devices/pairings|device-pairing)(?:/|$)',
        'telemetry': r'/api/telemetry(?!/delete$)(?:/|$)',  # Off never blocks deleting data already sent
        'mobile': r'/api/mobile(?:/|$)',
        'local_models': r'/api/endpoints(?:/|$)',
        'notifications': r'/api/push(?:/|$)',
        'explanations': r'/api/sessions/[^/]+/explanation(?:/|$)',
        'risk_labels': r'/api/preferences/risk-labels$',
    }
    return next((key for key, pattern in patterns.items() if re.match(pattern, path)), None)


class FeatureBoundary:
    def __init__(self, app, ctx):
        self.app, self.ctx = app, ctx

    async def __call__(self, scope, receive, send):
        token = CURRENT.set(self.ctx.settings.features)
        try:
            key = path_feature(scope.get('path', ''))
            allowed = key is None or enabled(self.ctx.settings, key)
            if key == 'orchestrator':
                from .orchestrator import planning, worklist
                path = scope.get('path', '')
                if '/worklist' in path:
                    allowed = allowed and worklist.enabled()
                if '/plan' in path:
                    allowed = allowed and planning.enabled()
            if key and not allowed:
                if scope['type'] == 'websocket':
                    await send({'type': 'websocket.close', 'code': 1008, 'reason': f'{FEATURES[key].label} is turned off in Admin features.'})
                elif scope['type'] == 'http':
                    from starlette.responses import JSONResponse
                    await JSONResponse({'detail': f'{FEATURES[key].label} is turned off in Admin features.'}, status_code=404)(scope, receive, send)
                return
            await self.app(scope, receive, send)
        finally:
            CURRENT.reset(token)
