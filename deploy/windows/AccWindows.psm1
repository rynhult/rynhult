<#
.SYNOPSIS
What the Windows installers share: WSL2 with Ubuntu and systemd, the logon task that keeps WSL running, logging and
dry runs, and the parser for the command from Host > Machines > Add machine.

.DESCRIPTION
Used by deploy/windows/install-acc.ps1 (Rynhult Setup, both roles) and deploy/node/windows/install.ps1 (the node-only
script). Call Initialize-AccRun first. In a dry run every change is printed ("+ ...") instead of made, and a failed
check is reported ("WOULD FAIL: ...") instead of stopping. Keep this file ASCII: Windows PowerShell 5.1 reads files
without a byte order mark as ANSI.
#>
Set-StrictMode -Version Latest
# A module does not see its caller's preferences: failures stop here too (native commands relax it, below).
$ErrorActionPreference = 'Stop'

$script:DryRun = $false
$script:LogPath = ''
$script:Problems = 0
$script:Distro = 'Ubuntu-24.04'
$script:InstallExit = 0
$script:NativeOutput = ''
$script:Helper = 'deploy/node/windows/wsl-node.sh'  # relative to the ACC tree, run inside WSL from there

# WSL's id for the Hyper-V firewall (Microsoft's docs on mirrored networking).
$script:WslVmCreatorId = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
$script:FirewallRule = 'ACC-WSL-Web'

# The command Host > Machines > Add machine shows (src/acc/server/api/nodes.py), and nothing else: the options in
# that order, an https origin without user, path or query, a code shaped like src/acc/server/nodes.py makes them, and
# a name as src/acc/node/config.py accepts it. tests/test_node_packages.py reads these strings and checks them with
# Python against tests/data/enroll-commands.json; deploy/windows/tests/Test-EnrollParser.ps1 checks this module.
$script:EnrollPattern = @(
    '^(?:rynhult-node|acc-node)[ \t]+enroll'
    '[ \t]+--primary[ \t]+https://(?<host>[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)(?::(?<port>[0-9]{1,5}))?/?'
    '[ \t]+(?:--trust[ \t]+(?<trust>system)|--ca-fingerprint[ \t]+(?<fingerprint>[0-9A-Fa-f]{64}))'
    '[ \t]+--code[ \t]+(?<code>[0-9a-f]{6}(?:-[0-9a-f]{6}){3})'
    '[ \t]+--name[ \t]+(?<name>[a-z0-9][a-z0-9-]{0,39})$'
) -join ''
$script:EnrollShape = 'rynhult-node enroll --primary https://<your Rynhult> (--trust system | --ca-fingerprint <64 hex digits>) --code <code> --name <name>'

# ------------------------------------------------------------------ logging and dry runs

# All elevated inputs and append targets must be protected against non-administrator replacement.
function Assert-AccProtectedPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    $trusted = @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $write = [Security.AccessControl.FileSystemRights]::WriteData -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    while ($current) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($item) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unsafe installer path: reparse point' }
            $acl = Get-Acl -LiteralPath $current
            $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
            if ($owner -notin $trusted) { throw 'Unsafe installer path: untrusted owner' }
            foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
                if ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
                if ($rule.AccessControlType -eq 'Allow' -and ($rule.FileSystemRights -band $write) -and
                    $rule.IdentityReference.Value -notin $trusted) { throw 'Unsafe installer path: writable by a non-administrator' }
            }
        }
        $current = Split-Path -Parent $current
    }
}

function Get-AccSystemTool([string]$Name) {
    if ($Name -notin @('wsl.exe', 'dism.exe', 'conhost.exe', 'notepad.exe')) { throw 'Unsupported system tool' }
    return (Join-Path ([Environment]::GetFolderPath('System')) $Name)
}

