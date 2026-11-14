#!/usr/bin/env bash
# Cloudora: set up (the first time) and run, on Linux or macOS. No root needed.
#
#   ./scripts/quickstart.sh                reachable from your network (HTTPS)
#   ./scripts/quickstart.sh --anywhere     reachable from any network through a Cloudflare quick tunnel
#   ./scripts/quickstart.sh --local        only from this computer (to try it out)
#   ./scripts/quickstart.sh --help         every option (e.g. --key-file to keep the same key, --view-only)
#
# Run it from a terminal inside the desktop session you want to share (not over SSH).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "Python 3.11 or newer is required (https://www.python.org/downloads/)" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11 or newer is required (this is $("$PY" --version))" >&2; exit 1; }
if [[ ! -x "$VENV/bin/cloudora" ]]; then
  echo "[cloudora] first run: installing into $VENV"
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "$ROOT"
  touch "$VENV/.cloudora-installed"
elif [[ "$ROOT/pyproject.toml" -nt "$VENV/.cloudora-installed" ]]; then
  echo "[cloudora] updated: installing what the new version needs"
  "$VENV/bin/pip" install -q -e "$ROOT"
  touch "$VENV/.cloudora-installed"
fi
if [[ "$(uname)" == "Linux" && "${XDG_SESSION_TYPE:-}" == "wayland" ]] && ! command -v gst-launch-1.0 >/dev/null; then
  echo "[cloudora] this desktop uses Wayland: install GStreamer's PipeWire support first:" >&2
  echo "         Fedora: sudo dnf install gstreamer1-plugins-good pipewire-gstreamer" >&2
  echo "         Debian/Ubuntu: sudo apt install gstreamer1.0-plugins-good gstreamer1.0-pipewire" >&2
fi
exec "$VENV/bin/cloudora" "$@"
