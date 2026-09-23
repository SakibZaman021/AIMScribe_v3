@echo off
REM ============================================================
REM  AIMScribe - start everything.
REM
REM  Double-click this. It starts Docker if the PC has slept or
REM  been restarted, waits for it, brings the stack up, and
REM  prints the addresses.
REM
REM  Every link being dead after a restart is almost always this:
REM  Docker Desktop was not running, so nothing was listening.
REM  Then the dashboard looks broken and the server looks guilty.
REM ============================================================
title AIMScribe - local stack
cd /d "%~dp0"

docker info >nul 2>&1
if not errorlevel 1 goto ready

echo.
echo Docker is not running. Starting Docker Desktop...
if exist "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe" (
    start "" "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"
) else if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
    start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
) else (
    echo.
    echo Docker Desktop is not where this expected to find it.
    echo Start it yourself, then run this again.
    pause
    exit /b 1
)

echo Waiting for it. After the PC has slept this takes about a minute.
for /l %%i in (1,1,60) do (
    timeout /t 5 /nobreak >nul
    docker info >nul 2>&1
    if not errorlevel 1 goto ready
)

echo.
echo Docker did not come up in five minutes. Open Docker Desktop and look at it.
pause
exit /b 1

:ready
python bootstrap.py %*
if errorlevel 1 pause
