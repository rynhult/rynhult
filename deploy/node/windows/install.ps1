<#
.SYNOPSIS
Install or update a Rynhult node on a Windows 11 PC: WSL2 Ubuntu with systemd, the Linux node install inside it, and a
logon task that keeps WSL running.

.DESCRIPTION
Run from an elevated PowerShell in a checkout of Rynhult:
    powershell -ExecutionPolicy Bypass -File deploy\node\windows\install.ps1
Idempotent: run it again after pulling an update. -WhatIf shows every step and changes nothing (inside an existing
WSL distribution it also runs the Linux install with --dry-run). If Windows has to restart to finish installing WSL,
the script says so: restart, then run it again.
Rynhult Setup (deploy/windows) does the same and enrolls the node too; both use deploy/windows/AccWindows.psm1.
Sessions run in WSL while you are signed in to Windows. See docs/MULTIHOST.md and docs/wiki/User-Guide/Nodes.md.

.PARAMETER Distro
The WSL distribution to use (installed when missing).

.PARAMETER Agent
The Linux account sessions run as inside WSL (default: <your Windows user name>-agent).

.PARAMETER TaskName
The Scheduled Task that keeps WSL running while you are signed in.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Distro = 'Ubuntu-24.04',
    [string]$Agent = '',
    [string]$TaskName = 'Rynhult node (WSL)'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'  # wsl.exe prints UTF-16 otherwise
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
Import-Module (Join-Path $repo 'deploy\windows\AccWindows.psm1') -Force

# -WhatIf (or No to -Confirm) walks through every step and changes nothing.
$dry = -not $PSCmdlet.ShouldProcess('this PC', 'Install or update the Rynhult node (WSL, systemd, node install, logon task)')
$logPath = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'ACC\logs\install-node.log'
Initialize-AccRun -Distro $Distro -LogPath $logPath -DryRun:$dry

try {

if (-not $Agent) { $Agent = "$(Get-AccAccountBase)-agent" }
if ($Agent -cnotmatch '^[a-z_][a-z0-9_-]{0,31}$') { throw "bad agent account name: $Agent" }
if ($dry) { Write-AccLog '(dry run: nothing is changed)' }

Write-AccStep 'prerequisites'
Test-AccPrerequisite

Write-AccStep "WSL2 with $Distro"
if ((Install-AccWslDistro) -eq 'restart') {
    Write-Warning 'WSL was just turned on: restart Windows, then run this script again to finish.'
    return
}

Write-AccStep 'systemd in WSL'
Enable-AccWslSystemd -Repo $repo

$Owner = Get-AccAccountBase
if ($Owner -eq $Agent) { throw 'The owner and agent accounts must be different' }
Write-AccStep "owner account $Owner inside WSL"
$code = Invoke-AccWsl -WorkingDirectory $repo -Arguments @('--', 'sh', 'deploy/node/windows/wsl-node.sh', 'owner', $Owner)
if ($code -ne 0) { throw "creating the owner account failed (exit $code)" }

Write-AccStep 'Rynhult node inside WSL (deploy/node/install.sh)'
Install-AccNodeInWsl -Repo $repo -Agent $Agent

Write-AccStep "logon task '$TaskName' keeps WSL running"
Register-AccNodeKeepAliveTask -TaskName $TaskName

Write-AccStep 'done'
Write-AccLog @"
   Next, on the dashboard: Host > Machines > Add machine, name this PC, then run the command it shows, inside WSL:
     wsl -d $Distro -u root -- acc-node enroll --primary https://<your Rynhult> --trust system --code <code> --name <name>
   It starts acc-node when it succeeds. (Rynhult Setup takes the command in its wizard and runs the enroll for you.)
"@
if ((Get-AccProblemCount) -gt 0) {
    Write-Warning "$(Get-AccProblemCount) problem(s) above would stop a real install"
}

} catch {
    Write-AccLog "error: $($_.Exception.Message)"
    Write-AccFailureContext
    exit 1
}
