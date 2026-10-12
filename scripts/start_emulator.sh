#!/bin/bash
# Boots the SubwayCam AVD (if it is not already running) and waits until Android is ready.
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$PROJECT_ROOT/logs"

if adb devices | grep -q "^$SERIAL[[:space:]]*device" && pgrep -f "qemu-system.*-avd $AVD_NAME" >/dev/null; then
  echo "emulator already running ($SERIAL)"
else
  echo "starting emulator $AVD_NAME ..."
  # GuestAngle: guest GLES via ANGLE->Vulkan->MoltenVK (host GLES is deprecated on macOS)
  # -grpc: fast host-side screenshots for the game-state guard (src/sscam/gamestate.py)
  nohup emulator -avd "$AVD_NAME" -gpu host -feature GuestAngle -no-boot-anim -no-audio \
    -netdelay none -netspeed full -grpc 8554 -grpc-use-token \
    > "$PROJECT_ROOT/logs/emulator.log" 2>&1 &
  adb -s "$SERIAL" wait-for-device
fi
for _ in $(seq 1 120); do
  [ "$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ] && break
  sleep 1
done
[ "$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ] \
  || { echo "emulator did not finish booting; see logs/emulator.log" >&2; exit 1; }
adb -s "$SERIAL" shell input keyevent 224 >/dev/null 2>&1 || true  # wake screen
adb -s "$SERIAL" shell wm dismiss-keyguard >/dev/null 2>&1 || true
# Park the emulator window at the top-left. macOS throttles the emulator to ~12-15 fps whenever its
# window is covered by other windows (60 fps when visible), so the camera preview goes beside it.
osascript -e "tell application \"System Events\" to tell (first process whose name contains \"qemu\") to set position of (first window whose name contains \"$AVD_NAME\") to {0, 40}" >/dev/null 2>&1 \
  || echo "note: could not move the emulator window (keep it uncovered while playing)" >&2
echo "emulator ready: $(adb -s "$SERIAL" shell getprop ro.product.model | tr -d '\r'), Android $(adb -s "$SERIAL" shell getprop ro.build.version.release | tr -d '\r')"
