"""The outer controller must detect leaks even when a worker cannot tear down."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from owned_gpg import agent_processes, new_home, stop_agents


def test_cleanup_stays_inside_registered_homes(tmp_path):
    home = new_home(tmp_path)
    try:
        subprocess.run(['gpg-connect-agent', '--homedir', str(home), 'GETINFO pid', '/bye'],
                       capture_output=True, check=True, timeout=10)
        assert agent_processes({str(home)})
        from owned_gpg import assert_no_gpg_agents
        assert_no_gpg_agents(tmp_path / 'unrelated')
        assert agent_processes({str(home)})
    finally:
        stop_agents(home)
    assert not agent_processes({str(home)})


@pytest.mark.parametrize('ending', ['leak', 'failure', 'crash'])
def test_controller_cleanup(tmp_path, ending):
    root = Path(__file__).resolve().parents[1]
    case = tmp_path / 'test_agent.py'
    case.write_text('''
import os
import subprocess
from owned_gpg import new_home

def test_agent(tmp_path_factory):
    home = new_home(tmp_path_factory.getbasetemp())
    subprocess.run(['gpg-connect-agent', '--homedir', str(home), 'GETINFO pid', '/bye'],
                   check=True, capture_output=True, timeout=10)
''' + {'leak': '', 'failure': '    assert False, "injected failure"\n',
       'crash': '    os._exit(23)\n'}[ending])
    base = tmp_path / 'nested'
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-n', '1', '--max-worker-restart=0',
         '-p', 'host_isolation', '-p', 'conftest', f'--basetemp={base}', str(case)],
        cwd=tmp_path, env=os.environ | {'PYTHONPATH': os.pathsep.join([str(root / 'tests'), str(root / 'src')])},
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'gpg-agent leak at session end' in result.stdout + result.stderr
    homes = {entry.read_text() for entry in base.glob('**/owned-gpg/*.home')}
    assert homes
    assert not agent_processes(homes)
    assert all(not Path(home).exists() for home in homes)
