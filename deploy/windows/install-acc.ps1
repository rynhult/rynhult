<#
.SYNOPSIS
Install Rynhult on a Windows 11 PC, inside WSL2 (Ubuntu 24.04 with systemd): the main Rynhult, or an additional machine for
a Rynhult you already have. Rynhult Setup (deploy/windows/acc.iss) runs this; it also works alone.

.DESCRIPTION
Run from an elevated PowerShell in a copy of Rynhult:
    powershell -ExecutionPolicy Bypass -File deploy\windows\install-acc.ps1 -Role Main -Owner alex -DryRun
    powershell -ExecutionPolicy Bypass -File deploy\windows\install-acc.ps1 -Role Node -EnrollCommand '<the command>'
Main: mirrored networking in %UserProfile%\.wslconfig (so phones and other PCs reach Rynhult), the owner's Linux account,
then deploy/setup.sh --yes inside WSL with the answers in its environment. Additional machine (Node): the node install
inside WSL, then acc-node enroll with the values from the command Host > Machines > Add machine shows. That command
is parsed, never run as given. Both: a logon task keeps WSL running while you are signed in.
If Windows must restart to finish installing WSL, the script arranges to continue after you sign in again (RunOnce)
and exits with 3010. -DryRun prints every step and changes nothing. Idempotent: run it again to update Rynhult.
Answers can also come from the environment, as deploy/setup.sh takes them: ACC_HOSTNAME, ACC_OWNER, ACC_AGENT_CLIS,
ACC_ZONE, CF_API_TOKEN (the Cloudflare token is only ever read from there), and ACC_ENROLL_COMMAND. Rynhult Setup
passes everything a person typed that way, never on a command line.
Exit codes: 0 done, 1 failed or rejected, 3010 Windows must restart (setup continues after the next sign-in).

.PARAMETER Role
Main (the main Rynhult) or Node (an additional machine).

.PARAMETER Hostname
Main: the name people open Rynhult at, e.g. rynhult.example.lan (default: what deploy/setup.sh picks).

.PARAMETER Owner
Main: the Linux user in WSL who owns Rynhult's projects (default: your Windows user name). Created when missing.

.PARAMETER AgentClis
Main: agent CLIs besides Claude Code, comma- or space-separated, from codex, copilot, antigravity, cursor, opencode; none for
none (default: codex).

.PARAMETER CloudflareZone
Main: a Cloudflare DNS zone for HTTPS and published sites (optional; needs CF_API_TOKEN in the environment).

.PARAMETER EnrollCommand
Node: the command from Host > Machines > Add machine.

.PARAMETER Agent
Node: the Linux account sessions run as inside WSL (default: <your Windows user name>-agent).

.PARAMETER Validate
Only check the answers: prints the problems and exits 1, or prints ok. Changes nothing (the wizard uses it).

.PARAMETER Uninstall
Remove the logon task and Rynhult inside WSL. Its data and the WSL distribution stay unless -RemoveDistro.

.PARAMETER RemoveDistro
With -Uninstall: also delete the WSL distribution and everything in it.

