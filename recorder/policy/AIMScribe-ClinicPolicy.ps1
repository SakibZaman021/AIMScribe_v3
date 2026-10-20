<#
.SYNOPSIS
    Optional, explicit machine settings for a dedicated consultation PC.

.DESCRIPTION
    The AIMScribe installer only installs the program. It does not change
    Windows policy, because an installer that quietly rewrites Windows Update,
    power and time settings is exactly what antivirus products flag.

    These settings make a consultation PC more reliable, and an administrator
    may choose to apply them - here, by hand, or as the equivalent Group Policy
    / Intune profile (INSTALL_FOR_IT.md lists the exact settings):

      WindowsUpdate   Active hours 07:00-23:00, set as policy, and no automatic
                      restart while someone is signed in (no restart mid-clinic).
      Power           Closing the lid does nothing (instead of sleeping), and
                      USB selective suspend is off (so the speakerphone is not
                      switched off to save power). Active power plan only.
      Time            The Windows Time service is set to start automatically,
                      syncs from the domain plus public time servers, and
                      resyncs now. Recording does not depend on it; file names
                      and timestamps follow the PC clock.

    Every change is printed. -WhatIf shows what would change and changes nothing.
    -Undo puts each setting back to Windows' default. Settings 3.4 and earlier
    installers wrote are removed by -Undo too.

.PARAMETER Only
    Apply one group: WindowsUpdate, Power or Time. Default: all three.

.PARAMETER Undo
    Remove the settings this script (or an AIMScribe 3.4 installer) applied.

.EXAMPLE
    .\AIMScribe-ClinicPolicy.ps1 -WhatIf
.EXAMPLE
    .\AIMScribe-ClinicPolicy.ps1
.EXAMPLE
    .\AIMScribe-ClinicPolicy.ps1 -Only Power
.EXAMPLE
    .\AIMScribe-ClinicPolicy.ps1 -Undo
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('WindowsUpdate', 'Power', 'Time')]
    [string[]] $Only = @('WindowsUpdate', 'Power', 'Time'),
    [switch] $Undo
)

$ErrorActionPreference = 'Stop'

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not $WhatIfPreference -and -not (Test-Elevated)) {
    Write-Host 'Run this from an elevated PowerShell (Run as administrator), or with -WhatIf to preview.' -ForegroundColor Yellow
    exit 2
}

$changes = New-Object System.Collections.Generic.List[object]
function Note([string] $Group, [string] $Setting, [string] $Value) {
    $changes.Add([pscustomobject]@{ Group = $Group; Setting = $Setting; Value = $Value })
}

