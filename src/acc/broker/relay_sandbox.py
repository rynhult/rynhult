"""Refuse native write policies that intersect the broker's relay records."""
from pathlib import Path

from acc.session_relay import protocol as p


def check(value,cwd,private):
    if not isinstance(value,dict):raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    if value.get('type')=='readOnly':return
    if (value.get('type')!='workspaceWrite' or value.get('excludeSlashTmp') is not True
            or value.get('excludeTmpdirEnvVar') is not True or not isinstance(value.get('writableRoots'),list)):
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    try:
        if not all(isinstance(root,str) and Path(root).is_absolute() for root in value['writableRoots']):
            raise ValueError('Invalid native write roots')
        protected=Path(private).resolve()
        roots=[Path(cwd).resolve(),*[Path(root).resolve() for root in value['writableRoots']]]
        if any(root==protected or root in protected.parents or protected in root.parents for root in roots):
            raise ValueError('Native write roots intersect relay records')
    except (OSError,ValueError,TypeError):
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED) from None
