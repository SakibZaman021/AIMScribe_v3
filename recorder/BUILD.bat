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

echo [1/7] Installing build dependencies...
python -m pip install --disable-pip-version-check -q -r requirements-build.txt
if errorlevel 1 (
    echo ERROR: dependency installation failed.
    pause
    exit /b 1
)

echo [2/7] Auditing dependencies for known vulnerabilities...
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

echo [3/7] Running the test suite...
python -m pytest -q tests
if errorlevel 1 (
    echo ERROR: tests failed. Not building.
    pause
    exit /b 1
)

echo [4/7] Drawing the icon...
python scripts\make_icon.py
if errorlevel 1 (
    echo ERROR: the icon could not be drawn.
    pause
    exit /b 1
)

echo [5/7] Cleaning previous builds...
REM An installed agent keeps its settings in .env beside the executable, and
REM this next line deletes the whole folder. A rebuild therefore used to
REM un-configure the machine it was built on: the agent came back with the
REM compiled defaults, no allowed origins, and refused every connection from
REM CMED's page with a 403 that looked like a browser fault. Keep it, and put
REM it back afterwards.
set "KEEPENV="
if exist "dist\AIMScribe_Agent\.env" (
    copy /y "dist\AIMScribe_Agent\.env" "%TEMP%\aimscribe_build_env.bak" >nul
    set "KEEPENV=1"
    echo       keeping the installed .env
)
if exist "dist"  rmdir /s /q dist
if exist "build" rmdir /s /q build

echo [6/7] Building executable...
pyinstaller ^
    --name=AIMScribe_Agent ^
    --onedir ^
    --windowed ^
    --noconfirm ^
    --clean ^
    --version-file=version_info.txt ^
    --icon=assets\aimscribe.ico ^
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
    --hidden-import=ui.overlay ^
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
if defined KEEPENV (
    copy /y "%TEMP%\aimscribe_build_env.bak" "dist\AIMScribe_Agent\.env" >nul
    del "%TEMP%\aimscribe_build_env.bak" >nul 2>&1
    echo       restored the installed .env
)

echo [7/7] Signing...
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
