<#
.SYNOPSIS
Runs ACC-Setup.exe silently as a dry run (/DRYRUN) for both roles, then its uninstaller, and checks the logs and that
nothing on the PC changed (no logon task, no RunOnce entry, .wslconfig as it was). CI runs this on the GitHub
Windows runner after building the installer:
    pwsh -File deploy/windows/tests/Invoke-SetupDryRun.ps1 -Setup deploy\windows\Output\Rynhult-Setup-<version>.exe
Exits 1 when a check fails; the logs are left in -LogDirectory.
#>
param(
    [Parameter(Mandatory = $true)][string]$Setup,
    [string]$LogDirectory = (Join-Path ([IO.Path]::GetTempPath()) 'acc-setup-logs'),
    [string]$InstallDirectory = (Join-Path ([IO.Path]::GetTempPath()) 'acc-setup-dry-run')
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:failures = 0
$setupExe = (Resolve-Path -LiteralPath $Setup).Path
$scriptLog = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'ACC\logs\install-acc.log'
$uninstallLog = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'ACC\logs\uninstall.log'
$wslConfig = Join-Path $env:USERPROFILE '.wslconfig'
$token = 'dry-run-token-0123456789abcdef'
$fingerprint = '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08'
$code = '3fa21c-0b9d4e-77aa01-c4d5e6'
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null

function Assert-That([bool]$Condition, [string]$What) {
    if ($Condition) {
        Write-Output "ok: $What"
    } else {
        $script:failures++
        Write-Output "FAIL: $What"
    }
}

function Get-FileText([string]$Path) {
    if (Test-Path -LiteralPath $Path) { return [IO.File]::ReadAllText($Path) }
    return ''
}

function Get-PcState {
    return [pscustomobject]@{
        WslConfig = Get-FileText $wslConfig
        Tasks     = @(Get-ScheduledTask -TaskName 'ACC*' -ErrorAction SilentlyContinue).Count
        RunOnce   = [bool](Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'ACC Setup' -ErrorAction SilentlyContinue)
        Rule      = [bool](Get-Command Get-NetFirewallHyperVRule -ErrorAction SilentlyContinue) -and
                    [bool](Get-NetFirewallHyperVRule -Name 'ACC-WSL-Web' -ErrorAction SilentlyContinue)
    }
}

# One silent run; returns (setup log, install-acc.ps1 log).
function Invoke-Setup([string]$Name, [string[]]$Options) {
    Remove-Item -LiteralPath $scriptLog -ErrorAction SilentlyContinue
    $setupLog = Join-Path $LogDirectory "setup-$Name.log"
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/DRYRUN', "/DIR=`"$InstallDirectory`"",
        "/LOG=`"$setupLog`"") + $Options
    # Not Write-Output: what this function writes to the pipeline is its result.
    Write-Information "== $Name" -InformationAction Continue
    $process = Start-Process -FilePath $setupExe -ArgumentList $arguments -Wait -PassThru
    Write-Information "   setup exit code $($process.ExitCode)" -InformationAction Continue
    Copy-Item -LiteralPath $scriptLog -Destination (Join-Path $LogDirectory "install-acc-$Name.log") -ErrorAction SilentlyContinue
    return @((Get-FileText $setupLog), (Get-FileText $scriptLog))
}

$before = Get-PcState

# ------------------------------------------------------------------ main ACC
$env:CF_API_TOKEN = ''
$setupLog, $log = Invoke-Setup 'main' @('/ROLE=main', '/HOSTNAME=acc.example.lan', '/OWNER=alex', '/CLIS=codex,cursor')
Assert-That ($log -match '\(dry run: nothing is changed\)') 'main: dry run announced'
Assert-That ($log -match '== mirrored networking') 'main: mirrored networking step'
Assert-That ($log -match 'networkingMode=mirrored') 'main: .wslconfig change shown'
Assert-That ($log -match 'wsl-node\.sh systemd') 'main: systemd step'
Assert-That ($log -match 'wsl-node\.sh owner alex') 'main: owner account'
Assert-That ($log -match 'wsl-node\.sh main') 'main: setup.sh inside WSL'
Assert-That ($log -match 'ACC_OWNER=alex ACC_AGENT_CLIS=codex cursor ACC_HOSTNAME=acc\.example\.lan') 'main: answers for setup.sh'
Assert-That ($log -match "Register-ScheduledTask 'Rynhult \(WSL\)'") 'main: logon task'
Assert-That ($log -match '== result: ok \(dry run\)') 'main: finished'
Assert-That ($setupLog -match 'install-acc\.ps1 exit code: 0') 'main: setup saw exit 0'
Assert-That (Test-Path -LiteralPath (Join-Path $InstallDirectory 'src\deploy\windows\install-acc.ps1')) 'files: the scripts are installed'
Assert-That (Test-Path -LiteralPath (Join-Path $InstallDirectory 'src\pyproject.toml')) 'files: the source tree is installed'
foreach ($left in '.git', '.venv', 'web\node_modules', 'data') {
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $InstallDirectory "src\$left"))) "files: no $left"
}

# ------------------------------------------------------------------ main ACC with a Cloudflare zone: the token stays secret
$env:CF_API_TOKEN = $token
$setupLog, $log = Invoke-Setup 'main-zone' @('/ROLE=main', '/OWNER=alex', '/CLIS=none', '/ZONE=example.com')
$env:CF_API_TOKEN = ''
Assert-That ($log -match 'ACC_ZONE=example\.com CF_API_TOKEN=\(hidden\)') 'zone: the token is passed, hidden'
Assert-That ($log -match "ACC_AGENT_CLIS= ") 'zone: no agent CLIs besides Claude Code'
Assert-That (-not ($log.Contains($token) -or $setupLog.Contains($token))) 'zone: the token is in no log'
Assert-That ($log -match '== result: ok \(dry run\)') 'zone: finished'

# ------------------------------------------------------------------ additional machine
$enroll = "acc-node enroll --primary https://acc.example.com --ca-fingerprint $fingerprint --code $code --name lab-pc"
$env:ACC_ENROLL_COMMAND = $enroll
$setupLog, $log = Invoke-Setup 'node' @('/ROLE=node')
$env:ACC_ENROLL_COMMAND = ''
Assert-That (-not ($log.Contains($code) -or $setupLog.Contains($code))) 'node: the code is in no log'
Assert-That ($log -match 'wsl-node\.sh owner \S+') 'node: owner account'
Assert-That ($log -match 'wsl-node\.sh install-primary https://acc\.example\.com ca [a-f0-9]{64} --agent \S+-agent') 'node: install from the main Rynhult'
Assert-That ($log.IndexOf('wsl-node.sh owner ') -lt $log.IndexOf('wsl-node.sh install-primary ')) 'node: owner before agent' 
Assert-That ($log -match [regex]::Escape("--exec /usr/local/bin/acc-node enroll --primary https://acc.example.com --ca-fingerprint $fingerprint --code [redacted] --name lab-pc")) 'node: enroll with the parsed values'
Assert-That ($log -match "Register-ScheduledTask 'Rynhult node \(WSL\)'") 'node: logon task'
Assert-That ($log -notmatch '== mirrored networking') 'node: no mirrored networking'
Assert-That ($log -match '== result: ok \(dry run\)') 'node: finished'

# System trust still needs a positional placeholder: Windows PowerShell drops empty native arguments.
$env:ACC_ENROLL_COMMAND = "acc-node enroll --primary https://acc.example.com --trust system --code $code --name lab-pc"
$setupLog, $log = Invoke-Setup 'node-system-trust' @('/ROLE=node')
$env:ACC_ENROLL_COMMAND = ''
Assert-That ($log -match 'wsl-node\.sh install-primary https://acc\.example\.com system - --agent \S+-agent') 'node: system trust preserves argument positions'
Assert-That ($log -notmatch '--allow-bundled-source') 'node: no implicit offline consent'

$env:ACC_ENROLL_COMMAND = $enroll
$setupLog, $log = Invoke-Setup 'node-explicit-offline' @('/ROLE=node', '/ALLOWBUNDLEDNODESOURCE=1')
$env:ACC_ENROLL_COMMAND = ''
Assert-That ($log -match 'wsl-node\.sh install-primary https://acc\.example\.com ca [a-f0-9]{64} --allow-bundled-source --agent \S+-agent') 'node: explicit offline consent reaches WSL'
Assert-That (-not ($log.Contains($code) -or $setupLog.Contains($code))) 'node offline: the code is in no log'

# ------------------------------------------------------------------ additional machine, a command that is not the one
$env:ACC_ENROLL_COMMAND = $enroll + '; calc.exe'
$setupLog, $log = Invoke-Setup 'node-rejected' @('/ROLE=node')
$env:ACC_ENROLL_COMMAND = ''
Assert-That ($log -match 'error: rejected: not the command') 'rejected: says why'
Assert-That ($log -notmatch 'wsl-node\.sh install') 'rejected: installs nothing'
Assert-That ($log -match '== result: failed') 'rejected: failed'
Assert-That ($setupLog -match 'install-acc\.ps1 exit code: 1') 'rejected: setup saw exit 1'

# ------------------------------------------------------------------ uninstall
Remove-Item -LiteralPath $uninstallLog -ErrorAction SilentlyContinue
$uninstaller = Join-Path $InstallDirectory 'unins000.exe'
Assert-That (Test-Path -LiteralPath $uninstaller) 'uninstall: the uninstaller is there'
if (Test-Path -LiteralPath $uninstaller) {
    Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/DRYRUN',
        "/LOG=`"$(Join-Path $LogDirectory 'uninstall-setup.log')`"") -Wait
    # The uninstaller hands over to a copy of itself in %TEMP%: wait for its log.
    for ($i = 0; $i -lt 120 -and -not ((Get-FileText $uninstallLog) -match '== result:'); $i++) { Start-Sleep -Seconds 1 }
    $log = Get-FileText $uninstallLog
    Copy-Item -LiteralPath $uninstallLog -Destination (Join-Path $LogDirectory 'uninstall.log') -ErrorAction SilentlyContinue
    Assert-That ($log -match '\(dry run: nothing is changed\)') 'uninstall: dry run (/DRYRUN reached the uninstaller)'
    Assert-That ($log -match 'kept the Ubuntu-24\.04 distribution') 'uninstall: the distribution stays'
    Assert-That ($log -match '== result: ok') 'uninstall: finished'
}

# ------------------------------------------------------------------ nothing changed
$after = Get-PcState
Assert-That ($after.WslConfig -ceq $before.WslConfig) 'pc: .wslconfig unchanged'
Assert-That ($after.Tasks -eq $before.Tasks) 'pc: no logon task registered'
Assert-That ($after.RunOnce -eq $before.RunOnce) 'pc: no RunOnce entry'
Assert-That ($after.Rule -eq $before.Rule) 'pc: no firewall rule'

Write-Output "$script:failures check(s) failed; logs in $LogDirectory"
if ($script:failures -gt 0) { exit 1 }
