#!/bin/bash
# Single entry point: boots the emulator, opens Subway Surfers, starts the camera controller.
#   ./run.sh              play (sends swipes)
#   ./run.sh --dry-run    show detections only, send nothing
#   ./run.sh --help       all controller options (--method, --camera, --duration, ...)
set -euo pipefail
source "$(dirname "$0")/scripts/env.sh"
cd "$PROJECT_ROOT"

[ -x .venv/bin/python ] || { echo "run scripts/setup.sh first" >&2; exit 1; }
for a in "$@"; do [ "$a" = "--help" ] || [ "$a" = "-h" ] && exec .venv/bin/python -m sscam.app --help; done

scripts/start_emulator.sh

if adb -s "$SERIAL" shell pm list packages "$GAME_PKG" | grep -q "$GAME_PKG"; then
  adb -s "$SERIAL" shell monkey -p "$GAME_PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
  echo "launched Subway Surfers"
else
  echo "Subway Surfers is not installed in the emulator yet - install it from the Play Store (see README)." >&2
fi

exec .venv/bin/python -m sscam.app "$@"
