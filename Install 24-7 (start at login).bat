@echo off
title Install Firm Radar 24/7
cd /d "%~dp0"
echo This makes the Firm Radar start by itself every time you log in to Windows.
echo (It does not change your podcast Narrative Radar.)
echo.
schtasks /create /tn "Firm Radar" /tr "cmd /c start \"Firm Radar\" /min \"%~dp0Start Firm Radar.bat\"" /sc onlogon /f
if errorlevel 1 (
  echo.
  echo That did not work. Right-click this file and choose "Run as administrator", then try again.
) else (
  echo.
  echo Done. From now on the Firm Radar starts on its own when you log in.
  echo Open it any time at http://localhost:8865
  echo To undo this, double-click "Remove 24-7.bat" in this folder.
)
echo.
pause
