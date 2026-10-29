# Native WSL is mocked inside the module; no Windows features or distributions change.
# Run with either Windows PowerShell 5.1 or pwsh, without Pester.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot '../AccWindows.psm1') -Force
$module = Get-Module AccWindows
& $module {
    function script:Get-AccWslVerboseList { return 'Ubuntu-24.04 Stopped 2' }
    function script:Test-AccWslDistro { return $script:Registered }
    function script:Invoke-AccNative {
        param($FilePath, $Arguments)
        $call = "$FilePath $($Arguments -join ' ')"
        $script:Calls.Add($call)
        if ($script:Replies.Count -eq 0) { throw "unexpected command: $call" }
        $reply = $script:Replies.Dequeue()
        $script:NativeOutput = $reply.text
        if ($reply.register) { $script:Registered = $true }
        return $reply.code
    }
    function script:Test-AccWslAvailable { return $script:Available }
    $cases = @(
        @{ name='installed'; replies=@(); registered=$true; want='ready'; calls=@() }
        @{ name='normal distro install'; replies=@(@{code=0;text='Default Version: 2'}, @{code=0;text='Installed';register=$true}); want='ready'; calls=@('wsl.exe --status','wsl.exe --install -d Ubuntu-24.04 --no-launch') }
        @{ name='stub bootstrap'; replies=@(@{code=1;text='The Windows Subsystem for Linux is not installed.'}, @{code=0;text='Installed'}, @{code=0;text='Installed';register=$true}); want='ready'; calls=@('wsl.exe --status','wsl.exe --install --no-distribution','wsl.exe --install -d Ubuntu-24.04 --no-launch') }
        @{ name='bootstrap reboot'; replies=@(@{code=1;text='not installed'}, @{code=3010;text='Restart required'}); want='restart'; calls=@('wsl.exe --status','wsl.exe --install --no-distribution') }
        @{ name='zero exit reboot'; replies=@(@{code=1;text='not installed'}, @{code=0;text='The requested operation is successful. Changes will not be effective until the system is rebooted.'}); want='restart'; calls=@('wsl.exe --status','wsl.exe --install --no-distribution') }
        @{ name='legacy fallback'; replies=@(@{code=1;text='not installed'}, @{code=1;text='Invalid command line option: --no-distribution'}, @{code=0;text='enabled'}, @{code=3010;text='enabled'}); want='restart'; calls=@('wsl.exe --status','wsl.exe --install --no-distribution','dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart','dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart') }
        @{ name='legacy help exit zero'; replies=@(@{code=1;text='not installed'}, @{code=0;text='Usage: wsl.exe [options]'}, @{code=0;text='enabled'}, @{code=0;text='enabled'}); want='restart'; calls=@('wsl.exe --status','wsl.exe --install --no-distribution','dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart','dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart') }
        @{ name='missing executable'; available=$false; replies=@(@{code=0;text='enabled'}, @{code=0;text='enabled'}); want='restart'; calls=@('dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart','dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart') }
        @{ name='DISM failure'; available=$false; replies=@(@{code=5;text='Access denied'}); want='error'; calls=@('dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart') }
        @{ name='resume installs distro'; resume=$true; replies=@(@{code=0;text='Default Version: 2'}, @{code=0;text='Installed';register=$true}); want='ready'; calls=@('wsl.exe --status','wsl.exe --install -d Ubuntu-24.04 --no-launch') }
        @{ name='distro download failure'; replies=@(@{code=0;text='Default Version: 2'}, @{code=1;text='Download failed'}); want='error'; calls=@('wsl.exe --status','wsl.exe --install -d Ubuntu-24.04 --no-launch') }
        @{ name='download failure'; replies=@(@{code=1;text='not installed'}, @{code=1;text='Download failed'}); want='error'; calls=@('wsl.exe --status','wsl.exe --install --no-distribution') }
        @{ name='combined install reboot'; replies=@(@{code=0;text='Default Version: 2'}, @{code=3010;text='Installed';register=$true}); want='restart'; calls=@('wsl.exe --status','wsl.exe --install -d Ubuntu-24.04 --no-launch') }
    )
    foreach ($case in $cases) {
        Initialize-AccRun
        $script:Available = -not ($case.ContainsKey('available') -and -not $case.available)
        $resume = $case.ContainsKey('resume') -and $case.resume
        $script:Registered = ($case.ContainsKey('registered') -and $case.registered)
        $script:Calls = New-Object 'System.Collections.Generic.List[string]'
        $script:Replies = New-Object System.Collections.Queue
        foreach ($reply in $case.replies) {
            if (-not $reply.ContainsKey('register')) { $reply.register=$false }
            $script:Replies.Enqueue($reply)
        }
        try { $got = Install-AccWslDistro -AfterRestart:$resume } catch { $got = 'error' }
        if ($got -ne $case.want -or ($script:Calls -join '|') -ne ($case.calls -join '|')) {
            throw "$($case.name): expected $($case.want), got $got; commands: $script:Calls"
        }
        Write-Output "ok: $($case.name)"
    }
}

# Logging is shared by both entry points and must also redact echoed native output.
$log = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) ('ACC-log-test-' + [guid]::NewGuid() + '\test.log')
$originalToken = $env:CF_API_TOKEN
try {
    Initialize-AccRun -LogPath $log
    Write-AccLog 'rejected --code 3fa21c-0b9d4e-77aa01-c3d2e1'
    $text = Get-Content -LiteralPath $log -Raw
    if ($text -match '3fa21c' -or $text -notmatch '\[redacted\]') { throw 'enrollment code leaked' }
    $env:CF_API_TOKEN = 'log-test-start_3fa21c-0b9d4e-77aa01-c3d2e1_log-test-end' # gitleaks:allow -- synthetic redaction fixture
    Write-AccLog "native failure: $env:CF_API_TOKEN"
    $text = Get-Content -LiteralPath $log -Raw
    if ($text -match 'log-test-start|log-test-end') { throw 'Cloudflare token partially leaked' }
    foreach ($i in 1..30) { Write-AccLog "line $i" }
    $context = (Write-AccFailureContext 6>&1 | Out-String)
    if ($context -notmatch [regex]::Escape($log) -or $context -match 'line 1\r?\n' -or $context -notmatch 'line 30') {
        throw 'failure context must include recent lines and full log path, bounded to 12 lines'
    }
} finally {
    $env:CF_API_TOKEN = $originalToken
    Remove-Item -LiteralPath (Split-Path -Parent $log) -Recurse -Force -ErrorAction SilentlyContinue
}
