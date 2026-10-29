"""Portable policy tripwires, not proof of Windows API behavior or PowerShell syntax."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "scripts/windows-phase0"
SCRIPTS = ("Install.ps1", "Collect.ps1", "Uninstall.ps1", "Common.ps1")
SOURCES = (*SCRIPTS, "Native.cs", "SpikeHost.cs")


def source(name):
    path = ROOT / name
    assert path.is_file(), f"missing owner-run probe: {name}"
    return path.read_text()


@pytest.mark.parametrize("name", SOURCES)
def test_probe_has_no_download_execution_or_security_configuration_changes(name):
    code = source(name)
    assert not re.search(
        r"\b(Invoke-Expression|iex|DownloadString|WebClient|Invoke-WebRequest|Invoke-RestMethod|"
        r"Start-BitsTransfer|Set-MpPreference|Add-MpPreference|Set-ExecutionPolicy|"
        r"New-NetFirewallRule|Set-NetFirewallProfile|netsh)\b|"
        r"ConsentPromptBehaviorAdmin|EnableLUA|DisableRealtimeMonitoring", code, re.I)
    assert not re.search(r"\b(password|passwd)\s*=\s*['\"][^'\"]+['\"]", code, re.I)
    assert not re.search(r"-(AsPlainText|EncodedCommand)\b|\bpassword\s*=.*sc\.exe", code, re.I)


def test_manifest_covers_every_allowed_mutation_and_no_firewall_changes():
    assert (ROOT / "Plan.json").is_file(), "missing declarative change inventory"
    plan = json.loads((ROOT / "Plan.json").read_text())
    assert plan["schemaVersion"] == 1
    assert plan["root"] == r"C:\RynhultSpike"
    assert plan["userName"] == "rynhult-spike-1"
    assert plan["serviceName"] == "RynhultSpike"
    assert plan["serviceStart"] == "AutomaticDelayed"
    assert plan["serviceRequiredPrivileges"] == ["SeChangeNotifyPrivilege"]  # finding 3: SCM strips the rest
    assert plan["userGroups"] == ["S-1-5-32-545"]  # Localized Windows group names are not reliable.
    assert plan["grantRights"] == ["SeServiceLogonRight"]
    assert set(plan["denyRights"]) == {
        "SeDenyInteractiveLogonRight", "SeDenyRemoteInteractiveLogonRight",
        "SeDenyBatchLogonRight", "SeDenyNetworkLogonRight",
    }
    assert set(plan["folders"]) == {"bin", "secrets", "evidence"}
    assert set(plan["files"]) == {"manifest.json", "config.json", r"bin\SpikeHost.exe", r"secrets\password.dpapi"}
    assert plan["profile"] == "CreateProfile/DeleteProfile by recorded SID"
    assert plan["firewall"] == []
    assert all(not Path(path).is_absolute() and ".." not in path for path in plan["folders"] + plan["files"])
    install, common, uninstall = (source(name) for name in ("Install.ps1", "Common.ps1", "Uninstall.ps1"))
    assert "Read-SpikePlan" in install and "Read-SpikePlan" in uninstall
    for key in ("user", "rights", "profile", "service", "folders", "files", "firewall", "pending"):
        assert re.search(rf"\b{key}\s*=", common), f"runtime manifest omits {key}"
    assert "Save-SpikeManifest" in install and "Save-SpikeManifest" in uninstall
    assert "Assert-SpikeManifest" in uninstall
    assert "Assert-NoReparseTree" in uninstall


def test_credentials_stay_secure_and_never_enter_service_command_lines():
    install, native = source("Install.ps1"), source("Native.cs")
    assert "GenerateSecret" in install and "ProtectSecret" in install
    assert "Write-SpikeFile" in install and "password.dpapi" in install
    assert "WriteNewFile" in native and "ref attributes, 1," in native
    assert "ProtectedData.Protect" in native and "DataProtectionScope.LocalMachine" in native
    assert "SecureStringToGlobalAllocUnicode" in native and "ZeroFreeGlobalAllocUnicode" in native
    assert not re.search(r"(Write-Host|Write-Output|Out-File|Set-Content).*\$(secret|password)\b", install, re.I)
    assert "CreateServiceW" in native and "IntPtr secret" in native
    assert not re.search(r"\b(sc|net)\.exe\b", install, re.I)


def test_pipe_authenticates_kernel_identity_and_reverts_before_reply():
    host, native = source("SpikeHost.cs"), source("Native.cs")
    assert "FILE_FLAG_FIRST_PIPE_INSTANCE" in host
    assert "PIPE_REJECT_REMOTE_CLIENTS" in host
    assert "ConvertStringSecurityDescriptorToSecurityDescriptor" in native
    assert "GetNamedPipeClientProcessId" in host and "ImpersonateNamedPipeClient" in host
    identity = host.index("ImpersonateNamedPipeClient")
    revert = host.index("RevertToSelf", identity)
    reply = host.index("WriteReply", revert)
    assert identity < revert < reply
    assert "Environment.FailFast" in host
    assert "TokenImpersonationLevel.Identification" in native
    assert "GetNamedPipeServerProcessId" in native
    assert "ServiceProcessId" in source("Collect.ps1")
    assert "config.AgentSid" in host and "clientSid ==" in host


def test_service_child_is_suspended_until_job_assignment_and_logs_boot_evidence():
    host = source("SpikeHost.cs")
    assert "CreatePseudoConsole" in host and "PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE" in host
    assert "CREATE_SUSPENDED" in host and "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in host
    assert host.index("AssignProcessToJobObject(job, child.hProcess)") < host.index("ResumeThread(child.hThread)")
    assert "WTSQuerySessionInformation" in host and "LastBootUpTime" in host
    assert "Registry.Users.OpenSubKey" in host
    assert "RYNHULT_CONPTY_OK" in host and "-t 127.0.0.1" in host
    assert "65536" in host  # Output is bounded even while ping runs unattended.


def test_owner_collection_and_admin_uninstall_do_not_claim_other_gates():
    collect, uninstall = source("Collect.ps1"), source("Uninstall.ps1")
    assert "Assert-SpikeOwner" in collect
    assert not re.search(r"\b(New-LocalUser|Start-Service|Stop-Service|Remove-Item|Set-Acl)\b", collect)
    assert "NOT-RUN" in collect and all(f"gate = {gate}" in collect for gate in (3, 4, 6))
    for operation in ("Stop-Service", "RemoveService", "RemoveRights", "DeleteProfile", "Remove-LocalUser"):
        assert operation in uninstall
    assert "Get-SpikeUser" in uninstall and "Get-SpikeService" in uninstall
    assert "Get-LocalUser" in source("Common.ps1")
    assert "$sidUser = Get-SpikeUser $plan $manifest.user.sid" in uninstall
    assert "(Get-SpikeUser $plan $sid)" in uninstall
    assert "GetRights" in uninstall and "Get-SpikeProfile" in uninstall
    assert re.search(r"Test-Path.*\$plan\.root", uninstall)
    for name in ("Install.ps1", "Uninstall.ps1"):
        code = source(name)
        assert "#Requires -RunAsAdministrator" in code
        assert "#Requires -Version 5.1" in code
        assert "Safety sign-off:" in code


def test_boot_pass_requires_successful_session_query_and_monotonic_child_observation():
    host, collect = source("SpikeHost.cs"), source("Collect.ps1")
    assert "Stopwatch" in host and "console.ObservedSeconds" in host
    assert "sessionQuerySucceeded = sessionError == null" in host
    assert "$boot.sessionQuerySucceeded -eq $true" in collect
    assert "$null -ne $boot.sessions" in collect
    assert "$boot.bootUtc" in collect and "$boot.observedSeconds -ge 10" in collect
    assert "$live = -not $fatal -and" in collect


POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


@pytest.mark.skipif(not POWERSHELL, reason="no PowerShell parser installed; native syntax remains unverified")
def test_powershell_parser_accepts_all_probe_scripts():
    paths = ",".join("'" + str(ROOT / name).replace("'", "''") + "'" for name in SCRIPTS)
    command = (
        f"$paths=@({paths}); $ErrorActionPreference='Stop'; $failed=$false; "
        "foreach($path in $paths) { $tokens=$null; $errors=$null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors); "
        "foreach($error in $errors) { Write-Output $error; $failed=$true } }; if($failed) { exit 1 }"
    )
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows in-box .NET Framework compiler")
def test_inbox_compiler_accepts_probe_sources_without_provisioning(tmp_path):
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    assert compiler.is_file(), "Windows qualification requires the in-box x64 compiler"
    binary = tmp_path / "SpikeHost.exe"
    result = subprocess.run(
        [str(compiler), "/nologo", "/target:exe", "/platform:x64", f"/out:{binary}",
         "/reference:System.Security.dll", "/reference:System.ServiceProcess.dll",
         "/reference:System.Web.Extensions.dll", "/reference:System.Management.dll",
         str(ROOT / "Native.cs"), str(ROOT / "SpikeHost.cs")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert binary.is_file()


def test_no_null_passed_to_dotnet_string_parameters():
    """PowerShell turns $null into "" for .NET string parameters; File.Replace then fails at runtime."""
    import re
    from pathlib import Path
    for script in (Path(__file__).resolve().parents[1] / 'scripts/windows-phase0').glob('*.ps1'):
        for number, line in enumerate(script.read_text().splitlines(), 1):
            assert not re.search(r'\[IO\.\w+\]::\w+\([^)]*\$null\)', line), f'{script.name}:{number} passes $null to a .NET method; use [NullString]::Value'


def test_local_user_description_fits_windows_limit():
    """New-LocalUser -Description accepts at most 48 characters; the marker is prefix + a 36-character GUID."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'scripts/windows-phase0'
    prefixes = {re.search(r"\$marker = '([^']*)' \+ \$manifest\.installId", (root / name).read_text()).group(1)
                for name in ('Install.ps1', 'Uninstall.ps1')}
    assert len(prefixes) == 1, prefixes
    assert len(prefixes.pop()) + 36 <= 48


