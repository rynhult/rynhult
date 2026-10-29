; Rynhult-Setup-<version>.exe: installs Rynhult on Windows 11, inside WSL2 (Ubuntu 24.04 with systemd), as the main Rynhult or
; as an additional machine for a Rynhult you already have. Built by .github/workflows/installers-windows.yml with
; Inno Setup 6.3 or later (unsigned for now):
;   ISCC.exe deploy\windows\acc.iss          -> deploy\windows\Output\Rynhult-Setup-<version>.exe
; The wizard only collects answers; deploy/windows/install-acc.ps1 does the work, and checks them again. Everything a
; person types reaches it as environment variables, never on a command line.
;
; Silent installs (CI, scripted): /VERYSILENT /SUPPRESSMSGBOXES plus
;   /ROLE=main|node  /HOSTNAME=...  /OWNER=...  /CLIS=codex,copilot,antigravity,cursor,opencode|none  /ZONE=...
;   ACC_ENROLL_COMMAND in the environment for node enrollment; /DRYRUN (install-acc.ps1 -DryRun: prints every step, changes nothing in Windows
;   or WSL; the files still go to /DIR). The Cloudflare token comes from the CF_API_TOKEN environment variable.
; Silent uninstall: unins000.exe /VERYSILENT [/REMOVEDISTRO] [/DRYRUN].

#if Ver < EncodeVer(6, 3, 0)
  #error Inno Setup 6.3 or later is needed (ExecAndLogOutput, x64compatible)
#endif

; The version is the one in pyproject.toml (its first line starting with version = "...").
#ifndef AppVersion
  #define AppVersion ""
  #define VersionLine ""
  #define VersionLoop 0
  #define PyProject AddBackslash(SourcePath) + "..\..\pyproject.toml"
  #define VersionFile FileOpen(PyProject)
  #if !VersionFile
    #error cannot read pyproject.toml
  #endif
  #sub ReadVersionLine
    #expr VersionLine = FileRead(VersionFile)
    #if Copy(VersionLine, 1, 11) == 'version = "'
      #expr AppVersion = Copy(VersionLine, 12, Len(VersionLine) - 12)
    #endif
  #endsub
  #for {VersionLoop = 0; AppVersion == "" && !FileEof(VersionFile); VersionLoop++} ReadVersionLine
  #expr FileClose(VersionFile)
  #if AppVersion == ""
    #error no version = "..." line in pyproject.toml
  #endif
#endif

; What a release leaves out: git, virtualenvs, node_modules, data, caches, secrets, build output.
#define Excludes ".git,.venv,node_modules,\data,__pycache__,.pytest_cache,.ruff_cache,.mypy_cache,*.pyc,.env,\.acc,\.claude,\src\acc\server\static,\web\dist,\deploy\windows\Output"

