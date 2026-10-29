#Requires -Version 5.1
# Safety sign-off: unelevated, read-only evidence and a fixed identification-only pipe probe; no credential access.
[CmdletBinding()]
param([ValidateRange(0, 300)][int] $WaitSeconds = 180, [switch] $Json)

. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-SpikePlatform
$plan = Read-SpikePlan
$config = Get-Content -LiteralPath (Join-Path $plan.root 'config.json') -Raw | ConvertFrom-Json
Assert-SpikeOwner $config.OwnerSid
Initialize-SpikeNative

function Read-ProbeEvidence([string] $Name) {
    $path = Join-Path (Join-Path $plan.root 'evidence') $Name
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    Assert-NoReparsePath $path
    $stream = [IO.File]::Open($path, 'Open', 'Read', ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
        if ($stream.Length -gt 1048576) { throw "Evidence exceeds one MiB: $Name" }
        $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8)
        try { return $reader.ReadToEnd() | ConvertFrom-Json }
        finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
}

function Test-RecentEvidence($Value) {
    if ($null -eq $Value -or $Value.installId -cne $config.InstallId) { return $false }
    $age = ([DateTime]::UtcNow - ([DateTime]$Value.utc).ToUniversalTime()).TotalSeconds
    return $age -ge -5 -and $age -le 120
}

$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
$pipeReply = $null
$pipeError = 'Service/evidence not ready.'
$servicePid = 0
do {
    $fatal = Read-ProbeEvidence 'fatal.json'
    if ($fatal) { $pipeError = 'Service reported: ' + $fatal.detail; break }
    $current = Read-ProbeEvidence 'current.json'
    $conpty = Read-ProbeEvidence 'conpty.json'
    $policy = Read-ProbeEvidence 'pipe-policy.json'
    $positive = Read-ProbeEvidence 'pipe-agent.json'
    if ($current -and $conpty -and $policy -and $positive) {
        try {
            $servicePid = [Rynhult.Spike.Native]::ServiceProcessId($plan.serviceName)
            $pipeReply = [Rynhult.Spike.Native]::QueryPipe($config.PipeName, $servicePid) | ConvertFrom-Json
            $pipeError = $null
            break
        } catch [System.Management.Automation.MethodInvocationException] {
            $pipeError = $_.Exception.InnerException.Message
        }
    }
    if ([DateTime]::UtcNow -ge $deadline) { break }
    Start-Sleep -Seconds 1
} while ($true)

$current = Read-ProbeEvidence 'current.json'
$conpty = Read-ProbeEvidence 'conpty.json'
$policy = Read-ProbeEvidence 'pipe-policy.json'
$positive = Read-ProbeEvidence 'pipe-agent.json'
$os = Get-CimInstance Win32_OperatingSystem
$osBoot = $os.LastBootUpTime.ToUniversalTime()
$boot = Read-ProbeEvidence ('boot-' + $osBoot.ToString('yyyyMMddTHHmmssZ') + '.json')
$live = -not $fatal -and (Test-RecentEvidence $current) -and $current.servicePid -eq $servicePid -and
    [Math]::Abs((([DateTime]$current.bootUtc).ToUniversalTime() - $osBoot).TotalSeconds) -lt 2
$conptyPassed = (Test-RecentEvidence $conpty) -and $conpty.passed -eq $true -and
    $conpty.childAlive -eq $true -and $conpty.observedSeconds -ge 10
$bootStatus = 'FAIL'
$bootDetail = 'Missing/stale evidence, session enumeration failed, a user was signed in, or ConPTY failed.'
if ($osBoot -le ([DateTime]$config.InstallUtc).ToUniversalTime()) {
    $bootStatus = 'NOT-RUN'
    $bootDetail = 'Reboot after installation; wait at the sign-in screen. Current ConPTY pass: ' + $conptyPassed
} elseif ($live -and $conptyPassed -and $boot -and $boot.installId -ceq $config.InstallId -and
          $boot.sessionQuerySucceeded -eq $true -and $null -ne $boot.sessions -and -not $boot.sessionQueryError -and
          [Math]::Abs((([DateTime]$boot.bootUtc).ToUniversalTime() - $osBoot).TotalSeconds) -lt 2 -and
          $boot.observedSeconds -ge 10 -and $boot.sessionId -eq 0 -and $boot.conptyPassed -eq $true -and
          @($boot.sessions | Where-Object hasUser -EQ $true).Count -eq 0) {
    $bootStatus = 'PASS'
    $bootDetail = 'This boot: Session 0 ConPTY captured echo/ping and was alive with no signed-in interactive users.'
}
$identityPassed = $live -and $current.agentSid -ceq $config.AgentSid -and $current.agentSid -cne $config.OwnerSid -and
    $current.isAdmin -eq $false -and $current.sessionId -eq 0 -and $current.profileHiveLoaded -eq $true -and
    $current.profilePath -eq $config.ProfilePath -and $current.profileEnvironment -eq $config.ProfilePath