def test_spike_host_avoids_service_process_ambiguous_names():
    """System.ServiceProcess also defines TimeoutException; csc rejects the bare name (CS0104)."""
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'scripts/windows-phase0/SpikeHost.cs').read_text()
    assert 'using System.ServiceProcess;' in source
    assert not re.search(r'(?<![.\w])TimeoutException\b', source)


def test_session_check_decides_by_state_and_fails_closed():
    """windows-test-pc's boot record failed the name query ('No more data is available'); the state now decides."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'scripts/windows-phase0/SpikeHost.cs').read_text()
    assert 'bool noUserState = session.State == 1 || (session.State >= 6 && session.State <= 9);' in source
    assert 'hasUser = !noUserState || !String.IsNullOrEmpty(userName)' in source
    assert 'throw new Win32Exception(); // name' not in source


def test_signed_in_check_uses_process_snapshot_with_positive_control():
    """windows-test-pc: WTSEnumerateSessionsW fails for the probe account; the process snapshot decides, failing closed."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'scripts/windows-phase0/SpikeHost.cs').read_text()
    assert 'if (winlogon.Count == 0) throw new Win32Exception(1168' in source
    assert 'name == "sihost" || name == "explorer" || name == "userinit"' in source
    assert 'source = "wts", wtsSessions = wts, wtsError = wtsError, hasUser = wtsUser' in source


