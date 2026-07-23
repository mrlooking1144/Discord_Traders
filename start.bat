@echo off
setlocal

cd /d "%~dp0"

set "PY_CMD="
py -3 -c "" >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py -3"
) else (
    python -c "" >nul 2>nul
    if not errorlevel 1 (
        set "PY_CMD=python"
    )
)

if not defined PY_CMD (
    echo.
    echo ERROR: Python was not found on this computer.
    echo Install Python 3 from https://www.python.org/downloads/ and try again.
    echo.
    pause
    exit /b 1
)

set "NEEDS_INSTALL=0"

if not exist ".venv\" (
    echo Creating virtual environment...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to create the virtual environment.
        echo.
        pause
        exit /b 1
    )
    set "NEEDS_INSTALL=1"
)

if "%NEEDS_INSTALL%"=="0" (
    .venv\Scripts\python.exe -c "import streamlit" >nul 2>nul
    if errorlevel 1 (
        set "NEEDS_INSTALL=1"
    )
)

if "%NEEDS_INSTALL%"=="1" (
    echo Installing dependencies...
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies.
        echo.
        pause
        exit /b 1
    )
)

echo Starting Discord Traders...
.venv\Scripts\python.exe -m streamlit run app/streamlit_app.py --server.showEmailPrompt=false
if errorlevel 1 (
    echo.
    echo ERROR: Streamlit failed to start.
    echo.
    pause
    exit /b 1
)

endlocal