# Gate 2 also bounds the host token's privileges (docs/WINDOWS-NATIVE.md, finding 3). Disabled is no boundary: a process
# can enable its own privileges, so a privilege fails the gate by being present, enabled or not.
$forbiddenPrivileges = @('SeImpersonatePrivilege', 'SeCreateGlobalPrivilege')  # from the SERVICE group; never allowed
$allowedPrivileges = @(
    'SeChangeNotifyPrivilege'        # Bypass traverse checking: the one required privilege; every token has it.
    'SeShutdownPrivilege'            # Users' default on client Windows: shut down or restart; no data or identity.
    'SeUndockPrivilege'              # Users' default: undock a laptop; hardware only.
    'SeIncreaseWorkingSetPrivilege'  # Users' default: grow the process's own working set; memory only.
    'SeTimeZonePrivilege'            # Users' default: the display time zone; the clock stays out (SeSystemtimePrivilege).
)
$heldPrivileges = @()
if ($live -and $current.PSObject.Properties['tokenPrivileges']) {
    $heldPrivileges = @($current.tokenPrivileges | ForEach-Object { [string]$_.name })
}
$forbiddenHeld = @($heldPrivileges | Where-Object { $_ -cin $forbiddenPrivileges })
$unexpectedHeld = @($heldPrivileges | Where-Object { $_ -cnotin $allowedPrivileges })
$configuredPrivileges = $null
try { $configuredPrivileges = @([Rynhult.Spike.Native]::RequiredPrivileges($plan.serviceName)) }
catch [System.Management.Automation.MethodInvocationException] { $configuredPrivileges = $null }
$configuredPassed = $null -ne $configuredPrivileges -and
    ($configuredPrivileges -join ',') -ceq (@($plan.serviceRequiredPrivileges) -join ',')
$privilegesPassed = $heldPrivileges.Count -gt 0 -and $forbiddenHeld.Count -eq 0 -and $unexpectedHeld.Count -eq 0 -and
    $configuredPassed
$identityDetail = 'Separate standard SID in Session 0 with its expected profile/hive/environment; token privileges: ' +
    ($heldPrivileges -join ', ') + '.'
if (-not $identityPassed) {
    $identityDetail = 'Missing/stale evidence, or the SID, session, administrator or profile check failed.'
} elseif ($forbiddenHeld.Count) {
    $identityDetail = 'The host token holds ' + ($forbiddenHeld -join ', ') + '. Reinstall so the service requires only ' +
        'SeChangeNotifyPrivilege.'
} elseif ($unexpectedHeld.Count) {
    $identityDetail = 'The host token holds privileges outside the allowlist: ' + ($unexpectedHeld -join ', ') + '.'
} elseif (-not $configuredPassed) {
    $identityDetail = 'The service''s required privileges are not exactly SeChangeNotifyPrivilege (reinstall).'
} elseif (-not $heldPrivileges.Count) {
    $identityDetail = 'current.json lists no token privileges.'
}
$pipePassed = $live -and $pipeReply -and $policy -and $positive -and
    $pipeReply.installId -ceq $config.InstallId -and $pipeReply.accepted -eq $false -and
    $pipeReply.clientSid -ceq $config.OwnerSid -and $pipeReply.servicePid -eq $servicePid -and
    $positive.installId -ceq $config.InstallId -and $positive.accepted -eq $true -and
    $positive.clientSid -ceq $config.AgentSid -and $positive.servicePid -eq $servicePid -and
    $policy.installId -ceq $config.InstallId -and $policy.firstInstance -eq $true -and
    $policy.localOnly -eq $true -and $policy.squattingBlocked -eq $true -and $policy.collisionError -eq 5
$gates = @(
    [pscustomobject]@{ gate = 1; status = $bootStatus; detail = $bootDetail }
    [pscustomobject]@{ gate = 2; status = $(if ($identityPassed -and $privilegesPassed) { 'PASS' } else { 'FAIL' });
        detail = $identityDetail }
    [pscustomobject]@{ gate = 3; status = 'NOT-RUN'; detail = 'Native provider hooks are outside this probe.' }
    [pscustomobject]@{ gate = 4; status = 'NOT-RUN'; detail = 'Codex sandbox denial is outside this probe.' }
    [pscustomobject]@{ gate = 5; status = $(if ($pipePassed) { 'PASS' } else { 'FAIL' });
        detail = 'SCM-bound server PID; owner SID refused, service SID accepted; explicit local DACL and first-instance collision refusal.' }
    [pscustomobject]@{ gate = 6; status = 'NOT-RUN'; detail = 'Guard feasibility is outside this probe.' }
)
$report = [pscustomobject][ordered]@{
    schemaVersion = 1; installId = $config.InstallId; collectedUtc = [DateTime]::UtcNow.ToString('o')
    os = [pscustomobject]@{ caption = $os.Caption; version = $os.Version; build = $os.BuildNumber; architecture = $os.OSArchitecture }
    bootUtc = $osBoot.ToString('o'); gates = $gates; pipeError = $pipeError
    tokenPrivileges = $heldPrivileges; serviceRequiredPrivileges = $configuredPrivileges
    fatal = $fatal; phase0Complete = $false
}
if ($Json) { $report | ConvertTo-Json -Depth 10 }
else {
    $gates | Format-Table gate, status, detail -Wrap
    if ($pipeError) { Write-Output ('Pipe diagnostic: ' + $pipeError) }
    Write-Output 'Exit 0 certifies only these three qualification probes, never phase 0 or product isolation.'
}
if (@($gates | Where-Object { $_.gate -in @(1, 2, 5) -and $_.status -ne 'PASS' }).Count) { exit 1 }
exit 0
