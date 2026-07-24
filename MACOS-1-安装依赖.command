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

find_compatible_python() {
    local candidate
    local candidates=()

    if command -v python3 >/dev/null 2>&1; then
        candidates+=("$(command -v python3)")
    fi
    candidates+=(
        "/opt/homebrew/opt/python@3.12/bin/python3.12"
        "/usr/local/opt/python@3.12/bin/python3.12"
    )

    for candidate in "${candidates[@]}"; do
        if [[ -x "$candidate" ]] &&
            "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            return 0
        fi
    done

    return 1
}

find_homebrew() {
    if command -v brew >/dev/null 2>&1; then
        BREW_BIN="$(command -v brew)"
    elif [[ -x "/opt/homebrew/bin/brew" ]]; then
        BREW_BIN="/opt/homebrew/bin/brew"
    elif [[ -x "/usr/local/bin/brew" ]]; then
        BREW_BIN="/usr/local/bin/brew"
    else
        return 1
    fi
}

ensure_homebrew() {
    if find_homebrew; then
        return 0
    fi

    if ! command -v curl >/dev/null 2>&1; then
        echo "Python is missing, and curl is unavailable for installing Homebrew."
        echo "Install Python 3.12 from https://www.python.org/downloads/macos/"
        return 1
    fi

    echo "Homebrew was not found. Starting the official Homebrew installer..."
    echo "The installer may request your macOS login password."
    if ! /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
        echo "Homebrew installation failed or was cancelled."
        return 1
    fi

    if ! find_homebrew; then
        echo "Homebrew was installed but could not be located."
        echo "Open a new Terminal window and run this installer again."
        return 1
    fi
}

install_managed_python() {
    if ! ensure_homebrew; then
        return 1
    fi

    echo "Installing Python 3.12 and Tkinter with Homebrew..."
    if ! "$BREW_BIN" install python@3.12 python-tk@3.12; then
        echo "Python installation failed."
        return 1
    fi

    PYTHON_BIN="$("$BREW_BIN" --prefix python@3.12)/bin/python3.12"
    if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "Python was installed but its executable could not be located."
        return 1
    fi
}

PYTHON_BIN=""
BREW_BIN=""

if ! find_compatible_python; then
    echo "Python 3.10 or newer was not found."
    if ! install_managed_python; then
        pause_and_exit 1
    fi
fi

if ! "$PYTHON_BIN" -c 'import tkinter' >/dev/null 2>&1; then
    echo "The detected Python does not include Tkinter."
    echo "Installing a complete Python 3.12 environment..."
    if ! install_managed_python; then
        pause_and_exit 1
    fi
fi

echo "Using $("$PYTHON_BIN" --version) at $PYTHON_BIN"

if [[ ! -x ".venv/bin/python" ]]; then
    echo "Creating project environment in .venv..."
    if ! "$PYTHON_BIN" -m venv .venv; then
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
echo "Double-click MACOS-2-启动WB分析.command to open the GUI."
pause_and_exit 0
