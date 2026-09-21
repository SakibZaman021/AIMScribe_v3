@echo off
rem The AIMScribe agent, against the local bench. Everything it needs
rem is in recorder\.env, including where it keeps its own state - so
rem an agent already enrolled on this machine is left alone.
cd /d "C:\Users\USER\Downloads\AIMScribe\AIMScribe_v3\recorder"
python main.py
pause