# Finding 3 (docs/WINDOWS-NATIVE.md): the SERVICE group brings SeImpersonatePrivilege and SeCreateGlobalPrivilege.
FORBIDDEN_PRIVILEGES = {"SeImpersonatePrivilege", "SeCreateGlobalPrivilege"}
ALLOWED_PRIVILEGES = {"SeChangeNotifyPrivilege", "SeShutdownPrivilege", "SeUndockPrivilege",
                      "SeIncreaseWorkingSetPrivilege", "SeTimeZonePrivilege"}


def test_install_requires_only_change_notify_before_the_first_start():
    install, common, uninstall = source("Install.ps1"), source("Common.ps1"), source("Uninstall.ps1")
    order = [install.index(step) for step in (
        "[Rynhult.Spike.Native]::CreateService(", "$manifest.pending = 'service-privileges'",
        "[Rynhult.Spike.Native]::RequireOnlyPrivileges($plan.serviceName, $required)",
        "[Rynhult.Spike.Native]::RequiredPrivileges($plan.serviceName)",
        "$manifest.service.requiredPrivileges = $required", "Start-Service -Name $plan.serviceName")]
    assert order == sorted(order), "privileges must be set, read back and journalled before the first start"
    assert "Compare-Object $required" not in install  # PowerShell 5.1 can't bind an empty array to it
    assert "(@($plan.serviceRequiredPrivileges) -join ',') -cne 'SeChangeNotifyPrivilege'" in common
    assert "service = [pscustomobject]@{ created = $false; requiredPrivileges = @() }" in common
    assert "if ($privilege -cnotin @($Plan.serviceRequiredPrivileges))" in common
    removed = uninstall.index("[Rynhult.Spike.Native]::RemoveService($plan.serviceName)")
    assert uninstall.index("$manifest.service.requiredPrivileges = @()") > removed


