@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"

if not defined PYTHON_CMD (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo Python 3.10 or newer was not found.
    echo Install Python from https://www.python.org/downloads/windows/
    echo During installation, enable "Add python.exe to PATH".
    pause
    exit /b 1
)

%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo Python 3.10 or newer is required.
    pause
    exit /b 1
)

%PYTHON_CMD% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo This Python installation does not include Tkinter.
    echo Reinstall Python from python.org and include Tcl/Tk support.
    pause
    exit /b 1
)

%PYTHON_CMD% -c "import numpy, scipy, skimage, PIL" >nul 2>&1
if errorlevel 1 (
    echo Installing required Python packages...
    %PYTHON_CMD% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

%PYTHON_CMD% run_wb.py
if errorlevel 1 (
    echo.
    echo The application exited with an error.
    pause
    exit /b 1
)

endlocal
