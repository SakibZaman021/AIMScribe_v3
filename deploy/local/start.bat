@echo off
title AIMScribe - local stack
cd /d "%~dp0"
python bootstrap.py %*
if errorlevel 1 pause
