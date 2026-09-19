<#
.SYNOPSIS
    Installs the AIMScribe agent on a doctor PC. Run elevated.

.DESCRIPTION
    Replaces install_autostart.bat, which dropped a shortcut in the user's Startup
    folder pointing at a user-writable path. Any malware running as that user could
    swap the target and inherit the autostart. This installer instead:

      * installs to %ProgramFiles%\AIMScribe, writable only by Administrators
      * generates a unique local API key per install
      * writes machine state to %ProgramData%\AIMScribe with restricted ACLs
      * registers a Scheduled Task that starts the agent at logon and restarts it
        if it exits, which is the watchdog until the Windows service ships
      * refuses to run if the executable is unsigned, unless explicitly overridden

    Audio capture must live in the user's session — a session 0 service has no
    default audio endpoint — so the agent runs as a logon task, not as a service.

.EXAMPLE
    .\install.ps1 -BackendUrl https://aimslab.internal -CmedOrigin https://cmed.aimslab.example
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $BackendUrl,
    [Parameter(Mandatory = $true)][string] $CmedOrigin,
    [string] $EnrollmentToken,
    [string] $SourceDir     = (Join-Path $PSScriptRoot 'dist\AIMScribe_Agent'),
    # The two public keys every agent needs. Identical on every PC, so they
    # ship with the installer rather than being copied by hand twenty times.
    [string] $KeysDir       = (Join-Path $PSScriptRoot 'keys'),
    [string] $InstallDir    = (Join-Path $env:ProgramFiles 'AIMScribe'),
    [string] $DataDir       = (Join-Path $env:ProgramData 'AIMScribe'),
    [int]    $SpoolGb       = 40,
    [switch] $AllowUnsigned
)

$ErrorActionPreference = 'Stop'

function Assert-Elevated {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This installer must be run from an elevated PowerShell prompt.'
    }
}

Write-Host '============================================================'
Write-Host ' AIMScribe Agent - Install'
Write-Host '============================================================'

Assert-Elevated

if (-not (Test-Path $SourceDir)) {
    throw "Build output not found at $SourceDir. Run BUILD.bat first."
}

$exeSource = Join-Path $SourceDir 'AIMScribe_Agent.exe'
if (-not (Test-Path $exeSource)) { throw "AIMScribe_Agent.exe not found in $SourceDir." }

# ---- signature ----
$signature = Get-AuthenticodeSignature $exeSource
if ($signature.Status -ne 'Valid') {
    if (-not $AllowUnsigned) {
        throw ("Executable signature is '$($signature.Status)'. A clinical PC must run a " +
               "signed build so tampering is detectable. Re-run with -AllowUnsigned only for testing.")
    }
    Write-Warning "Installing an UNSIGNED build ($($signature.Status)). Testing only."
} else {
    Write-Host "Signature valid: $($signature.SignerCertificate.Subject)"
}

# ---- files ----
#
# Every install is a replacement, because this is also the upgrade path and a
# partial install from a failed run leaves files behind. Copy-Item -Force cannot
# overwrite a file that is locked or has had its inheritance stripped, and fails
# with a bare "Access denied" that says nothing about why.
Write-Host "Installing to $InstallDir ..."

$task = Get-ScheduledTask -TaskName 'AIMScribe Agent' -ErrorAction SilentlyContinue
if ($task) {
    Write-Host '  stopping the existing agent'
    Stop-ScheduledTask -TaskName 'AIMScribe Agent' -ErrorAction SilentlyContinue
}
Get-Process -Name 'AIMScribe_Agent' -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "  stopping running agent (PID $($_.Id))"
    Stop-Process -Id $_.Id -Force
}
Start-Sleep -Seconds 2

