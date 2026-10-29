"""Native Windows security contracts; Linux runs skip, never substitute text checks."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "deploy/windows/AccWindows.psm1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="requires Windows ACLs and PowerShell")


def ps(script):
    return subprocess.run([POWERSHELL, "-NoProfile", "-Command", f"Import-Module '{MODULE}' -Force; " + script],
                          capture_output=True, text=True, timeout=30)


def test_windows_redacts_credentials():
    result = ps("Protect-AccLogText 'Bearer sample-access-token token=sample-api-token https://alex:sample-password@example.com/repo'")
    assert result.returncode == 0
    assert "sample-" not in result.stdout
    assert "[redacted]" in result.stdout


def test_windows_refuses_user_writable_log(tmp_path):
    result = ps(f"try {{ Initialize-AccRun -LogPath '{tmp_path / 'install.log'}'; Write-AccLog 'unsafe'; exit 0 }} catch {{ exit 7 }}")
    assert result.returncode == 7
    assert not (tmp_path / "install.log").exists()


def test_windows_log_viewer_uses_a_fixed_system_executable():
    result = ps(r'''& (Get-Module AccWindows) {
        function script:Start-Process { param($FilePath, $ArgumentList) Write-Output $FilePath }
        Open-AccLog (Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'ACC-security-example.log')
    }''')
    assert result.returncode == 0
    assert result.stdout.strip().lower() == str(Path(os.environ["SystemRoot"]) / "System32/notepad.exe").lower()


def test_windows_offline_source_requires_explicit_switch_at_wsl_boundary():
    result = ps(r'''& (Get-Module AccWindows) {
        $ErrorActionPreference = 'Stop'
        $script:DryRun = $false
        $script:Helper = 'deploy/node/windows/wsl-node.sh'
        function script:Invoke-AccWsl {
            param([string[]]$Arguments, [string]$WorkingDirectory)
            $script:Captured = $Arguments
            return 0
        }
        $enroll = [pscustomobject]@{ Primary = 'https://acc.example.lan'; Trust = 'system' }
        Install-AccNodeInWsl -Repo 'C:\ACC' -Agent 'alex-agent' -Enroll $enroll
        if ($script:Captured -contains '--allow-bundled-source') { throw 'implicit offline consent' }
        Install-AccNodeInWsl -Repo 'C:\ACC' -Agent 'alex-agent' -Enroll $enroll -AllowBundledSource
        if ($script:Captured -notcontains '--allow-bundled-source') { throw 'offline consent not forwarded' }
        if (($script:Captured[-2..-1] -join '|') -ne '--agent|alex-agent') { throw 'agent args changed' }
        Write-Output 'offline consent boundary passed'
    }''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "offline consent boundary passed" in result.stdout


def test_windows_wsl_discovery_ignores_lookup_shadows():
    result = ps(r'''& (Get-Module AccWindows) {
        function script:wsl.exe { $global:LASTEXITCODE = 0; Write-Output 'unsafe-lookup-shadow' }
        Get-AccWslDistro
    }''')
    assert result.returncode == 0
    assert "unsafe-lookup-shadow" not in result.stdout