function Open-AccLog([string]$Path) {
    Assert-AccProtectedPath $Path
    # Never dispatch an elevated process through a per-user .log file association.
    $viewer = Get-AccSystemTool 'notepad.exe'
    Start-Process -FilePath $viewer -ArgumentList @("`"$Path`"")
}

function Initialize-AccPrivateDirectory([string]$Path) {
    Assert-AccProtectedPath $Path
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $admins = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $acl.SetOwner($admins)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Initialize-AccRun {
    param(
        [string]$Distro = 'Ubuntu-24.04',
        [string]$LogPath = '',
        [switch]$DryRun
    )
    $script:Distro = $Distro
    if ($LogPath) { $LogPath = [IO.Path]::GetFullPath($LogPath) }
    $script:LogPath = $LogPath
    $script:DryRun = [bool]$DryRun
    $script:Problems = 0
    if ($LogPath) {
        Assert-AccProtectedPath $LogPath
        $folder = Split-Path -Parent $LogPath
        if ($folder) { Initialize-AccPrivateDirectory $folder }
        if (Test-Path -LiteralPath $LogPath) {
            # Replace inherited/public ACLs from an older installer before appending.
            $acl = Get-Acl -LiteralPath $LogPath
            $acl.SetAccessRuleProtection($false, $false)
            foreach ($rule in @($acl.Access)) { if (-not $rule.IsInherited) { $acl.RemoveAccessRuleSpecific($rule) } }
            Set-Acl -LiteralPath $LogPath -AclObject $acl
        }
    }
}

function Write-AccLog([string]$Message) {
    $Message = Protect-AccLogText $Message
    Write-Information $Message -InformationAction Continue
    if ($script:LogPath) {
        Add-Content -LiteralPath $script:LogPath -Value $Message -Encoding UTF8
    }
}

function Protect-AccLogText([string]$Message) {
    # Redact native output, dry runs and excerpts of logs from older installers.
    if ($env:CF_API_TOKEN -and $env:CF_API_TOKEN.Trim()) { $Message = $Message.Replace($env:CF_API_TOKEN.Trim(), '[redacted]') }
    $Message = $Message -replace '(?i)([a-z][a-z0-9+.-]*://)[^/@\s]*@', '$1[redacted]@'
    $Message = $Message -replace '(?i)((?:Bearer|Basic)\s+).*', '$1[redacted]'
    $Message = $Message -replace '(?i)((?:token|password|secret|key|code)["\x27]?\s*[:=]\s*).*', '$1[redacted]'
    return ($Message -replace '(?i)[0-9a-f]{6}(?:-[0-9a-f]{6}){3}', '[redacted]')
}

function Write-AccFailureContext {
    if ($script:LogPath) {
        $tail = @(Get-Content -LiteralPath $script:LogPath -Tail 12 -ErrorAction SilentlyContinue)
        foreach ($line in $tail) { Write-Information ((Protect-AccLogText $line).Substring(0, [Math]::Min(300, (Protect-AccLogText $line).Length))) -InformationAction Continue }
        Write-Information "Full log: $($script:LogPath)" -InformationAction Continue
    }
}

function Write-AccStep([string]$Message) { Write-AccLog "== $Message" }

function Write-AccOk([string]$Message) { Write-AccLog "  ok: $Message" }

# A failed check stops the install; in a dry run it is reported and the walk-through goes on.
function Write-AccFailure([string]$Message) {
    if ($script:DryRun) {
        Write-AccLog "  WOULD FAIL: $Message"
        $script:Problems++
    } else {
        throw $Message
    }
}

function Get-AccProblemCount { return $script:Problems }

function Test-AccDryRun { return $script:DryRun }

# Prints the change; makes it unless this is a dry run.
function Invoke-AccChange {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    Write-AccLog "+ $Description"
    if (-not $script:DryRun) {
        # Whatever the action prints is logged, never returned: callers' return values stay theirs.
        & $Action | ForEach-Object { Write-AccLog ('  ' + ("$_" -replace "`0", '')) }
    }
}

# Runs a program with its output logged; returns its exit code.
function Invoke-AccNative {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $ErrorActionPreference = 'Continue'
    $script:NativeOutput = ''
    if ($FilePath -in @('wsl.exe', 'dism.exe')) { $FilePath = Get-AccSystemTool $FilePath }
    & $FilePath @Arguments 2>&1 | ForEach-Object {
        $line = ("$_" -replace "`0", '')
        $script:NativeOutput += $line + "`n"
        Write-AccLog ('  ' + $line)
    }
    return $LASTEXITCODE
}

