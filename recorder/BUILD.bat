@echo off
setlocal enabledelayedexpansion
title AIMScribe Agent - Build

REM ============================================================
REM Builds a release: dist\AIMScribe_Agent\ (the program, every file signed)
REM and dist\AIMScribeSetup.exe (the signed installer).
REM
REM   Release:  set AIMS_SIGN_PROVIDER=azure ^| thumbprint ^| signpath
REM             (and its settings - see signing\sign.ps1 and INSTALL_FOR_IT.md)
REM             set AIMS_BACKEND=https://<the AIMS LAB server>
REM             (optional) set AIMS_ORIGIN=https://<a bench page> - CMED's come from the server
REM             BUILD.bat
REM
REM   Dev only: set DEV_UNSIGNED=1   (prints a warning; never give it out)
REM
REM Optional:
REM   AIMS_BUILD_BOOTLOADER=1   compile PyInstaller's launcher from source on
REM                             this PC (needs Visual Studio Build Tools), so
REM                             the launcher is not byte-identical to the one
REM                             in every other PyInstaller program - which is
REM                             what heuristic antivirus sometimes flags.
REM   ALLOW_VULNERABLE_BUILD=true   continue past pip-audit findings.
REM ============================================================

cd /d "%~dp0"

echo.
echo ============================================================
echo  AIMScribe Agent - Build
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not on PATH.
    echo        Install it with:  winget install --id Python.Python.3.12 -e
    goto :fail
)

if "%DEV_UNSIGNED%"=="1" goto :settings_ok
if "%AIMS_SIGN_PROVIDER%"=="" (
    echo ERROR: no signing certificate configured.
    echo        Set AIMS_SIGN_PROVIDER ^(azure, thumbprint or signpath^) and its settings,
    echo        or DEV_UNSIGNED=1 for a build that stays on this PC.
    goto :fail
)
:settings_ok
if "%AIMS_BACKEND%"=="" (
    echo ERROR: set AIMS_BACKEND=https://^<the AIMS LAB server^> - new PCs are pointed at it.
    goto :fail
)
REM AIMS_ORIGIN is optional: CMED's EHR addresses come from the AIMS LAB server,
REM so one installer serves every hospital. Set it only for a development bench.

echo [1/9] Installing pinned dependencies (hashes checked)...
python -m pip install --disable-pip-version-check -q --require-hashes -r requirements.lock
if errorlevel 1 (
    echo ERROR: dependency installation failed. requirements.lock is regenerated with:
    echo        python -m piptools compile --generate-hashes --strip-extras -o requirements.lock requirements.txt
    goto :fail
)
if "%AIMS_BUILD_BOOTLOADER%"=="1" (
    echo       building PyInstaller's launcher from source...
    set PYINSTALLER_COMPILE_BOOTLOADER=1
    python -m pip install --disable-pip-version-check -q --no-binary pyinstaller pyinstaller==6.21.0
) else (
    python -m pip install --disable-pip-version-check -q pyinstaller==6.21.0
)
if errorlevel 1 goto :fail
python -m pip install --disable-pip-version-check -q pytest pytest-asyncio pip-audit
if errorlevel 1 goto :fail

echo [2/9] Auditing dependencies for known vulnerabilities...
python -m pip_audit --require-hashes -r requirements.lock
if errorlevel 1 (
    echo.
    echo WARNING: pip-audit reported findings. Review them before releasing.
    if /i not "%ALLOW_VULNERABLE_BUILD%"=="true" (
        echo Set ALLOW_VULNERABLE_BUILD=true to continue anyway.
        goto :fail
    )
)

echo [3/9] Running the test suite...
python -m pytest -q -p no:cacheprovider tests
if errorlevel 1 (
    echo ERROR: tests failed. Not building.
    goto :fail
)

echo [4/9] Drawing the icon...
python scripts\make_icon.py
if errorlevel 1 goto :fail

echo [5/9] Cleaning previous builds...
REM An installed agent keeps its settings in .env beside the executable, and
REM the next line deletes the whole folder. Keep it, and put it back afterwards.
set "KEEPENV="
if exist "dist\AIMScribe_Agent\.env" (
    copy /y "dist\AIMScribe_Agent\.env" "%TEMP%\aimscribe_build_env.bak" >nul
    set "KEEPENV=1"
    echo       keeping the installed .env
)
if exist "dist"  rmdir /s /q dist
if exist "build" rmdir /s /q build

