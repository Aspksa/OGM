@echo off
setlocal
pushd "%~dp0" || goto fail
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"
if exist "runtime\python\python.exe" set "PY=runtime\python\python.exe"
echo Close OGM before restoring. Available backups:
%PY% maintenance.py list || goto fail
echo Run the following command from this directory to restore a selected file:
echo %PY% maintenance.py restore BACKUP.sqlite3
echo A verified pre-restore backup is created automatically.
pause
popd
exit /b 0
:fail
pause
popd
exit /b 1
