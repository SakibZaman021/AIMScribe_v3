; ============================================================
; AIMScribe Agent - Windows installer
;
; Produces AIMScribeSetup.exe: one signed file for hospital IT to run on a
; doctor PC, by hand or silently from a management tool.
;
; What it does, and nothing more - the way Zoom or WhatsApp install:
;   * copies the signed program into Program Files
;   * writes the program's settings (.env) beside it
;   * creates the four data folders under ProgramData
;   * registers the logon task that starts the agent
;
; What it no longer does. Machine policy - Windows Update active hours, the
; power plan, the time service - used to be changed here, and an unsigned
; installer quietly rewriting Windows policy is exactly what antivirus
; products score as malware. Those settings are now an explicit, separate,
; signed step an administrator chooses to run (policy\AIMScribe-ClinicPolicy.ps1)
; or deploys as a Group Policy / Intune profile. See INSTALL_FOR_IT.md.
;
; Build (BUILD.bat does this for you):
;   signed:   ISCC /DSignedBuild "/Saimssign=powershell.exe -NoProfile
;                  -ExecutionPolicy Bypass -File signing\sign.ps1 -File $f"
;                  /DDefaultBackend=https://<server> /DDefaultOrigin=https://<cmed page>
;                  installer.iss
;   dev:      ISCC /DDefaultBackend=... /DDefaultOrigin=... installer.iss
;             (unsigned - never give it to a clinic)
;
; Silent install, for IT and management tools:
;   AIMScribeSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;       /BACKEND=https://<server> /ORIGIN=https://<cmed page>
;       /TOKEN=<enrolment token> [/PIN=<sha256 SPKI pin>[,<backup pin>]]
;       /LOG="%TEMP%\aimscribe-setup.log"
;   Exit code 0 only when the program is installed AND the logon task exists.
; ============================================================

#define AppName        "AIMScribe Agent"
#define AppVersion     "3.8.0"

; The same mark as the agent, drawn by scripts/make_icon.py.
#define AppIcon        "assets\aimscribe.ico"
#define AppPublisher   "AIMS LAB, United International University"
#define AppExe         "AIMScribe_Agent.exe"

; The AIMS LAB server's public keys the agent trusts. A development build for a
; test server (the local stack) bundles that server's keys instead: BUILD.bat
; with AIMS_KEYS_DIR set. A signed release always uses keys\.
#ifndef KeysDir
  #define KeysDir      "keys"
#endif
#ifdef SignedBuild
  #undef KeysDir
  #define KeysDir      "keys"
#endif

; The server and the CMED page a NEW PC is pointed at. Required: an installer
; built without them used to fall back to the old v1 server on Render and a
; test page on vercel.app, so a PC set up from it talked to the wrong system.
; An upgrade ignores these and keeps what that PC already has; /BACKEND= and
; /ORIGIN= on the command line override both.
#ifndef DefaultBackend
  #error Build with /DDefaultBackend=https://<the AIMS LAB server> (see BUILD.bat)
#endif
#ifndef DefaultOrigin
  #error Build with /DDefaultOrigin=https://<CMED's page> (see BUILD.bat)
#endif

