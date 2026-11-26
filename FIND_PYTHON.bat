@echo off
rem Called from START.bat or RESTORE.bat. Delayed expansion must be disabled.
set "OGM_PY_EXE="
set "OGM_PY_ARGS="
set "OGM_CANDIDATE_ARGS="
set "OGM_CANDIDATE=%~dp0runtime\python\python.exe"
call :probe
if defined OGM_PY_EXE exit /b 0
rem Prefer working tested versions, not the launcher's default registration.
for %%V in (3.13 3.12 3.11 3.14) do (
  if not defined OGM_PY_EXE (
    set "OGM_CANDIDATE=py.exe"
    set "OGM_CANDIDATE_ARGS=-%%V"
    call :probe
  )
)
if defined OGM_PY_EXE exit /b 0
set "OGM_CANDIDATE_ARGS="
rem Bypass a stale launcher by checking the actual installation directories.
for %%V in (313 312 311 314) do (
  if not defined OGM_PY_EXE (
    set "OGM_CANDIDATE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    call :probe
  )
  if not defined OGM_PY_EXE (
    set "OGM_CANDIDATE=%ProgramFiles%\Python%%V\python.exe"
    call :probe
  )
)
if defined OGM_PY_EXE exit /b 0
rem Search PATH but never start Microsoft's Store alias as a fallback.
for /f "delims=" %%P in ('where.exe python.exe 2^>nul') do (
  if not defined OGM_PY_EXE (
    if /i not "%%P"=="%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" (
      set "OGM_CANDIDATE=%%P"
      call :probe
    )
  )
)
if defined OGM_PY_EXE exit /b 0
set "OGM_CANDIDATE=py.exe"
set "OGM_CANDIDATE_ARGS=-3"
call :probe
if defined OGM_PY_EXE exit /b 0
echo [ERROR] No working Python 3.11+ installation found.
echo Install Python 3.13 from python.org or put a complete runtime in runtime\python.
echo Diagnostic commands: py -0p  and  where python
exit /b 1

:probe
if /i "%OGM_CANDIDATE%"=="py.exe" (
  where.exe py.exe >nul 2>&1 || exit /b 1
) else (
  if not exist "%OGM_CANDIDATE%" exit /b 1
)
"%OGM_CANDIDATE%" %OGM_CANDIDATE_ARGS% -c "import sys,sqlite3,venv; assert sys.version_info >= (3,11); assert sqlite3.sqlite_version_info >= (3,38,0)" >nul 2>&1
if errorlevel 1 exit /b 1
set "OGM_PY_EXE=%OGM_CANDIDATE%"
set "OGM_PY_ARGS=%OGM_CANDIDATE_ARGS%"
exit /b 0
