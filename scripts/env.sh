# Shared environment for the scripts (sourced, not executed).
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$ANDROID_HOME/cmdline-tools/latest/bin:$JAVA_HOME/bin:/opt/homebrew/bin:$PATH"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AVD_NAME="${AVD_NAME:-SubwayCam}"
SERIAL="${SERIAL:-emulator-5554}"
SYSTEM_IMAGE="system-images;android-35;google_apis_playstore;arm64-v8a"
GAME_PKG="com.kiloo.subwaysurf"