if (Test-Path $InstallDir) {
    Write-Host '  removing the previous version'
    # Restore inheritance first: a previous run may have stripped it, and the
    # program files carry nothing worth preserving - configuration and audio
    # live in DataDir, which is never touched here.
    & icacls $InstallDir /reset /T /Q 2>&1 | Out-Null
    try {
        Remove-Item $InstallDir -Recurse -Force -ErrorAction Stop
    }
    catch {
        throw ("Could not replace $InstallDir : $($_.Exception.Message)`n" +
               "Something is holding a file open - antivirus, Explorer, or a " +
               "running agent. Close it, or reboot, and run this again.")
    }
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item (Join-Path $SourceDir '*') $InstallDir -Recurse -Force

# ---- pinned public keys ----
#
# cmed_grant_pub.pem verifies the recording grant; without it the agent refuses
# to record. aimslab_receipt_pub.pem verifies purge receipts; without it local
# audio is never deleted, which is the safe direction but fills the disk.
$keyTargets = @{
    'cmed_grant_pub.pem'      = 'grant verification'
    'aimslab_receipt_pub.pem' = 'purge receipt verification'
}
$missingKeys = @()
foreach ($keyName in $keyTargets.Keys) {
    $src = Join-Path $KeysDir $keyName
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $DataDir 'keys') -Force
        Write-Host "  installed $keyName ($($keyTargets[$keyName]))"
    } else {
        $missingKeys += $keyName
    }
}

Write-Host "Preparing machine state in $DataDir ..."
foreach ($sub in @('spool', 'keys', 'logs', 'state')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataDir $sub) | Out-Null
}

# Administrators and SYSTEM full control; the interactive user gets Modify so the
# agent can write its spool and logs, but cannot replace the program files.
#
# Set on the folder only, and let (OI)(CI) propagate. Adding /T here strips
# inherited permissions from every existing FILE, while (OI)(CI) grants attach
# only to directories - so every file is left with an empty permission list and
# becomes unreadable by everyone, including its owner. That silently locked the
# agent out of its own device key and its spooled audio.
#
# /reset first, because a previous run may already have done exactly that.
& icacls $DataDir /reset /T /C /Q 2>&1 | Out-Null
& icacls $DataDir /inheritance:r `
    /grant:r 'SYSTEM:(OI)(CI)F' `
    /grant:r 'Administrators:(OI)(CI)F' `
    /grant:r 'Users:(OI)(CI)M' /Q | Out-Null

& icacls $InstallDir /reset /T /C /Q 2>&1 | Out-Null
& icacls $InstallDir /inheritance:r `
    /grant:r 'SYSTEM:(OI)(CI)F' `
    /grant:r 'Administrators:(OI)(CI)F' `
    /grant:r 'Users:(OI)(CI)RX' /Q | Out-Null

# ---- configuration ----
# RandomNumberGenerator::GetBytes(int) is a static added in .NET 5. Windows
# PowerShell 5.1 - which is what ships on a hospital PC and is what an
# administrator will actually run this in - is on .NET Framework, where that
# static does not exist and the installer dies before writing anything.
# Create() plus the instance method works on both.
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $apiKeyBytes = New-Object byte[] 32
    $rng.GetBytes($apiKeyBytes)
    $apiKey = [Convert]::ToBase64String($apiKeyBytes)
}
finally {
    $rng.Dispose()
}
$spoolBytes = [int64]$SpoolGb * 1GB

$envPath = Join-Path $InstallDir '.env'
$envBody = @"
# Generated by install.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'). Do not edit by hand.
AIMS_BACKEND_URL=$BackendUrl
# Protocol 2. The v1 prefix serves the transcription API and has none of the
# enrolment or session endpoints, so an agent pointed there fails at enrolment.
AIMS_BACKEND_API_PREFIX=/api/v2

# mTLS is not used. Render terminates TLS itself and offers no client
# certificates, so the agent authenticates with the device bearer token issued
# at enrolment. Set these only if the backend moves somewhere that requires one.
AIMS_CLIENT_CERT_PATH=
AIMS_CLIENT_KEY_PATH=

AIMS_SAMPLE_RATE=44100
AIMS_CHANNELS=1
AIMS_SAMPLE_WIDTH=2
AIMS_FRAMES_PER_BUFFER=2048

AIMS_SEGMENT_MIN_SECONDS=170
AIMS_SEGMENT_MAX_SECONDS=190
AIMS_SILENCE_RMS=320
AIMS_SILENCE_HOLD_SECONDS=1.0

AIMS_SPOOL_DIR=$DataDir\spool
AIMS_SPOOL_MAX_BYTES=$spoolBytes
AIMS_PURGE_GRACE_HOURS=24

