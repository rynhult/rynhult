# Native boundary checks. Run on a disposable Windows runner in both PowerShell versions.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot '..\AccWindows.psm1') -Force
foreach ($text in @('Bearer sample-access-token', 'token=sample-api-token', 'https://alex:sample-password@example.com/repo', 'Authorization: Basic sample-credential', '{"api_key": "sample-key"}')) {
    $safe = Protect-AccLogText $text
    if ($safe -match 'sample-' -or $safe -notmatch '\[redacted\]') { throw 'credential reached diagnostic output' }
}
# An existing user-writable location must be rejected, not chmodded or opened by an elevated append.
$unsafe = Join-Path ([IO.Path]::GetTempPath()) ('acc-security-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $unsafe | Out-Null
try {
    $identity = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl = Get-Acl -LiteralPath $unsafe
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $unsafe -AclObject $acl
    $refused = $false
    try { Initialize-AccRun -LogPath (Join-Path $unsafe 'unsafe.log') } catch { $refused = $true }
    if (-not $refused -or (Test-Path -LiteralPath (Join-Path $unsafe 'unsafe.log'))) { throw 'unsafe log location accepted' }
} finally {
    Remove-Item -LiteralPath $unsafe -Force -Recurse
}
$viewer = & (Get-Module AccWindows) {
    function script:Start-Process { param($FilePath, $ArgumentList) Write-Output $FilePath; Write-Verbose ($ArgumentList -join ' ') }
    Open-AccLog (Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'ACC-security-example.log')
}
$expected = Join-Path ([Environment]::GetFolderPath('System')) 'notepad.exe'
if ($viewer -ne $expected) { throw 'log viewer did not use the fixed system executable' }
$names = & (Get-Module AccWindows) {
    function script:wsl.exe { $global:LASTEXITCODE = 0; Write-Output 'unsafe-lookup-shadow' }
    Get-AccWslDistro
}
if ($names -contains 'unsafe-lookup-shadow') { throw 'WSL discovery used a command lookup shadow' }
Write-Output 'Installer security checks passed'
