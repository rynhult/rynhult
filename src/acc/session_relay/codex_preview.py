"""Frozen Codex owner-preview support, independent of any session's grant."""
from types import MappingProxyType

from . import protocol as p
from .adapters.codex import ADAPTER_VERSION, NATIVE_SCHEMA

CLI_VERSION='0.167.1'
CLI_DIGEST='sha256:dd5bfdf19d794dcc6fb482d2d5a2d9fb31d11f6caa44b303bb72890d461e8a02'
QUALIFICATION_ID='rynhult-relay-codex-owner-preview-2026-11-16'
OPERATIONS=('text','terminal.input')
DECLARATION=p.CapabilityDeclaration('codex',CLI_VERSION,CLI_DIGEST,ADAPTER_VERSION,NATIVE_SCHEMA,'stdio',
    QUALIFICATION_ID,'2026-11-16',MappingProxyType({'placement':'local-or-node','actor':'install-owner'}),
    MappingProxyType({name:p.CapabilityEntry(p.Support.QUALIFIED,'Owner preview.',
        evidence=('docs/decisions/0074-relay-preview.md',
                  'docs/handoffs/2026-11-16-relay-codex-preview-native-20.json')) for name in OPERATIONS}))
