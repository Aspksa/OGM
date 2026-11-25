@echo off
setlocal
pushd "%~dp0" || goto fail
where py >nul 2>&1
if not errorlevel 1 (
  set "PY=py -3"
) else (
  set "PY=python"
)
if exist "runtime\python\python.exe" set "PY=runtime\python\python.exe"
%PY% --version || goto fail
%PY% -c "import xlrd, openpyxl" >nul 2>&1
if errorlevel 1 (
  %PY% -m pip install -r requirements.txt || goto fail
)
if not exist "static\operations-ui.js" (
  if exist "runtime\node\npm.cmd" set "PATH=%~dp0runtime\node;%PATH%"
  call npm.cmd install --no-audit --no-fund || goto fail
  call npm.cmd run build || goto fail
)
%PY% -u server.py
if errorlevel 1 goto fail
popd
exit /b 0
:fail
echo OGM startup failed. Check the messages above.
pause
popd
exit /b 1
