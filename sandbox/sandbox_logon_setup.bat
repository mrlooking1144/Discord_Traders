@echo off
setlocal

rem Runs INSIDE Windows Sandbox at logon (see discord_traders_uat.wsb's
rem LogonCommand). This file lives at <mapped-read-only-folder>\sandbox\,
rem so %~dp0 resolves inside the read-only mapped copy of the
rem release-candidate repository - it is never run against the live
rem development working directory.
rem
rem Copies the read-only release-candidate repository into a writable
rem Sandbox-local folder, so start.bat can safely create .venv and other
rem runtime files there. Nothing is ever written back into the read-only
rem mapped folder, and no delete/purge operation is used anywhere here,
rem so nothing outside the writable UAT folder below can ever be removed
rem by this script.

for %%I in ("%~dp0..") do set "SOURCE=%%~fI"
set "DEST=C:\Users\WDAGUtilityAccount\Desktop\Discord_Traders_UAT"

echo Copying release-candidate repository to a writable folder...
echo   From (read-only): %SOURCE%
echo   To  (writable):   %DEST%
echo.

robocopy "%SOURCE%" "%DEST%" /E /XD .git .venv __pycache__ /NFL /NDL /NJH /NJS /NC /NS /NP
set "RC=%ERRORLEVEL%"

rem Robocopy exit codes: 0-7 are all success variants (0 = nothing to
rem copy, 1 = files copied, up to 7 = combinations of copied/extra/
rem mismatched, none of which indicate failure); 8 or higher means at
rem least one failure occurred and the copy must not be trusted.
if %RC% GEQ 8 (
    echo.
    echo ERROR: Copying the release-candidate repository failed ^(robocopy exit code %RC%^).
    echo The writable UAT folder may be missing or incomplete - do not run
    echo start.bat from it.
    echo.
    pause
    exit /b 1
)

echo.
echo Setup complete. Run start.bat ONLY from the writable copy:
echo   %DEST%\start.bat
echo.
echo Next steps depend on which test you are running:
echo   - Python-missing test: do NOT install Python. Double-click
echo     start.bat in %DEST% now and confirm the error message.
echo   - Full happy-path test: install Python first
echo     (https://www.python.org/downloads/), THEN double-click
echo     start.bat in %DEST%.
echo.
pause