function Format-AccCommandLine([string[]]$Arguments) {
    return (($Arguments | ForEach-Object { if ($_ -match '[\s"]' -or $_ -eq '') { '"' + $_ + '"' } else { $_ } }) -join ' ')
}

# ------------------------------------------------------------------ Windows and WSL

function Test-AccAdministrator {
    $current = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $current.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Windows 11 (MinimumBuild: 22621 for mirrored networking), an elevated prompt, and virtualization.
function Test-AccPrerequisite {
    param([int]$MinimumBuild = 22000)
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    if ([int]$os.BuildNumber -ge $MinimumBuild -and $os.ProductType -eq 1) {
        Write-AccOk "$($os.Caption) (build $($os.BuildNumber))"
    } else {
        Write-AccFailure "needs Windows 11, build $MinimumBuild or later (this is $($os.Caption), build $($os.BuildNumber))"
    }
    if (Test-AccAdministrator) {
        Write-AccOk 'running as administrator'
    } else {
        Write-AccFailure 'run this from an elevated PowerShell (Run as administrator)'
    }
    $computer = Get-CimInstance -ClassName Win32_ComputerSystem
    $cpu = Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1
    if ($computer.HypervisorPresent) {
        Write-AccOk 'virtualization (a hypervisor is running)'
    } elseif ($cpu.VirtualizationFirmwareEnabled) {
        Write-AccOk 'virtualization enabled in firmware'
    } else {
        Write-AccFailure 'turn on virtualization (Intel VT-x or AMD-V/SVM) in the BIOS/UEFI settings, then run this again'
    }
}

# Native commands' stderr must not become terminating errors (Windows PowerShell 5.1): each wrapper relaxes it.
function Get-AccWslDistro {
    $ErrorActionPreference = 'Continue'
    if (-not (Test-Path -LiteralPath (Get-AccSystemTool 'wsl.exe'))) { return @() }
    $names = & (Get-AccSystemTool 'wsl.exe') --list --quiet 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $names) { return @() }
    return @($names | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
}

function Test-AccWslDistro { return ((Get-AccWslDistro) -contains $script:Distro) }

function Get-AccWslVerboseList {
    $ErrorActionPreference = 'Continue'
    return (((& (Get-AccSystemTool 'wsl.exe') --list --verbose 2>$null) | Out-String) -replace "`0", '')
}

# Runs a command in the distribution as root, its output logged; returns the exit code. In a dry run it is only
# printed, unless it is -ReadOnly (a dry run inside WSL, say).
function Invoke-AccWsl {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = '',
        [switch]$ReadOnly
    )
    $wslArguments = @('-d', $script:Distro, '-u', 'root')
    if ($WorkingDirectory) { $wslArguments += @('--cd', $WorkingDirectory) }
    $wslArguments += $Arguments
    Write-AccLog "+ wsl.exe $(Format-AccCommandLine $wslArguments)"
    if ($script:DryRun -and -not $ReadOnly) { return 0 }
    return (Invoke-AccNative -FilePath wsl.exe -Arguments $wslArguments)
}

