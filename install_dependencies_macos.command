#!/bin/zsh

cd "${0:A:h}" || exit 1

pause_and_exit() {
    local exit_code="$1"
    echo
    read -r "?Press Enter to close this window..."
    exit "$exit_code"
}

echo "Western Blotting Analysis - macOS dependency installer"
echo "Project directory: $PWD"
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.10 or newer was not found."
    echo "Install Python from https://www.python.org/downloads/macos/"
    pause_and_exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 3.10 or newer is required."
    python3 --version
    pause_and_exit 1
fi

if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
    echo "This Python installation does not include Tkinter."
    echo "Install the current macOS Python package from python.org."
    pause_and_exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
    echo "Creating project environment in .venv..."
    if ! python3 -m venv .venv; then
        echo "Unable to create the project virtual environment."
        pause_and_exit 1
    fi
fi

echo "Updating pip..."
if ! ".venv/bin/python" -m pip install --upgrade pip; then
    echo "Unable to update pip. Check the network connection and try again."
    pause_and_exit 1
fi

echo "Installing project dependencies..."
if ! ".venv/bin/python" -m pip install -r requirements.txt; then
    echo "Dependency installation failed."
    pause_and_exit 1
fi

if ! ".venv/bin/python" -c 'import tkinter, numpy, scipy, skimage, PIL'; then
    echo "Dependency verification failed."
    pause_and_exit 1
fi

echo
echo "Installation completed successfully."
echo "Double-click 启动WB分析.command to open the GUI."
pause_and_exit 0
