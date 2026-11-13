@echo off
rem Cloudora for Windows: double-click to start (reachable from your network). Close this window to stop.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0quickstart.ps1" %*
pause