# Like Invoke-AccWsl, but returns the command's output (trimmed) and never logs it.
function Get-AccWslOutput([string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'
    $output = & (Get-AccSystemTool 'wsl.exe') -d $script:Distro -u root @Arguments 2>$null
    return (($output | Out-String) -replace "`0", '').Trim()
}

function Test-AccWslAvailable { return (Test-Path -LiteralPath (Get-AccSystemTool 'wsl.exe')) }

function Test-AccRestartRequired([int]$Code) {
    # Some WSL versions report reboot-required in text with exit 0.
    return ($Code -eq 3010 -or $script:NativeOutput -match '(?i)restart required|reboot required|system is rebooted|restart.*(?:computer|machine|system)')
}

function Install-AccWsl {
    # The inbox wsl.exe may be a stub: install WSL before requesting Ubuntu.
    # Microsoft documents --no-distribution and the restart requirement:
    # https://learn.microsoft.com/windows/wsl/install
    # https://learn.microsoft.com/windows/wsl/basic-commands#install
    if (Test-AccWslAvailable) {
        $code = Invoke-AccNative -FilePath wsl.exe -Arguments @('--status')
        if ($code -eq 0 -or $script:NativeOutput -notmatch '(?i)not installed|wsl.exe --install|wsl --install|0x8007019e|optional component') { return 'ready' }
        Invoke-AccChange 'wsl.exe --install --no-distribution' {
            $script:InstallExit = Invoke-AccNative -FilePath wsl.exe -Arguments @('--install', '--no-distribution')
        }
        if ($script:DryRun) { return 'ready' }
        if (Test-AccRestartRequired $script:InstallExit) { return 'restart' }
        # Older commands may print help for an unavailable option with exit 0.
        $unsupported = $script:NativeOutput -match '(?i)invalid.*(?:option|argument|command)|unrecognized.*(?:option|argument|command)|(?:option|command).*not supported|usage:'
        if (-not $unsupported) {
            if ($script:InstallExit -eq 0) { return 'ready' }
            # Network and Store errors remain errors, rather than triggering DISM.
            throw "installing WSL failed (exit $($script:InstallExit))"
        }
    }
    # Legacy fallback; restart before requesting Ubuntu again.
    # https://learn.microsoft.com/windows/wsl/install-manual
    foreach ($feature in 'Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform') {
        Invoke-AccChange "enable Windows feature $feature (DISM, no restart)" {
            $code = Invoke-AccNative -FilePath dism.exe -Arguments @('/online', '/enable-feature', "/featurename:$feature", '/all', '/norestart')
            if ($code -ne 0 -and $code -ne 3010) { throw "enabling $feature failed (exit $code)" }
        }
    }
    if ($script:DryRun) { return 'ready' }
    return 'restart'
}

# Returns 'ready' (installed, on WSL2; in a dry run also when it would be installed) or 'restart' (Windows must
# restart before the distribution can be used).
function Install-AccWslDistro {
    param([switch]$AfterRestart)
    $distro = $script:Distro
    if (Test-AccWslDistro) {
        Write-AccOk "$distro is installed"
    } else {
        if ((Install-AccWsl) -eq 'restart') { return 'restart' }
        Invoke-AccChange "wsl.exe --install -d $distro --no-launch" {
            $script:InstallExit = Invoke-AccNative -FilePath wsl.exe -Arguments @('--install', '-d', $distro, '--no-launch')
        }
        if ($script:DryRun) { return 'ready' }
        $code = $script:InstallExit
        if (Test-AccRestartRequired $code) { return 'restart' }
        # 3010: installed, Windows must restart (ERROR_SUCCESS_REBOOT_REQUIRED)
        if ($code -ne 0 -and $code -ne 3010) { throw "wsl --install -d $distro failed (exit $code)" }
        if (-not (Test-AccWslDistro)) {
            if (-not $AfterRestart) {
                Write-AccLog "  WSL was just turned on: Windows must restart to finish installing $distro"
                return 'restart'
            }
            # Do not elevate a per-user Store launcher/alias. Initialize the distro from Start, then rerun Setup.
            if (-not (Test-AccWslDistro)) {
                throw "$distro is still not installed after the restart: open it once from the Start menu, then run this again"
            }
        }
    }
    if ((Get-AccWslVerboseList) -match "(?m)^\*?\s*$([regex]::Escape($distro))\s+\S+\s+1\s*$") {
        Invoke-AccChange "wsl.exe --set-version $distro 2" {
            $code = Invoke-AccNative -FilePath wsl.exe -Arguments @('--set-version', $distro, '2')
            if ($code -ne 0) { throw "wsl --set-version $distro 2 failed (exit $code)" }
        }
    } else {
        Write-AccOk "$distro runs on WSL2"
    }
    return 'ready'
}

function Enable-AccWslSystemd {
    param([Parameter(Mandatory = $true)][string]$Repo)
    $code = Invoke-AccWsl -WorkingDirectory $Repo -Arguments @('--', 'sh', $script:Helper, 'systemd')
    if ($script:DryRun) { return }
    if ($code -eq 10) {
        $null = Invoke-AccNative -FilePath wsl.exe -Arguments @('--terminate', $script:Distro)  # systemd on the next start
    } elseif ($code -ne 0) {
        throw "turning on systemd failed (exit $code)"
    }
    $state = Get-AccWslOutput @('--', 'systemctl', 'is-system-running', '--wait')
    if ($state -notmatch 'running|degraded') {
        throw "systemd did not start in $($script:Distro) (state: $state); try: wsl --shutdown, then run this again"
    }
    Write-AccOk "systemd is $state"
}

# deploy/node/install.sh inside WSL, from a copy of the tree. A dry run lets the Linux install show its own steps
# when the distribution is already there.
function Install-AccNodeInWsl {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string]$Agent,
        $Enroll = $null,
        [switch]$AllowBundledSource
    )
    if ($script:DryRun -and (Test-AccWslDistro)) {
        $code = Invoke-AccWsl -ReadOnly -WorkingDirectory $Repo -Arguments @(
            '--', 'bash', 'deploy/node/install.sh', '--dry-run', '--install-prereqs', '--agent', $Agent)
        if ($code -ne 0) { Write-AccLog "  the Linux dry run reported problems (exit $code)" }
        return
    }
    $arguments = @('--', 'sh', $script:Helper, 'install')
    if ($null -ne $Enroll) {
        # Windows PowerShell drops empty native arguments; keep the positional trust slot occupied.
        $fingerprint = '-'
        if ($Enroll.Trust -eq 'ca') { $fingerprint = $Enroll.Fingerprint }
        $arguments = @('--', 'sh', $script:Helper, 'install-primary', $Enroll.Primary, $Enroll.Trust, $fingerprint)
        if ($AllowBundledSource) { $arguments += '--allow-bundled-source' }
    }
    $code = Invoke-AccWsl -WorkingDirectory $Repo -Arguments ($arguments + @('--agent', $Agent))
    if ($code -ne 0) { throw "the Linux node install failed inside $($script:Distro) (exit $code)" }
}

