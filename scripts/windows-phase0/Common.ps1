#Requires -Version 5.1
# Safety sign-off: helpers have no startup side effects; mutations require explicit installer calls.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:SpikeSource = $PSScriptRoot
$script:AdminSddl = 'O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'

function Assert-SpikePlatform {
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess -or $PSVersionTable.PSEdition -ne 'Desktop') {
        throw 'Use 64-bit Windows PowerShell 5.1 on the designated Windows 11 x64 test PC.'
    }
    $os = Get-CimInstance Win32_OperatingSystem
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID = 'C:'"
    if ([int]$os.BuildNumber -lt 22000 -or $os.ProductType -ne 1 -or
        $disk.FileSystem -ne 'NTFS' -or $disk.DriveType -ne 3 -or
        @(Get-CimInstance Win32_Processor | Where-Object Architecture -ne 9).Count -ne 0) {
        throw 'This probe requires Windows 11 x64 with local fixed NTFS on C:.'
    }
}

function Initialize-SpikeNative {
    if (-not ('Rynhult.Spike.Native' -as [type])) {
        Add-Type -TypeDefinition (Get-Content -LiteralPath (Join-Path $script:SpikeSource 'Native.cs') -Raw) `
            -ReferencedAssemblies 'System.dll', 'System.Core.dll', 'System.Security.dll'
    }
}

function Read-SpikePlan {
    $plan = Get-Content -LiteralPath (Join-Path $script:SpikeSource 'Plan.json') -Raw | ConvertFrom-Json
    if ($plan.schemaVersion -ne 1 -or $plan.root -cne 'C:\RynhultSpike' -or
        $plan.userName -cne 'rynhult-spike-1' -or $plan.serviceName -cne 'RynhultSpike' -or
        $plan.serviceStart -cne 'AutomaticDelayed' -or @($plan.firewall).Count -ne 0 -or
        (@($plan.serviceRequiredPrivileges) -join ',') -cne 'SeChangeNotifyPrivilege') {
        throw 'Unsupported change plan; do not repurpose this probe as an installer.'
    }
    return $plan
}

function Assert-NoReparsePath([string] $Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing a reparse point: $current"
            }
        }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent) { break }
        $current = $parent.FullName
    }
}

function Assert-NoReparseTree([string] $Path) {
    Assert-NoReparsePath $Path
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push($Path)
    while ($pending.Count) {
        foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing to traverse/delete a reparse point: $($item.FullName)"
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
}

function Assert-AdminOwnedFile([string] $Path) {
    Assert-NoReparsePath $Path
    $acl = Get-Acl -LiteralPath $Path
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne 'S-1-5-32-544') {
        throw "Not owned by Administrators: $Path"
    }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq 'Allow' -and
            $rule.IdentityReference.Value -notin @('S-1-5-18', 'S-1-5-32-544') -and
            ([int]$rule.FileSystemRights -band 0xD0156)) {
            throw "Non-administrator can modify protected state: $Path"
        }
    }
}

function Set-SpikeAcl([string] $Path, [string] $Sddl, [switch] $File, [switch] $AccessOnly) {
    if ($File) { $acl = New-Object Security.AccessControl.FileSecurity }
    else { $acl = New-Object Security.AccessControl.DirectorySecurity }
    if ($AccessOnly) {
        $acl.SetSecurityDescriptorSddlForm($Sddl, [Security.AccessControl.AccessControlSections]::Access)
    } else { $acl.SetSecurityDescriptorSddlForm($Sddl) }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Get-SpikeUser($Plan, [string] $Sid) {
    if ($Sid) { return Get-LocalUser | Where-Object { $_.SID.Value -ceq $Sid } }
    return Get-LocalUser | Where-Object Name -EQ $Plan.userName
}

function Get-SpikeService($Plan) {
    return Get-CimInstance Win32_Service -Filter "Name = '$($Plan.serviceName)'"
}

function Get-SpikeProfile([string] $Sid) {
    [void][Security.Principal.SecurityIdentifier]::new($Sid)
    return Get-CimInstance Win32_UserProfile -Filter "SID = '$Sid'"
}

function New-SpikeManifest($Plan, [string] $OwnerSid) {
    return [pscustomobject][ordered]@{
        schemaVersion = 1
        installId = [guid]::NewGuid().ToString('D')
        startedUtc = [DateTime]::UtcNow.ToString('o')
        ownerSid = $OwnerSid
        plan = $Plan
        planSha256 = (Get-FileHash -LiteralPath (Join-Path $script:SpikeSource 'Plan.json') -Algorithm SHA256).Hash
        state = 'Installing'
        pending = 'none'
        user = [pscustomobject]@{ sid = $null; created = $false; groups = @() }
        rights = @()
        profile = [pscustomobject]@{ path = $null; created = $false }
        service = [pscustomobject]@{ created = $false; requiredPrivileges = @() }
        folders = @('.')
        files = @('manifest.json')
        firewall = @()
        binarySha256 = $null
    }
}

function Save-SpikeManifest($Manifest) {
    $path = Join-Path $Manifest.plan.root 'manifest.json'
    $temporary = Join-Path $Manifest.plan.root ('manifest.' + [guid]::NewGuid().ToString('N') + '.tmp')
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Manifest | ConvertTo-Json -Depth 12))
    [Rynhult.Spike.Native]::WriteNewFile($temporary, $bytes, $script:AdminSddl)
    if (Test-Path -LiteralPath $path) { [IO.File]::Replace($temporary, $path, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $path) }
}

function Write-SpikeFile($Plan, [string] $Relative, [byte[]] $Bytes, [string] $Sddl) {
    if ($Relative -notin $Plan.files) { throw "File outside the change inventory: $Relative" }
    [Rynhult.Spike.Native]::WriteNewFile((Join-Path $Plan.root $Relative), $Bytes, $Sddl)
}

function Assert-SpikeManifest($Manifest, $Plan) {
    if ($Manifest.schemaVersion -ne 1 -or
        $Manifest.planSha256 -cne (Get-FileHash -LiteralPath (Join-Path $script:SpikeSource 'Plan.json') -Algorithm SHA256).Hash -or
        ($Manifest.plan | ConvertTo-Json -Depth 8 -Compress) -cne ($Plan | ConvertTo-Json -Depth 8 -Compress) -or
        @($Manifest.firewall).Count -ne 0) { throw 'Manifest/plan mismatch; retain state for manual review.' }
    [void][guid]::ParseExact($Manifest.installId, 'D')
    [void][Security.Principal.SecurityIdentifier]::new($Manifest.ownerSid)
    if ($Manifest.user.sid) { [void][Security.Principal.SecurityIdentifier]::new($Manifest.user.sid) }
    foreach ($folder in $Manifest.folders) {
        if ($folder -notin (@('.') + @($Plan.folders))) { throw 'Foreign folder in manifest.' }
    }
    foreach ($file in $Manifest.files) {
        if ($file -notin $Plan.files) { throw 'Foreign file in manifest.' }
    }
    foreach ($right in $Manifest.rights) {
        if ($right -notin (@($Plan.grantRights) + @($Plan.denyRights))) { throw 'Foreign right in manifest.' }
    }
    foreach ($privilege in $Manifest.service.requiredPrivileges) {
        if ($privilege -cnotin @($Plan.serviceRequiredPrivileges)) { throw 'Foreign service privilege in manifest.' }
    }
}

function Assert-SpikeOwner([string] $OwnerSid) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or $identity.User.Value -cne $OwnerSid) {
        throw 'Collect must run unelevated as the owner SID recorded by Install.'
    }
}
