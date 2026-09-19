@echo off
setlocal enabledelayedexpansion
title AIMScribe Agent - Build

REM ============================================================
REM Produces dist\AIMScribe_Agent\ for the MSI to package.
REM
REM Changed from v1:
REM   --onedir instead of --onefile. onefile re-extracts the whole app to %TEMP%
REM   on every launch, which is both a slow start and a DLL-planting surface on a
REM   clinical PC. onedir installs once under %ProgramFiles% with admin-only ACLs.
REM
REM   Authenticode signing is now part of the build, not an afterthought. Set
REM   SIGN_CERT_THUMBPRINT to sign; unsigned builds are marked as such and must
REM   not be distributed.
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
    pause
    exit /b 1
)

echo [1/6] Installing build dependencies...
python -m pip install --disable-pip-version-check -q -r requirements-build.txt
if errorlevel 1 (
    echo ERROR: dependency installation failed.
    pause
    exit /b 1
)

echo [2/6] Auditing dependencies for known vulnerabilities...
python -m pip_audit -r requirements.txt
if errorlevel 1 (
    echo.
    echo WARNING: pip-audit reported findings. Review them before releasing.
    echo.
    if /i not "%ALLOW_VULNERABLE_BUILD%"=="true" (
        echo Set ALLOW_VULNERABLE_BUILD=true to continue anyway.
        pause
        exit /b 1
    )
)

echo [3/6] Running the test suite...
python -m pytest -q tests
if errorlevel 1 (
    echo ERROR: tests failed. Not building.
    pause
    exit /b 1
)

echo [4/6] Cleaning previous builds...
if exist "dist"  rmdir /s /q dist
if exist "build" rmdir /s /q build

echo [5/6] Building executable...
pyinstaller ^
    --name=AIMScribe_Agent ^
    --onedir ^
    --windowed ^
    --noconfirm ^
    --clean ^
    --version-file=version_info.txt ^
    --hidden-import=pystray._win32 ^
    --hidden-import=uvicorn.logging ^
    --hidden-import=uvicorn.loops.auto ^
    --hidden-import=uvicorn.protocols.http.auto ^
    --hidden-import=uvicorn.protocols.websockets.auto ^
    --hidden-import=uvicorn.protocols.websockets.wsproto_impl ^
    --hidden-import=uvicorn.lifespan.on ^
    --hidden-import=cryptography.hazmat.primitives.asymmetric.ed25519 ^
    --hidden-import=cryptography.hazmat.primitives.ciphers.aead ^
    --hidden-import=jwt.algorithms ^
    --collect-all=pystray ^
    --exclude-module=tkinter ^
    --exclude-module=matplotlib ^
    --exclude-module=numpy ^
    --exclude-module=PyQt5 ^
    --exclude-module=PyQt6 ^
    --exclude-module=IPython ^
    --exclude-module=pytest ^
    main.py
if errorlevel 1 (
    echo ERROR: build failed.
    pause
    exit /b 1
)

copy /y ".env.example" "dist\AIMScribe_Agent\.env.example" >nul

echo [6/6] Signing...
if "%SIGN_CERT_THUMBPRINT%"=="" (
    echo.
    echo ************************************************************
    echo  UNSIGNED BUILD - development only.
    echo  Set SIGN_CERT_THUMBPRINT to an Authenticode certificate and
    echo  rebuild before distributing to any clinical PC. Unsigned
    echo  binaries trip SmartScreen, are flagged by antivirus, and
    echo  cannot be checked for tampering by the watchdog service.
    echo ************************************************************
    echo.
) else (
    signtool sign /sha1 %SIGN_CERT_THUMBPRINT% /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
        "dist\AIMScribe_Agent\AIMScribe_Agent.exe"
    if errorlevel 1 (
        echo ERROR: signing failed.
        pause
        exit /b 1
    )
    signtool verify /pa "dist\AIMScribe_Agent\AIMScribe_Agent.exe"
    echo Signed successfully.
)

echo.
echo ============================================================
echo  BUILD COMPLETE
echo ============================================================
echo  Output: %~dp0dist\AIMScribe_Agent\
echo.
echo  Next: install with an elevated PowerShell prompt
echo        .\install.ps1 -BackendUrl https://aimslab.internal -HospitalId HOSP001
echo.
pause