# The user name Windows knows you by, as a Linux account name (at most 25 characters, so "<name>-agent" fits).
function Get-AccAccountBase {
    $base = ($env:USERNAME.ToLowerInvariant() -replace '[^a-z0-9_-]', '-').Trim('-')
    if ($base -notmatch '^[a-z_]') { $base = "u$base" }
    if ($base.Length -gt 25) { $base = $base.Substring(0, 25).TrimEnd('-') }
    return $base
}

function Register-AccKeepAliveTask {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $distro = $script:Distro
    Invoke-AccChange "Register-ScheduledTask '$TaskName' at logon of $user (wsl -d $distro, kept running)" {
        # conhost --headless: no console window. WSL stops when nothing runs in it; this sleep keeps it up.
        $console = Get-AccSystemTool 'conhost.exe'
        $wsl = Get-AccSystemTool 'wsl.exe'
        $action = New-ScheduledTaskAction -Execute $console `
            -Argument "--headless `"$wsl`" -d $distro --exec /bin/sleep infinity"
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
        $taskPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $action -Trigger $trigger -Settings $settings `
            -Principal $taskPrincipal -Description 'Keeps WSL (and Rynhult inside it) running while you are signed in.' `
            -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName -TaskPath '\'
    }
}

# Only retire an installer-owned node task after its replacement has started. Task names are global to this PC;
# another person's task or another distro is never ours to stop, even when the old name happens to match.
$script:LegacyNodeTask = 'ACC node (WSL)'

function Get-AccKeepAliveIdentity {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return @{ Sid = $identity.User.Value; Name = $identity.Name }
}