AIMS_BIND_HOST=127.0.0.1
AIMS_BIND_PORT=5050
AIMS_ALLOWED_ORIGINS=$CmedOrigin
AIMS_ALLOWED_HOSTS=localhost:5050,127.0.0.1:5050,[::1]:5050
AIMS_LOCAL_API_KEY=$apiKey
AIMS_REQUIRE_GRANT=true
AIMS_ENABLE_DOCS=false

AIMS_GRANT_PUBLIC_KEY_PATH=$DataDir\keys\cmed_grant_pub.pem
AIMS_GRANT_ISSUER=cmed
AIMS_GRANT_AUDIENCE=aimscribe-recorder
AIMS_RECEIPT_PUBLIC_KEY_PATH=$DataDir\keys\aimslab_receipt_pub.pem

AIMS_PAUSE_SELF_AUTHORISE_SECONDS=300

AIMS_HEARTBEAT_SECONDS=30
AIMS_LOG_LEVEL=INFO
AIMS_LOG_RETENTION_DAYS=30
AIMS_REDACT_LOGS=true
AIMS_ALLOW_PLAINTEXT_KEYSTORE=false
"@
Set-Content -Path $envPath -Value $envBody -Encoding utf8
& icacls $envPath /inheritance:r `
    /grant:r 'SYSTEM:F' /grant:r 'Administrators:F' /grant:r 'Users:R' /Q | Out-Null

# ---- enrollment ----
if ($EnrollmentToken) {
    $tokenPath = Join-Path $DataDir 'state\enrollment.token'
    Set-Content -Path $tokenPath -Value $EnrollmentToken -Encoding utf8 -NoNewline
    & icacls $tokenPath /inheritance:r `
        /grant:r 'SYSTEM:F' /grant:r 'Administrators:F' /grant:r 'Users:R' /Q | Out-Null
    Write-Host 'Enrollment token staged. The agent enrolls itself on first start.'
} else {
    Write-Warning ('No -EnrollmentToken supplied. The agent will start but refuse to ' +
                   'record until it is enrolled.')
}

# ---- autostart ----
Write-Host 'Registering the logon task ...'
$exe = Join-Path $InstallDir 'AIMScribe_Agent.exe'

$action    = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $InstallDir
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -GroupId 'S-1-5-32-545' -RunLevel Limited  # Users
$settings  = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -DontStopOnIdleEnd `
                -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount 999 `
                -RestartInterval (New-TimeSpan -Minutes 1) `
                -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'AIMScribe Agent' -Force `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'AIMScribe clinical audio agent. Starts at logon and restarts if it stops.' | Out-Null

# Remove the insecure v1 autostart if this machine is being upgraded.
$legacy = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\AIMScribe Recorder.lnk'
if (Test-Path $legacy) {
    Remove-Item $legacy -Force
    Write-Host 'Removed the old Startup-folder shortcut.'
}

Write-Host ''
Write-Host '============================================================'
Write-Host ' INSTALLED'
Write-Host '============================================================'
Write-Host " Program:  $InstallDir"
Write-Host " Data:     $DataDir"
Write-Host " Spool:    $SpoolGb GB (about $([math]::Round($SpoolGb * 1GB / (44100*2*3600), 1)) hours of audio)"
Write-Host ''
Write-Host ' STILL REQUIRED before this PC can record:'
if (-not $EnrollmentToken) {
    Write-Host "   * Re-run with -EnrollmentToken, or write the token to $DataDir\state\enrollment.token"
}
Write-Host "   1. Place the mTLS client certificate at $DataDir\keys\device.crt and .key"
Write-Host "   2. Copy the CMED grant public key to $DataDir\keys\cmed_grant_pub.pem"
Write-Host "   3. Copy the AIMS LAB receipt public key to $DataDir\keys\aimslab_receipt_pub.pem"
Write-Host ''
Write-Host ' Give this key to the CMED deployment for this PC:'
Write-Host "   AIMS_LOCAL_API_KEY = $apiKey"
Write-Host ''
Write-Host ' The agent starts at next logon, or start it now from the install folder.'
Write-Host ''