echo [6/9] Building the program from AIMScribe_Agent.spec...
python -m PyInstaller --noconfirm --clean AIMScribe_Agent.spec
if errorlevel 1 (
    echo ERROR: build failed.
    goto :fail
)
copy /y ".env.example" "dist\AIMScribe_Agent\.env.example" >nul
if defined KEEPENV (
    copy /y "%TEMP%\aimscribe_build_env.bak" "dist\AIMScribe_Agent\.env" >nul
    del "%TEMP%\aimscribe_build_env.bak" >nul 2>&1
    echo       restored the installed .env
)

echo [7/9] Signing every program file...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File signing\sign.ps1 -Folder dist\AIMScribe_Agent
if errorlevel 1 (
    echo ERROR: signing failed - nothing was released.
    goto :fail
)

echo [8/9] Building the installer...
set "ISCC="
REM Outside the loop: the ")" in ProgramFiles(x86) would end a ( ) block.
set "PF86=%ProgramFiles(x86)%"
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%PF86%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
    if exist %%P set "ISCC=%%~P"
)
if not defined ISCC (
    echo ERROR: Inno Setup 6 not found.  winget install --id JRSoftware.InnoSetup -e
    goto :fail
)
if "%DEV_UNSIGNED%"=="1" (
    REM AIMS_KEYS_DIR: a test server's public keys, for dev builds only.
    if "%AIMS_KEYS_DIR%"=="" (
        "%ISCC%" /Q "/DDefaultBackend=%AIMS_BACKEND%" "/DDefaultOrigin=%AIMS_ORIGIN%" installer.iss
    ) else (
        "%ISCC%" /Q "/DDefaultBackend=%AIMS_BACKEND%" "/DDefaultOrigin=%AIMS_ORIGIN%" "/DKeysDir=%AIMS_KEYS_DIR%" installer.iss
    )
) else (
    "%ISCC%" /Q /DSignedBuild "/Saimssign=powershell.exe -NoProfile -ExecutionPolicy Bypass -File %~dp0signing\sign.ps1 -File $f" "/DDefaultBackend=%AIMS_BACKEND%" "/DDefaultOrigin=%AIMS_ORIGIN%" installer.iss
)
if errorlevel 1 (
    echo ERROR: the installer did not build.
    goto :fail
)

echo [9/9] Checking the installer's signature...
powershell.exe -NoProfile -Command "$s = Get-AuthenticodeSignature 'dist\AIMScribeSetup.exe'; Write-Host ('  ' + $s.Status + '  ' + $s.SignerCertificate.Subject); if ($env:DEV_UNSIGNED -ne '1' -and $s.Status -ne 'Valid') { exit 1 }"
if errorlevel 1 (
    echo ERROR: AIMScribeSetup.exe is not validly signed.
    goto :fail
)
certutil -hashfile dist\AIMScribeSetup.exe SHA256 | findstr /v "hash CertUtil" > dist\AIMScribeSetup.exe.sha256

echo.
echo ============================================================
echo  BUILD COMPLETE
echo ============================================================
echo  Program:   %~dp0dist\AIMScribe_Agent\
echo  Installer: %~dp0dist\AIMScribeSetup.exe  (SHA-256 in AIMScribeSetup.exe.sha256)
echo.
echo  Before giving it out (INSTALL_FOR_IT.md, "Each release"):
echo    1. submit AIMScribeSetup.exe and AIMScribe_Agent.exe to Microsoft as
echo       "Software developer" at https://www.microsoft.com/wdsi/filesubmission
echo    2. install it on one test PC with the check:  policy\AIMScribe-Check.ps1
echo.
if "%DEV_UNSIGNED%"=="1" (
    echo  ************************************************************
    echo   UNSIGNED DEVELOPMENT BUILD - NEVER GIVE THIS TO A CLINIC.
    echo  ************************************************************
)
if not defined CI pause
exit /b 0

:fail
if not defined CI pause
exit /b 1