function Get-AccOwnedNodeKeepAliveTask {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*') { return $null }
        throw
    }
    if ($null -eq $task) { return $null }
    $identity = Get-AccKeepAliveIdentity
    $console = Get-AccSystemTool 'conhost.exe'
    $wsl = Get-AccSystemTool 'wsl.exe'
    $expected = @("--headless `"$wsl`" -d $($script:Distro) --exec /bin/sleep infinity")
    # The original installer (#119) emitted this same protected executable without quotes.
    # Only accept that form when the path has no whitespace, so its executable is unambiguous.
    if ($wsl -notmatch '\s') { $expected += "--headless $wsl -d $($script:Distro) --exec /bin/sleep infinity" }
    if (@($task).Count -ne 1 -or $task.TaskPath -ne '\' -or $task.TaskName -ne $TaskName -or
        ($task.Principal.UserId -ne $identity.Sid -and $task.Principal.UserId -ne $identity.Name) -or
        "$($task.Principal.LogonType)" -ne 'Interactive' -or "$($task.Principal.RunLevel)" -ne 'Limited' -or
        @($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ne $console -or
        $expected -cnotcontains $task.Actions[0].Arguments) {
        throw "Task '$TaskName' does not match this person's Rynhult node in $($script:Distro). Left unchanged; review its owner, action and distribution in Task Scheduler."
    }
    return $task
}

function Unregister-AccNodeKeepAliveTask {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    $task = Get-AccOwnedNodeKeepAliveTask -TaskName $TaskName
    if ($null -eq $task) { Write-AccOk "no task '$TaskName'"; return }
    Invoke-AccChange "Stop and unregister verified node task '$TaskName'" {
        Stop-ScheduledTask -InputObject $task -ErrorAction Stop
        Unregister-ScheduledTask -InputObject $task -Confirm:$false -ErrorAction Stop
    }
}

function Remove-AccLegacyNodeTask {
    Unregister-AccNodeKeepAliveTask -TaskName $script:LegacyNodeTask
}

function Wait-AccNodeKeepAliveTask {
    param([Parameter(Mandatory = $true)][string]$TaskName, [double]$TimeoutSeconds = 10)
    $timer = [Diagnostics.Stopwatch]::StartNew()
    do {
        $task = Get-AccOwnedNodeKeepAliveTask -TaskName $TaskName
        if ($null -ne $task -and "$($task.State)" -eq 'Running') { return }
        Start-Sleep -Milliseconds 100
    } while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds)
    throw "Task '$TaskName' was not observed running within $TimeoutSeconds seconds."
}

function Register-AccNodeKeepAliveTask {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    # Validate both names before any mutation, including a collision at the new name.
    $null = Get-AccOwnedNodeKeepAliveTask -TaskName $TaskName
    if ($TaskName -ne $script:LegacyNodeTask) {
        $null = Get-AccOwnedNodeKeepAliveTask -TaskName $script:LegacyNodeTask
    }
    try {
        Register-AccKeepAliveTask -TaskName $TaskName
        if (-not (Test-AccDryRun)) { Wait-AccNodeKeepAliveTask -TaskName $TaskName }
    } catch {
        throw "Replacement task '$TaskName' was not confirmed started; existing legacy task retained. The replacement may have been registered. Inspect both names in Task Scheduler before retrying. $($_.Exception.Message)"
    }
    if ($TaskName -ne $script:LegacyNodeTask) {
        try { Remove-AccLegacyNodeTask } catch {
            throw "Replacement '$TaskName' started, but legacy task cleanup failed; both may remain. Inspect both names in Task Scheduler. $($_.Exception.Message)"
        }
    }
}

function Unregister-AccKeepAliveTask {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Invoke-AccChange "Unregister-ScheduledTask '$TaskName'" {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    } else {
        Write-AccOk "no task '$TaskName'"
    }
}

# ------------------------------------------------------------------ mirrored networking (main ACC)

# .wslconfig text with networkingMode=mirrored in [wsl2]; every other line stays as it was.
function Get-AccWslConfigText([string]$Text) {
    $lines = New-Object System.Collections.Generic.List[string]
    if ($Text) { foreach ($line in ($Text -split "\r?\n")) { $lines.Add($line) } }
    while ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq '') { $lines.RemoveAt($lines.Count - 1) }
    $section = ''
    $header = -1
    $mode = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $trimmed = $lines[$i].Trim()
        if ($trimmed -match '^\[([^\]]+)\]') {
            $section = $Matches[1].Trim().ToLowerInvariant()
            if ($section -eq 'wsl2' -and $header -lt 0) { $header = $i }
        } elseif ($section -eq 'wsl2' -and $trimmed -match '^networkingMode\s*=\s*([^#;\s]*)') {
            $mode = $i
            if ($Matches[1] -eq 'mirrored') { return $Text }
        }
    }
    if ($mode -ge 0) {
        $lines[$mode] = 'networkingMode=mirrored'
    } elseif ($header -ge 0) {
        $lines.Insert($header + 1, 'networkingMode=mirrored')
    } else {
        if ($lines.Count -gt 0) { $lines.Add('') }
        $lines.Add('[wsl2]')
        $lines.Add('networkingMode=mirrored')
    }
    return (($lines -join "`r`n") + "`r`n")
}

# Returns $true when %UserProfile%\.wslconfig changed (WSL must restart to use it).
function Enable-AccMirroredNetworking {
    $path = Join-Path $env:USERPROFILE '.wslconfig'
    $old = ''
    if (Test-Path -LiteralPath $path) { $old = [IO.File]::ReadAllText($path) }
    $new = Get-AccWslConfigText $old
    if ($new -ceq $old) {
        Write-AccOk "networkingMode=mirrored already in $path"
        return $false
    }
    Invoke-AccChange "write networkingMode=mirrored to [wsl2] in $path (other settings kept)" {
        [IO.File]::WriteAllText($path, $new, (New-Object Text.UTF8Encoding $false))  # no byte order mark
    }
    return $true
}

# Mirrored networking puts WSL behind the Hyper-V firewall: let HTTP and HTTPS (Caddy) in.
function Register-AccWslFirewallRule {
    if (-not (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) {
        Write-AccLog '  this Windows has no Hyper-V firewall cmdlets: allow TCP 80 and 443 to WSL yourself if other devices cannot reach Rynhult'
        return
    }
    if (Test-AccWslFirewallRule) {
        Write-AccOk "Hyper-V firewall rule $($script:FirewallRule)"
        return
    }
    $rule = $script:FirewallRule
    $creator = $script:WslVmCreatorId
    Invoke-AccChange "New-NetFirewallHyperVRule $rule (WSL, inbound TCP 80 and 443)" {
        New-NetFirewallHyperVRule -Name $rule -DisplayName 'Rynhult in WSL: HTTP and HTTPS' -Direction Inbound `
            -VMCreatorId $creator -Protocol TCP -LocalPorts 80, 443 -Action Allow | Out-Null
    }
}

