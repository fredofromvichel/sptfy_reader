#!/bin/sh
# Aktualisiert beim Start Abhängigkeiten (und optional den Tool-Code) und startet dann den Export.
set -u
DATA_DIR="${DATA_DIR:-/data}"
export HOME="${HOME:-/tmp}"
[ -w "$HOME" ] || export HOME=/tmp
mkdir -p "$DATA_DIR"
export PYTHONPATH="$DATA_DIR/.pylib${PYTHONPATH:+:$PYTHONPATH}"
APP="/app/exporter.py"

if [ "${SKIP_UPDATE:-0}" != "1" ]; then
  echo "[update] Python-Abhängigkeiten (spotipy, pyyaml) ..."
  pip install --quiet --disable-pip-version-check --upgrade --target "$DATA_DIR/.pylib" \
    -r /app/requirements.txt || echo "[update] fehlgeschlagen - nutze vorhandene Versionen"

  # Optional: Tool-Code selbst aus einem Git-Repo aktualisieren (TOOL_REPO=https://... [TOOL_REF=main])
  if [ -n "${TOOL_REPO:-}" ]; then
    T="$DATA_DIR/.tool"
    if [ -d "$T/.git" ]; then
      git -C "$T" pull --ff-only --quiet || echo "[update] git pull fehlgeschlagen - nutze letzten Stand"
    else
      git clone --depth 1 ${TOOL_REF:+--branch "$TOOL_REF"} "$TOOL_REPO" "$T" \
        || echo "[update] git clone fehlgeschlagen - nutze eingebauten Code"
    fi
    [ -f "$T/exporter.py" ] && APP="$T/exporter.py"
  fi
fi

exec python "$APP" "$@"
