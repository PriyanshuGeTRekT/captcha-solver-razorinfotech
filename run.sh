#!/usr/bin/env bash
# Backlink Generator launcher for macOS / Linux.
# Usage: bash run.sh   (macOS: you can rename to run.command and double-click)
set -e
cd "$(dirname "$0")"

echo ""
echo " ============================================================"
echo "    BACKLINK GENERATOR"
echo " ============================================================"
echo ""

# 1. Find Python 3
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo " [X] Python 3 was not found."
  echo "     Install Python 3.12 from https://www.python.org/downloads/ and run again."
  read -r -p "Press Enter to close..."
  exit 1
fi
echo " Using $($PYTHON --version 2>&1)  (Python 3.11 or 3.12 recommended)"
echo ""

# 2. Create a private environment on first run
if [ ! -x ".venv/bin/python" ]; then
  echo " First-time setup: creating a private environment..."
  "$PYTHON" -m venv .venv
fi
PY=".venv/bin/python"

# 3. Install everything the first time only
if [ ! -f ".venv/.setup_done" ]; then
  echo ""
  echo " Installing components. The FIRST run downloads ~1-2 GB and can take 10-20 min."
  echo ""

  install_fail() {
    echo ""
    echo " ============================================================"
    echo " [X] Setup could not finish. The error is in the text ABOVE."
    echo " ============================================================"
    echo "   1. Use Python 3.12 (not 3.13+): https://www.python.org/downloads/"
    echo "   2. Check internet access and ~2 GB free disk space."
    echo "   3. Delete the .venv folder and run this file again."
    read -r -p "Press Enter to close..."
    exit 1
  }

  echo " [1/4] Updating installer tools (pip, setuptools, wheel)..."
  "$PY" -m pip install --upgrade pip setuptools wheel || install_fail

  echo ""
  echo " [2/4] Installing PyTorch (CPU build - the big one)..."
  "$PY" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu || install_fail

  echo ""
  echo " [3/4] Installing the remaining packages..."
  "$PY" -m pip install -r requirements.txt || install_fail

  echo ""
  echo " [4/4] Installing the browser engine..."
  "$PY" -m playwright install chromium || install_fail

  touch ".venv/.setup_done"
  echo ""
  echo " Setup complete."
fi

# 4. Open the browser shortly after the server starts
( sleep 6; (open http://localhost:8000 2>/dev/null || xdg-open http://localhost:8000 2>/dev/null) ) &

echo ""
echo " Starting the app... your browser will open at http://localhost:8000"
echo " Keep this window open. To stop the app: press Ctrl+C."
echo ""

"$PY" web_server.py
