@echo off
rem Get the latest NetRush code from GitHub and restart (run as Administrator, from any folder).
cd /d "%~dp0..\.."
git pull --ff-only || (echo Update failed: see the message above. & exit /b 1)
call "%~dp0restart.cmd"
