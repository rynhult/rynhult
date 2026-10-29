# Windows phase 0: owner-run qualification probe

**Experimental, not a native Rynhult installer.** These scripts exercise only
gates **1, 2 and 5** in [WINDOWS-NATIVE.md](../../docs/WINDOWS-NATIVE.md).
No Windows run has been performed as part of this implementation. A successful
install is not qualification; even three PASS results leave phase 0 incomplete.

The designated machine is called `windows-test-pc` here (an anonymized label).
Installation and removal require an owner
at an elevated console. Subsequent collection is unattended and unelevated as
the recorded owner. Never launch these through an agent, WSL session, Inbox
tool, or a live Rynhult hook.

## Before starting

- Use a reviewed, private checkout of this revision, retained until uninstall.
  Examples below assume `C:\src\rynhult`. Do not run an elevated script from a
  checkout writable by untrusted users.
- Use **64-bit Windows PowerShell 5.1**, Windows 11 x64 and fixed local NTFS
  on C:. The in-box .NET Framework `Framework64\v4.0.30319\csc.exe` must have a
  valid Authenticode signature. No SDK, package installation or download is
  needed. ARM64, Windows Server, remote filesystems and PowerShell 7 execution
  of the scripts are outside this probe.
- Read `Plan.json` and the safety sign-off comments in the sources. They mark
  the reviewed scope, **not digital signatures or independent certification**.
  Follow the machine's signing policy; do not lower execution policy or
  weaken Defender, UAC or firewall settings to run the probe.
- Installation refuses any existing `C:\RynhultSpike`, `rynhult-spike-1` or
  `RynhultSpike` service. On windows-test-pc, manually inspect and archive the old four
  read-only probe scripts and `elev-probe.out` **before** installing. They have
  no manifest and are not adopted or deleted by these scripts.
- Save work before reboot. Coordinate a sign-in-screen wait without automatic
  logon, remote logon or another person logging in. If the current setup
  automatically signs someone in, do not claim the no-login gate passed.

Optional non-provisioning checks, if Python and pytest are already available:

```powershell
Set-Location C:\src\rynhult
$env:PYTHONPATH = 'src'
python -m pytest -q --noconftest tests/test_windows_phase0.py
```

On Windows these tests also compile the host into pytest's temporary directory;
they never run it, create users or register services. A PowerShell parser check
runs when PowerShell is available, preferring Windows PowerShell 5.1.
`--noconftest` avoids importing the main application's Linux-only fixtures.
Portable source-policy checks are not proof
of ACL enforcement, correct Win32 signatures or Windows behavior.

## Exact owner procedure

1. In an ordinary, **unelevated** Windows PowerShell window, record the owner SID:

   ```powershell
   [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
   ```

2. Open **Windows PowerShell as administrator**, change to the reviewed checkout,
   and install once:

   ```powershell
   Set-Location C:\src\rynhult
   powershell.exe -NoProfile -NonInteractive -File .\scripts\windows-phase0\Install.ps1
   ```

   The default owner SID is the elevated account's SID. If elevation used
   **another administrator account**, append `-OwnerSid S-1-5-21-...` using the
   exact SID from step 1. The owner must differ from the newly created account.
   Installation writes a protected recovery manifest before provisioning.
   On error, keep this checkout and the manifest; use Uninstall rather than
   deleting the folder or retrying Install over partial resources.

3. Back in the **unelevated owner** window, collect a pre-reboot baseline:

   ```powershell
   Set-Location C:\src\rynhult
   powershell.exe -NoProfile -NonInteractive -File .\scripts\windows-phase0\Collect.ps1
   $LASTEXITCODE
   ```

   Collection waits up to 180 seconds for initial evidence; `-WaitSeconds 0`
   performs one attempt. Gate 1 must be **NOT-RUN** until a reboot after
   installation. Exit **1** at this point is expected. Gates 2 and 5 can pass.
   Collection explicitly refuses an elevated token, even on a machine whose
   UAC settings normally make elevation invisible; it never changes that policy.

4. Restart Windows using **Start > Power > Restart**. **Do not sign in**:
   wait at the Windows sign-in screen for at least **three minutes** after it
   appears, with the PC awake. The delayed automatic service must start its
   Session 0 ConPTY child and observe it for ten seconds while no interactive
   session has a user. Nobody should connect through Remote Desktop either.
   On slow machines wait longer; do not start the service manually for this gate.

