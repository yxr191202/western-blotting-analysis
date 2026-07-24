@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="
set "PYTHON_ARGS="

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    py -3 --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=py"
        set "PYTHON_ARGS=-3"
    )
)

if not defined PYTHON_EXE (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
    echo Python 3.10 or newer was not found.
    echo Install Python from https://www.python.org/downloads/windows/
    echo During installation, enable "Add python.exe to PATH".
    pause
    exit /b 1
)

"%PYTHON_EXE%" %PYTHON_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo Python 3.10 or newer is required.
    pause
    exit /b 1
)

"%PYTHON_EXE%" %PYTHON_ARGS% -c "import tkinter, numpy, scipy, skimage, PIL" >nul 2>&1
if errorlevel 1 (
    echo Required dependencies are not installed.
    echo Double-click install_dependencies_windows.bat first.
    pause
    exit /b 1
)

"%PYTHON_EXE%" %PYTHON_ARGS% run_wb.py
if errorlevel 1 (
    echo.
    echo The application exited with an error.
    pause
    exit /b 1
)

endlocal
