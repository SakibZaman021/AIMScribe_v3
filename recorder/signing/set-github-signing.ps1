<#
.SYNOPSIS
    Give the cloud build its DigiCert KeyLocker credentials, checked first.

.DESCRIPTION
    Sets, in the GitHub environment "recorder-release" of SakibZaman021/AIMScribe_v3:

      secrets    SM_API_KEY, SM_CLIENT_CERT_FILE_B64, SM_CLIENT_CERT_PASSWORD
      variables  SM_HOST, SM_CODE_SIGNING_CERT_SHA1_HASH, AIMS_SIGN_PUBLISHER

    The API key and the .p12 password are asked for without echo and never
    written to disk or to the command line. Before anything is sent, the .p12
    is opened with its password, the thumbprint is checked to be 40
    hexadecimal characters, and the host must be https.

    Needs the GitHub CLI signed in as the repository's owner (gh auth status).
    INSTALL_FOR_IT.md section 7.2 says where each value comes from in
    DigiCert ONE.

.PARAMETER ClientCertificate
    The client authentication certificate (.p12) downloaded from DigiCert ONE.
.PARAMETER Thumbprint
    The SHA-1 thumbprint of the CODE-SIGNING certificate (not the .p12's).
.PARAMETER Publisher
    The code-signing certificate's exact common name (CN), e.g.
    "United International University". Every PC will require this name.
.PARAMETER SmHost
    The DigiCert ONE host. Default https://clientauth.one.digicert.com
.PARAMETER Check
    Check everything and show what would be set; send nothing to GitHub.

.EXAMPLE
    .\signing\set-github-signing.ps1 -ClientCertificate C:\keys\github-signer.p12 `
        -Thumbprint 0123456789ABCDEF0123456789ABCDEF01234567 `
        -Publisher "United International University" -Check
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ClientCertificate,
    [Parameter(Mandatory = $true)] [string] $Thumbprint,
    [Parameter(Mandatory = $true)] [string] $Publisher,
    [string] $SmHost = 'https://clientauth.one.digicert.com',
    [string] $Repository = 'SakibZaman021/AIMScribe_v3',
    [string] $Environment = 'recorder-release',
    [switch] $Check
)

$ErrorActionPreference = 'Stop'

function Read-Secret([string] $Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

# ---------------------------------------------------------------- the values, checked
$thumb = ($Thumbprint -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
if ($thumb.Length -ne 40) {
    throw "The thumbprint must be 40 hexadecimal characters (got $($thumb.Length)). " +
          "Copy the code-signing certificate's SHA-1 thumbprint from DigiCert ONE."
}
$Publisher = $Publisher.Trim()
if ($Publisher.Length -lt 3) { throw 'Give the code-signing certificate''s exact common name (CN).' }
if ($Publisher -match '[",]') {
    Write-Warning "The publisher name contains a comma or a quote. That is allowed, but check it is the CN exactly as DigiCert issued it."
}
if ($SmHost -notmatch '^https://[A-Za-z0-9.-]+/?$') { throw "SmHost must be an https address such as https://clientauth.one.digicert.com" }
$SmHost = $SmHost.TrimEnd('/')
$p12Path = (Resolve-Path -LiteralPath $ClientCertificate).Path

$apiKey = Read-Secret 'DigiCert ONE API key (input hidden)'
if ($apiKey.Length -lt 20) { throw 'That API key is too short to be one.' }
$p12Password = Read-Secret 'Password of the .p12 (input hidden)'

# The .p12 must open with that password, or the build would fail much later.
$bytes = [IO.File]::ReadAllBytes($p12Path)
try {
    $client = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
        $bytes, $p12Password,
        [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
} catch {
    throw "The .p12 does not open with that password: $($_.Exception.Message)"
}
if (-not $client.HasPrivateKey) { throw 'The .p12 holds no private key: download the client certificate again.' }
if ($client.Thumbprint -eq $thumb) {
    throw ('That thumbprint is the client authentication certificate''s, not the code-signing ' +
           'certificate''s. Copy the thumbprint of the code-signing certificate in DigiCert ONE.')
}
$b64 = [Convert]::ToBase64String($bytes)

Write-Host ''
Write-Host "Repository:   $Repository  (environment $Environment)" -ForegroundColor Cyan
Write-Host "Client cert:  $($client.Subject)  (valid until $($client.NotAfter.ToString('yyyy-MM-dd')))"
Write-Host "Host:         $SmHost"
Write-Host "Thumbprint:   $thumb"
Write-Host "Publisher:    $Publisher   <- every PC will require exactly this name"
Write-Host ''

$secrets = [ordered]@{
    SM_API_KEY              = $apiKey
    SM_CLIENT_CERT_FILE_B64 = $b64
    SM_CLIENT_CERT_PASSWORD = $p12Password
}
$variables = [ordered]@{
    SM_HOST                        = $SmHost
    SM_CODE_SIGNING_CERT_SHA1_HASH = $thumb
    AIMS_SIGN_PUBLISHER            = $Publisher
}

if ($Check) {
    Write-Host 'Check only - nothing was sent. Would set:' -ForegroundColor Yellow
    foreach ($k in $secrets.Keys)   { Write-Host "  secret    $k  ($($secrets[$k].Length) characters)" }
    foreach ($k in $variables.Keys) { Write-Host "  variable  $k = $($variables[$k])" }
    exit 0
}

# ---------------------------------------------------------------- to GitHub
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) { throw 'The GitHub CLI (gh) is not installed: winget install --id GitHub.cli -e' }
& gh auth status *> $null
if ($LASTEXITCODE -ne 0) { throw 'gh is not signed in: run  gh auth login' }
& gh api "repos/$Repository/environments/$Environment" --silent
if ($LASTEXITCODE -ne 0) { throw "The environment $Environment does not exist in $Repository, or this account cannot see it." }

foreach ($k in $secrets.Keys) {
    # Through standard input: the value never appears on a command line.
    $secrets[$k] | & gh secret set $k --env $Environment --repo $Repository
    if ($LASTEXITCODE -ne 0) { throw "Setting the secret $k failed." }
}
foreach ($k in $variables.Keys) {
    & gh variable set $k --env $Environment --repo $Repository --body $variables[$k]
    if ($LASTEXITCODE -ne 0) { throw "Setting the variable $k failed." }
}
$apiKey = $p12Password = $b64 = $null

Write-Host ''
Write-Host 'Done. Next, a dry run that signs but publishes nothing:' -ForegroundColor Green
Write-Host '  git tag recorder-v3.8.4-dryrun'
Write-Host '  git push origin recorder-v3.8.4-dryrun'
Write-Host "Then watch it:  gh run watch --repo $Repository"
