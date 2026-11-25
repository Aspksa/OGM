@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
  set "PY=py -3"
) else (
  set "PY=python"
)
%PY% --version || goto fail
if not exist "static\app.js" (
  call npm.cmd install --no-audit --no-fund || goto fail
  call npm.cmd run build || goto fail
)
%PY% server.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo OGM startup failed. Check the messages above.
pause
exit /b 1
