"""No-account metadata proof; one writable tmp mount, no host settings."""
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path


def capture(tmp_path, mode):
    if mode not in ('bash','write','edit','symlink','hardlink','proc_root','proc_fd','run_read','run_write',
                    'write_symlink','edit_symlink','write_proc_root','edit_proc_fd'):
        raise ValueError('Unknown metadata probe')
    policy_file=Path(__file__).with_name('claude_metadata_policy.py')
    spec=importlib.util.spec_from_file_location('metadata_policy',policy_file)
    policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
    lab=Path(tmp_path)/'metadata-lab';lab.mkdir(mode=0o700)
    fixture=lab/'lab';fixture.mkdir(mode=0o700)
    (fixture/'home').mkdir(mode=0o700)
    (fixture/'run').mkdir(mode=0o700)
    (fixture/'run/frozen.json').write_text(json.dumps(policy.freeze({'disableAllHooks':False},'/tmp/lab/run')))
    (fixture/'mode.json').write_text(json.dumps(mode))
    (fixture/'driver.py').write_bytes(Path(__file__).with_name('metadata_security_fixture.py').read_bytes())
    cli_digest=hashlib.sha256(Path('/usr/bin/claude').read_bytes()).hexdigest()
    assert cli_digest=='356733f185fa988e6bf3aa9a555269fdd167982a2e2a80d3c8a78037bb92ad58'
    command=['bwrap','--unshare-all','--die-with-parent','--new-session','--cap-drop','ALL']
    for name in ('usr','bin','lib','lib64'):
        if Path('/'+name).exists():command+=['--ro-bind','/'+name,'/'+name]
    rg=Path(shutil.which('rg')).parent
    command+=['--proc','/proc','--dev','/dev','--remount-ro','/dev',
        '--bind',str(lab),'/tmp','--remount-ro','/', '--clearenv',
        '--setenv','HOME','/tmp/lab/home','--setenv','CLAUDE_CONFIG_DIR','/tmp/lab/home/.claude',
        '--setenv','PATH',f'/usr/bin:/bin:{rg}','--setenv','TMPDIR','/tmp/lab',
        '--chdir','/tmp/lab','--','/usr/bin/python3','/tmp/lab/driver.py']
    version=subprocess.run(command[:-2]+['/usr/bin/claude','--version'],env={'PATH':'/usr/bin:/bin'},
        capture_output=True,text=True,timeout=15,check=True).stdout.strip()
    assert version=='2.1.308 (Claude Code)'
    result=subprocess.run(command,env={'PATH':'/usr/bin:/bin'},capture_output=True,text=True,timeout=45)
    if result.returncode:raise RuntimeError(result.stderr[-8000:])
    proof=json.loads(result.stdout)
    proof.update(hostWritesConfinedTo=str(lab),cliVersion=version,cliSha256=cli_digest,
        productionEnforced=False)
    return proof
