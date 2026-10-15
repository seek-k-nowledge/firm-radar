@echo off
title Remove Firm Radar 24/7
schtasks /delete /tn "Firm Radar" /f
echo.
echo The Firm Radar will no longer start by itself. You can still start it with "Start Firm Radar.bat".
echo (Your podcast Narrative Radar is not affected.)
echo.
pause
