@echo off
setlocal
cd /d "%~dp0"

echo Western Blotting Analysis - Windows dependency installer
echo Project directory: %CD%
echo.

set "PYTHON_EXE="
set "PYTHON_ARGS="

py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3"
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

"%PYTHON_EXE%" %PYTHON_ARGS% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo This Python installation does not include Tkinter.
    echo Reinstall Python from python.org and include Tcl/Tk support.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating project environment in .venv...
    "%PYTHON_EXE%" %PYTHON_ARGS% -m venv .venv
    if errorlevel 1 (
        echo Unable to create the project virtual environment.
        pause
        exit /b 1
    )
)

set "VENV_PYTHON=.venv\Scripts\python.exe"

echo Updating pip...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo Unable to update pip. Check the network connection and try again.
    pause
    exit /b 1
)

echo Installing project dependencies...
"%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

"%VENV_PYTHON%" -c "import tkinter, numpy, scipy, skimage, PIL"
if errorlevel 1 (
    echo Dependency verification failed.
    pause
    exit /b 1
)

echo.
echo Installation completed successfully.
echo Double-click start_wb_windows.bat to open the GUI.
pause
endlocal
