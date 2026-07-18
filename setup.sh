#!/usr/bin/env bash
# BusinessSearchAgent — environment setup (macOS / Linux / WSL / Git Bash)
#
#   ./setup.sh              create .venv and verify
#   ./setup.sh --offline    skip the live network check
#   ./setup.sh --recreate   delete an existing .venv first
#
# There are no runtime dependencies to install; the venv exists to pin the
# interpreter and isolate the project from system Python.

set -euo pipefail
cd "$(dirname "$0")"

OFFLINE=""
RECREATE=""
for arg in "$@"; do
    case "$arg" in
        --offline)  OFFLINE="--offline" ;;
        --recreate) RECREATE="1" ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

echo "BusinessSearchAgent - environment setup"
echo

# --- locate a suitable interpreter (>= 3.11) --------------------------------
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            echo "  interpreter : $("$candidate" --version)  ($candidate)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "  ERROR: no Python >= 3.11 found." >&2
    echo "  Install from https://www.python.org/downloads/" >&2
    exit 1
fi

# --- create the virtual environment -----------------------------------------
if [ -n "$RECREATE" ] && [ -d .venv ]; then
    echo "  removing existing .venv ..."
    rm -rf .venv
fi

if [ -d .venv ]; then
    echo "  .venv       : already exists (use --recreate to rebuild)"
else
    echo "  creating .venv ..."
    "$PYTHON" -m venv .venv
    echo "  .venv       : created"
fi

# Git Bash on Windows puts the interpreter in Scripts/, POSIX in bin/
if [ -x .venv/bin/python ]; then
    VENV_PY=.venv/bin/python
else
    VENV_PY=.venv/Scripts/python.exe
fi

# --- no dependencies, but keep pip current ----------------------------------
echo "  upgrading pip (quiet) ..."
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
echo "  dependencies: none required (standard library only)"

# --- .env from template ------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  .env        : created from .env.example (all values blank)"
else
    echo "  .env        : already exists, left untouched"
fi

# --- verify -------------------------------------------------------------------
echo
if "$VENV_PY" verify.py $OFFLINE; then
    echo
    echo "Activate the environment with:"
    echo "    source .venv/bin/activate      # or .venv/Scripts/activate on Windows"
    echo
    echo "Then try:"
    echo "    python agent.py Concord MA"
else
    echo
    echo "Verification failed - see above." >&2
    exit 1
fi
