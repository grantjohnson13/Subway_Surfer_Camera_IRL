#!/bin/bash
# One-time (idempotent) setup: Homebrew deps, Android SDK pieces, the SubwayCam AVD,
# the Python venv, the pose models and the SwipeTest probe app.
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$PROJECT_ROOT"

echo "== Homebrew packages"
brew list openjdk@17 >/dev/null 2>&1 || brew install openjdk@17
brew list python@3.13 >/dev/null 2>&1 || brew install python@3.13
brew list scrcpy >/dev/null 2>&1 || brew install scrcpy
brew list --cask android-commandlinetools >/dev/null 2>&1 || brew install --cask android-commandlinetools

echo "== Android SDK packages"
yes | sdkmanager --sdk_root="$ANDROID_HOME" --licenses >/dev/null || true
sdkmanager --sdk_root="$ANDROID_HOME" "cmdline-tools;latest" "platform-tools" "emulator" \
  "platforms;android-35" "build-tools;36.0.0" "$SYSTEM_IMAGE" | grep -v '^\[' || true

echo "== AVD $AVD_NAME"
if ! emulator -list-avds | grep -qx "$AVD_NAME"; then
  echo no | "$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager" create avd -n "$AVD_NAME" -k "$SYSTEM_IMAGE" -d pixel_6
fi
CFG="$HOME/.android/avd/$AVD_NAME.avd/config.ini"
setkey() { grep -q "^$1=" "$CFG" && sed -i '' "s|^$1=.*|$1=$2|" "$CFG" || echo "$1=$2" >> "$CFG"; }
setkey PlayStore.enabled yes      # avdmanager writes "no" even for Play images
setkey hw.ramSize 6144
setkey hw.cpu.ncore 6
setkey hw.gpu.enabled yes
setkey hw.gpu.mode host
setkey hw.keyboard yes

echo "== Python venv"
PY=/opt/homebrew/bin/python3.13
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q -e . --no-deps
# mediapipe depends on opencv-contrib-python; a second OpenCV wheel would clobber cv2.
.venv/bin/pip uninstall -y -q opencv-python 2>/dev/null || true

echo "== Pose models"
mkdir -p models
for v in lite full; do
  f="models/pose_landmarker_$v.task"
  [ -s "$f" ] || curl -sfL -o "$f" "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_$v/float16/latest/pose_landmarker_$v.task"
done

echo "== Emulator gRPC stubs (fast screenshots for the game-state guard)"
if [ ! -f src/sscam/emu_grpc/emulator_controller_pb2_grpc.py ]; then
  mkdir -p src/sscam/emu_grpc && touch src/sscam/emu_grpc/__init__.py
  .venv/bin/python -m grpc_tools.protoc -I"$ANDROID_HOME/emulator/lib" --python_out=src/sscam/emu_grpc \
    --grpc_python_out=src/sscam/emu_grpc "$ANDROID_HOME/emulator/lib/emulator_controller.proto"
  sed -i '' 's/^import emulator_controller_pb2 as/from . import emulator_controller_pb2 as/' src/sscam/emu_grpc/emulator_controller_pb2_grpc.py
fi

echo "== SwipeTest probe app"
[ -f tools/swipetest/swipetest.apk ] || tools/swipetest/build.sh

echo "setup complete"
