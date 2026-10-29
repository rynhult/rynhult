"""Execute task migration with Scheduler/identity fakes, never register a task. Also runnable on Windows CI."""
import shutil
import subprocess
from pathlib import Path

import pytest

SHELLS = list(dict.fromkeys(p for name in ('pwsh', 'powershell.exe') if (p := shutil.which(name))))
PS = None
MODULE = Path(__file__).resolve().parents[1] / 'deploy/windows/AccWindows.psm1'
pytestmark = pytest.mark.skipif(not SHELLS, reason='PowerShell unavailable; fake Scheduler migration needs pwsh or powershell.exe')


@pytest.fixture(params=SHELLS or [None], autouse=True)
def powershell_runtime(request, monkeypatch):
    monkeypatch.setitem(globals(), 'PS', request.param)

HARNESS = r'''
& (Get-Module AccWindows) {
    $ErrorActionPreference = 'Stop'
    $script:Distro = 'Ubuntu-24.04'
    $script:Calls = [System.Collections.Generic.List[string]]::new()
    $script:Tasks = @{}
    $script:FailStart = $false
    function script:Get-AccKeepAliveIdentity { return @{Sid='S-1-5-21-123';Name='EXAMPLE\Alex'} }
    function script:Get-AccSystemTool { param($Name) return "C:\Windows\System32\$Name" }
    function script:Write-AccOk { param($Text) }
    function script:Invoke-AccChange { param($Description, $Action) & $Action }
    function script:New-TestTask {
        param($Name, $Person='EXAMPLE\Alex', $Distro='Ubuntu-24.04')
        return [pscustomobject]@{ TaskName=$Name; TaskPath='\'; State='Running';
          Principal=[pscustomobject]@{UserId=$Person;LogonType='Interactive';RunLevel='Limited'};
          Actions=@([pscustomobject]@{Execute='C:\Windows\System32\conhost.exe';
            Arguments="--headless `"C:\Windows\System32\wsl.exe`" -d $Distro --exec /bin/sleep infinity"}) }
    }
    function script:Get-ScheduledTask {
        [CmdletBinding()] param($TaskName, $TaskPath)
        if ($TaskPath -ne '\') { throw 'unscoped scheduler lookup' }
        return $script:Tasks[$TaskName]
    }
    function script:Register-AccKeepAliveTask {
        param($TaskName)
        $script:Calls.Add("register:$TaskName")
        $script:Tasks[$TaskName] = New-TestTask $TaskName
        if ($script:FailStart) { throw 'simulated start failure' }
        $script:Calls.Add("start:$TaskName")
    }
    function script:Stop-ScheduledTask {
        [CmdletBinding()] param($InputObject)
        $script:Calls.Add("stop:$($InputObject.TaskName)")
    }
    function script:Unregister-ScheduledTask {
        [CmdletBinding(SupportsShouldProcess=$true)] param($InputObject)
        $script:Calls.Add("remove:$($InputObject.TaskName)")
        $script:Tasks.Remove($InputObject.TaskName)
    }
    $old = 'ACC node (WSL)'; $new = 'Rynhult node (WSL)'
    $script:Tasks[$old] = New-TestTask $old
    __CASE__
    Write-Output 'migration passed'
}
'''


def run(case):
    quoted = str(MODULE).replace("'", "''")
    return subprocess.run([PS, '-NoProfile', '-NonInteractive', '-Command',
                           f"Import-Module '{quoted}' -Force; " + HARNESS.replace('__CASE__', case)],
                          capture_output=True, text=True, timeout=30)


def test_replacement_starts_before_cleanup_and_upgrade_is_idempotent():
    result = run(r'''
        Register-AccNodeKeepAliveTask $new
        if (($script:Calls -join '|') -ne "register:$new|start:$new|stop:$old|remove:$old") { throw 'wrong order' }
        Register-AccNodeKeepAliveTask $new
        if ($script:Tasks.ContainsKey($old) -or -not $script:Tasks.ContainsKey($new)) { throw 'bad final tasks' }
        Unregister-AccNodeKeepAliveTask $new
        Remove-AccLegacyNodeTask
        if ($script:Tasks.Count -ne 0) { throw 'uninstall left tasks' }
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_failed_replacement_preserves_working_legacy_and_reports_leftovers():
    result = run(r'''
        $script:FailStart = $true
        $message = ''
        try { Register-AccNodeKeepAliveTask $new } catch { $message = $_.Exception.Message }
        if ($message -notlike '*legacy task retained*' -or $message -notlike '*may have been registered*') { throw 'no recovery message' }
        if (-not $script:Tasks.ContainsKey($old)) { throw 'working legacy task removed' }
        if (($script:Calls -join '|') -like '*stop:*') { throw 'stopped before replacement started' }
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('change', [
    "$script:Tasks[$old].Principal.UserId = 'EXAMPLE\\Blair'",
    "$script:Tasks[$old] = New-TestTask $old 'EXAMPLE\\Alex' 'Other-Distro'",
    "$script:Tasks[$old].Actions[0].Execute = 'C:\\other.exe'",
    "$script:Tasks[$new] = New-TestTask $new 'EXAMPLE\\Blair'",
])
def test_foreign_or_changed_tasks_are_left_untouched(change):
    result = run(change + r'''
        $refused = $false
        try { Register-AccNodeKeepAliveTask $new } catch { $refused = $true }
        if (-not $refused -or $script:Calls.Count -ne 0) { throw 'changed an unverified task' }
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_uninstall_refuses_foreign_legacy_and_custom_legacy_name_is_not_retired():
    result = run(r''' 
        $script:Tasks[$old].Principal.UserId = 'EXAMPLE\Blair'
        $refused = $false
        try { Remove-AccLegacyNodeTask } catch { $refused = $true }
        if (-not $refused -or $script:Calls.Count -ne 0) { throw 'removed foreign legacy task' }
        $script:Tasks[$old] = New-TestTask $old
        Register-AccNodeKeepAliveTask $old
        if (-not $script:Tasks.ContainsKey($old) -or ($script:Calls -join '|') -like '*remove:*') { throw 'retired requested custom task' }
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_queued_replacement_is_not_treated_as_running():
    result = run(r'''
        $script:Tasks[$new] = New-TestTask $new
        $script:Tasks[$new].State = 'Ready'
        $refused = $false
        try { Wait-AccNodeKeepAliveTask $new -TimeoutSeconds 0.01 } catch { $refused = $true }
        if (-not $refused) { throw 'non-running replacement accepted' }
        if (-not $script:Tasks.ContainsKey($old) -or $script:Calls.Count -ne 0) { throw 'legacy touched' }
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_original_installer_unquoted_system_path_is_migrated():
    result = run(r'''
        $script:Tasks[$old].Actions[0].Arguments = '--headless C:\Windows\System32\wsl.exe -d Ubuntu-24.04 --exec /bin/sleep infinity'
        Register-AccNodeKeepAliveTask $new
        if ($script:Tasks.ContainsKey($old) -or -not $script:Tasks.ContainsKey($new)) { throw 'original installer migration failed' }
    ''')
    assert result.returncode == 0, result.stdout + result.stderr
