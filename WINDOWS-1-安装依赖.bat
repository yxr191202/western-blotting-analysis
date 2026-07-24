@echo off
setlocal
cd /d "%~dp0"

echo Western Blotting Analysis - Windows dependency installer
echo Project directory: %CD%
echo.

call :find_compatible_python

if not defined PYTHON_EXE (
    echo Python 3.10 or newer was not found.
    echo Installing Python 3.12...
    call :install_python
    if errorlevel 1 (
        echo Automatic Python installation failed.
        echo Install Python from https://www.python.org/downloads/windows/
        pause
        exit /b 1
    )
    call :find_compatible_python
)

if not defined PYTHON_EXE (
    echo Python was installed but could not be located.
    echo Restart Windows, then run this installer again.
    pause
    exit /b 1
)

for /f "delims=" %%V in ('"%PYTHON_EXE%" %PYTHON_ARGS% --version 2^>^&1') do set "PYTHON_VERSION=%%V"
echo Using %PYTHON_VERSION%

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
echo Double-click WINDOWS-2-启动WB分析.bat to open the GUI.
pause
endlocal
exit /b 0

:find_compatible_python
set "PYTHON_EXE="
set "PYTHON_ARGS="

py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3.12"
    exit /b 0
)

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3"
    exit /b 0
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=python"
    exit /b 0
)

set "LOCAL_PY_LAUNCHER=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if exist "%LOCAL_PY_LAUNCHER%" (
    "%LOCAL_PY_LAUNCHER%" -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=%LOCAL_PY_LAUNCHER%"
        set "PYTHON_ARGS=-3.12"
        exit /b 0
    )
)

set "LOCAL_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%LOCAL_PYTHON%" (
    "%LOCAL_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=%LOCAL_PYTHON%"
        exit /b 0
    )
)

exit /b 1

:install_python
where winget >nul 2>&1
if errorlevel 1 goto install_python_direct

winget install --id Python.Python.3.12 -e --source winget --scope user --silent --accept-package-agreements --accept-source-agreements
if %ERRORLEVEL% EQU 0 exit /b 0
echo WinGet installation failed. Trying the official Python installer...

:install_python_direct
where powershell >nul 2>&1
if errorlevel 1 (
    echo Neither WinGet nor PowerShell is available.
    exit /b 1
)

set "PYTHON_INSTALLER=%TEMP%\wb-python-3.12.10-installer.exe"
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
set "PYTHON_SHA256=67B5635E80EA51072B87941312D00EC8927C4DB9BA18938F7AD2D27B328B95FB"

if /I "%PROCESSOR_ARCHITECTURE%"=="ARM64" (
    set "PYTHON_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-arm64.exe"
    set "PYTHON_SHA256=377AC8FD478987940088E879441E702A71B53164D2A1E6F1D51FF77A7E470258"
)

if /I "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 (
    set "PYTHON_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10.exe"
    set "PYTHON_SHA256=FDFE385B94F5B8785A0226A886979527FD26EB65DEFDBF29992FD22CC4B0E31E"
)

echo Downloading the official Python 3.12.10 installer...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri '%PYTHON_URL%' -OutFile '%PYTHON_INSTALLER%'; if ((Get-FileHash -Algorithm SHA256 '%PYTHON_INSTALLER%').Hash -ne '%PYTHON_SHA256%') { Write-Error 'Python installer checksum verification failed.'; exit 2 }"
if errorlevel 1 exit /b 1

"%PYTHON_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
set "PYTHON_INSTALL_EXIT=%ERRORLEVEL%"
del /q "%PYTHON_INSTALLER%" >nul 2>&1

if "%PYTHON_INSTALL_EXIT%"=="0" exit /b 0
if "%PYTHON_INSTALL_EXIT%"=="3010" exit /b 0
exit /b 1