.PARAMETER Resume
Continue after a restart, with the answers saved before it (the RunOnce entry passes this).
#>
[CmdletBinding()]
param(
    [ValidateSet('Main', 'Node')][string]$Role = 'Main',
    [string]$Hostname = '',
    [string]$Owner = '',
    [string]$AgentClis = '',
    [string]$CloudflareZone = '',
    [string]$EnrollCommand = '',
    [string]$Agent = '',
    [string]$Distro = 'Ubuntu-24.04',
    [string]$LogPath = '',
    [switch]$AllowBundledSource,
    [switch]$DryRun,
    [switch]$Validate,
    [switch]$OpenLog,
    [switch]$Uninstall,
    [switch]$RemoveDistro,
    [switch]$Resume
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'  # wsl.exe prints UTF-16 otherwise
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch { Write-Verbose 'no console to set' }
Import-Module (Join-Path $PSScriptRoot 'AccWindows.psm1') -Force

$MainTask = 'ACC (WSL)'
$NodeTask = 'Rynhult node (WSL)'  # also deploy/node/windows/install.ps1 and Add machine's block (api/nodes.py)
$RunOnceKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce'
$RunOnceName = 'ACC Setup'
$ProtectedRoot = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'ACC'
$StatePath = Join-Path $ProtectedRoot 'private\resume.json'
$RestartExit = 3010
# Windows PowerShell, also when this runs in PowerShell 7: it is on every Windows 11.
$WindowsPowerShell = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
if (-not $LogPath) { $LogPath = Join-Path $ProtectedRoot 'logs\install-acc.log' }

if ($OpenLog) { Open-AccLog $LogPath; exit 0 }

# ------------------------------------------------------------------ continuing after a restart

# The answers, for the run after the restart. The Cloudflare token is encrypted for this Windows user (DPAPI).
function Save-ResumeState([hashtable]$State, [string]$Token) {
    if ($Token) {
        Add-Type -AssemblyName System.Security
        $bytes = [Text.Encoding]::UTF8.GetBytes($Token)
        $sealed = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, 'CurrentUser')
        $State['Token'] = [Convert]::ToBase64String($sealed)
    }
    Assert-AccProtectedPath $StatePath
    $folder = Split-Path -Parent $StatePath
    Initialize-AccPrivateDirectory $folder
    ($State | ConvertTo-Json) | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

function Read-ResumeState {
    Assert-AccProtectedPath $StatePath
    if (-not (Test-Path -LiteralPath $StatePath)) { throw "nothing to resume: $StatePath is missing (run Rynhult Setup again)" }
    $saved = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $state = @{}
    foreach ($property in $saved.PSObject.Properties) { $state[$property.Name] = [string]$property.Value }
    if ($state.ContainsKey('Token') -and $state['Token']) {
        Add-Type -AssemblyName System.Security
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($state['Token']), $null, 'CurrentUser')
        $state['Token'] = [Text.Encoding]::UTF8.GetString($bytes)
    }
    return $state
}

function Clear-ResumeState {
    if (Test-Path -LiteralPath $StatePath) {
        Invoke-AccChange "delete $StatePath" { Remove-Item -LiteralPath $StatePath -Force }
    }
    if (Get-ItemProperty -Path $RunOnceKey -Name $RunOnceName -ErrorAction SilentlyContinue) {
        Invoke-AccChange "delete RunOnce '$RunOnceName'" { Remove-ItemProperty -Path $RunOnceKey -Name $RunOnceName }
    }
}

# ------------------------------------------------------------------ answers

if ($Resume) {
    # RunOnce starts this without elevation: ask for it (the same Windows user, so the same WSL and answers).
    if (-not (Test-AccAdministrator)) {
        Assert-AccProtectedPath $PSCommandPath
        Assert-AccProtectedPath (Join-Path $PSScriptRoot 'AccWindows.psm1')
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Resume')
        Start-Process -FilePath $WindowsPowerShell -Verb RunAs -ArgumentList $arguments
        exit 0
    }
    $state = Read-ResumeState
    $Role = $state['Role']; $Hostname = $state['Hostname']; $Owner = $state['Owner']; $AgentClis = $state['AgentClis']
    $CloudflareZone = $state['Zone']; $EnrollCommand = $state['EnrollCommand']; $Agent = $state['Agent']
    $Distro = $state['Distro']; $LogPath = $state['LogPath']
    $AllowBundledSource = ([string]$state['AllowBundledSource'] -ceq 'True')
    if ($state.ContainsKey('Token')) { $env:CF_API_TOKEN = $state['Token'] }
}
if (-not $Hostname) { $Hostname = (Get-AccEnvironmentValue "ACC_HOSTNAME") }
if (-not $Owner) { $Owner = (Get-AccEnvironmentValue "ACC_OWNER") }
if (-not $AgentClis) { $AgentClis = (Get-AccEnvironmentValue "ACC_AGENT_CLIS") }
if (-not $CloudflareZone) { $CloudflareZone = (Get-AccEnvironmentValue "ACC_ZONE") }
if (-not $EnrollCommand) { $EnrollCommand = (Get-AccEnvironmentValue "ACC_ENROLL_COMMAND") }
$Hostname = $Hostname.Trim().ToLowerInvariant().TrimEnd('.')
$Owner = $Owner.Trim()
if (-not $Owner) { $Owner = Get-AccAccountBase }
if (-not $AgentClis.Trim()) { $AgentClis = 'codex' }  # setup.sh's default
$CloudflareZone = $CloudflareZone.Trim().ToLowerInvariant().TrimEnd('.')
$token = "$env:CF_API_TOKEN".Trim()
if (-not $Agent) { $Agent = "$(Get-AccAccountBase)-agent" }
if ($Owner -eq $Agent) { throw 'The owner and agent accounts must be different' }

