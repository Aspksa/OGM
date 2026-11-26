@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || goto fail
chcp 65001 >nul
set "PYTHONUTF8=1"
call FIND_PYTHON.bat || goto fail
if "%~1"=="" goto list
echo Close OGM and other SQLite clients before continuing.
choice /C YN /M "Restore selected backup"
if errorlevel 2 goto done
"%OGM_PY_EXE%" %OGM_PY_ARGS% maintenance.py restore "%~1" || goto fail
goto done
:list
"%OGM_PY_EXE%" %OGM_PY_ARGS% maintenance.py list || goto fail
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