5. Sign in as the recorded owner. Run the same unelevated Collect command.
   Gates **1, 2 and 5 must all say PASS**, exit code **0**. Gates 3, 4 and 6 stay
   **NOT-RUN**. To archive a machine-readable result unattended:

   ```powershell
   $archive = Join-Path $env:USERPROFILE 'Desktop\phase0-evidence'
   New-Item -ItemType Directory -Path $archive
   powershell.exe -NoProfile -NonInteractive -File .\scripts\windows-phase0\Collect.ps1 -Json > "$archive\report.json"
   $collectionExit = $LASTEXITCODE
   $collectionExit
   ```

   Pick a new archive path if it already exists. Keep the report and raw evidence
   private: they contain account SIDs, profile paths, OS details and token
   privilege names. Do not commit them. The collector does not access the DPAPI
   credential or modify the installation; only the service writes probe evidence.
   For raw-file archiving, stop the service first as shown below: a copying tool
   that denies delete-sharing can interrupt the live writer's atomic replacements.

6. Open an **elevated** Windows PowerShell window and remove the probe from the
   **same checkout/revision**, after saving any evidence you need:

   To archive raw evidence, first run these optional commands in that elevated
   window (choose a new private destination):

   ```powershell
   Stop-Service -Name RynhultSpike
   Copy-Item -LiteralPath C:\RynhultSpike\evidence -Destination "$env:USERPROFILE\Desktop\phase0-raw-evidence" -Recurse
   ```

   Then uninstall:

   ```powershell
   Set-Location C:\src\rynhult
   powershell.exe -NoProfile -NonInteractive -File .\scripts\windows-phase0\Uninstall.ps1
   ```

   Success returns JSON with `removed: true` and verifies absence of the
   service, user, profile files/registration, direct LSA rights and probe folder.
   If removal fails, keep the journal and retry only after reviewing the error.
   Close service-management windows if deletion is pending; wait for a loaded
   profile to unload. Do not force-kill by process name or delete an active
   profile. Reparse points, identity mismatches and unexpected rights require
   manual review, not a bypass.

## What the gates prove

| Gate | Evidence and required result |
|---|---|
| 1 | A new boot since installation, Session 0 service evidence and no signed-in users at the actual ConPTY observation: no `sihost.exe`, `explorer.exe` or `userinit.exe` in any non-zero session of the process snapshot (positive control: `winlogon.exe` must be visible outside Session 0), and no user in the WTS session list when the probe account may read it (on windows-test-pc it may not; the failure is recorded). `cmd.exe /c` emitted `RYNHULT_CONPTY_OK`; a long-running `PING.EXE -t 127.0.0.1` produced output and its parent was alive after ten monotonic seconds. Missing or denied session enumeration fails, never implies nobody was signed in. |
| 2 | The host token has the recorded, separate non-administrator SID; its private profile path, `USERPROFILE` and loaded registry hive agree. **Its privileges** (from `current.json`, present whether enabled or not) must not include `SeImpersonatePrivilege` or `SeCreateGlobalPrivilege` and must all be on the allowlist below, and the service's configured required privileges must be exactly `SeChangeNotifyPrivilege`. Installer grants only service logon directly and denies interactive, RDP, batch and network logon. This is not a Windows sandbox. |
| 5 | A local-only named pipe with an explicit DACL and `FILE_FLAG_FIRST_PIPE_INSTANCE` rejects a second first-instance handle with error 5. The service checks the kernel client PID and impersonated token SID, then reverts before writing. Its own SID is the positive control; the owner is deliberately allowed transport access but receives `accepted:false`. The client verifies the pipe's server PID against SCM before sending anything. This is not a full hostile-account/remote-attack test matrix. |

**Token privileges (Gate 2).** The built-in `SERVICE` group brings
`SeImpersonatePrivilege` (a known route from a service account to SYSTEM) and
`SeCreateGlobalPrivilege` into every service token. Install therefore sets the
service's required privileges (`SERVICE_CONFIG_REQUIRED_PRIVILEGES_INFO`) to
`SeChangeNotifyPrivilege` only, before the first start, so SCM removes every
other privilege from the token. Install reads the setting back and records it in
the manifest; deleting the service removes it. A privilege that is present but
disabled still fails the gate, because a process can enable its own privileges.
The allowlist, each a builtin Users default on client Windows:

| Privilege | Why it may stay |
|---|---|
| `SeChangeNotifyPrivilege` | Bypass traverse checking: the one required privilege, in every token. |
| `SeShutdownPrivilege` | Shut down or restart: availability only, no data or other identity. |
| `SeUndockPrivilege` | Undock a laptop: hardware only. |
| `SeIncreaseWorkingSetPrivilege` | Grow the process's own working set: memory only. |
| `SeTimeZonePrivilege` | The display time zone; changing the clock (`SeSystemtimePrivilege`) is not allowed. |

With the required privileges set, only `SeChangeNotifyPrivilege` is expected;
the others were observed disabled on windows-test-pc before this setting existed and are
tolerated, not needed. Agent children will still need a restricted token of
their own (not part of this probe).

**Signed-in check (Gate 1).** A session counts as signed in when it runs `sihost.exe`, `explorer.exe` or
`userinit.exe`, or when the WTS session list shows a user. On windows-test-pc the probe account may not read that list. A
user signed in with a replacement shell (Assigned Access, Shell Launcher or a custom `Shell` value) runs none of those
three processes, so if the WTS list is also unavailable, that sign-in could be missed and Gate 1 could pass. This is
qualification evidence only, not a product boundary: on a machine with a replacement shell, confirm by hand that
nobody is signed in (security review 7, SR7-10).

The pipe has no command dispatcher. Both clients request identification-only
impersonation; the service's token has no impersonation privilege. Unexpected IPC/probe errors
fail closed and produce `fatal.json` rather than continuing with a false PASS.
Fatal evidence latches failure for this installation, even across reboot;
review it and uninstall/reinstall for a new attempt rather than deleting it.
The first boot observation is retained, so logging out and restarting the
service cannot erase an earlier failed no-login observation for that boot.

Evidence is written by the test account, not an independent security monitor:
it qualifies this fixed probe and is not tamper-proof product attestation.
Provider hooks, login/refresh/resume, Codex read/write/network sandbox denial
and guard feasibility are **not tested**. No CLI authentication is read.

## Change inventory and restoration boundary

`Plan.json` is the fixed inventory; the Administrators-owned `manifest.json`
embeds its hash, owner/install/user SIDs, intent before mutations, created files
and folders, group membership, rights, profile, service and binary hash.

- A new standard user belongs to builtin Users, never Administrators. Its
  random 384-bit password suffix exists as a SecureString and briefly zeroed
  native buffers; the only script-created persisted credential is
  `secrets\password.dpapi`, protected by machine DPAPI **and** an explicit
  Administrators/SYSTEM-only ACL. Windows necessarily retains its own protected
  account/service credentials; passwords never enter command lines, JSON or logs.
- `C:\RynhultSpike` and protected files are Administrators-owned. The owner and
  service can read/traverse their designated paths but cannot rewrite the
  executable, configuration, credential or manifest. Only evidence is writable
  by the service; it keeps bounded output and at most eight boot records.
- The user profile is created and deleted through Windows by recorded SID.
  SCM loads it for service logon. An unnamed Job kills only this probe's child
  tree on shutdown/crash. The service is Automatic (Delayed Start), with no
  recovery action, scheduled task, PATH/registry-policy or firewall change.
- The service's required privileges are `SeChangeNotifyPrivilege` only
  (`serviceRequiredPrivileges` in `Plan.json`), recorded in the manifest.
- Uninstall disables the owned user, stops/deletes the identified service (and with it the required-privileges setting),
  removes direct rights, deletes its unloaded profile and user, then removes
  the folder with the manifest last. It refuses reparse trees and does not
  clear attributes on agent-writable files, avoiding outside hard-link metadata
  changes. A read-only/locked file can therefore require review rather than
  forced removal.

If power fails between creating the protected root and the first manifest,
only an empty owned folder can remain without a journal: inspect it manually.
After successful removal the manifest is gone, so rerunning Uninstall refuses
the missing journal rather than claiming another verified removal. Archives
you chose to save, optional pytest output, Windows event/audit logs,
SID-allocation history and normal OS compilation caches are not erased.
Existing machine settings and resources are not adopted, disabled or restored
speculatively.