$enroll = $null
$problems = @()
if ($Uninstall) {
    # nothing to check
} elseif ($Role -eq 'Main') {
    $problems = Test-AccMainSetting -Hostname $Hostname -Owner $Owner -AgentClis $AgentClis -Zone $CloudflareZone -Token $token
} else {
    try {
        $enroll = ConvertFrom-AccEnrollCommand $EnrollCommand
    } catch {
        $problems = @("rejected: $($_.Exception.Message)")
    }
    if ($Agent -cnotmatch '^[a-z_][a-z0-9_-]{0,31}$') { $problems += "bad agent account name: $Agent" }
}

if ($Validate) {
    if ($problems) {
        Initialize-AccRun -Distro $Distro -LogPath $LogPath
        $problems | ForEach-Object { Write-AccLog "error: $_" }
        Write-AccFailureContext
        exit 1
    }
    Write-Output 'ok'
    exit 0
}

Initialize-AccRun -Distro $Distro -LogPath $LogPath -DryRun:$DryRun
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Write-AccLog ''
Write-AccLog "==== Rynhult Setup $(Get-Date -Format s): $(if ($Uninstall) { 'uninstall' } else { $Role }) ($repo)"
if ($DryRun) { Write-AccLog '(dry run: nothing is changed)' }

function Exit-Setup([string]$Result, [int]$Code) {
    if ((Test-AccDryRun) -and (Get-AccProblemCount) -gt 0) {
        Write-AccLog "dry run: $(Get-AccProblemCount) problem(s) above would stop a real install"
    }
    Write-AccLog "== result: $Result$(if (Test-AccDryRun) { ' (dry run)' })"
    if ($Code -eq 1) { Write-AccFailureContext }
    if ($Resume -and [Environment]::UserInteractive) { Read-Host 'Press Enter to close this window' | Out-Null }
    exit $Code
}

if ($problems) {
    $problems | ForEach-Object { Write-AccLog "error: $_" }
    Exit-Setup 'failed' 1
}

# ------------------------------------------------------------------ uninstall

