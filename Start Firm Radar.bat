@echo off
title Firm Radar - client monitoring
cd /d "%~dp0engine"
set PY=python
where py >nul 2>nul && set PY=py
echo Checking the mood scorer is installed (first time only takes a minute)...
%PY% -m pip install -q vaderSentiment >nul 2>nul
set FIRST=1
:loop
echo.
echo Starting the Firm Radar. Your client page: http://localhost:8865
echo (Your podcast Narrative Radar is separate, at http://localhost:8765)
echo Leave this window open (you can minimize it). Close it to stop.
if "%FIRST%"=="1" (%PY% watch.py) else (%PY% watch.py --no-open)
set FIRST=0
echo.
echo The Firm Radar stopped (maybe the internet dropped). Restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
