<#
.SYNOPSIS
    Sign AIMScribe's files with Authenticode, whichever certificate AIMS LAB holds.

.DESCRIPTION
    Two ways to call it:

      sign.ps1 -Folder dist\AIMScribe_Agent
          Every .exe, .dll and .pyd in the folder that is not already validly
          signed by its own maker is signed (Python's and OpenSSL's DLLs keep
          their vendors' signatures). Then every file is verified, and the
          script fails if any shipped file is left unsigned.

      sign.ps1 -File AIMScribeSetup.exe
          One file. Inno Setup calls this for the setup and its uninstaller
          (installer.iss: SignTool=aimssign).

    SHA-256 file digest, RFC 3161 timestamp with a SHA-256 digest. The
    timestamp is what keeps a signed release valid after the certificate
    itself expires.

    Which certificate, chosen by AIMS_SIGN_PROVIDER:

      azure       Azure Trusted Signing (Microsoft's signing service). Needs
                  the Trusted Signing dlib (NuGet Microsoft.Trusted.Signing.Client)
                  and a metadata.json naming the account and certificate
                  profile:
                    AIMS_SIGN_DLIB      path to Azure.CodeSigning.Dlib.dll
                    AIMS_SIGN_METADATA  path to metadata.json
                  Signs in as the Azure identity of the build (az login, or
                  AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET).

      thumbprint  A certificate in the Windows certificate store, found by its
                  SHA-1 thumbprint: an OV or EV certificate whose key is on a
                  hardware token, or in a cloud HSM through its vendor's
                  Windows key storage provider (SSL.com eSigner, DigiCert
                  KeyLocker, Sectigo). AIMS_SIGN_THUMBPRINT=<40 hex characters>.

      signpath    SignPath (free for open-source projects). Signing happens in
                  SignPath's CI, not here: the build artefact is uploaded and
                  comes back signed. This script then only verifies.

    AIMS_SIGN_TIMESTAMP  timestamp server (default: the provider's, or
                         http://timestamp.digicert.com for a store certificate)
    AIMS_SIGN_PUBLISHER  text the signer's subject must contain
                         (default "AIMS LAB")
    DEV_UNSIGNED=1       local test builds only: nothing is signed, a large
                         warning is printed, and the script still succeeds.

.EXAMPLE
    $env:AIMS_SIGN_PROVIDER = 'thumbprint'; $env:AIMS_SIGN_THUMBPRINT = '0123...'
    .\signing\sign.ps1 -Folder dist\AIMScribe_Agent
#>
[CmdletBinding(DefaultParameterSetName = 'Folder')]
param(
    [Parameter(ParameterSetName = 'Folder', Mandatory = $true)] [string] $Folder,
    [Parameter(ParameterSetName = 'File', Mandatory = $true)]   [string] $File,
    [Parameter(ParameterSetName = 'Folder')] [switch] $VerifyOnly
)

$ErrorActionPreference = 'Stop'
$Provider  = ($env:AIMS_SIGN_PROVIDER, 'thumbprint' | Where-Object { $_ } | Select-Object -First 1).ToLowerInvariant()
$Publisher = ($env:AIMS_SIGN_PUBLISHER, 'AIMS LAB' | Where-Object { $_ } | Select-Object -First 1)
$Shipped   = '*.exe', '*.dll', '*.pyd'

function Write-Banner([string] $Text, [string] $Colour = 'Yellow') {
    $line = '*' * 68
    Write-Host ''
    Write-Host $line -ForegroundColor $Colour
    foreach ($l in $Text -split "`n") { Write-Host (" " + $l) -ForegroundColor $Colour }
    Write-Host $line -ForegroundColor $Colour
    Write-Host ''
}

if ($env:DEV_UNSIGNED -eq '1') {
    Write-Banner ("UNSIGNED DEVELOPMENT BUILD - NEVER GIVE THIS TO A CLINIC.`n" +
                  "Windows SmartScreen and Smart App Control will block it, antivirus`n" +
                  "will flag it, and nobody can tell it apart from a tampered copy.") 'Red'
    exit 0
}

function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    throw ("signtool.exe not found. Install the Windows SDK signing tools: " +
           "winget install --id Microsoft.WindowsSDK.10.0.26100 -e  (only 'Signing Tools' is needed)")
}

function Test-VendorSigned([string] $Path) {
    $sig = Get-AuthenticodeSignature -FilePath $Path
    return $sig.Status -eq 'Valid'
}

function Test-OurSignature([string] $Path) {
    $sig = Get-AuthenticodeSignature -FilePath $Path
    return ($sig.Status -eq 'Valid' -and $sig.SignerCertificate.Subject -like "*$Publisher*" -and
            $null -ne $sig.TimeStamperCertificate)
}

function Invoke-Sign([string[]] $Paths) {
    if ($Provider -eq 'signpath') {
        throw ("AIMS_SIGN_PROVIDER=signpath: signing is done in SignPath's CI. Upload " +
               "dist\AIMScribe_Agent as the artefact, download the signed result, then run " +
               "sign.ps1 -Folder ... -VerifyOnly.")
    }
    $signtool = Find-SignTool
    $common = @('sign', '/fd', 'SHA256', '/td', 'SHA256', '/d', 'AIMScribe',
                '/du', 'https://github.com/SakibZaman021/AIMScribe_v3')
    switch ($Provider) {
        'azure' {
            if (-not $env:AIMS_SIGN_DLIB -or -not (Test-Path $env:AIMS_SIGN_DLIB)) {
                throw 'AIMS_SIGN_DLIB must point to Azure.CodeSigning.Dlib.dll'
            }
            if (-not $env:AIMS_SIGN_METADATA -or -not (Test-Path $env:AIMS_SIGN_METADATA)) {
                throw 'AIMS_SIGN_METADATA must point to metadata.json (Endpoint, CodeSigningAccountName, CertificateProfileName)'
            }
            $ts = ($env:AIMS_SIGN_TIMESTAMP, 'http://timestamp.acs.microsoft.com' | Where-Object { $_ } | Select-Object -First 1)
            $signArgs = $common + @('/tr', $ts, '/dlib', $env:AIMS_SIGN_DLIB, '/dmdf', $env:AIMS_SIGN_METADATA)
        }
        'thumbprint' {
            if ($env:AIMS_SIGN_THUMBPRINT -notmatch '^[0-9A-Fa-f]{40}$') {
                throw ("No certificate configured. Set AIMS_SIGN_PROVIDER (azure | thumbprint | signpath) " +
                       "and its settings, or DEV_UNSIGNED=1 for a local test build. See INSTALL_FOR_IT.md.")
            }
            $ts = ($env:AIMS_SIGN_TIMESTAMP, 'http://timestamp.digicert.com' | Where-Object { $_ } | Select-Object -First 1)
            $signArgs = $common + @('/tr', $ts, '/sha1', $env:AIMS_SIGN_THUMBPRINT)
        }
        default { throw "Unknown AIMS_SIGN_PROVIDER '$Provider' (azure | thumbprint | signpath)" }
    }
    # A few files per call: a timestamp server that drops one request then
    # costs one retry, not the whole build.
    for ($i = 0; $i -lt $Paths.Count; $i += 20) {
        $batch = $Paths[$i..([Math]::Min($i + 19, $Paths.Count - 1))]
        $ok = $false
        foreach ($attempt in 1..3) {
            & $signtool @signArgs @batch
            if ($LASTEXITCODE -eq 0) { $ok = $true; break }
            Write-Host "signtool failed (attempt $attempt); retrying in 5 s" -ForegroundColor Yellow
            Start-Sleep -Seconds 5
        }
        if (-not $ok) { throw "Signing failed for: $($batch -join ', ')" }
    }
}

function Assert-Verified([string[]] $Paths) {
    $signtool = $null
    try { $signtool = Find-SignTool } catch { }
    $bad = @()
    foreach ($p in $Paths) {
        $sig = Get-AuthenticodeSignature -FilePath $p
        if ($sig.Status -ne 'Valid') { $bad += "$p ($($sig.Status))"; continue }
        if ($signtool) {
            & $signtool verify /pa /all /q $p | Out-Null
            if ($LASTEXITCODE -ne 0) { $bad += "$p (signtool verify failed)" }
        }
    }
    if ($bad) {
        throw ("These files would ship unsigned or with a broken signature:`n  " + ($bad -join "`n  "))
    }
}

# ---------------------------------------------------------------- one file (Inno Setup)
if ($PSCmdlet.ParameterSetName -eq 'File') {
    $full = (Resolve-Path $File).Path
    Invoke-Sign @($full)
    Assert-Verified @($full)
    if (-not (Test-OurSignature $full)) { throw "$full is not signed by '$Publisher' with a timestamp" }
    Write-Host "signed: $full" -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------- the whole folder
$root = (Resolve-Path $Folder).Path
$all = @(Get-ChildItem $root -Recurse -File -Include $Shipped | Select-Object -ExpandProperty FullName)
if (-not $all) { throw "No program files under $root - build first." }

$ours = @($all | Where-Object { -not (Test-VendorSigned $_) })
Write-Host ("{0} program files: {1} already signed by their makers, {2} to sign" -f
            $all.Count, ($all.Count - $ours.Count), $ours.Count) -ForegroundColor Cyan

if (-not $VerifyOnly -and $ours) { Invoke-Sign $ours }

Assert-Verified $all
$main = Join-Path $root 'AIMScribe_Agent.exe'
if ((Test-Path $main) -and -not (Test-OurSignature $main)) {
    throw "AIMScribe_Agent.exe is not signed by '$Publisher' with a timestamp."
}
Write-Host "All $($all.Count) program files are validly signed." -ForegroundColor Green