if ($Uninstall) {
    try {
        Write-AccStep 'logon tasks'
        $null = Get-AccOwnedNodeKeepAliveTask -TaskName $NodeTask
        $null = Get-AccOwnedNodeKeepAliveTask -TaskName 'ACC node (WSL)'
        Unregister-AccKeepAliveTask -TaskName $MainTask
        Unregister-AccNodeKeepAliveTask -TaskName $NodeTask
        Remove-AccLegacyNodeTask
        Clear-ResumeState
        Write-AccStep "Rynhult inside $Distro"
        if (Test-AccWslDistro) {
            $arguments = @('--', 'sh', 'deploy/node/windows/wsl-node.sh', 'uninstall')
            if ($DryRun) { $arguments += '--dry-run' }  # lists what it would remove
            $code = Invoke-AccWsl -ReadOnly:$DryRun -WorkingDirectory $repo -Arguments $arguments
            if ($code -ne 0) { Write-AccLog "  removing Rynhult inside WSL reported a problem (exit $code)" }
        } else {
            Write-AccOk "skipped: $Distro is not installed"
        }
        Unregister-AccWslFirewallRule
        if ($RemoveDistro -and (Test-AccWslDistro)) {
            Invoke-AccChange "wsl.exe --unregister $Distro (deletes the distribution and everything in it)" {
                $null = Invoke-AccNative -FilePath wsl.exe -Arguments @('--unregister', $Distro)
            }
        } else {
            Write-AccLog "  kept the $Distro distribution and its data (wsl --unregister $Distro deletes it)"
        }
        Write-AccLog "  kept %UserProfile%\.wslconfig (networkingMode=mirrored may be set there)"
    } catch {
        Write-AccLog "error: $($_.Exception.Message)"
        Exit-Setup 'failed' 1
    }
    Exit-Setup 'ok' 0
}

# ------------------------------------------------------------------ install