[Setup]
AppId={{6A0C3E52-9D0B-4C51-9B2E-ACC0000000A1}
AppName=Rynhult
AppVersion={#AppVersion}
AppVerName=Rynhult {#AppVersion}
AppPublisher=Rynhult contributors
AppPublisherURL=https://rynhult.dev
AppSupportURL=https://github.com/rynhult/rynhult
AppUpdatesURL=https://github.com/rynhult/rynhult/releases
DefaultDirName={autopf}\ACC
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible or arm64
ArchitecturesInstallIn64BitMode=x64compatible or arm64
LicenseFile=..\..\LICENSE
OutputDir=Output
OutputBaseFilename=Rynhult-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayName=Rynhult {#AppVersion}
CloseApplications=no

[Files]
; Taken into {tmp} before installing, so the wizard can check answers with the same code that installs.
Source: "install-acc.ps1"; Flags: dontcopy
Source: "AccWindows.psm1"; Flags: dontcopy
; The Rynhult source tree, as a release ships it.
Source: "..\..\*"; DestDir: "{app}\src"; Excludes: "{#Excludes}"; Flags: recursesubdirs createallsubdirs ignoreversion

[UninstallDelete]
Type: filesandordirs; Name: "{app}\src"

[Code]
const
  RestartExitCode = 3010;
  Distro = 'Ubuntu-24.04';

var
  RolePage: TInputOptionWizardPage;
  MainPage: TInputQueryWizardPage;
  CliPage: TInputOptionWizardPage;
  NodePage: TInputQueryWizardPage;
  ProgressPage: TOutputProgressWizardPage;
  ScriptRan: Boolean;
  ScriptResult: Integer;
  CurrentStep: String;
  RemoveDistro: Boolean;
  RecentOutput: String;
  OpenLogButton: TNewButton;

function SetEnvironmentVariable(lpName: String; lpValue: String): BOOL;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

function HasSwitch(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

function IsDryRun: Boolean;
begin
  Result := HasSwitch('/DRYRUN');
end;

function PowerShellExe: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function LogFile(const Name: String): String;
begin
  Result := ExpandConstant('{autopf}\ACC\logs\') + Name;
end;

procedure OpenInstallLog(Sender: TObject);
var
  Code: Integer;
begin
  Exec(PowerShellExe, '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\src\deploy\windows\install-acc.ps1') + '" -OpenLog -LogPath "' +
    LogFile('install-acc.log') + '"', '', SW_HIDE, ewNoWait, Code);
end;

procedure ShowInstallFailure(const Message: String);
var
  Form: TSetupForm;
  Details: TNewMemo;
  OpenButton, CloseButton: TNewButton;
begin
  if WizardSilent then
    Exit;
#if Ver >= EncodeVer(6, 6, 0)
  Form := CreateCustomForm(ScaleX(560), ScaleY(340), False, False);
#else
  Form := CreateCustomForm;
  Form.ClientWidth := ScaleX(560);
  Form.ClientHeight := ScaleY(340);
#endif
  try
    Form.Caption := 'Rynhult Setup failed';
    Details := TNewMemo.Create(Form);
    Details.Parent := Form;
    Details.SetBounds(ScaleX(12), ScaleY(12), Form.ClientWidth - ScaleX(24), Form.ClientHeight - ScaleY(62));
    Details.ReadOnly := True;
    Details.ScrollBars := ssVertical;
    Details.Text := Message + #13#10#13#10 + RecentOutput + #13#10 + 'Full log: ' + LogFile('install-acc.log');
    OpenButton := TNewButton.Create(Form);
    OpenButton.Parent := Form;
    OpenButton.SetBounds(ScaleX(12), Form.ClientHeight - ScaleY(38), ScaleX(100), ScaleY(25));
    OpenButton.Caption := 'Open log';
    OpenButton.OnClick := @OpenInstallLog;
    CloseButton := TNewButton.Create(Form);
    CloseButton.Parent := Form;
    CloseButton.SetBounds(Form.ClientWidth - ScaleX(112), Form.ClientHeight - ScaleY(38), ScaleX(100), ScaleY(25));
    CloseButton.Caption := 'Close';
    CloseButton.ModalResult := mrOk;
    CloseButton.Default := True;
    CloseButton.Cancel := True;
    Form.ShowModal;
  finally
    Form.Free;
  end;
end;

function ScriptCommand(const Script, Arguments: String): String;
begin
  Result := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script + '" ' + Arguments;
  if IsDryRun then
    Result := Result + ' -DryRun';
end;

function IsMainRole: Boolean;
begin
  Result := RolePage.SelectedValueIndex = 0;
end;

function RoleName: String;
begin
  if IsMainRole then
    Result := 'Main'
  else
    Result := 'Node';
end;

function CliName(I: Integer): String;
begin
  case I of
    0: Result := 'codex';
    1: Result := 'copilot';
    2: Result := 'antigravity';
    3: Result := 'cursor';
  else
    Result := 'opencode';
  end;
end;

function SelectedClis: String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to 4 do
    if CliPage.Values[I] then
    begin
      if Result <> '' then
        Result := Result + ',';
      Result := Result + CliName(I);
    end;
  if Result = '' then
    Result := 'none';
end;

// The Windows user name as a Linux one, the way install-acc.ps1 makes it.
function DefaultOwner: String;
var
  I: Integer;
  Name: String;
  C: Char;
begin
  Result := '';
  Name := Lowercase(GetUserNameString);
  for I := 1 to Length(Name) do
  begin
    C := Name[I];
    if ((C >= 'a') and (C <= 'z')) or ((C >= '0') and (C <= '9')) or (C = '_') or (C = '-') then
      Result := Result + C
    else
      Result := Result + '-';
  end;
end;

// The answers go to install-acc.ps1 in its environment (inherited by the PowerShell this starts), never on its
// command line. In a silent install the token is already in the environment.
procedure ExportAnswers;
begin
  SetEnvironmentVariable('ACC_HOSTNAME', Trim(MainPage.Values[0]));
  SetEnvironmentVariable('ACC_OWNER', Trim(MainPage.Values[1]));
  SetEnvironmentVariable('ACC_ZONE', Trim(MainPage.Values[2]));
  if Trim(MainPage.Values[3]) <> '' then
    SetEnvironmentVariable('CF_API_TOKEN', Trim(MainPage.Values[3]));
  SetEnvironmentVariable('ACC_AGENT_CLIS', SelectedClis);
  SetEnvironmentVariable('ACC_ENROLL_COMMAND', Trim(NodePage.Values[0]));
end;

procedure InitializeWizard;
var
  Clis: String;
begin
  RolePage := CreateInputOptionPage(wpLicense, 'What this PC will be',
    'Rynhult runs on this PC inside WSL (Ubuntu 24.04).', 'Choose one:', True, False);
  RolePage.Add('Main Rynhult: the dashboard and agent sessions run on this PC');
  RolePage.Add('Additional machine: this PC runs sessions for a Rynhult you already have');
  if CompareText(ExpandConstant('{param:ROLE|main}'), 'node') = 0 then
    RolePage.SelectedValueIndex := 1
  else
    RolePage.SelectedValueIndex := 0;

  MainPage := CreateInputQueryPage(RolePage.ID, 'Main Rynhult', 'The questions deploy/setup.sh asks.',
    'Leave a box empty for its default. Without a Cloudflare zone, Rynhult uses its own certificate, which your ' +
    'devices are asked to trust.');
  MainPage.Add('Name people open Rynhult at, e.g. acc.example.lan (empty: this PC''s name):', False);
  MainPage.Add('Owner: the Linux user in WSL who owns Rynhult''s projects:', False);
  MainPage.Add('Cloudflare DNS zone for HTTPS and published sites (optional):', False);
  MainPage.Add('Cloudflare API token with Zone:DNS:Edit on that zone (only with a zone):', True);
  MainPage.Values[0] := ExpandConstant('{param:HOSTNAME|}');
  MainPage.Values[1] := ExpandConstant('{param:OWNER|' + DefaultOwner + '}');
  MainPage.Values[2] := ExpandConstant('{param:ZONE|}');

  CliPage := CreateInputOptionPage(MainPage.ID, 'Agent CLIs',
    'Which agent CLIs should Rynhult install besides Claude Code?', 'Claude Code is always installed.', False, False);
  CliPage.Add('Codex');
  CliPage.Add('GitHub Copilot CLI');
  CliPage.Add('Antigravity');
  CliPage.Add('Cursor CLI');
  CliPage.Add('OpenCode');
  Clis := ',' + Lowercase(ExpandConstant('{param:CLIS|codex}')) + ',';
  CliPage.Values[0] := Pos(',codex,', Clis) > 0;
  CliPage.Values[1] := Pos(',copilot,', Clis) > 0;
  CliPage.Values[2] := Pos(',antigravity,', Clis) > 0;
  CliPage.Values[3] := Pos(',cursor,', Clis) > 0;
  CliPage.Values[4] := Pos(',opencode,', Clis) > 0;

  NodePage := CreateInputQueryPage(CliPage.ID, 'Additional machine', 'Connect this PC to your Rynhult.',
    'On your Rynhult, open Host → Machines → Add machine, name this PC and copy the command it shows. ' +
    'Its code works for 10 minutes. Setup checks the command and uses only its values: it never runs it as given.');
  NodePage.Add('Paste the command from Host → Machines → Add machine:', False);
  // Inno logs its command line: secrets must only arrive in the environment.
  NodePage.Values[0] := GetEnv('ACC_ENROLL_COMMAND');

  OpenLogButton := TNewButton.Create(WizardForm);
  OpenLogButton.Parent := WizardForm;
  OpenLogButton.SetBounds(ScaleX(12), WizardForm.CancelButton.Top, ScaleX(100), WizardForm.CancelButton.Height);
  OpenLogButton.Caption := 'Open log';
  OpenLogButton.OnClick := @OpenInstallLog;
  OpenLogButton.Visible := False;

  ProgressPage := CreateOutputProgressPage('Installing Rynhult in WSL',
    'This takes a while (10 to 30 minutes on a new PC). The full log: ' + LogFile('install-acc.log'));
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  if (PageID = MainPage.ID) or (PageID = CliPage.ID) then
    Result := not IsMainRole
  else if PageID = NodePage.ID then
    Result := IsMainRole
  else
    Result := False;
end;

// The same checks install-acc.ps1 makes before it changes anything (-Validate), so the wizard can say what is wrong.
function AnswersAreValid: Boolean;
var
  Output: TExecOutput;
  Code, I: Integer;
  Message: String;
begin
  Result := False;
  ExtractTemporaryFile('install-acc.ps1');
  ExtractTemporaryFile('AccWindows.psm1');
  ExportAnswers;
  try
    if not ExecAndCaptureOutput(PowerShellExe, ScriptCommand(ExpandConstant('{tmp}\install-acc.ps1'),
      '-Validate -Role ' + RoleName + ' -LogPath "' + LogFile('install-acc.log') + '"'), '', SW_HIDE, ewWaitUntilTerminated, Code, Output) then
    begin
      ShowInstallFailure('Could not start PowerShell to check the answers: ' + SysErrorMessage(Code));
      Exit;
    end;
  except
    ShowInstallFailure('Could not check the answers: ' + GetExceptionMessage);
    Exit;
  end;
  if Code = 0 then
  begin
    Result := True;
    Exit;
  end;
  Message := '';
  for I := 0 to GetArrayLength(Output.StdOut) - 1 do
    Message := Message + Output.StdOut[I] + #13#10;
  for I := 0 to GetArrayLength(Output.StdErr) - 1 do
    Message := Message + Output.StdErr[I] + #13#10;
  ShowInstallFailure(Trim(Message));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if WizardSilent then
    Exit;  // install-acc.ps1 checks them and writes what it refused to the log
  if (CurPageID = MainPage.ID) or (CurPageID = NodePage.ID) then
    Result := AnswersAreValid;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
begin
  if IsMainRole then
  begin
    Result := 'Main Rynhult, in WSL (' + Distro + ')' + NewLine;
    if Trim(MainPage.Values[0]) <> '' then
      Result := Result + Space + 'Name: ' + Trim(MainPage.Values[0]) + NewLine;
    Result := Result + Space + 'Owner: ' + Trim(MainPage.Values[1]) + NewLine;
    Result := Result + Space + 'Agent CLIs: Claude Code, ' + SelectedClis + NewLine;
    if Trim(MainPage.Values[2]) <> '' then
      Result := Result + Space + 'Cloudflare zone: ' + Trim(MainPage.Values[2]) + ' (token given)' + NewLine;
    Result := Result + Space + 'Mirrored networking in %UserProfile%\.wslconfig' + NewLine;
  end
  else
    Result := 'Additional machine, in WSL (' + Distro + ')' + NewLine + Space + 'Enrollment command supplied (code hidden)' + NewLine;
  Result := Result + NewLine + 'Windows may restart once to finish installing WSL; setup then continues after you sign in.';
end;

procedure OnScriptOutput(const S: String; const Error, FirstLine: Boolean);
begin
  Log(S);
  RecentOutput := RecentOutput + Copy(S, 1, 300) + #13#10;
  while Length(RecentOutput) > 3600 do
    Delete(RecentOutput, 1, Pos(#10, RecentOutput));
  if WizardSilent then
    Exit;
  if Copy(S, 1, 3) = '== ' then
    CurrentStep := Copy(S, 4, Length(S));
  ProgressPage.SetText(CurrentStep, Copy(Trim(S), 1, 150));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Arguments: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  ExportAnswers;
  Arguments := ScriptCommand(ExpandConstant('{app}\src\deploy\windows\install-acc.ps1'),
    '-Role ' + RoleName + ' -LogPath "' + LogFile('install-acc.log') + '"');
  if ExpandConstant('{param:ALLOWBUNDLEDNODESOURCE|0}') = '1' then
    Arguments := Arguments + ' -AllowBundledSource';
  if not WizardSilent then
  begin
    ProgressPage.ProgressBar.Style := npbstMarquee;
    ProgressPage.Show;
  end;
  try
    try
      ScriptRan := ExecAndLogOutput(PowerShellExe, Arguments, ExpandConstant('{app}'), SW_HIDE,
        ewWaitUntilTerminated, ScriptResult, @OnScriptOutput);
    except
      Log('install-acc.ps1 did not start: ' + GetExceptionMessage);
      ScriptRan := False;
    end;
  finally
    if not WizardSilent then
      ProgressPage.Hide;
  end;
  Log(Format('install-acc.ps1 exit code: %d', [ScriptResult]));
  if not ScriptRan or ((ScriptResult <> 0) and (ScriptResult <> RestartExitCode)) then
    ShowInstallFailure('Installing Rynhult in WSL did not finish. Fix the error below, then run setup again.');
end;

function NeedRestart: Boolean;
begin
  Result := ScriptRan and (ScriptResult = RestartExitCode);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  OpenLogButton.Visible := CurPageID = wpFinished;
  if CurPageID <> wpFinished then
    Exit;
  if NeedRestart then
    WizardForm.FinishedLabel.Caption := 'Windows must restart to finish installing WSL. After you sign in again, ' +
      'setup continues in a window of its own (Windows asks for permission first).'
  else if ScriptRan and (ScriptResult = 0) then
  begin
    if IsMainRole then
      WizardForm.FinishedLabel.Caption := 'Rynhult is installed in WSL. Open it in a browser and set up the first ' +
        'user with the one-time token from:' + #13#10 + 'wsl -d ' + Distro + ' -u root -- cat /srv/acc/data/secrets/setup_token' + #13#10#13#10 +
        'The log: ' + LogFile('install-acc.log')
    else
      WizardForm.FinishedLabel.Caption := 'This PC is enrolled: it shows on Host → Machines on your Rynhult.' + #13#10#13#10 +
        'The log: ' + LogFile('install-acc.log');
  end
  else
    WizardForm.FinishedLabel.Caption := 'Installing Rynhult in WSL did not finish. What happened is in ' +
      LogFile('install-acc.log') + '. Fix what it says, then run this setup again.';
end;

// ------------------------------------------------------------------ uninstall

// The distribution holds the user's data: it goes only when this box is ticked (or /REMOVEDISTRO is given).
function AskRemoveDistro: Boolean;
var
  Form: TSetupForm;
  Info: TNewStaticText;
  Box: TNewCheckBox;
  OkButton, CancelButton: TNewButton;
begin
#if Ver >= EncodeVer(6, 6, 0)
  Form := CreateCustomForm(ScaleX(440), ScaleY(170), False, False);
#else
  Form := CreateCustomForm;
  Form.ClientWidth := ScaleX(440);
  Form.ClientHeight := ScaleY(170);
#endif
  try
    Form.Caption := 'Uninstall Rynhult';
    Info := TNewStaticText.Create(Form);
    Info.Parent := Form;
    Info.Left := ScaleX(12);
    Info.Top := ScaleY(12);
    Info.Width := Form.ClientWidth - ScaleX(24);
    Info.WordWrap := True;
    Info.Caption := 'This removes Rynhult''s services and programs inside WSL and the task that keeps WSL running. ' +
      'Your data in WSL (projects, the database, settings) and the ' + Distro + ' distribution stay.';
    Box := TNewCheckBox.Create(Form);
    Box.Parent := Form;
    Box.Left := ScaleX(12);
    Box.Top := ScaleY(84);
    Box.Width := Form.ClientWidth - ScaleX(24);
    Box.Caption := 'Also delete the ' + Distro + ' WSL distribution and everything in it';
    Box.Checked := False;
    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := 'Uninstall';
    OkButton.Width := ScaleX(90);
    OkButton.Height := ScaleY(25);
    OkButton.Left := Form.ClientWidth - ScaleX(192);
    OkButton.Top := Form.ClientHeight - ScaleY(37);
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := 'Cancel';
    CancelButton.Width := ScaleX(90);
    CancelButton.Height := ScaleY(25);
    CancelButton.Left := Form.ClientWidth - ScaleX(98);
    CancelButton.Top := Form.ClientHeight - ScaleY(37);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    Result := Form.ShowModal = mrOk;
    RemoveDistro := Box.Checked;
  finally
    Form.Free;
  end;
end;

function InitializeUninstall: Boolean;
begin
  Result := True;
  RemoveDistro := HasSwitch('/REMOVEDISTRO');
  if not UninstallSilent then
    Result := AskRemoveDistro;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Arguments: String;
  Code: Integer;
begin
  if CurUninstallStep <> usUninstall then
    Exit;  // before the files go: the script and the WSL helper are among them
  Arguments := ScriptCommand(ExpandConstant('{app}\src\deploy\windows\install-acc.ps1'),
    '-Uninstall -LogPath "' + LogFile('uninstall.log') + '"');
  if RemoveDistro then
    Arguments := Arguments + ' -RemoveDistro';
  try
    ExecAndLogOutput(PowerShellExe, Arguments, '', SW_HIDE, ewWaitUntilTerminated, Code, nil);
    Log(Format('install-acc.ps1 -Uninstall exit code: %d', [Code]));
  except
    Log('install-acc.ps1 -Uninstall did not start: ' + GetExceptionMessage);
  end;
end;
