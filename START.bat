@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
pushd "%~dp0" || goto fail_no_dir
echo [OGM] Checking Python...
call FIND_PYTHON.bat || goto fail
echo [OGM] Selected: "%OGM_PY_EXE%" %OGM_PY_ARGS%
"%OGM_PY_EXE%" %OGM_PY_ARGS% --version || goto fail
if /i "%~1"=="--check-python" goto done
"%OGM_PY_EXE%" %OGM_PY_ARGS% -c "import xlrd, openpyxl" >nul 2>&1
if errorlevel 1 (
  echo [OGM] Installing Excel dependencies...
  "%OGM_PY_EXE%" %OGM_PY_ARGS% -m pip install -r requirements.txt || goto fail
)
"%OGM_PY_EXE%" %OGM_PY_ARGS% build_check.py >nul 2>&1
if errorlevel 1 (
  echo [OGM] Building frontend...
  if exist "runtime\node\npm.cmd" set "PATH=%~dp0runtime\node;%PATH%"
  where.exe npm.cmd >nul 2>&1 || goto no_node
  call npm.cmd install --no-audit --no-fund || goto fail
  call npm.cmd run build || goto fail
  "%OGM_PY_EXE%" %OGM_PY_ARGS% build_check.py mark || goto fail
)
if /i "%~1"=="--check" goto done
echo [OGM] Starting local server...
"%OGM_PY_EXE%" %OGM_PY_ARGS% -u server.py
if errorlevel 1 goto fail
:done
popd
exit /b 0
:no_node
echo [ERROR] Frontend needs rebuilding. Install Node.js or add runtime\node.
goto fail
:fail
popd
:fail_no_dir
echo [ERROR] OGM startup failed. See the stage and messages above.
if /i "%~1"=="--check" exit /b 1
if /i "%~1"=="--check-python" exit /b 1
pause
exit /b 1
