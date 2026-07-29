@echo off
REM Double-click to run the Turing host game helper (Windows / Sunshine PC).
REM Announces the focused window on the LAN — the Mini-PC finds it automatically.
cd /d "%~dp0"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 "%~dp0foreground_reporter.py" %*
  goto :eof
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python "%~dp0foreground_reporter.py" %*
  goto :eof
)

echo Python 3 not found. Install from https://www.python.org/downloads/
echo Enable "Add python.exe to PATH" during setup.
pause
exit /b 1
