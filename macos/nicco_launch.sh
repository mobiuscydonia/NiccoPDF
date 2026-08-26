#!/bin/bash
# NiccoPDF macOS launcher core.
# Usage: nicco_launch.sh <dir containing app.py> [file.pdf ...]
# Finds a Python 3 that has Tk, does a one-time install of the PDF
# components into a private venv, then starts the app.
set -u

APP_DIR="${1:-$(pwd)}"
shift || true
APPSUP="$HOME/Library/Application Support/NiccoPDF"
VENV="$APPSUP/venv"
LOG="$APPSUP/launcher.log"
mkdir -p "$APPSUP"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >>"$LOG" 2>/dev/null || true; }

dialog() {
  /usr/bin/osascript -e "display dialog \"$1\" with title \"NiccoPDF\" buttons {\"OK\"} default button 1" >/dev/null 2>&1 || true
}

note() {
  /usr/bin/osascript -e "display notification \"$1\" with title \"NiccoPDF\"" >/dev/null 2>&1 || true
}

has_tk()   { "$1" -c 'import tkinter' >/dev/null 2>&1; }
has_deps() { "$1" -c 'import pymupdf, PIL' >/dev/null 2>&1; }

if [ ! -f "$APP_DIR/app.py" ]; then
  dialog "NiccoPDF could not find app.py next to the launcher. Please keep the downloaded files together."
  log "app.py not found in $APP_DIR"
  exit 1
fi

PY=""

# 1) a previously prepared venv
if [ -x "$VENV/bin/python3" ] && has_tk "$VENV/bin/python3" && has_deps "$VENV/bin/python3"; then
  PY="$VENV/bin/python3"
  log "using existing venv"
fi

# 2) find a base Python 3 that includes Tk
if [ -z "$PY" ]; then
  BASE=""
  for cand in /Library/Frameworks/Python.framework/Versions/*/bin/python3 \
              /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$cand" ] || continue
    if has_tk "$cand"; then BASE="$cand"; break; fi
  done
  if [ -z "$BASE" ]; then
    log "no python3 with tkinter found"
    dialog "NiccoPDF needs Python 3 (with Tk). Install Python from python.org — it includes everything needed — then open NiccoPDF again. Opening the download page now."
    /usr/bin/open "https://www.python.org/downloads/" >/dev/null 2>&1 || true
    exit 1
  fi
  log "base python: $BASE"

  if has_deps "$BASE"; then
    PY="$BASE"
  else
    # one-time component install into a private venv
    note "First-run setup: installing components (about a minute)…"
    log "creating venv at $VENV"
    if ! "$BASE" -m venv "$VENV" >>"$LOG" 2>&1; then
      dialog "Setup failed while creating a Python environment. Details: $LOG"
      exit 1
    fi
    "$VENV/bin/python3" -m pip install --upgrade pip >>"$LOG" 2>&1 || true
    if ! "$VENV/bin/python3" -m pip install pymupdf pymupdf-fonts pillow >>"$LOG" 2>&1; then
      dialog "Setup failed while installing components (is the internet reachable?). Details: $LOG"
      exit 1
    fi
    if ! has_deps "$VENV/bin/python3"; then
      dialog "Setup finished but the components did not load. Details: $LOG"
      exit 1
    fi
    PY="$VENV/bin/python3"
    note "Setup complete — starting NiccoPDF."
    log "venv ready"
  fi
fi

cd "$APP_DIR" || exit 1
log "launching with $PY"
exec "$PY" "$APP_DIR/app.py" "$@"
