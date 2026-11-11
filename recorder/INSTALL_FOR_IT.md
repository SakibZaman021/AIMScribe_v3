# Installing AIMScribe on clinic PCs — for IT

AIMScribe should install like Zoom or WhatsApp: double-click (or one silent
command), no warnings, **without turning off any Windows protection**. If
Windows or an antivirus blocks it, that is a fault in our release, not a
setting for you to change — tell AIMS LAB.

> **Never disable Microsoft Defender, SmartScreen, Smart App Control or your
> antivirus to install AIMScribe.** A release that needs that is not a release.

---

## 1. Why it was being blocked (and what changed)

| Before (3.4 and earlier) | Now (3.5) |
|---|---|
| Nothing was code-signed: Windows showed "Unknown publisher", Smart App Control blocked it outright | Every program file, the installer and the uninstaller are signed by **AIMS LAB, United International University**, with a timestamp |
| The installer changed Windows Update, power-plan and time-service settings | The installer only installs. Those settings are a separate, optional, signed step you choose to run (section 5) |
| The agent was re-launched every minute all day and could not be ended by Windows | Started once at sign-in; Windows restarts it up to 3 times if it fails |
| Setup force-killed the running agent | Setup asks the agent to finish its recording and exit; force only after 30 s |
| An installer built without settings pointed PCs at an old server | The installer cannot be built without the server and CMED addresses |
| Every PC had to be clicked through by hand | One silent command (section 3) |

**The PC needs a TPM 2.0 security chip, switched on** (every Windows 11 PC
has one; most Windows 10 PCs since 2016 do). From 3.6 the PC's identity key is
created inside that chip during setup: programs on the PC can ask the chip to
sign, but nothing - not an administrator, not malware - can copy the key off
the PC. Check with `tpmtool getdeviceinformation` ("TPM Present: True",
"TPM Version: 2.0"). A PC enrolled before 3.6 keeps its existing key and
updates normally.

---

## 2. Before you install: check the file

Right-click `AIMScribeSetup.exe` → **Properties → Digital Signatures**. It must
show **AIMS LAB, United International University**, with a timestamp. Or, in
PowerShell:

```powershell
Get-AuthenticodeSignature .\AIMScribeSetup.exe | Format-List Status, SignerCertificate, TimeStamperCertificate
# Status must be: Valid
```

Compare the SHA-256 with the one AIMS LAB sends with each release
(`AIMScribeSetup.exe.sha256`):

```powershell
Get-FileHash .\AIMScribeSetup.exe -Algorithm SHA256
```

The PC check (read-only, changes nothing) says the same and more:

```powershell
powershell -ExecutionPolicy Bypass -File .\AIMScribe-Check.ps1 -Setup .\AIMScribeSetup.exe -Server https://<server>
```

It reports PASS / WARN / FAIL for: signature, Smart App Control, Defender,
SmartScreen, the microphone, the clock against the server, port 5050, the
server, the CMED page, enrolment and the startup task. After installing it is
in the Start menu as **AIMScribe – check this PC**.

---

## 3. Installing

**By hand.** Run `AIMScribeSetup.exe` as an administrator. It asks for the
server address, CMED's web address and the enrolment token for this PC.

**Silently, from a script or a management tool:**

```
AIMScribeSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART ^
    /BACKEND=https://<the AIMS LAB server> ^
    /ORIGIN=https://<CMED's page> ^
    /TOKEN=<the enrolment token for THIS PC> ^
    /LOG="%TEMP%\aimscribe-setup.log"
```

