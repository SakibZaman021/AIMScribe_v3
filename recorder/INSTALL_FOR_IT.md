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
| other | Inno Setup's standard codes (e.g. 2 = cancelled, 5 = file in use) |

An **upgrade** keeps the PC's own server, CMED address, local key and
enrolment: run the new installer the same way, no token needed.

**Uninstall** removes the program and the startup task. Recordings not yet
delivered to the server are deliberately kept in `C:\ProgramData\AIMScribe`.

### Fleet tools

- **Intune**: wrap with the Win32 Content Prep Tool
  (`IntuneWinAppUtil.exe -c . -s AIMScribeSetup.exe -o out`). Install command:
  the silent command above. Detection rule: file
  `C:\Program Files\AIMScribe\AIMScribe_Agent.exe`, version ≥ 3.5.0.
  Return code 8 = failed.
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
trust per publisher. Options, in order of preference (check current prices
and eligibility before buying):

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
