#Requires -Version 5.1
#Requires -RunAsAdministrator
# Safety sign-off: owner-run, fixed resources, no downloads or firewall changes; retain manifest on failure.
[CmdletBinding()]
param([string] $OwnerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value)

. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-SpikePlatform
[void][Security.Principal.SecurityIdentifier]::new($OwnerSid)
$plan = Read-SpikePlan
Assert-NoReparsePath $plan.root
if ((Test-Path -LiteralPath $plan.root) -or (Get-SpikeUser $plan) -or (Get-SpikeService $plan)) {
    throw 'Probe resources already exist. Archive old unowned probes or use this install''s manifest to uninstall; nothing was changed.'
}
$compiler = Join-Path ([Environment]::GetFolderPath('Windows')) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler) -or (Get-AuthenticodeSignature -LiteralPath $compiler).Status -ne 'Valid') {
    throw 'The signed in-box x64 .NET Framework compiler is unavailable; nothing was changed.'
}
Initialize-SpikeNative
$manifest = New-SpikeManifest $plan $OwnerSid
$marker = 'Rynhult p0 ' + $manifest.installId
$rootSddl = $script:AdminSddl + "(A;;FRFX;;;$OwnerSid)"
[Rynhult.Spike.Native]::CreateDirectory($plan.root, $rootSddl)
Save-SpikeManifest $manifest

$manifest.pending = 'user'
Save-SpikeManifest $manifest
$secret = [Rynhult.Spike.Native]::GenerateSecret()
try {
    $user = New-LocalUser -Name $plan.userName -Password $secret -AccountNeverExpires `
        -PasswordNeverExpires -UserMayNotChangePassword -Description $marker
    $manifest.user.sid = $user.SID.Value
    $manifest.user.created = $true
    Save-SpikeManifest $manifest
    $sid = $manifest.user.sid
    if (@(Get-LocalGroupMember -SID 'S-1-5-32-545' | Where-Object { $_.SID.Value -ceq $sid }).Count -eq 0) {
        Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $user
    }
    $manifest.user.groups = @($plan.userGroups)
    Save-SpikeManifest $manifest
    Set-SpikeAcl $plan.root ($rootSddl + "(A;;FRFX;;;$sid)")

    $manifest.pending = 'folders'
    Save-SpikeManifest $manifest
    foreach ($folder in $plan.folders) {
        $sddl = $script:AdminSddl
        if ($folder -eq 'bin') { $sddl += "(A;OICI;FRFX;;;$sid)(A;OICI;FRFX;;;$OwnerSid)" }
        if ($folder -eq 'evidence') { $sddl += "(A;OICI;0x1301bf;;;$sid)(A;OICI;FRFX;;;$OwnerSid)" }
        [Rynhult.Spike.Native]::CreateDirectory((Join-Path $plan.root $folder), $sddl)
        $manifest.folders += $folder
        Save-SpikeManifest $manifest
    }

    $manifest.pending = 'files'
    Save-SpikeManifest $manifest
    Write-SpikeFile $plan 'secrets\password.dpapi' ([Rynhult.Spike.Native]::ProtectSecret($secret)) $script:AdminSddl
    $manifest.files += 'secrets\password.dpapi'
    Save-SpikeManifest $manifest

    $manifest.pending = 'rights'
    Save-SpikeManifest $manifest
    if (@([Rynhult.Spike.Native]::GetRights($sid)).Count -ne 0) { throw 'Fresh SID unexpectedly has direct LSA rights.' }
    $rights = [string[]](@($plan.grantRights) + @($plan.denyRights))
    [Rynhult.Spike.Native]::AddRights($sid, $rights)
    if (Compare-Object ($rights | Sort-Object) (@([Rynhult.Spike.Native]::GetRights($sid)) | Sort-Object)) {
        throw 'Assigned rights do not match the plan.'
    }
    $manifest.rights = $rights
    Save-SpikeManifest $manifest

    $manifest.pending = 'profile'
    Save-SpikeManifest $manifest
    $manifest.profile.path = [Rynhult.Spike.Native]::CreateProfile($sid, $plan.userName)
    $manifest.profile.created = $true
    Save-SpikeManifest $manifest
    Assert-NoReparsePath $manifest.profile.path
    # Preserve Windows' profile owner; assigning an arbitrary owner would need restore privilege.
    Set-SpikeAcl $manifest.profile.path ("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;$sid)") -AccessOnly

    $manifest.pending = 'files'
    Save-SpikeManifest $manifest
    $binary = Join-Path $plan.root 'bin\SpikeHost.exe'
    & $compiler /nologo /target:exe /platform:x64 /optimize+ "/out:$binary" `
        /reference:System.Security.dll /reference:System.ServiceProcess.dll `
        /reference:System.Web.Extensions.dll /reference:System.Management.dll `
        (Join-Path $PSScriptRoot 'Native.cs') (Join-Path $PSScriptRoot 'SpikeHost.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Local test-host compilation failed; uninstall using the retained manifest.' }
    Set-SpikeAcl $binary ($script:AdminSddl + "(A;;FRFX;;;$sid)(A;;FRFX;;;$OwnerSid)") -File
    $manifest.binarySha256 = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash
    $manifest.files += 'bin\SpikeHost.exe'
    Save-SpikeManifest $manifest

    $config = [ordered]@{
        InstallId = $manifest.installId; InstallUtc = $manifest.startedUtc
        AgentSid = $sid; OwnerSid = $OwnerSid; ProfilePath = $manifest.profile.path
        ServiceName = $plan.serviceName; PipeName = 'RynhultSpike.' + $manifest.installId
        Rights = $rights
    }
    $configSddl = $script:AdminSddl + "(A;;FR;;;$sid)(A;;FR;;;$OwnerSid)"
    Write-SpikeFile $plan 'config.json' ([Text.UTF8Encoding]::new($false).GetBytes(($config | ConvertTo-Json))) $configSddl
    $manifest.files += 'config.json'
    Save-SpikeManifest $manifest

    $manifest.pending = 'service'
    Save-SpikeManifest $manifest
    [Rynhult.Spike.Native]::CreateService($plan.serviceName, $binary, $plan.userName, $secret, $marker, $OwnerSid)
    $manifest.service.created = $true
    Save-SpikeManifest $manifest

    # The SERVICE group brings SeImpersonatePrivilege and SeCreateGlobalPrivilege. Before the first start, make SCM
    # keep only the planned privileges in the service's token (Gate 2 checks the token).
    $manifest.pending = 'service-privileges'
    Save-SpikeManifest $manifest
    $required = [string[]]@($plan.serviceRequiredPrivileges)
    [Rynhult.Spike.Native]::RequireOnlyPrivileges($plan.serviceName, $required)
    # Joined strings, not Compare-Object: it can't bind an empty array, which is what a missing setting reads as.
    if ((@([Rynhult.Spike.Native]::RequiredPrivileges($plan.serviceName)) -join ',') -cne ($required -join ',')) {
        throw 'The service''s required privileges do not match the plan; uninstall using the retained manifest.'
    }
    $manifest.service.requiredPrivileges = $required
    Save-SpikeManifest $manifest
} finally {
    $secret.Dispose()
}
Start-Service -Name $plan.serviceName
$manifest.state = 'Installed'
$manifest.pending = 'none'
Save-SpikeManifest $manifest
Write-Output 'Installed the qualification probe. Run Collect unelevated, then reboot and wait at the sign-in screen. No gates are certified by installation.'