Optional: `/PIN=<pin>[,<backup pin>]` (the server's TLS key pins, from AIMS LAB),
`/MICROPHONE=<name>` (default `Jabra`).

| Exit code | Meaning |
|---|---|
| 0 | Installed, and the startup task exists |
| 7 | Refused before copying anything — a value was missing or wrong (the log says which) |
| 8 | Files installed but the startup task could not be registered — run again |
| 9 | This PC has no usable TPM 2.0 chip — switch it on in the BIOS/UEFI settings, or use another PC (new PCs only; an enrolled PC still updates) |
| other | Inno Setup's standard codes (e.g. 2 = cancelled, 5 = file in use) |

An **upgrade** keeps the PC's own server, CMED address, local key and
enrolment: run the new installer the same way, no token needed.

**Uninstall** removes the program and the startup task. Recordings not yet
delivered to the server are deliberately kept in `C:\ProgramData\AIMScribe`.

### Fleet tools

- **Intune**: wrap with the Win32 Content Prep Tool
  (`IntuneWinAppUtil.exe -c . -s AIMScribeSetup.exe -o out`). Install command:
  the silent command above. Detection rule: file
  `C:\Program Files\AIMScribe\AIMScribe_Agent.exe`, version ≥ 3.6.0.
  Return codes 8 and 9 = failed.
- **PDQ / SCCM / GPO startup script**: the same command.
- **winget**: a manifest pointing at the signed release with its SHA-256 can be
  published later; until then use the file from AIMS LAB.

---

## 4. What the installer does — the whole list

1. Copies the signed program to `C:\Program Files\AIMScribe`.
2. Writes the settings file there (`.env`), readable but not changeable by
   ordinary users.
3. Creates `C:\ProgramData\AIMScribe\spool`, `logs`, `state`, `keys`
   (ordinary users may write here; the agent runs as the signed-in doctor
   because Windows only gives a signed-in user the microphone).
4. Registers the task **AIMScribe Agent**: at sign-in, least privilege,
   restarted up to 3 times if it fails.
5. Registers the task **AIMScribe Updater** (3.8.4+): SYSTEM, at start-up and
   every 30 minutes, installing only updates the AIMS LAB server offers that
   are signed by AIMS LAB, between consultations (section 7). Uninstalling
   removes both tasks.

Nothing else. No registry policy, no services, no firewall rules (the agent
listens on 127.0.0.1:5050 only, which never leaves the PC), no drivers.

---

## 5. Optional: clinic PC policy (separate, explicit)

A PC used only for consultations is more reliable with three Windows settings.
They are **not** applied by the installer. Apply them deliberately, either with
the signed script or as your own Group Policy / Intune profile.

```powershell
# Preview - changes nothing:
& "C:\Program Files\AIMScribe\tools\AIMScribe-ClinicPolicy.ps1" -WhatIf
# Apply (elevated):
& "C:\Program Files\AIMScribe\tools\AIMScribe-ClinicPolicy.ps1"
# One group only:  -Only Power      Put everything back:  -Undo
```

| Group | Setting | Group Policy / Intune equivalent |
|---|---|---|
| Windows Update | Active hours 07:00–23:00, no automatic restart with users signed in | *Computer Configuration → Administrative Templates → Windows Components → Windows Update → Manage end user experience*: "Turn off auto-restart for updates during active hours" (07–23) and "No auto-restart with logged on users for scheduled automatic updates installations". Intune: Update rings → Active hours 7–23 |
| Power | Lid close = do nothing; USB selective suspend = disabled (so the speakerphone is not powered off) | *Administrative Templates → System → Power Management → Button Settings*: "Select the lid switch action" = Take no action (plugged in and on battery). USB selective suspend: Intune Settings catalog → Power → "USB selective suspend" or `powercfg` in a startup script |
| Time | Windows Time service automatic; domain + public time servers | Domain PCs already sync from the domain controller. Otherwise *System → Windows Time Service → Time Providers → Configure Windows NTP Client* |

PCs that had AIMScribe 3.4 or earlier already carry the Windows Update values
(that installer wrote them). `-Undo` removes them if you do not want them.

---

## 6. For AIMS LAB: signing and each release

### Getting a code-signing certificate (once)

The certificate must name the same publisher every time —
**AIMS LAB, United International University** — because SmartScreen builds its
trust per publisher.

> **Decided 9 Nov 2026: an OV certificate from DigiCert, in KeyLocker**
> (option 2). Azure Trusted Signing (option 1) is not open to organisations
> in Bangladesh. Section 7.2 has the purchase, the GitHub settings and the
> dry run. A local build uses the same certificate once DigiCert's KeyLocker
> tools are installed on the build PC (`smctl windows ksp register`,
> `smctl windows certsync`), with `AIMS_SIGN_PROVIDER=thumbprint` and the
> thumbprint below.

The options that were weighed:

1. **Azure Trusted Signing** (Microsoft). Low monthly cost, keys held by
   Microsoft, short-lived certificates, strong SmartScreen standing.
   *Eligibility must be checked*: identity validation for organisations has
   been limited to certain countries; confirm UIU / AIMS LAB in Bangladesh can
   be validated before relying on it. Build settings:
   `AIMS_SIGN_PROVIDER=azure`, `AIMS_SIGN_DLIB=<path>\Azure.CodeSigning.Dlib.dll`,
   `AIMS_SIGN_METADATA=<path>\metadata.json` (template: `signing\metadata.example.json`).
2. **An OV code-signing certificate** from a public CA (DigiCert, Sectigo,
   SSL.com, GlobalSign). Since 2023 the key must live on a hardware token or
   in the CA's cloud HSM (SSL.com eSigner, DigiCert KeyLocker), which installs
   a Windows key storage provider so `signtool` can use it like a normal
   certificate. Build settings: `AIMS_SIGN_PROVIDER=thumbprint`,
   `AIMS_SIGN_THUMBPRINT=<40 hex characters>`. An EV certificate no longer gives
   instant SmartScreen trust, so it is not worth the extra cost for that alone.
3. **SignPath Foundation** — free signing for open-source projects (our
   repositories are public). Signing happens in SignPath's CI: upload
   `dist\AIMScribe_Agent` and the setup as the artefact, download the signed
   result, then `signing\sign.ps1 -Folder dist\AIMScribe_Agent -VerifyOnly`.
   `AIMS_SIGN_PROVIDER=signpath`.

The build machine needs the signing tool once:
`winget install --id Microsoft.WindowsSDK.10.0.26100 -e` (only "Signing Tools").

### Building a release

```bat
set AIMS_SIGN_PROVIDER=thumbprint
set AIMS_SIGN_THUMBPRINT=<thumbprint>
set AIMS_BACKEND=https://<the AIMS LAB server>
set AIMS_ORIGIN=https://<CMED's page>
BUILD.bat
```

BUILD.bat installs the pinned, hash-checked dependencies
(`requirements.lock`), audits them, runs the tests, builds from
`AIMScribe_Agent.spec` (onedir, no UPX), **signs every file and fails if any
shipped file is left unsigned**, builds the signed installer (Inno Setup signs
the setup and its uninstaller through `signing\sign.ps1`), and writes
`AIMScribeSetup.exe.sha256`.

`DEV_UNSIGNED=1` builds without a certificate for a developer's own PC. It
prints a warning; never give that file out.

### Each release, before it goes to clinics

1. **Submit to Microsoft** at <https://www.microsoft.com/en-us/wdsi/filesubmission>
   as *Software developer*, "Incorrectly detected / I believe this file is
   safe", for both `AIMScribeSetup.exe` and `AIMScribe_Agent.exe`. Keep the
   submission IDs with the release. If a clinic uses ESET, Kaspersky or another
   product, submit to that vendor's false-positive form too.
2. **Install on one test PC** with Defender, SmartScreen and Smart App Control
   all on, then run **AIMScribe – check this PC**: no FAIL rows.
3. Send IT the setup and its `.sha256`.

### How SmartScreen trust builds

A new certificate starts with no reputation, so the first releases may still
show a SmartScreen notice ("Windows protected your PC" with **More info → Run
anyway** showing *AIMS LAB* as the publisher — never "Unknown publisher").
The notice fades as the same signed publisher accumulates clean installs.
Never re-sign with a different certificate or publisher name: that resets it.
Files copied from a network share or deployed by Intune carry no "downloaded
from the internet" mark and are not shown the notice at all.

### If antivirus still flags the program

- The launcher PyInstaller ships is byte-identical in every PyInstaller
  program, including malware, and heuristic scanners sometimes flag it. Build
  with `AIMS_BUILD_BOOTLOADER=1` (needs Visual Studio Build Tools) to compile
  our own.
- If that is not enough, the program can be compiled with **Nuitka**
  (`python -m nuitka --standalone --windows-console-mode=disable
  --include-package=uvicorn,pystray,jwt,cryptography main.py`), which produces
  native code that scanners rarely flag. The signing step is unchanged.

### The older AIMScribe.exe (v2 / earlier v3 builds)

The same pipeline signs them: build the old program's folder, then

```bat
powershell -ExecutionPolicy Bypass -File <AIMScribe_v3>\recorder\signing\sign.ps1 -Folder <old>\dist\<program folder>
```

and add `SignTool=aimssign $f` / `SignedUninstaller=yes` (under
`#ifdef SignedBuild`) to its Inno Setup script, compiled with the same `/S`
option BUILD.bat uses. Better still, move those PCs to 3.5: an upgrade keeps
their settings and enrolment.

---

## 7. Automatic updates (3.8.4 and later)

From 3.8.4 a PC updates itself. Nobody visits it and nobody runs a setup: a
version reaches the PCs an AIMS LAB admin chooses, and each PC installs it
between consultations. IT installs by hand **once** (section 7.3); every
version after that arrives by itself.

### 7.1 How it works, and what it will not do

```
git tag recorder-v3.8.5          GitHub Actions (recorder-release.yml)          AIMS LAB dashboard
git push origin recorder-v3.8.5  -> build, test, sign (DigiCert KeyLocker, OV) -> PCs -> Recorder updates:
                                 -> refuse anything unsigned                       add 3.8.5, pick a stage
                                 -> GitHub Release: AIMScribeSetup.exe + .sha256   (off / test PCs /
                                                                                    hospitals / all, pause)
Each PC, inside the stage:
  agent (the doctor, least privilege)   heartbeat reply carries a notice signed by the server
                                        -> C:\ProgramData\AIMScribe\state\update_notice.json
  task "AIMScribe Updater" (SYSTEM,     installs only when ALL hold, else does nothing:
   at start-up and every 30 minutes)     1. the notice's signature checks against the server key
                                            pinned under Program Files, under its own signing
                                            domain (a CMED address list or a purge receipt
                                            signed with the same key is refused)
                                         2. it names this PC, is from the last 7 days, and its
                                            release number is not older than one already seen
                                         3. the version is newer than the installed one, or the
                                            server signed it as a rollback
                                         4. the download's SHA-256 is the notice's
                                         5. its Authenticode signature is valid and its signer
                                            (certificate CN or O) is exactly AIMS LAB
                                         6. no consultation is recording (it waits)
                                        then: AIMScribeSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

- The setup stops the agent politely (an open recording is closed and kept),
  keeps the PC's enrolment, TPM key and settings, and starts the agent again
  for the signed-in doctor - exactly as a manual upgrade does.
- The download is kept in SYSTEM's own profile folder
  (`C:\Windows\System32\config\systemprofile\AppData\Local\AIMScribe\updater`),
  which no ordinary user can open: the file checked is the file run.
- A version that fails three times is not tried again on that PC; the
  dashboard shows **Gave up** with the reason and raises one alert a day.
  Adding the version again (a new release number) gives it three new tries.
- **Pause** stops every PC that has not started installing within a minute.
  A PC already running setup finishes.
- The updater's own log: `C:\ProgramData\AIMScribe\logs\updater.log`. Setup's
  log of the last install: `setup.log` in the SYSTEM folder above.
- The dashboard's **PCs** table shows every PC's version and what its updater
  last said (Waiting, Installing, Installed, Failed, Gave up, Refused).
  `AIMS_MIN_RECORDER_VERSION` on the server still raises *recorder_outdated*
  for anything older, as before.

### 7.2 For AIMS LAB: the certificate and the cloud build (once)

> **Status (9 Nov 2026): no code-signing certificate exists yet.** Every
> AIMScribe build so far, 3.8.3 included, is unsigned, so the updater - which
> requires an Authenticode signature by `AIMS_SIGN_PUBLISHER` - has nothing it
> may install until the certificate below is in place. (Azure Trusted Signing
> is not an option: it issues public-trust certificates only to organisations
> in the USA, Canada, the EU and the UK.)

**The chosen route: an OV code-signing certificate from DigiCert, kept in
DigiCert KeyLocker.** The private key lives in DigiCert's HSM and never
touches a PC or GitHub; the build signs through it. DigiCert, or a DigiCert
reseller, sells it with *KeyLocker* as the delivery method (not a USB token).

**A. Buy it (AIMS LAB / UIU administration - a purchase only they can make)**

1. Order an **OV Code Signing** certificate with **KeyLocker** delivery. The
   organisation on the order must be a legal entity the CA can verify -
   almost certainly *United International University* (AIMS LAB is part of
   it). The certificate's name is that legal name, and that is the publisher
   every PC will check. Have ready: the university's registration or charter
   (UGC), its address and main phone number as listed in a public directory,
   and an authorised signatory for the verification call.
2. Ask, before paying: **how many signatures a year are included.** One
   release uses about **27** (24 Python modules, the program, the setup and
   its uninstaller; the other 29 files carry their makers' signatures), and
   each dry run uses as many. 300 a year covers about ten releases.
3. When it is issued, in **DigiCert ONE -> KeyLocker**:
   - **Settings -> Users / Service users**: create a service user for GitHub;
     create its **API key** (shown once).
   - **Client authentication certificate**: create one for that user; download
     the `.p12` and keep its **password**.
   - **Certificates**: note the code-signing certificate's **SHA-1 thumbprint**
     (40 hexadecimal characters) and its **subject** (`CN=...`, `O=...`).
   - Note the **host** shown for the API (for example
     `https://clientauth.one.digicert.com`).

**B. Give it to the build (GitHub -> Settings -> Environments -> `recorder-release`)**

The repository is public, so **no secret is ever committed**. The environment
already exists (created 9 Nov 2026): only tags matching `recorder-v*` may use
it, and `AIMS_BACKEND` is set. Optionally add yourself as a required reviewer,
so each build waits for a click before it can sign anything.

| Kind | Name | Value |
|---|---|---|
| secret | `SM_API_KEY` | the service user's API key |
| secret | `SM_CLIENT_CERT_FILE_B64` | the `.p12`, base64: PowerShell `[Convert]::ToBase64String([IO.File]::ReadAllBytes('C:\path\client.p12'))` |
| secret | `SM_CLIENT_CERT_PASSWORD` | the `.p12` password |
| variable | `SM_HOST` | the DigiCert ONE host from A3 |
| variable | `SM_CODE_SIGNING_CERT_SHA1_HASH` | the certificate's SHA-1 thumbprint (public, not a secret) |
| variable | `AIMS_SIGN_PUBLISHER` | the certificate's **exact** CN (or O), e.g. `United International University` |
| variable (optional) | `AIMS_SIGN_PROVIDER` | `keylocker` (default); `azure` or `signpath` only if one of those ever becomes the route |
| variable (optional) | `ALLOW_VULNERABLE_BUILD` | `true` only to release past a pip-audit finding you have reviewed |

**The easy way:** `signing\set-github-signing.ps1` asks for the API key and
the `.p12` password without showing them, checks that the `.p12` opens, that
the thumbprint is 40 characters and is the code-signing certificate's (not the
`.p12`'s), then sets all six. Run it with `-Check` first to see what it would
set; nothing is sent then.

```powershell
cd recorder
.\signing\set-github-signing.ps1 -ClientCertificate C:\keys\github-signer.p12 `
    -Thumbprint <code-signing certificate's SHA-1> `
    -Publisher "<the certificate's exact CN>" -Check
# then the same without -Check
```

Or by hand, if `gh` is signed in as the repository's owner:

```powershell
gh secret set SM_API_KEY --env recorder-release
gh secret set SM_CLIENT_CERT_PASSWORD --env recorder-release
[Convert]::ToBase64String([IO.File]::ReadAllBytes('C:\path\client.p12')) | gh secret set SM_CLIENT_CERT_FILE_B64 --env recorder-release
gh variable set SM_HOST --env recorder-release --body "https://clientauth.one.digicert.com"
gh variable set SM_CODE_SIGNING_CERT_SHA1_HASH --env recorder-release --body "<40 hex characters>"
gh variable set AIMS_SIGN_PUBLISHER --env recorder-release --body "<the certificate's exact CN>"
```

(`gh secret set` without `--body` asks for the value, so it is never typed
on a command line or kept in shell history.)

**C. Prove it with a dry run, then release**

```powershell
git tag recorder-v3.8.4-dryrun
git push origin recorder-v3.8.4-dryrun
```

The *recorder-release* run installs DigiCert's tools (checked to be signed by
DigiCert), syncs the certificate from KeyLocker, prints **Signing
certificate: CN=...**, builds, runs the tests, signs, and checks every file -
and publishes nothing; the signed setup is kept as a build artefact for a
week. If it fails on the publisher, it prints the certificate's actual
subject: set `AIMS_SIGN_PUBLISHER` to its CN exactly and push another dry-run
tag. When a dry run is green, push the real tag `recorder-v3.8.4`.

**How the name is used.** The build refuses a setup whose signer's CN/O is
not `AIMS_SIGN_PUBLISHER`, and the installer writes the same name into each
PC's `.env` (`AIMS_UPDATE_PUBLISHER`), which its updater then requires. A PC
keeps that value across updates. Changing the certificate's name later (a
different legal entity) means one manual install of a release carrying the
new name. A renewed certificate with the same name needs nothing: only the
thumbprint variable changes.

**Alternatives kept in the workflow** (not the route now):
*Azure Trusted Signing* (`AIMS_SIGN_PROVIDER=azure`) needs secrets
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (an app
registration with a federated credential for
`repo:SakibZaman021/AIMScribe_v3:environment:recorder-release` and the role
*Trusted Signing Certificate Profile Signer*) and variables
`AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT`, `AZURE_SIGNING_PROFILE`.
*SignPath* (`AIMS_SIGN_PROVIDER=signpath`) needs secret `SIGNPATH_API_TOKEN`
and variables `SIGNPATH_ORGANIZATION_ID`, `SIGNPATH_PROJECT_SLUG`,
`SIGNPATH_POLICY_SLUG`, `SIGNPATH_ARTIFACT_CONFIG_ZIP`,
`SIGNPATH_ARTIFACT_CONFIG_PE`, and a policy that accepts API submissions.
Neither has been run.

### 7.3 For IT: the one manual install (3.8.4)

3.8.3 and earlier have no updater, so each PC needs 3.8.4 installed once, the
way every release has been installed until now (section 3). Then:

1. **Check the file** as in section 2: `Get-AuthenticodeSignature` says
   `Valid` and *AIMS LAB*; `Get-FileHash` equals the `.sha256` on the GitHub
   Release page.
2. Install it - by hand or silently - while no consultation is recording.
3. On the PC, as administrator:

   ```powershell
   schtasks /Query /TN "AIMScribe Updater" /V /FO LIST | Select-String "Run As User|Task To Run|Repeat|Status"
   ```

   Expected: *Run As User* `SYSTEM`; *Task To Run* `"C:\Program Files\AIMScribe\AIMScribe_Agent.exe" --update`;
   repeats every 30 minutes; also *At system start up*.

   ```powershell
   Select-String -Path "C:\Program Files\AIMScribe\.env" -Pattern "AIMS_UPDATE_PUBLISHER"
   schtasks /Run /TN "AIMScribe Updater"
   Get-Content C:\ProgramData\AIMScribe\logs\updater.log -Tail 5
   ```

   Expected: `AIMS_UPDATE_PUBLISHER=AIMS LAB` (the certificate's name), and
   `Update: idle` in the log (no release is offered yet).
4. On the dashboard the PC shows version **3.8.4**.

Nothing else changes for the doctor. A PC that misses this step keeps working
on its old version and shows *recorder_outdated* once the server's minimum is
raised.

### 7.4 Test plan: the first automatic update (a canary)

Done once with a throwaway version, before any clinic depends on automatic
updates. Use two bench PCs first (A and B), then one clinic PC.

| # | Do | Expect |
|---|---|---|
| 1 | Install 3.8.4 by hand on bench PCs A and B (7.3). | Both on the dashboard at 3.8.4; `updater.log` says idle. |
| 2 | Push tag `recorder-v3.8.5-dryrun`, then `recorder-v3.8.5` (any harmless change). | The *recorder-release* run is green; the GitHub Release has `AIMScribeSetup.exe` and `.sha256`; the run's *Refuse anything unsigned* step lists `OK ... AIMS LAB` for the setup and the program. |
| 3 | Dashboard -> PCs -> Recorder updates: **Add release** 3.8.5, leave SHA-256 and address empty. | The release appears at stage *off*, with the SHA-256 read from the GitHub Release - compare it with the Release page. No PC is offered it. |
| 4 | **Set rollout**: stage *Test PCs only*, Test PCs = A's name. | Within a minute A shows *Will update* / *Notice received*; B shows nothing. `C:\ProgramData\AIMScribe\state\update_notice.json` exists on A only. |
| 5 | On A, start a consultation from CMED's test page and keep it recording; run `schtasks /Run /TN "AIMScribe Updater"`. | A's updater reports **Waiting** ("a consultation is recording"); the recording is not interrupted; nothing is downloaded. |
| 6 | End the consultation. Run the task again (or wait up to 30 min). | **Installing**, then about a minute later **Installed 3.8.5**; the agent is back in the tray; A shows 3.8.5. Records again normally. |
| 7 | **Tamper test** on B: quit AIMScribe from B's tray (its next heartbeat would remove the file), copy A's `update_notice.json` into B's state folder, run the task on B. | B reports **Refused**: "addressed to another PC". Edit one character of the version inside the copied file and run again: "signature does not check". B installs nothing. |
| 8 | **Pause**, then set stage *Test PCs and named hospitals* with B's hospital. | While paused nothing is offered. Resume: B updates as A did (5-6). |
| 9 | **Bad file test** (optional): add release 3.8.6 with a wrong SHA-256 (any 64 hex characters) for test PC A only. | A reports **Failed** ("SHA-256 ... not ... as the server said") three times, then **Gave up**; one *recorder_update_failed* alert; 3.8.5 still runs. |
| 10 | **Rollback test**: add release 3.8.4 with *Rollback: yes*, stage Test PCs = A. | A installs 3.8.4 (it is signed as a rollback) and reports Installed 3.8.4. A rollback not marked as one is refused by the dashboard. |
| 11 | One clinic PC: add the current version, stage *Test PCs* with that PC, during clinic hours. | It waits through consultations and installs between them; the doctor sees at most the tray icon restart. |
| 12 | All clinics: stage *Test PCs and named hospitals* one hospital at a time, then *Every PC*. | Each PC shows Installed; anything else shows its reason. |

If any step does not behave as written, **Pause** and tell AIMS LAB: a paused
release reaches no further PC, and every PC keeps recording on what it has.