try {
    Write-AccStep 'prerequisites'
    # Mirrored networking needs Windows 11 22H2.
    Test-AccPrerequisite -MinimumBuild $(if ($Role -eq 'Main') { 22621 } else { 22000 })

    Write-AccStep "WSL2 with $Distro"
    if ((Install-AccWslDistro -AfterRestart:$Resume) -eq 'restart') {
        Assert-AccProtectedPath $PSCommandPath
        Assert-AccProtectedPath (Join-Path $PSScriptRoot 'AccWindows.psm1')
        $command = "`"$WindowsPowerShell`" -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Resume"
        Invoke-AccChange "save the answers to $StatePath and set RunOnce '$RunOnceName' (setup continues after the next sign-in)" {
            Save-ResumeState -State @{
                Role = $Role; Hostname = $Hostname; Owner = $Owner; AgentClis = $AgentClis; Zone = $CloudflareZone
                EnrollCommand = $EnrollCommand; Agent = $Agent; Distro = $Distro; LogPath = $LogPath
                AllowBundledSource = [bool]$AllowBundledSource
            } -Token $token
            New-Item -Path $RunOnceKey -Force | Out-Null
            New-ItemProperty -Path $RunOnceKey -Name $RunOnceName -Value $command -PropertyType String -Force | Out-Null
        }
        Write-AccLog '  Restart Windows and sign in again: setup continues by itself.'
        if ($Role -eq 'Node') {
            Write-AccLog '  The code in the enroll command works for 10 minutes: if it has expired by then, the install still'
            Write-AccLog '  finishes and tells you how to enroll with a new command.'
        }
        Exit-Setup 'restart needed' $RestartExit
    }

    if ($Role -eq 'Main') {
        Write-AccStep 'mirrored networking (other devices reach Rynhult at this PC''s address)'
        if (Enable-AccMirroredNetworking) {
            Invoke-AccChange 'wsl.exe --shutdown (WSL starts again with the new networking)' {
                $null = Invoke-AccNative -FilePath wsl.exe -Arguments @('--shutdown')
            }
        }
        Register-AccWslFirewallRule
    }

    Write-AccStep 'systemd in WSL'
    Enable-AccWslSystemd -Repo $repo

    Write-AccStep "owner account $Owner inside WSL"
        $code = Invoke-AccWsl -WorkingDirectory $repo -Arguments @('--', 'sh', 'deploy/node/windows/wsl-node.sh', 'owner', $Owner)
        if ($code -ne 0) { throw "creating the owner account failed (exit $code)" }

    if ($Role -eq 'Main') {
        Write-AccStep 'Rynhult inside WSL (deploy/setup.sh --yes)'
        # The answers travel as environment variables (WSLENV), as setup.sh takes them: never on a command line.
        $answers = [ordered]@{ ACC_OWNER = $Owner; ACC_AGENT_CLIS = (ConvertTo-AccAgentCliList $AgentClis) }
        if (-not $answers['ACC_AGENT_CLIS']) { $answers['ACC_AGENT_CLIS'] = ' ' }  # none: setup.sh keeps a set value
        if ($Hostname) { $answers['ACC_HOSTNAME'] = $Hostname }
        if ($CloudflareZone) { $answers['ACC_ZONE'] = $CloudflareZone; $answers['CF_API_TOKEN'] = $token }
        foreach ($name in @($answers.Keys)) { Set-Item -Path "Env:$name" -Value $answers[$name] }
        $shared = @("$env:WSLENV" -split ':' | Where-Object { $_ -and $answers.Keys -notcontains ($_ -replace '/.*$', '') })
        $env:WSLENV = (($shared + @($answers.Keys)) -join ':')
        $shown = $answers.Keys | ForEach-Object { if ($_ -eq 'CF_API_TOKEN') { "$_=(hidden)" } else { "$_=$($answers[$_])" } }
        Write-AccLog "  with $($shown -join ' ') (WSLENV)"
        $code = Invoke-AccWsl -WorkingDirectory $repo -Arguments @('--', 'sh', 'deploy/node/windows/wsl-node.sh', 'main')
        if ($code -ne 0) { throw "deploy/setup.sh failed inside $Distro (exit $code)" }
        $task = $MainTask
    } else {
        Write-AccStep 'Rynhult node inside WSL (deploy/node/install.sh)'
        Install-AccNodeInWsl -Repo $repo -Agent $Agent -Enroll $enroll -AllowBundledSource:$AllowBundledSource
        $task = $NodeTask
    }

    # Before enrolling: should the code have expired, only the enroll is left to do.
    Write-AccStep "logon task '$task' keeps WSL running"
    if ($Role -eq 'Node') { Register-AccNodeKeepAliveTask -TaskName $task }
    else { Register-AccKeepAliveTask -TaskName $task }
    Clear-ResumeState

    # An update: the node is enrolled already, and the code in the command is spent.
    $enrolled = ($Role -eq 'Node') -and (Test-AccWslDistro) -and
        ((Get-AccWslOutput @('--exec', 'sh', '-c', '. /usr/local/bin/acc-env.sh; acc_setting_file ACC_NODE_ID /etc/acc-node.env')) -ne '')
    if ($enrolled) {
        Write-AccStep 'enroll'
        Write-AccOk 'already enrolled (/etc/acc-node.env in WSL): not enrolling again'
    } elseif ($Role -eq 'Node') {
        Write-AccStep "enroll with $($enroll.Primary) as $($enroll.Name)"
        # Each value checked by ConvertFrom-AccEnrollCommand, passed as its own argument, without a shell.
        $code = Invoke-AccWsl -Arguments (@('--exec', '/usr/local/bin/acc-node') + (ConvertTo-AccEnrollArgument $enroll))
        if ($code -ne 0) {
            Write-AccLog "  enrolling failed (exit $code). If the code expired (it works for 10 minutes), make a new one:"
            Write-AccLog '  Host > Machines > Add machine, then run its command here, in an elevated PowerShell:'
            Write-AccLog "    wsl -d $Distro -u root -- <the command>"
            throw 'enrolling failed'
        }
    }

    Write-AccStep 'done'
    if ($Role -eq 'Main') {
        $url = if ($Hostname) { "https://$Hostname" } else { 'the address setup.sh printed above' }
        Write-AccLog "   Open $url and set up the first user with this one-time token:"
        Write-AccLog "     wsl -d $Distro -u root -- cat /srv/acc/data/secrets/setup_token"
    } else {
        Write-AccLog '   This PC is enrolled: it shows on Host > Machines on your Rynhult.'
    }
} catch {
    Write-AccLog "error: $($_.Exception.Message)"
    Exit-Setup 'failed' 1
}
Exit-Setup 'ok' 0
