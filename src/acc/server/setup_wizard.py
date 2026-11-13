"""Person-owned checklist data, never readiness, policy or reusable authority."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Step = Literal['recovery', 'level', 'security', 'machine', 'provider', 'project', 'backups', 'updates', 'features', 'finish']
State = Literal['pending', 'completed', 'deferred']


class Binding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    machine: str = Field(min_length=1, max_length=200)
    account: str = Field(min_length=1, max_length=200)
    machine_id: str | None = Field(default=None, min_length=1, max_length=200)


class ProgressIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0, strict=True)
    step: Step
    state: State
    binding: Binding | None = None
    project_id: int | None = Field(default=None, ge=1, strict=True)


def initial(*, fresh=False):
    return {'recovery_required': fresh, 'version': 1, 'preset_version': 1, 'revision': 0, 'steps': {},
            'binding': None, 'project_id': None, 'updated_at': None}
