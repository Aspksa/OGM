@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || goto fail
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"
if exist "runtime\python\python.exe" set "PY=runtime\python\python.exe"
if "%~1"=="" goto list
echo Close OGM and other SQLite clients before continuing.
choice /C YN /M "Restore selected backup"
if errorlevel 2 goto done
%PY% maintenance.py restore "%~1" || goto fail
goto done
:list
%PY% maintenance.py list || goto fail
echo Run RESTORE.bat BACKUP.sqlite3 using one filename listed above.
:done
pause
popd
exit /b 0
:fail
echo Restore failed. Current data was not deliberately overwritten on validation failure.
pause
popd
exit /b 1