[Setup]
SetupIconFile={#AppIcon}
AppId={{8F3A2C41-6E5B-4D77-9A18-3C7E5B2D9F04}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/SakibZaman021/AIMScribe_v3
AppSupportURL=https://github.com/SakibZaman021/AIMScribe_v3
DefaultDirName={autopf}\AIMScribe
DefaultGroupName=AIMScribe
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=AIMScribeSetup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Program Files and the machine-wide data folders need administrator rights,
; and the logon task is registered for the machine. Not overridable: a
; per-user install would put the settings and the trusted keys somewhere the
; doctor's account can change.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
; Setup stops the agent itself in PrepareToInstall, so Windows' RestartManager
; has nothing useful to add - and a great deal to take away. It scans for
; anything holding our files, finds a Windows service that cannot be closed
; (Program Compatibility Assistant, typically), and asks Abort/Retry/Ignore.
; On a silent install that prompt defaults to Abort, so the upgrade rolls back
; and reports failure while the old version keeps running.
CloseApplications=no
RestartApplications=no

; Every file a clinic receives is signed by the same publisher: the setup, the
; uninstaller Setup writes, and (by signing\sign.ps1, before this runs) every
; program file inside. Unsigned is for a developer's own PC only.
#ifdef SignedBuild
SignTool=aimssign $f
SignedUninstaller=yes
SignToolRetryCount=3
SignToolRetryDelay=2000
#endif

; A clinical PC should be told what it is running, and who made it.
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=AIMScribe consultation recording agent - setup
VersionInfoProductName=AIMScribe
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright=Copyright (c) 2026 AIMS LAB, United International University

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\AIMScribe_Agent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Public halves only. Identical on every PC and carrying no secret, which is
; why they can ship in the installer instead of being copied by hand.
; Beside the program, under Program Files, where only an administrator can
; change them. They used to sit in ProgramData, which every user may modify:
; replacing the grant key there let anyone on the PC sign their own grants.
Source: "{#KeysDir}\aimslab_grant_pub.pem";   DestDir: "{app}\keys"; Flags: ignoreversion
Source: "{#KeysDir}\aimslab_receipt_pub.pem"; DestDir: "{app}\keys"; Flags: ignoreversion
; The check IT runs before and after installing, and the optional clinic
; policy step (both signed with everything else).
Source: "policy\AIMScribe-Check.ps1";        DestDir: "{app}\tools"; Flags: ignoreversion
Source: "policy\AIMScribe-ClinicPolicy.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion

[InstallDelete]
; The copies an earlier version put in ProgramData. Nothing reads them now,
; and a file that looks trusted but is not is only a trap.
Type: files; Name: "{commonappdata}\AIMScribe\keys\aimslab_grant_pub.pem"
Type: files; Name: "{commonappdata}\AIMScribe\keys\aimslab_receipt_pub.pem"

[Dirs]
; The agent runs as whoever signs in (audio needs the user's session), so it
; writes audio, logs, its state and its own keys as that user: Users need
; Modify on those four folders - and only those. The folder above them is left
; as Windows has it, and the settings and the trusted public keys live under
; Program Files, which no ordinary user can change.
Name: "{commonappdata}\AIMScribe"
Name: "{commonappdata}\AIMScribe\spool";  Permissions: users-modify
Name: "{commonappdata}\AIMScribe\logs";   Permissions: users-modify
Name: "{commonappdata}\AIMScribe\state";  Permissions: users-modify
Name: "{commonappdata}\AIMScribe\keys";   Permissions: users-modify

[Icons]
Name: "{group}\AIMScribe Agent"; Filename: "{app}\{#AppExe}"
Name: "{group}\AIMScribe - check this PC"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy RemoteSigned -NoExit -File ""{app}\tools\AIMScribe-Check.ps1"""; \
    WorkingDir: "{app}"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start AIMScribe now"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""AIMScribe Agent"" /F"; \
    Flags: runhidden; RunOnceId: "RemoveTask"

[Code]
const
  { The agent's own names (recorder/main.py). The stop event is the polite way
    to ask a running agent to finish its recording and exit, so an upgrade
    never cuts a consultation off mid-sentence. }
  StopEventName = 'Global\AIMScribeAgentStop';
  EVENT_MODIFY_STATE = $0002;
  StopWaitSeconds = 30;

var
  ConfigPage: TInputQueryWizardPage;
  KeptApiKey: String;
  InstallProblem: String;
  { 9 when this PC has no TPM 2.0 chip to keep its identity key in. }
  InstallExitCode: Integer;

function OpenEvent(dwDesiredAccess: Longword; bInheritHandle: Boolean;
  lpName: String): Longword;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(hEvent: Longword): Boolean;
  external 'SetEvent@kernel32.dll stdcall';
function CloseHandle(hObject: Longword): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

{ ------------------------------------------------------------ the old settings }

{ The settings file of the version being upgraded, if there is one. }
function PreviousEnv(): String;
begin
  if WizardForm.PrevAppDir <> '' then
    Result := AddBackslash(WizardForm.PrevAppDir) + '.env'
  else
    Result := ExpandConstant('{autopf}\AIMScribe\.env');
end;

{ One value from that file, or '' when it has none. }
function EnvValue(const Key: String): String;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := '';
  if not LoadStringsFromFile(PreviousEnv(), Lines) then Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Pos(Key + '=', Lines[I]) = 1 then
    begin
      Result := Trim(Copy(Lines[I], Length(Key) + 2, MaxInt));
      Exit;
    end;
end;

{ What a setting should be, in order: the command line (/BACKEND= etc.), the
  value this PC already has, then the build's default. An upgrade keeps the
  PC's own settings: it used to offer the build's defaults, so pressing Next
  on an upgrade quietly moved a PC to another server. }
function Choose(const Param, Key, Default: String): String;
begin
  Result := Trim(ExpandConstant('{param:' + Param + '|}'));
  if Result = '' then Result := EnvValue(Key);
  if Result = '' then Result := Default;
end;

{ An upgrade, not a first install. Enrolment lives under ProgramData and is
  never touched by Setup, so a machine that has already been enrolled keeps its
  doctor and hospital across every upgrade. }
function AlreadyEnrolled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonappdata}\AIMScribe\state\device.token'));
end;

procedure InitializeWizard;
begin
  if AlreadyEnrolled() then
    ConfigPage := CreateInputQueryPage(wpSelectDir,
      'AIMScribe Configuration',
      'This PC is already set up. Updating it.',
      'It keeps the doctor and hospital it was enrolled with, so no enrolment' + #13#10 +
      'token is needed. Leave the last box empty and press Next.')
  else
    ConfigPage := CreateInputQueryPage(wpSelectDir,
      'AIMScribe Configuration',
      'Which server, and which clinic is this PC for?',
      'No enrolment token is needed: this PC joins AIMS LAB by itself the first' + #13#10 +
      'time CMED opens a patient on it, at the clinic CMED names. A token from the' + #13#10 +
      'administrator still works. Doctors need nothing: CMED names them.');

  ConfigPage.Add('Backend URL:', False);
  ConfigPage.Add('CMED web address:', False);
  if AlreadyEnrolled() then
    ConfigPage.Add('Enrolment token (leave empty - already enrolled):', False)
  else
    ConfigPage.Add('Enrolment token (optional):', False);

  ConfigPage.Values[0] := Choose('BACKEND', 'AIMS_BACKEND_URL', '{#DefaultBackend}');
  ConfigPage.Values[1] := Choose('ORIGIN', 'AIMS_ALLOWED_ORIGINS', '{#DefaultOrigin}');
  ConfigPage.Values[2] := Trim(ExpandConstant('{param:TOKEN|}'));
  { Kept across an upgrade only if it was made by the secure generator. A key
    from 3.4 or earlier came from Random() and is replaced, once. }
  if EnvValue('AIMS_LOCAL_API_KEY_SOURCE') = 'csprng' then
    KeptApiKey := EnvValue('AIMS_LOCAL_API_KEY')
  else
    KeptApiKey := '';
end;

{ ------------------------------------------------------------ checking the input }

function StartsWith(const S, Prefix: String): Boolean;
begin
  Result := Copy(S, 1, Length(Prefix)) = Prefix;
end;

{ '' when the three values are usable; otherwise what is wrong, in words an
  IT person can act on. Used by the wizard page and, for a silent install,
  before anything is copied - a silent install with a bad value must fail
  with a non-zero exit code, not install a PC that cannot work. }
function ConfigProblem(): String;
var
  Backend, Origin, Token: String;
begin
  Result := '';
  Backend := Trim(ConfigPage.Values[0]);
  Origin := Trim(ConfigPage.Values[1]);
  Token := Trim(ConfigPage.Values[2]);
  if Backend = '' then
    Result := 'A backend URL is required (/BACKEND=https://...).'
  else if not StartsWith(Lowercase(Backend), 'https://')
#ifndef SignedBuild
    { An unsigned development build may point at a test server on this
      network (the local stack). A signed release never accepts http. }
    and not StartsWith(Lowercase(Backend), 'http://')
#endif
  then
    Result := 'The backend URL must start with https:// - recordings never travel ' +
              'unencrypted.'
  else if Origin = '' then
    Result := 'The CMED web address is required (/ORIGIN=https://...). The agent ' +
              'refuses connections from any other page, so recording cannot start ' +
              'without it.'
  else if (Pos('*', Origin) > 0) or (Pos(' ', Origin) > 0) then
    Result := 'The CMED web address must be exact addresses, with no * or spaces.'
  else if (Token <> '') and (Length(Token) < 20) then
    // A real token is 43 characters. Anything much shorter is a placeholder
    // typed to get past this page, and the machine then installs cleanly,
    // starts cleanly, and refuses to record.
    Result := 'That does not look like an enrolment token. A token is about 43 ' +
              'characters, issued by the AIMScribe administrator for this PC. Leave ' +
              'it empty and this PC joins by itself at its first consultation.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Problem: String;
begin
  Result := True;
  if CurPageID <> ConfigPage.ID then Exit;
  Problem := ConfigProblem();
  if Problem <> '' then
  begin
    if not WizardSilent() then MsgBox(Problem, mbError, MB_OK);
    { A silent install is refused in PrepareToInstall, with an exit code. }
    Result := WizardSilent();
    Exit;
  end;
  { No token is fine from 3.7: the PC's key is in its TPM (setup stops on a PC
    without one), and it registers itself at its first CMED consultation. }
end;

{ ------------------------------------------------------------ the local key }

{ Windows' own cryptographic random number generator. Pascal's Random() is a
  predictable sequence seeded from the clock. }
function BCryptGenRandom(hAlgorithm: Longword; pbBuffer: AnsiString;
  cbBuffer: Longword; dwFlags: Longword): Longword;
  external 'BCryptGenRandom@bcrypt.dll stdcall';

{ A local API key, unique per install: 44 characters from 62, about 262 bits,
  without bias - bytes from 248 up are thrown away rather than folded onto the
  first letters. }
function NewApiKey(): String;
var
  Buffer: AnsiString;
  Alphabet: String;
  I, B: Integer;
begin
  Alphabet := 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  Result := '';
  while Length(Result) < 44 do
  begin
    SetLength(Buffer, 64);
    { BCRYPT_USE_SYSTEM_PREFERRED_RNG = 2; STATUS_SUCCESS = 0 }
    if BCryptGenRandom(0, Buffer, 64, 2) <> 0 then
      RaiseException('Windows could not supply secure random numbers for the ' +
                     'recorder''s local key.' + #13#10 + 'AIMScribe has not been installed.');
    for I := 1 to 64 do
    begin
      B := Ord(Buffer[I]);
      if (B < 248) and (Length(Result) < 44) then
        Result := Result + Copy(Alphabet, (B mod 62) + 1, 1);
    end;
  end;
end;

{ ------------------------------------------------------------ writing the settings }

procedure WriteEnvFile();
var
  Env: TStringList;
  DataDir, Pins: String;
begin
  DataDir := ExpandConstant('{commonappdata}\AIMScribe');
  Pins := Choose('PIN', 'AIMS_TLS_PINS', '');
  Env := TStringList.Create;
  try
    Env.Add('# Written by AIMScribeSetup {#AppVersion}. Do not edit by hand.');
    Env.Add('AIMS_BACKEND_URL=' + Trim(ConfigPage.Values[0]));
    Env.Add('# Protocol 2. The v1 prefix serves the transcription API and has');
    Env.Add('# none of the enrolment or session endpoints.');
    Env.Add('AIMS_BACKEND_API_PREFIX=/api/v2');
    Env.Add('# The server''s public key, pinned (SHA-256 of its SubjectPublicKeyInfo,');
    Env.Add('# base64; current and backup, comma-separated). Empty: the Windows');
    Env.Add('# certificate store decides, which antivirus HTTPS inspection can subvert.');
    Env.Add('AIMS_TLS_PINS=' + Pins);
    Env.Add('');
    Env.Add('AIMS_SAMPLE_RATE=44100');
    Env.Add('AIMS_CHANNELS=1');
    Env.Add('AIMS_SAMPLE_WIDTH=2');
    Env.Add('AIMS_FRAMES_PER_BUFFER=2048');
    Env.Add('# The clinic speakerphone, opened by name and set to 100% when a');
    Env.Add('# recording starts. Without it, the PC''s own microphone records.');
    Env.Add('AIMS_PREFERRED_MICROPHONE=' + Choose('MICROPHONE', 'AIMS_PREFERRED_MICROPHONE', 'Jabra'));
    Env.Add('');
    Env.Add('# A clip is the unit of upload, retry and loss. Short clips mean');
    Env.Add('# a failure risks a minute, not three. The server merges them back.');
    Env.Add('AIMS_SEGMENT_MIN_SECONDS=30');
    Env.Add('AIMS_SEGMENT_MAX_SECONDS=60');
    Env.Add('AIMS_SEGMENT_GRACE_SECONDS=15');
    Env.Add('AIMS_SILENCE_HOLD_SECONDS=3');
    Env.Add('');
    Env.Add('AIMS_SPOOL_DIR=' + DataDir + '\spool');
    Env.Add('# 40 GB is about three weeks of backend downtime at 44.1 kHz.');
    Env.Add('AIMS_SPOOL_MAX_BYTES=42949672960');
    Env.Add('# Receipted audio is kept this long as a safety net before deletion.');
    Env.Add('AIMS_PURGE_GRACE_HOURS=24');
    Env.Add('');
    Env.Add('AIMS_BIND_HOST=127.0.0.1');
    Env.Add('AIMS_BIND_PORT=5050');
    Env.Add('# Exact origins only. The agent refuses the WebSocket handshake from');
    Env.Add('# anything else, which is what stops a random page recording.');
    Env.Add('AIMS_ALLOWED_ORIGINS=' + Trim(ConfigPage.Values[1]));
    Env.Add('AIMS_ALLOWED_HOSTS=localhost:5050,127.0.0.1:5050,[::1]:5050');
    if KeptApiKey <> '' then
      Env.Add('AIMS_LOCAL_API_KEY=' + KeptApiKey)
    else
      Env.Add('AIMS_LOCAL_API_KEY=' + NewApiKey());
    Env.Add('AIMS_LOCAL_API_KEY_SOURCE=csprng');
    Env.Add('AIMS_REQUIRE_GRANT=true');
    Env.Add('AIMS_ENABLE_DOCS=false');
    Env.Add('');
    Env.Add('AIMS_GRANT_ISSUER=aimslab');
    Env.Add('AIMS_GRANT_AUDIENCE=aimscribe-recorder');
    Env.Add('# The server''s public keys, trusted from Program Files only.');
    Env.Add('AIMS_GRANT_PUBLIC_KEY_PATH=' + ExpandConstant('{app}\keys\aimslab_grant_pub.pem'));
    Env.Add('AIMS_RECEIPT_PUBLIC_KEY_PATH=' + ExpandConstant('{app}\keys\aimslab_receipt_pub.pem'));
    Env.Add('# This file is the only source of these settings: the recorder ignores');
    Env.Add('# AIMS_* environment variables, which any program on the PC can set.');
    Env.Add('');
    Env.Add('AIMS_HEARTBEAT_SECONDS=30');
    Env.Add('AIMS_LOG_LEVEL=INFO');
    Env.Add('AIMS_REDACT_LOGS=true');
    Env.Add('# Never true on a clinical PC: private keys would sit unprotected.');
    Env.Add('AIMS_ALLOW_PLAINTEXT_KEYSTORE=false');
    Env.SaveToFile(ExpandConstant('{app}\.env'));
  finally
    Env.Free;
  end;
end;

procedure StageEnrollmentToken();
var
  Token: TStringList;
begin
  if Trim(ConfigPage.Values[2]) = '' then Exit;
  Token := TStringList.Create;
  try
    Token.Add(Trim(ConfigPage.Values[2]));
    Token.SaveToFile(ExpandConstant('{commonappdata}\AIMScribe\state\enrollment.token'));
  finally
    Token.Free;
  end;
end;

{ ------------------------------------------------------------ the logon task }

procedure Append(var Lines: TArrayOfString; const Line: String);
begin
  SetArrayLength(Lines, GetArrayLength(Lines) + 1);
  Lines[GetArrayLength(Lines) - 1] := Line;
end;

{ '' on success, otherwise why the task is not there. }
function RegisterLogonTask(): String;
var
  ResultCode: Integer;
  Xml: TArrayOfString;
  XmlPath: String;
begin
  { A logon task, not a service. Audio capture needs the user's session: a
    session 0 service has no default audio endpoint and would record silence.

    Defined by XML rather than by schtasks flags, because the two settings that
    matter most have no flags and default to the wrong value on a laptop:
      DisallowStartIfOnBatteries  defaults True - the agent never starts at
                                  logon on an unplugged laptop.
      StopIfGoingOnBatteries      defaults True - Windows would kill the agent
                                  mid-consultation when someone unplugged it.

    Started once at sign-in, and restarted by Windows if it fails - three
    times, a minute apart. It used to be re-launched every minute all day and
    could not be ended by Windows; that is how persistent malware behaves, and
    antivirus products treat it accordingly. A normal app is started once. }
  Result := '';
  XmlPath := ExpandConstant('{tmp}\aimscribe_task.xml');
  SetArrayLength(Xml, 0);
    { No encoding declaration, and saved without a byte-order mark below.
      schtasks parses the first two bytes literally. }
  Append(Xml, '<?xml version="1.0"?>');
  Append(Xml, '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">');
  Append(Xml, '  <RegistrationInfo>');
  Append(Xml, '    <Author>AIMS LAB</Author>');
  Append(Xml, '    <Description>Starts the AIMScribe consultation recording agent at sign-in.</Description>');
  Append(Xml, '  </RegistrationInfo>');
  Append(Xml, '  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>');
  Append(Xml, '  <Principals><Principal id="Author">');
  Append(Xml, '    <GroupId>S-1-5-32-545</GroupId>');
    { Least privilege: nothing the agent does needs administrator rights. }
  Append(Xml, '    <RunLevel>LeastPrivilege</RunLevel>');
  Append(Xml, '  </Principal></Principals>');
  Append(Xml, '  <Settings>');
  Append(Xml, '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>');
  Append(Xml, '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>');
  Append(Xml, '    <AllowHardTerminate>true</AllowHardTerminate>');
  Append(Xml, '    <StartWhenAvailable>true</StartWhenAvailable>');
  Append(Xml, '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>');
  Append(Xml, '    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd></IdleSettings>');
  Append(Xml, '    <AllowStartOnDemand>true</AllowStartOnDemand>');
  Append(Xml, '    <Enabled>true</Enabled>');
  Append(Xml, '    <Hidden>false</Hidden>');
  Append(Xml, '    <RunOnlyIfIdle>false</RunOnlyIfIdle>');
    { The agent is meant to run all day. }
  Append(Xml, '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>');
  Append(Xml, '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>');
  Append(Xml, '    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>');
  Append(Xml, '  </Settings>');
  Append(Xml, '  <Actions Context="Author"><Exec>');
  Append(Xml, '    <Command>"' + ExpandConstant('{app}\{#AppExe}') + '"</Command>');
  Append(Xml, '  </Exec></Actions>');
  Append(Xml, '</Task>');
  SaveStringsToUTF8FileWithoutBOM(XmlPath, Xml, False);

  Exec('schtasks.exe', '/Delete /TN "AIMScribe Agent" /F', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);

  { Three ways this goes wrong, and all three used to end with Setup reporting
    success: schtasks not launching, schtasks failing, and schtasks reporting
    success while creating nothing. }
  if not Exec('schtasks.exe', '/Create /TN "AIMScribe Agent" /XML "' + XmlPath + '" /F',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'Could not run schtasks.exe to register the startup task.';
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    Result := 'Registering the startup task failed (schtasks returned ' +
              IntToStr(ResultCode) + ').';
    Exit;
  end;
  Exec('schtasks.exe', '/Query /TN "AIMScribe Agent"', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ResultCode <> 0 then
    Result := 'The startup task was reported as created but does not exist.';
end;

{ ------------------------------------------------------------ stopping a running agent }

{ True while any copy of the agent is running, for any user. tasklist run
  elevated sees every session; its output goes through a file because Exec
  cannot capture it. }
function AgentRunning(): Boolean;
var
  ResultCode: Integer;
  OutFile: String;
  Output: AnsiString;
begin
  // GetTempDir rather than the tmp constant: this also runs in the uninstaller.
  OutFile := AddBackslash(GetTempDir()) + 'aimscribe_tasklist.txt';
  DeleteFile(OutFile);
  Result := False;
  if Exec(ExpandConstant('{cmd}'),
          '/C tasklist /FI "IMAGENAME eq {#AppExe}" /NH > "' + OutFile + '"',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    if LoadStringFromFile(OutFile, Output) then
      Result := Pos(Lowercase('{#AppExe}'), Lowercase(String(Output))) > 0;
end;

{ Ask the agent to finish and exit: it closes the open recording as
  "software_update", writes what it holds to disk, and leaves. Only if it has
  not gone after StopWaitSeconds is it ended by force - the timed fallback,
  logged, never the first resort. Returns how it stopped, for the log. }
function StopAgent(): String;
var
  Handle: Longword;
  I, ResultCode: Integer;
begin
  Result := 'not running';
  if not AgentRunning() then Exit;

  Handle := OpenEvent(EVENT_MODIFY_STATE, False, StopEventName);
  if Handle <> 0 then
  begin
    SetEvent(Handle);
    CloseHandle(Handle);
    for I := 1 to StopWaitSeconds do
    begin
      Sleep(1000);
      if not AgentRunning() then
      begin
        Result := 'stopped when asked';
        Exit;
      end;
    end;
  end;

  { An older agent (3.4 and before) has no stop event; the task's own End is
    the next most polite thing. }
  Exec('schtasks.exe', '/End /TN "AIMScribe Agent"', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  for I := 1 to 5 do
  begin
    Sleep(1000);
    if not AgentRunning() then
    begin
      Result := 'stopped by the task scheduler';
      Exit;
    end;
  end;

  Exec('taskkill.exe', '/IM "{#AppExe}" /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(2000);
  Result := 'ended by force after waiting ' + IntToStr(StopWaitSeconds) + ' s';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { A silent install with a bad value fails here, before anything is copied,
    with a non-zero exit code a management tool will report. }
  Result := ConfigProblem();
  if Result <> '' then Exit;
  Log('Running agent: ' + StopAgent());
end;

function InitializeUninstall(): Boolean;
begin
  Log('Running agent: ' + StopAgent());
  Result := True;
end;

{ ------------------------------------------------------------ after the copy }

{ This PC's identity key is created inside its TPM chip, as administrator -
  which only Setup is - so nothing on the PC can ever copy it. The agent, run
  as whoever signs in, may use the key to sign but never export it. A PC
  enrolled before 3.6 keeps the software key it enrolled with, so a missing
  TPM does not stop its update; a new PC without one cannot be set up. }
function ProvisionDeviceKey(): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if not Exec(ExpandConstant('{app}\{#AppExe}'), '--provision-device-key', '',
              SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    ResultCode := 10;
  Log('Device key in the TPM: exit ' + IntToStr(ResultCode));
  if (ResultCode = 0) or AlreadyEnrolled() then Exit;
  if ResultCode = 9 then
  begin
    InstallExitCode := 9;
    Result := 'This PC has no usable TPM 2.0 security chip, so AIMScribe cannot ' +
              'keep its identity key safe here. Switch the TPM on in the PC''s ' +
              'firmware (BIOS/UEFI) settings, or use another PC.';
  end
  else
    Result := 'This PC''s identity key could not be created in its TPM chip ' +
              '(code ' + IntToStr(ResultCode) + '). See the AIMScribe log.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  WriteEnvFile();
  StageEnrollmentToken();
  InstallProblem := ProvisionDeviceKey();
  if InstallProblem = '' then
    InstallProblem := RegisterLogonTask();
  if InstallProblem <> '' then
  begin
    Log('INSTALL INCOMPLETE: ' + InstallProblem);
    if not WizardSilent() then
      MsgBox(InstallProblem + #13#10 + #13#10 +
             'The program is installed but will not start by itself. Run Setup ' +
             'again, or contact AIMS LAB.', mbError, MB_OK);
    Exit;
  end;
  { A silent install has no "Start now" box to tick: start it in the signed-in
    doctor's session through the task, as Windows will at every sign-in. }
  if WizardSilent() then
    Exec('schtasks.exe', '/Run /TN "AIMScribe Agent"', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
end;

{ Non-zero when the PC was left unable to start the agent, so a management
  tool (Intune, PDQ, a script) reports the failure instead of a green tick. }
function GetCustomSetupExitCode(): Integer;
begin
  if InstallProblem = '' then
    Result := 0
  else if InstallExitCode <> 0 then
    Result := InstallExitCode
  else
    Result := 8;
end;

procedure InitializeUninstallProgressForm();
begin
  // Audio and configuration under ProgramData are deliberately left behind.
  // A consultation that has not yet reached the archive exists only there, and
  // an uninstall must not be the thing that destroys it.
end;