function Test-AccWslFirewallRule {
    if (-not (Get-Command Get-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) { return $false }
    try {
        return [bool](Get-NetFirewallHyperVRule -Name $script:FirewallRule -ErrorAction SilentlyContinue)
    } catch {
        Write-AccLog "  could not read the Hyper-V firewall: $($_.Exception.Message)"
        return $false
    }
}

function Unregister-AccWslFirewallRule {
    if (-not (Test-AccWslFirewallRule)) { return }
    $rule = $script:FirewallRule
    Invoke-AccChange "Remove-NetFirewallHyperVRule $rule" { Remove-NetFirewallHyperVRule -Name $rule }
}

# ------------------------------------------------------------------ answers

# Parses the command from Host > Machines > Add machine. Throws unless it is exactly that command; the values come
# back checked, for passing as separate arguments. It is never run as given.
function ConvertFrom-AccEnrollCommand {
    param([AllowEmptyString()][string]$Command)
    $text = "$Command".Trim()
    if ($text.Length -eq 0) { throw "paste the command from Host > Machines > Add machine ($($script:EnrollShape))" }
    if ($text.Length -gt 1024) { throw 'that is too long to be the command from Host > Machines > Add machine' }
    $match = [regex]::Match($text, $script:EnrollPattern)
    if (-not $match.Success) {
        throw "not the command from Host > Machines > Add machine; expected: $($script:EnrollShape)"
    }
    $port = $match.Groups['port'].Value
    if ($port -and ([int]$port -lt 1 -or [int]$port -gt 65535)) { throw "bad port $port in --primary" }
    $primary = 'https://' + $match.Groups['host'].Value.ToLowerInvariant()
    if ($port) { $primary += ":$([int]$port)" }
    $fingerprint = $match.Groups['fingerprint'].Value.ToLowerInvariant()
    $trust = 'ca'
    if ($match.Groups['trust'].Success) { $trust = 'system' }
    return [pscustomobject]@{
        Primary     = $primary
        Trust       = $trust
        Fingerprint = $fingerprint
        Code        = $match.Groups['code'].Value
        Name        = $match.Groups['name'].Value
    }
}

# acc-node's arguments for a parsed command.
function ConvertTo-AccEnrollArgument {
    param([Parameter(Mandatory = $true)]$Enroll)
    $arguments = @('enroll', '--primary', $Enroll.Primary)
    if ($Enroll.Trust -eq 'system') {
        $arguments += @('--trust', 'system')
    } else {
        $arguments += @('--ca-fingerprint', $Enroll.Fingerprint)
    }
    return $arguments + @('--code', $Enroll.Code, '--name', $Enroll.Name)
}

$script:DnsName = '^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$'
$script:AgentClis = @('codex', 'copilot', 'antigravity', 'cursor', 'opencode')

# What the main ACC is set up with (deploy/setup.sh's questions); returns the problems, none when all is well.
function Test-AccMainSetting {
    param(
        [AllowEmptyString()][string]$Hostname = '',
        [AllowEmptyString()][string]$Owner = '',
        [AllowEmptyString()][string]$AgentClis = '',
        [AllowEmptyString()][string]$Zone = '',
        [AllowEmptyString()][string]$Token = ''
    )
    $problems = @()
    if ($Hostname -and $Hostname -cnotmatch $script:DnsName) {
        $problems += "the name to open Rynhult at must be a DNS name like acc.example.lan (lowercase letters, digits, dots and hyphens): $Hostname"
    }
    if ($Owner -cnotmatch '^[a-z_][a-z0-9_-]{0,25}$' -or $Owner -eq 'root') {
        $problems += "the owner must be a Linux user name other than root: lowercase letters, digits, - and _, at most 26 characters (got '$Owner')"
    }
    foreach ($cli in ($AgentClis -split '[,\s]+' | Where-Object { $_ -and $_ -ne 'none' })) {
        if ($script:AgentClis -cnotcontains $cli) {
            $problems += "unknown agent CLI '$cli' (choose from: $($script:AgentClis -join ', '))"
        }
    }
    if ($Zone -and ($Zone -cnotmatch $script:DnsName -or $Zone -notmatch '\.')) {
        $problems += "the Cloudflare zone must be a domain like example.com: $Zone"
    }
    if ($Zone -and -not $Token) { $problems += "a Cloudflare zone needs its API token (Zone:DNS:Edit on $Zone)" }
    if ($Token -and -not $Zone) { $problems += 'a Cloudflare token needs its zone' }
    if ($Token -and $Token -cnotmatch '^[A-Za-z0-9_-]{20,200}$') {
        $problems += 'that does not look like a Cloudflare API token (letters, digits, - and _)'
    }
    return , $problems
}

# "codex,cursor" or "codex cursor" or "none" -> "codex cursor" (setup.sh's ACC_AGENT_CLIS; empty for none).
function ConvertTo-AccAgentCliList([string]$AgentClis) {
    return ((@($AgentClis -split '[,\s]+' | Where-Object { $_ -and $_ -ne 'none' })) -join ' ')
}


# Presence, including an empty value, selects the Rynhult spelling.
function Get-AccEnvironmentValue([string]$Name) {
    $newName = 'RYNHULT_' + $Name.Substring(4)
    $values = [Environment]::GetEnvironmentVariables('Process')
    if ($values.Contains($newName)) { return [string]$values[$newName] }
    return [string]$values[$Name]
}
Export-ModuleMember -Function *-Acc*
