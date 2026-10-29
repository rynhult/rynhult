#Requires -Version 5.1
#Requires -RunAsAdministrator
# Safety sign-off: remove only manifest-owned resources, by SID; refuse reparse points and identity mismatches.
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-SpikePlatform
$plan = Read-SpikePlan
$path = Join-Path $plan.root 'manifest.json'
Assert-AdminOwnedFile $plan.root
Assert-AdminOwnedFile $path
$manifest = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
Assert-SpikeManifest $manifest $plan
$previousPending = $manifest.pending
Initialize-SpikeNative
$marker = 'Rynhult p0 ' + $manifest.installId
$user = Get-SpikeUser $plan
if ($manifest.user.sid) {
    $sidUser = Get-SpikeUser $plan $manifest.user.sid
    if ($sidUser -and (-not $user -or $sidUser.SID.Value -cne $user.SID.Value)) {
        throw 'The manifest-owned account was renamed; retain state for manual review.'
    }
}
if ($user) {
    if ($user.Description -cne $marker -or ($manifest.user.sid -and $user.SID.Value -cne $manifest.user.sid)) {
        throw 'The local user no longer belongs to this manifest.'
    }
    # Recover a power loss after account creation but before the SID was journalled.
    $manifest.user.sid = $user.SID.Value
    Save-SpikeManifest $manifest
    Disable-LocalUser -SID $user.SID
}
$sid = $manifest.user.sid
$manifest.state = 'Removing'
$manifest.pending = 'remove-service'
Save-SpikeManifest $manifest
$service = Get-SpikeService $plan
if ($service) {
    $binary = '"' + (Join-Path $plan.root 'bin\SpikeHost.exe') + '"'
    $startName = $service.StartName
    if ($startName.StartsWith('.\')) { $startName = [Environment]::MachineName + $startName.Substring(1) }
    $startSid = [Security.Principal.NTAccount]::new($startName).Translate([Security.Principal.SecurityIdentifier]).Value
    $partialCreate = -not $manifest.service.created -and $previousPending -in @('service', 'remove-service') -and -not $service.Description
    if ($service.PathName -cne $binary -or $startSid -cne $sid -or
        ($service.Description -cne $marker -and -not $partialCreate)) {
        throw 'The service no longer belongs to this manifest.'
    }
    $controller = Get-Service -Name $plan.serviceName
    try {
        if ($controller.Status -ne 'Stopped') {
            Stop-Service -InputObject $controller
            $controller.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
        }
    } finally { $controller.Dispose() }
    [Rynhult.Spike.Native]::RemoveService($plan.serviceName)
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ((Get-SpikeService $plan) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (Get-SpikeService $plan) { throw 'Service deletion is pending; close service-management tools and retry. Other resources were retained.' }
}
# Deleting the service also deletes its SCM configuration, including the required privileges Install set.
$manifest.service.created = $false
$manifest.service.requiredPrivileges = @()
Save-SpikeManifest $manifest

if ($sid) {
    $manifest.pending = 'remove-rights'
    Save-SpikeManifest $manifest
    foreach ($right in [Rynhult.Spike.Native]::GetRights($sid)) {
        if ($right -notin (@($plan.grantRights) + @($plan.denyRights))) { throw 'Unexpected direct right; retain state for review.' }
    }
    [Rynhult.Spike.Native]::RemoveRights($sid)
    if (@([Rynhult.Spike.Native]::GetRights($sid)).Count) { throw 'LSA assignments remain.' }
    $manifest.rights = @()
    $manifest.pending = 'remove-profile'
    Save-SpikeManifest $manifest
    $profile = Get-SpikeProfile $sid
    if ($profile) {
        if ($manifest.profile.path -and $profile.LocalPath -cne $manifest.profile.path) { throw 'Profile path changed.' }
        $manifest.profile.path = $profile.LocalPath
        Save-SpikeManifest $manifest
        Assert-NoReparsePath $profile.LocalPath
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ($profile -and $profile.Loaded -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $profile = Get-SpikeProfile $sid
        }
        if ($profile -and $profile.Loaded) { throw 'Profile is still loaded; do not delete it or force-kill processes.' }
        if ($profile) { [Rynhult.Spike.Native]::DeleteProfile($sid) }
    }
    if ((Get-SpikeProfile $sid) -or ($manifest.profile.path -and (Test-Path -LiteralPath $manifest.profile.path))) {
        throw 'User profile or its files remain.'
    }
    $manifest.profile.created = $false
    $manifest.pending = 'remove-user'
    Save-SpikeManifest $manifest
    if ($user) { Remove-LocalUser -SID ([Security.Principal.SecurityIdentifier]::new($sid)) }
}
if ((Get-SpikeUser $plan) -or ($sid -and (Get-SpikeUser $plan $sid)) -or (Get-SpikeService $plan)) {
    throw 'User/service removal was incomplete.'
}
if ($sid -and (@([Rynhult.Spike.Native]::GetRights($sid)).Count -or (Get-SpikeProfile $sid))) {
    throw 'SID rights/profile cleanup was incomplete.'
}
$manifest.pending = 'remove-root'
Save-SpikeManifest $manifest
Assert-NoReparseTree $plan.root
# Preserve the recovery journal until every other item is gone. Do not clear attributes
# on agent-writable files: a hard link must never modify an outside file's metadata.
foreach ($item in Get-ChildItem -LiteralPath $plan.root -Force) {
    if ($item.Name -ceq 'manifest.json') { continue }
    if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName, $true) }
    else { [IO.File]::Delete($item.FullName) }
}
[IO.File]::Delete($path)
[IO.Directory]::Delete($plan.root, $false)
if (Test-Path -LiteralPath $plan.root) { throw 'Probe folder remains.' }
[pscustomobject]@{ removed = $true; installId = $manifest.installId; user = 'absent'; service = 'absent';
    profile = 'absent'; rights = 'absent'; folder = 'absent'; firewallChanges = 0 } | ConvertTo-Json
