<#
.SYNOPSIS
Self-test of the Windows installer's checks (deploy/windows/AccWindows.psm1), without Pester: the enroll command
parser against tests/data/enroll-commands.json (shared with tests/test_node_packages.py), the .wslconfig edit and the
main ACC's answers. Exits 1 when anything fails. CI runs it in PowerShell 7 and Windows PowerShell 5.1:
    pwsh -File deploy/windows/tests/Test-EnrollParser.ps1
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
Import-Module (Join-Path $repo 'deploy\windows\AccWindows.psm1') -Force
$script:failures = 0
$script:checks = 0

function Assert-That([bool]$Condition, [string]$What) {
    $script:checks++
    if (-not $Condition) {
        $script:failures++
        Write-Output "FAIL: $What"
    }
}

# ------------------------------------------------------------------ the enroll command
$cases = Get-Content -LiteralPath (Join-Path $repo 'tests\data\enroll-commands.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Assert-That (@($cases.accepted).Count -ge 3 -and @($cases.rejected).Count -ge 10) 'the data file lists both kinds'
foreach ($case in $cases.accepted) {
    try {
        $parsed = ConvertFrom-AccEnrollCommand $case.command
        foreach ($field in 'primary', 'trust', 'fingerprint', 'code', 'name') {
            Assert-That ($parsed.$field -ceq $case.$field) "accepted '$($case.command)': $field is '$($parsed.$field)', expected '$($case.$field)'"
        }
        $arguments = ConvertTo-AccEnrollArgument $parsed
        $expected = @('enroll', '--primary', $case.primary)
        if ($case.trust -eq 'system') { $expected += @('--trust', 'system') } else { $expected += @('--ca-fingerprint', $case.fingerprint) }
        $expected += @('--code', $case.code, '--name', $case.name)
        Assert-That (($arguments -join '|') -ceq ($expected -join '|')) "acc-node arguments for '$($case.command)': $($arguments -join ' ')"
    } catch {
        Assert-That $false "accepted '$($case.command)' was rejected: $($_.Exception.Message)"
    }
}
foreach ($case in $cases.rejected) {
    $rejected = $false
    try { ConvertFrom-AccEnrollCommand $case.command | Out-Null } catch { $rejected = $true }
    Assert-That $rejected "rejected ($($case.why)) was accepted: '$($case.command)'"
}

# ------------------------------------------------------------------ .wslconfig: mirrored, everything else kept
$wslConfigCases = @(
    @{ before = ''; after = "[wsl2]`r`nnetworkingMode=mirrored`r`n" }
    @{ before = "[wsl2]`r`nmemory=8GB`r`n"; after = "[wsl2]`r`nnetworkingMode=mirrored`r`nmemory=8GB`r`n" }
    @{ before = "[wsl2]`nnetworkingMode=NAT`nmemory=8GB`n"; after = "[wsl2]`r`nnetworkingMode=mirrored`r`nmemory=8GB`r`n" }
    @{ before = "# mine`r`n[experimental]`r`nautoMemoryReclaim=gradual`r`n"
       after = "# mine`r`n[experimental]`r`nautoMemoryReclaim=gradual`r`n`r`n[wsl2]`r`nnetworkingMode=mirrored`r`n" }
    @{ before = "[experimental]`r`nnetworkingMode=nat`r`n[WSL2]`r`nswap=0`r`n"
       after = "[experimental]`r`nnetworkingMode=nat`r`n[WSL2]`r`nnetworkingMode=mirrored`r`nswap=0`r`n" }
    @{ before = "[wsl2]`r`nnetworkingMode = mirrored # set by hand`r`n"; after = "[wsl2]`r`nnetworkingMode = mirrored # set by hand`r`n" }
)
foreach ($case in $wslConfigCases) {
    $after = Get-AccWslConfigText $case.before
    Assert-That ($after -ceq $case.after) ".wslconfig from '$($case.before -replace "`r?`n", '\n')': got '$($after -replace "`r?`n", '\n')'"
    Assert-That ((Get-AccWslConfigText $after) -ceq $after) ".wslconfig edit is idempotent for '$($case.before -replace "`r?`n", '\n')'"
}

# ------------------------------------------------------------------ the main ACC's answers
$token = 'abcdefghij0123456789_-XYZ' # gitleaks:allow -- synthetic parser fixture
$good = @(
    @{ Owner = 'alex' }
    @{ Owner = 'alex'; Hostname = 'acc.example.lan'; AgentClis = 'codex,cursor' }
    @{ Owner = 'alex'; AgentClis = 'none' }
    @{ Owner = 'alex'; AgentClis = 'codex copilot antigravity cursor'; Zone = 'example.com'; Token = $token }
)
$bad = @(
    @{ Owner = '' }
    @{ Owner = 'root' }
    @{ Owner = 'Alex' }
    @{ Owner = 'a' * 27 }
    @{ Owner = 'alex'; Hostname = 'acc example.lan' }
    @{ Owner = 'alex'; Hostname = 'acc.example.lan;calc' }
    @{ Owner = 'alex'; AgentClis = 'codex,emacs' }
    @{ Owner = 'alex'; Zone = 'example.com' }
    @{ Owner = 'alex'; Token = $token }
    @{ Owner = 'alex'; Zone = 'example'; Token = $token }
    @{ Owner = 'alex'; Zone = 'example.com'; Token = 'short' }
    @{ Owner = 'alex'; Zone = 'example.com'; Token = 'has spaces in it 0123456789' }
)
foreach ($settings in $good) {
    $problems = Test-AccMainSetting @settings
    Assert-That (@($problems).Count -eq 0) "main settings $($settings | ConvertTo-Json -Compress) refused: $($problems -join '; ')"
}
foreach ($settings in $bad) {
    $problems = Test-AccMainSetting @settings
    Assert-That (@($problems).Count -gt 0) "main settings $($settings | ConvertTo-Json -Compress) accepted"
}
Assert-That ((ConvertTo-AccAgentCliList 'codex,cursor') -ceq 'codex cursor') 'agent CLI list'
Assert-That ((ConvertTo-AccAgentCliList 'none') -ceq '') 'agent CLI list: none'

Write-Output "$($script:checks) checks, $($script:failures) failed (PowerShell $($PSVersionTable.PSVersion))"
if ($script:failures -gt 0) { exit 1 }
