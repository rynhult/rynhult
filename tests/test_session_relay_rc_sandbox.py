import pytest

from acc.broker.relay_sandbox import check
from acc.session_relay import protocol as p


@pytest.mark.parametrize('root',['parent','exact','child','tmp','environment','full','missing'])
def test_unsafe_native_write_roots_refuse(root,tmp_path):
    private=tmp_path/'runs'/'relay';work=tmp_path/'work'
    policy={'type':'workspaceWrite','writableRoots':[],'excludeSlashTmp':True,'excludeTmpdirEnvVar':True}
    if root=='parent':policy['writableRoots']=[str(tmp_path)]
    if root=='exact':policy['writableRoots']=[str(private)]
    if root=='child':policy['writableRoots']=[str(private/'payloads')]
    if root=='tmp':policy['excludeSlashTmp']=False
    if root=='environment':policy['excludeTmpdirEnvVar']=False
    if root=='full':policy['type']='dangerFullAccess'
    if root=='missing':policy=None
    with pytest.raises(p.RelayError):check(policy,work,private)


def test_safe_native_write_roots_and_readonly(tmp_path):
    check({'type':'workspaceWrite','writableRoots':[str(tmp_path/'other')],
        'excludeSlashTmp':True,'excludeTmpdirEnvVar':True},tmp_path/'work',tmp_path/'runs'/'relay')
    check({'type':'readOnly'},tmp_path/'work',tmp_path/'runs'/'relay')