# ---------------------------------------------------------------- Windows Update
$WU = @(
    @{ Path = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings';           Name = 'ActiveHoursStart';                 Value = 7 },
    @{ Path = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings';           Name = 'ActiveHoursEnd';                   Value = 23 },
    @{ Path = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings';           Name = 'SmartActiveHoursState';            Value = 0 },
    @{ Path = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate';      Name = 'SetActiveHours';                   Value = 1 },
    @{ Path = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate';      Name = 'ActiveHoursStart';                 Value = 7 },
    @{ Path = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate';      Name = 'ActiveHoursEnd';                   Value = 23 },
    @{ Path = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU';   Name = 'NoAutoRebootWithLoggedOnUsers';    Value = 1 }
)

function Set-WindowsUpdate {
    foreach ($s in $WU) {
        $label = "$($s.Path)\$($s.Name)"
        if ($Undo) {
            if (Get-ItemProperty -Path $s.Path -Name $s.Name -ErrorAction SilentlyContinue) {
                if ($PSCmdlet.ShouldProcess($label, 'Remove')) {
                    Remove-ItemProperty -Path $s.Path -Name $s.Name
                }
                Note 'WindowsUpdate' $label '(removed - Windows default)'
            }
        } else {
            if ($PSCmdlet.ShouldProcess($label, "Set to $($s.Value)")) {
                if (-not (Test-Path $s.Path)) { New-Item -Path $s.Path -Force | Out-Null }
                New-ItemProperty -Path $s.Path -Name $s.Name -PropertyType DWord -Value $s.Value -Force | Out-Null
            }
            Note 'WindowsUpdate' $label "$($s.Value)"
        }
    }
}

# ---------------------------------------------------------------- Power
$USB_SUB = '2a737441-1930-4402-8d77-b2bebba308a3'
$USB_SUSPEND = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226'

function Invoke-PowerCfg([string[]] $PcArgs) {
    & powercfg.exe @PcArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "powercfg $($PcArgs -join ' ') failed ($LASTEXITCODE)" }
}

function Set-Power {
    # Windows defaults: lid = sleep (1), USB selective suspend = enabled (1).
    $lid = if ($Undo) { 1 } else { 0 }
    $usb = if ($Undo) { 1 } else { 0 }
    foreach ($mode in '/SETACVALUEINDEX', '/SETDCVALUEINDEX') {
        if ($PSCmdlet.ShouldProcess("active power plan $mode lid action", "Set to $lid")) {
            Invoke-PowerCfg @($mode, 'SCHEME_CURRENT', 'SUB_BUTTONS', 'LIDACTION', "$lid")
        }
        if ($PSCmdlet.ShouldProcess("active power plan $mode USB selective suspend", "Set to $usb")) {
            Invoke-PowerCfg @($mode, 'SCHEME_CURRENT', $USB_SUB, $USB_SUSPEND, "$usb")
        }
    }
    if ($PSCmdlet.ShouldProcess('active power plan', 'Apply')) {
        Invoke-PowerCfg @('/SETACTIVE', 'SCHEME_CURRENT')
    }
    Note 'Power' 'Lid close action (AC and battery)' ($(if ($Undo) { 'sleep (Windows default)' } else { 'do nothing' }))
    Note 'Power' 'USB selective suspend (AC and battery)' ($(if ($Undo) { 'enabled (Windows default)' } else { 'disabled' }))
}

# ---------------------------------------------------------------- Time
function Set-Time {
    if ($Undo) {
        if ($PSCmdlet.ShouldProcess('Windows Time', 'Restore default peers')) {
            & w32tm.exe /config /syncfromflags:DOMHIER /manualpeerlist:time.windows.com,0x9 /update | Out-Null
        }
        Note 'Time' 'Windows Time peers' 'time.windows.com / domain (Windows default)'
        return
    }
    if ($PSCmdlet.ShouldProcess('Windows Time service', 'Start automatically, sync, resync now')) {
        Set-Service -Name w32time -StartupType Automatic
        if ((Get-Service w32time).Status -ne 'Running') { Start-Service w32time }
        & w32tm.exe /config /syncfromflags:ALL '/manualpeerlist:time.windows.com,0x9 time.google.com,0x9 pool.ntp.org,0x9' /update | Out-Null
        & w32tm.exe /resync /force | Out-Null   # a blocked time server is not an error here
    }
    Note 'Time' 'Windows Time service' 'automatic, running'
    Note 'Time' 'Time sources' 'domain + time.windows.com, time.google.com, pool.ntp.org'
}

$verb = if ($Undo) { 'Undoing' } elseif ($WhatIfPreference) { 'Previewing (nothing is changed)' } else { 'Applying' }
Write-Host "AIMScribe clinic policy - $verb : $($Only -join ', ')" -ForegroundColor Cyan

if ('WindowsUpdate' -in $Only) { Set-WindowsUpdate }
if ('Power' -in $Only)         { Set-Power }
if ('Time' -in $Only)          { Set-Time }

$changes | Format-Table -AutoSize | Out-String | Write-Host
if ($WhatIfPreference) {
    Write-Host 'Nothing was changed (-WhatIf). Run again without -WhatIf, elevated, to apply.' -ForegroundColor Yellow
} else {
    Write-Host 'Done.' -ForegroundColor Green
}