def test_native_sets_required_privileges_as_a_multistring():
    native = source("Native.cs")
    assert 'EntryPoint = "ChangeServiceConfig2W", SetLastError = true)]\n' \
           "        static extern bool SetRequiredPrivileges(IntPtr service, uint level, ref RequiredPrivilegesInfo value);" in native
    assert "struct RequiredPrivilegesInfo { public IntPtr Names; }" in native
    assert "SetRequiredPrivileges(service, 6, ref info)" in native  # SERVICE_CONFIG_REQUIRED_PRIVILEGES_INFO
    assert 'String.Join("\\0", privileges) + "\\0\\0"' in native  # NUL-separated, double-NUL terminated
    assert "OpenServiceW(manager, name, 2); // SERVICE_CHANGE_CONFIG" in native
    assert "QueryServiceConfig(service, 6, buffer, needed, out needed)" in native
    assert "OpenServiceW(manager, name, 1); // SERVICE_QUERY_CONFIG" in native
    assert "Marshal.FreeHGlobal(names)" in native and "Marshal.FreeHGlobal(buffer)" in native


def test_native_declares_no_names_that_clash_with_system_service_process():
    """Native.cs is compiled with SpikeHost.cs, which imports System.ServiceProcess (CS0104 on real Windows)."""
    native = source("Native.cs")
    assert "using System.ServiceProcess;" not in native
    declared = set(re.findall(r"\b(?:struct|class|enum)\s+(\w+)", native))
    clashes = {"ServiceBase", "ServiceController", "ServiceControllerStatus", "ServiceType", "ServiceStartMode",
               "ServiceAccount", "SessionChangeDescription", "SessionChangeReason", "PowerBroadcastStatus",
               "TimeoutException", "ServiceProcessDescriptionAttribute", "ServiceControllerPermission"}
    assert not declared & clashes


def collect_list(code, name):
    match = re.search(rf"\${name} = @\((.*?)\)\s*(?:#[^\n]*)?\n", code, re.S)
    assert match, f"{name} not found"
    return match.group(1)


def test_gate_2_fails_on_impersonation_global_objects_and_unlisted_privileges():
    collect = source("Collect.ps1")
    forbidden = set(re.findall(r"'(Se\w+)'", collect_list(collect, "forbiddenPrivileges")))
    assert forbidden == FORBIDDEN_PRIVILEGES
    allowed_block = collect_list(collect, "allowedPrivileges")
    allowed = re.findall(r"^\s*'(Se\w+)'\s*#\s*(\S.*)$", allowed_block, re.M)
    assert {name for name, _ in allowed} == ALLOWED_PRIVILEGES, "every allowed privilege needs a reason"
    assert len(allowed) == len(re.findall(r"'Se\w+'", allowed_block))
    assert not FORBIDDEN_PRIVILEGES & ALLOWED_PRIVILEGES
    # Present is enough to fail: a process can enable its own privileges, so "disabled" is no boundary.
    assert "$heldPrivileges = @($current.tokenPrivileges | ForEach-Object { [string]$_.name })" in collect
    assert ".enabled" not in collect
    assert "$forbiddenHeld = @($heldPrivileges | Where-Object { $_ -cin $forbiddenPrivileges })" in collect
    assert "$unexpectedHeld = @($heldPrivileges | Where-Object { $_ -cnotin $allowedPrivileges })" in collect
    assert ("$privilegesPassed = $heldPrivileges.Count -gt 0 -and $forbiddenHeld.Count -eq 0 -and "
            "$unexpectedHeld.Count -eq 0 -and\n    $configuredPassed") in collect
    assert "[Rynhult.Spike.Native]::RequiredPrivileges($plan.serviceName)" in collect
    assert "status = $(if ($identityPassed -and $privilegesPassed) { 'PASS' } else { 'FAIL' })" in collect
    assert "tokenPrivileges = $heldPrivileges" in collect  # in the report, for the owner's archive


def test_readme_lists_the_same_privilege_allowlist():
    readme = source("README.md")
    rows = set(re.findall(r"^\| `(Se\w+)` \| \S", readme, re.M))
    assert rows == ALLOWED_PRIVILEGES
    assert "SeImpersonatePrivilege" in readme and "SeCreateGlobalPrivilege" in readme


@pytest.mark.parametrize("name", SCRIPTS)
def test_powershell_scripts_keep_crlf_line_endings(name):
    data = (ROOT / name).read_bytes()
    assert b"\r\n" in data and not re.search(rb"(?<!\r)\n", data), f"{name} must use CRLF only"
