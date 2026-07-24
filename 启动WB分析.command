#!/bin/zsh

cd "${0:A:h}" || exit 1

if [[ -x ".venv/bin/python" ]]; then
    PYTHON_BIN=".venv/bin/python"
else
    PYTHON_BIN="python3"
fi

if ! "$PYTHON_BIN" -c 'import tkinter, numpy, scipy, skimage, PIL' >/dev/null 2>&1; then
    echo "Required dependencies are not installed."
    echo "Double-click install_dependencies_macos.command first."
    echo
    read -r "?Press Enter to close this window..."
    exit 1
fi

"$PYTHON_BIN" run_wb.py
exit_code=$?
if (( exit_code != 0 )); then
    echo
    echo "The application exited with an error."
    read -r "?Press Enter to close this window..."
fi
exit "$exit_code"
