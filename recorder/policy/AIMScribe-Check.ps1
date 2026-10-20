<#
.SYNOPSIS
    AIMScribe - check this PC, before and after installing.

.DESCRIPTION
    Read-only. Changes nothing. Prints a PASS / WARN / FAIL table that IT can
    paste back to AIMS LAB. Safe to run as an ordinary user; a few rows say
    more when run as administrator.

      Signature        AIMScribeSetup.exe and the installed agent are signed by
                       AIMS LAB and the signature is valid (not just present)
      Smart App Control / Defender / SmartScreen
                       their state - never turn these off for AIMScribe
      Microphone       a recording device is present, and the preferred one
      Clock            the PC clock against the server's (Date header)
      Port 5050        free before installing; ours after
      Server           https://<server>/health/live answers
      CMED page        the CMED address is configured
      Logon task       registered, and how it starts

.PARAMETER Setup
    Path of AIMScribeSetup.exe to check before installing.

.PARAMETER Server
    The AIMS LAB server (https://...). Read from the installed settings if not given.

.EXAMPLE
    .\AIMScribe-Check.ps1 -Setup .\AIMScribeSetup.exe -Server https://aimscribe.example.org
.EXAMPLE
    & "C:\Program Files\AIMScribe\tools\AIMScribe-Check.ps1"
#>
[CmdletBinding()]
param(
    [string] $Setup,
    [string] $Server,
    [string] $InstallDir = (Join-Path $env:ProgramFiles 'AIMScribe'),
    [string] $Publisher = 'AIMS LAB'
)

$ErrorActionPreference = 'Continue'
$rows = New-Object System.Collections.Generic.List[object]
function Row([string] $Check, [string] $State, [string] $Detail) {
    $rows.Add([pscustomobject]@{ Check = $Check; Result = $State; Detail = $Detail })
}

# ---------------------------------------------------------------- settings
$envFile = Join-Path $InstallDir '.env'
$settings = @{}
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $settings[$Matches[1]] = $Matches[2].Trim() }
    }
}
if (-not $Server) { $Server = $settings['AIMS_BACKEND_URL'] }
$installed = Test-Path (Join-Path $InstallDir 'AIMScribe_Agent.exe')

# ---------------------------------------------------------------- signatures
function Test-Signed([string] $Label, [string] $Path) {
    if (-not (Test-Path $Path)) { Row $Label 'WARN' "not found: $Path"; return }
    $sig = Get-AuthenticodeSignature -FilePath $Path
    $subject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '' }
    if ($sig.Status -eq 'Valid' -and $subject -like "*$Publisher*") {
        $ts = if ($sig.TimeStamperCertificate) { 'timestamped' } else { 'NOT timestamped' }
        Row $Label 'PASS' "signed by $($sig.SignerCertificate.GetNameInfo('SimpleName', $false)), $ts"
    } elseif ($sig.Status -eq 'Valid') {
        Row $Label 'FAIL' "signed, but by '$subject' - not AIMS LAB. Do not install."
    } elseif ($sig.Status -eq 'NotSigned') {
        Row $Label 'FAIL' 'not signed - a development build. Ask AIMS LAB for the signed release.'
    } else {
        Row $Label 'FAIL' "signature $($sig.Status): $($sig.StatusMessage). Do not install."
    }
}
if ($Setup) { Test-Signed 'Signature: setup' $Setup }
if ($installed) {
    Test-Signed 'Signature: agent' (Join-Path $InstallDir 'AIMScribe_Agent.exe')
    $unsigned = Get-ChildItem $InstallDir -Recurse -Include *.exe, *.dll, *.pyd -File |
        Where-Object { (Get-AuthenticodeSignature $_.FullName).Status -ne 'Valid' }
    if ($unsigned) {
        Row 'Signature: all files' 'FAIL' "$(@($unsigned).Count) unsigned, e.g. $($unsigned[0].Name)"
    } else {
        Row 'Signature: all files' 'PASS' 'every program file is validly signed'
    }
}

# ---------------------------------------------------------------- Windows protection
try {
    $sac = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState -ErrorAction Stop
    $state = @{ 0 = 'Off'; 1 = 'On'; 2 = 'Evaluation' }[[int]$sac.VerifiedAndReputablePolicyState]
    $detail = if ($state -eq 'Off') { 'Off' } else { "$state - only a signed AIMScribe will install" }
    Row 'Smart App Control' 'PASS' $detail
} catch { Row 'Smart App Control' 'PASS' 'not present on this Windows version' }

try {
    $mp = Get-MpComputerStatus -ErrorAction Stop
    if ($mp.RealTimeProtectionEnabled) {
        Row 'Microsoft Defender' 'PASS' "real-time protection on (signatures $($mp.AntivirusSignatureVersion))"
    } else {
        Row 'Microsoft Defender' 'WARN' 'real-time protection is OFF - turn it back on; AIMScribe does not need it off'
    }
} catch { Row 'Microsoft Defender' 'WARN' 'state not readable (another antivirus may be in charge)' }

