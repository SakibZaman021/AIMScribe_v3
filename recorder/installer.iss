; ============================================================
; AIMScribe Agent - Windows installer
;
; Produces AIMScribeSetup.exe: one file for hospital IT to run on a doctor PC.
; No PowerShell, no execution policy, no command-line arguments.
;
; What the administrator is asked for:
;   * the enrolment token for that doctor
;   * the backend URL and the CMED origin, pre-filled with the defaults below
;
; Everything else - the per-install API key, the directory permissions, the
; logon task, the pinned public keys - happens without being asked, because
; every one of those was a step someone could get wrong twenty-nine times.
;
; Build:  ISCC.exe installer.iss
; ============================================================

#define AppName        "AIMScribe Agent"
#define AppVersion     "2.3.1"
#define AppPublisher   "AIMS LAB"
#define AppExe         "AIMScribe_Agent.exe"

; Defaults offered on the configuration page. Override per site.
#define DefaultBackend "https://aimscribe-backend-render.onrender.com"
#define DefaultOrigin  "https://aim-scribe-exe.vercel.app"

[Setup]
AppId={{8F3A2C41-6E5B-4D77-9A18-3C7E5B2D9F04}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\AIMScribe
DefaultGroupName=AIMScribe
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=AIMScribeSetup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Program files and the machine-wide data directory both need administrator
; rights, and the logon task is registered for the machine.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
; Setup stops the agent itself in PrepareToInstall, so Windows' RestartManager
; has nothing useful to add - and a great deal to take away. It scans for
; anything holding our files, finds a Windows service that cannot be closed
; (Program Compatibility Assistant, typically), and asks Abort/Retry/Ignore.
; On a silent install that prompt defaults to Abort, so the upgrade rolls back
; and reports failure while the old version keeps running. Across a fleet that
; is a machine that looks updated and is not.
CloseApplications=no
RestartApplications=no

; A clinical PC should be told what it is running.
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=AIMScribe consultation recording agent

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\AIMScribe_Agent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Public halves only. Identical on every PC and carrying no secret, which is
; why they can ship in the installer instead of being copied by hand.
Source: "keys\cmed_grant_pub.pem";      DestDir: "{commonappdata}\AIMScribe\keys"; Flags: ignoreversion
Source: "keys\aimslab_receipt_pub.pem"; DestDir: "{commonappdata}\AIMScribe\keys"; Flags: ignoreversion

[Dirs]
; The agent writes audio and logs as the signed-in doctor, so Users need Modify
; here - but only here. It cannot alter its own program files.
Name: "{commonappdata}\AIMScribe";        Permissions: users-modify
Name: "{commonappdata}\AIMScribe\spool";  Permissions: users-modify
Name: "{commonappdata}\AIMScribe\logs";   Permissions: users-modify
Name: "{commonappdata}\AIMScribe\state";  Permissions: users-modify
Name: "{commonappdata}\AIMScribe\keys";   Permissions: users-modify

[Icons]
Name: "{group}\AIMScribe Agent"; Filename: "{app}\{#AppExe}"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start AIMScribe now"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""AIMScribe Agent"" /F"; \
    Flags: runhidden; RunOnceId: "RemoveTask"
Filename: "taskkill.exe"; Parameters: "/IM {#AppExe} /F"; \
    Flags: runhidden; RunOnceId: "StopAgent"

[Code]
var
  ConfigPage: TInputQueryWizardPage;

{ An upgrade, not a first install. Enrolment lives under ProgramData and is
  never touched by Setup, so a machine that has already been enrolled keeps its
  doctor and hospital across every upgrade. Detecting that is what lets the
  token field be left empty without a frightening warning. }
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
      'Which backend, and which doctor is this PC for?',
      'The enrolment token names the doctor and hospital this machine records for.' + #13#10 +
      'Obtain one per PC from the AIMScribe administrator.');

  ConfigPage.Add('Backend URL:', False);
  ConfigPage.Add('CMED web address:', False);
  if AlreadyEnrolled() then
    ConfigPage.Add('Enrolment token (leave empty - already enrolled):', False)
  else
    ConfigPage.Add('Enrolment token:', False);

  ConfigPage.Values[0] := '{#DefaultBackend}';
  ConfigPage.Values[1] := '{#DefaultOrigin}';
  ConfigPage.Values[2] := '';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ConfigPage.ID then
  begin
    if Trim(ConfigPage.Values[0]) = '' then
    begin
      MsgBox('A backend URL is required.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if Trim(ConfigPage.Values[1]) = '' then
    begin
      MsgBox('The CMED web address is required. The agent refuses connections' + #13#10 +
             'from any other origin, so recording cannot start without it.',
             mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if Trim(ConfigPage.Values[2]) = '' then
    begin
      // An upgrade of an enrolled machine is the ordinary case across a fleet,
      // and it needs no token at all. Warning here sent people hunting for a
      // token that does not exist and cannot be reissued - the original was
      // consumed when the PC was first set up.
      if not AlreadyEnrolled() then
      begin
        if MsgBox('No enrolment token was entered.' + #13#10 + #13#10 +
                  'AIMScribe will install and start, but will refuse to record ' +
                  'until this PC is enrolled. Continue anyway?',
                  mbConfirmation, MB_YESNO) = IDNO then
          Result := False;
      end;
    end
    else if Length(Trim(ConfigPage.Values[2])) < 20 then
    begin
      // A real token is 43 characters. Anything much shorter is a placeholder
      // typed to get past this page, and the machine then installs cleanly,
      // starts cleanly, and refuses to record - which is discovered by a doctor
      // with a patient in front of them rather than by the person installing it.
      MsgBox('That does not look like an enrolment token.' + #13#10 + #13#10 +
             'A token is about 43 characters, issued by the AIMScribe ' +
             'administrator for this specific PC. Leave the field empty if you ' +
             'intend to enrol this machine later.',
             mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ A local API key, unique per install. Not security-critical on its own - the
  listener is bound to 127.0.0.1 and every recording still needs a signed grant
  - but a shared key across thirty machines would be one stolen value away from
  driving all of them. }
function NewApiKey(): String;
var
  I: Integer;
  Alphabet: String;
begin
  Alphabet := 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  Result := '';
  for I := 1 to 44 do
    Result := Result + Copy(Alphabet, Random(Length(Alphabet)) + 1, 1);
end;

procedure WriteEnvFile();
var
  Env: TStringList;
  DataDir: String;
begin
  DataDir := ExpandConstant('{commonappdata}\AIMScribe');
  Env := TStringList.Create;
  try
    Env.Add('# Written by AIMScribeSetup. Do not edit by hand.');
    Env.Add('AIMS_BACKEND_URL=' + Trim(ConfigPage.Values[0]));
    Env.Add('# Protocol 2. The v1 prefix serves the transcription API and has');
    Env.Add('# none of the enrolment or session endpoints.');
    Env.Add('AIMS_BACKEND_API_PREFIX=/api/v2');
    Env.Add('');
    Env.Add('AIMS_SAMPLE_RATE=44100');
    Env.Add('AIMS_CHANNELS=1');
    Env.Add('AIMS_SAMPLE_WIDTH=2');
    Env.Add('AIMS_FRAMES_PER_BUFFER=2048');
    Env.Add('');
    Env.Add('# A clip is the unit of upload, retry and loss. Short clips mean');
    Env.Add('# a failure risks a minute, not three, and transcripts arrive while');
    Env.Add('# the doctor is still talking. The server merges them back.');
    Env.Add('AIMS_SEGMENT_MIN_SECONDS=30');
    Env.Add('AIMS_SEGMENT_MAX_SECONDS=60');
    Env.Add('# Keep listening this much longer for a natural pause before');
    Env.Add('# cutting a talker mid-sentence.');
    Env.Add('AIMS_SEGMENT_GRACE_SECONDS=15');
    Env.Add('# A cut needs three seconds of quiet - far longer than a breath');
    Env.Add('# between sentences - so it cannot land inside a phrase. Past 60s');
    Env.Add('# the clip is overdue and half that will do.');
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
    Env.Add('AIMS_LOCAL_API_KEY=' + NewApiKey());
    Env.Add('AIMS_REQUIRE_GRANT=true');
    Env.Add('AIMS_ENABLE_DOCS=false');
    Env.Add('');
    Env.Add('AIMS_GRANT_ISSUER=cmed');
    Env.Add('AIMS_GRANT_AUDIENCE=aimscribe-recorder');
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

procedure Append(var Lines: TArrayOfString; const Line: String);
begin
  SetArrayLength(Lines, GetArrayLength(Lines) + 1);
  Lines[GetArrayLength(Lines) - 1] := Line;
end;

procedure RegisterLogonTask();
var
  ResultCode: Integer;
  Xml: TArrayOfString;
  XmlPath: String;
begin
  { A logon task, not a service. Audio capture needs the user's session: a
    session 0 service has no default audio endpoint and would record silence.

    Defined by XML rather than by schtasks flags, because the two settings that
    matter most have no flags and default to the wrong value on a laptop:

      DisallowStartIfOnBatteries  defaults True - the agent then never starts at
                                  logon on an unplugged laptop, and schtasks
                                  still reports success. This cost an evening to
                                  find on a machine sitting at 19% battery.
      StopIfGoingOnBatteries      defaults True - Windows would kill the agent
                                  mid-consultation the moment someone unplugged
                                  the trolley.

    Doctors' machines are laptops and they are not always plugged in. }

  XmlPath := ExpandConstant('{tmp}\aimscribe_task.xml');
  SetArrayLength(Xml, 0);
    { No encoding declaration, and saved without a byte-order mark below.
      schtasks parses the first two bytes literally: a BOM, or a declaration
      that disagrees with the actual encoding, fails with nothing more useful
      than "(1,2): incorrect document syntax". }
  Append(Xml, '<?xml version="1.0"?>');
  Append(Xml, '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">');
  Append(Xml, '  <RegistrationInfo>');
  Append(Xml, '    <Description>Starts the AIMScribe recording agent at logon.</Description>');
  Append(Xml, '  </RegistrationInfo>');
  Append(Xml, '  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>');
  Append(Xml, '  <Principals><Principal id="Author">');
  Append(Xml, '    <GroupId>S-1-5-32-545</GroupId>');
  Append(Xml, '    <RunLevel>HighestAvailable</RunLevel>');
  Append(Xml, '  </Principal></Principals>');
  Append(Xml, '  <Settings>');
  Append(Xml, '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>');
  Append(Xml, '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>');
  Append(Xml, '    <AllowHardTerminate>false</AllowHardTerminate>');
  Append(Xml, '    <StartWhenAvailable>true</StartWhenAvailable>');
  Append(Xml, '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>');
  Append(Xml, '    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd></IdleSettings>');
  Append(Xml, '    <AllowStartOnDemand>true</AllowStartOnDemand>');
  Append(Xml, '    <Enabled>true</Enabled>');
  Append(Xml, '    <Hidden>false</Hidden>');
  Append(Xml, '    <RunOnlyIfIdle>false</RunOnlyIfIdle>');
    { A consultation can outlast any limit worth setting, and the agent is meant
      to run all day. }
  Append(Xml, '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>');
  Append(Xml, '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>');
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
    success while creating nothing. A machine that half-installed is worse than
    one that plainly did not: the second gets fixed, the first gets used. }
  if not Exec('schtasks.exe', '/Create /TN "AIMScribe Agent" /XML "' + XmlPath + '" /F',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    RaiseException('Could not run schtasks.exe to register the startup task.' + #13#10 +
                   'AIMScribe has not been installed.');

  if ResultCode <> 0 then
    RaiseException('Registering the startup task failed (schtasks returned ' +
                   IntToStr(ResultCode) + ').' + #13#10 +
                   'AIMScribe has not been installed.');

  Exec('schtasks.exe', '/Query /TN "AIMScribe Agent"', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ResultCode <> 0 then
    RaiseException('The startup task was reported as created but does not ' +
                   'exist.' + #13#10 + 'AIMScribe has not been installed.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // Reinstalling or upgrading over a running agent leaves its files locked, and
  // Setup reports only "file in use". Stop it first: this is the upgrade path
  // for every machine in the fleet, not just a convenience today.
  Result := '';

  Exec('schtasks.exe', '/End /TN "AIMScribe Agent"', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('taskkill.exe', '/IM "{#AppExe}" /F', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // Give Windows a moment to release the handles before Setup copies over them.
  Sleep(2000);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    WriteEnvFile();
    StageEnrollmentToken();
    RegisterLogonTask();
  end;
end;

procedure InitializeUninstallProgressForm();
begin
  // Audio and configuration under ProgramData are deliberately left behind.
  // A consultation that has not yet reached the archive exists only there, and
  // an uninstall must not be the thing that destroys it.
end;