$ss = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer' -Name SmartScreenEnabled -ErrorAction SilentlyContinue).SmartScreenEnabled
if ($ss -eq 'Off') { Row 'SmartScreen' 'WARN' 'turned off - turn it back on' } else { Row 'SmartScreen' 'PASS' 'on (Windows default)' }

# ---------------------------------------------------------------- microphone
try {
    $mics = @(Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction Stop |
        Where-Object { $_.FriendlyName -match 'microphone|mic|headset|speakerphone|jabra|input' })
    $preferred = $settings['AIMS_PREFERRED_MICROPHONE']
    if (-not $preferred) { $preferred = 'Jabra' }
    $match = $mics | Where-Object { $_.FriendlyName -like "*$preferred*" }
    if ($match) { Row 'Microphone' 'PASS' "$($match[0].FriendlyName)" }
    elseif ($mics) { Row 'Microphone' 'WARN' "'$preferred' not found; would use $($mics[0].FriendlyName)" }
    else { Row 'Microphone' 'FAIL' 'no recording device found - plug in the speakerphone' }
} catch { Row 'Microphone' 'WARN' "could not list audio devices: $($_.Exception.Message)" }

# ---------------------------------------------------------------- port 5050
$listener = Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -eq 'AIMScribe_Agent') { Row 'Port 5050' 'PASS' 'used by the AIMScribe agent' }
    else { Row 'Port 5050' 'FAIL' "used by $($proc.ProcessName) (PID $($listener.OwningProcess)) - AIMScribe needs it" }
} elseif ($installed) {
    Row 'Port 5050' 'WARN' 'free - the agent is not running (sign out and in, or start it from the Start menu)'
} else {
    Row 'Port 5050' 'PASS' 'free'
}

# ---------------------------------------------------------------- server and clock
if ($Server) {
    $base = $Server.TrimEnd('/')
    if (-not $base.StartsWith('https://')) { Row 'Server' 'FAIL' "$base is not https://" }
    $answered = $false
    foreach ($path in '/health/live', '/health') {
        try {
            $r = Invoke-WebRequest -Uri "$base$path" -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
            Row 'Server' 'PASS' "$base$path answered $($r.StatusCode)"
            $answered = $true
            $date = $r.Headers['Date']
            if ($date) {
                $offset = ((Get-Date).ToUniversalTime() - ([datetime]::Parse($date)).ToUniversalTime()).TotalSeconds
                $o = [math]::Round($offset)
                if ([math]::Abs($o) -le 60) { Row 'Clock' 'PASS' "within $o s of the server" }
                elseif ([math]::Abs($o) -le 300) { Row 'Clock' 'WARN' "$o s off the server - uploads still work; fix Windows time" }
                else { Row 'Clock' 'FAIL' "$o s off the server - set the time (Settings > Time > Sync now)" }
            }
            break
        } catch {
            $msg = $_.Exception.Message
            if ($msg -match 'trust|certificate|SSL|TLS') {
                Row 'Server' 'FAIL' "TLS problem ($msg). HTTPS inspection by antivirus? Exclude the server address from inspection."
                $answered = $true
                break
            }
        }
    }
    if (-not $answered) { Row 'Server' 'FAIL' "$base did not answer (network, firewall or proxy)" }
} else {
    Row 'Server' 'WARN' 'not checked - pass -Server https://...'
}

# ---------------------------------------------------------------- settings and task
if ($installed) {
    $origin = $settings['AIMS_ALLOWED_ORIGINS']
    if ($origin -and $origin.StartsWith('https://')) { Row 'CMED page' 'PASS' $origin }
    elseif ($origin) { Row 'CMED page' 'WARN' "$origin is not https://" }
    else { Row 'CMED page' 'FAIL' 'not configured - recording cannot start' }

    $enrolled = Test-Path (Join-Path $env:ProgramData 'AIMScribe\state\device.token')
    $staged = Test-Path (Join-Path $env:ProgramData 'AIMScribe\state\enrollment.token')
    if ($enrolled) { Row 'Enrolment' 'PASS' 'enrolled' }
    elseif ($staged) { Row 'Enrolment' 'WARN' 'token staged; enrols when the agent next starts' }
    else { Row 'Enrolment' 'FAIL' 'no enrolment token - run Setup again with /TOKEN=' }

    $task = Get-ScheduledTask -TaskName 'AIMScribe Agent' -ErrorAction SilentlyContinue
    if ($task) { Row 'Logon task' 'PASS' "registered ($($task.State)), starts at sign-in" }
    else { Row 'Logon task' 'FAIL' 'missing - run Setup again' }
} else {
    Row 'Installed' 'PASS' 'not installed yet (pre-install check)'
}

$rows | Format-Table -AutoSize -Wrap
$fails = @($rows | Where-Object Result -eq 'FAIL').Count
$warns = @($rows | Where-Object Result -eq 'WARN').Count
Write-Host ("{0} checks: {1} FAIL, {2} WARN" -f $rows.Count, $fails, $warns) -ForegroundColor ($(if ($fails) { 'Red' } elseif ($warns) { 'Yellow' } else { 'Green' }))
exit $(if ($fails) { 1 } else { 0 })
