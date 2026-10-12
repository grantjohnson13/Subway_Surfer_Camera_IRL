#!/bin/bash
# Builds the SwipeTest probe APK without Gradle (javac -> d8 -> aapt2 -> apksigner).
set -euo pipefail
cd "$(dirname "$0")"
SDK="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
BT="$SDK/build-tools/36.0.0"
JAR="$SDK/platforms/android-35/android.jar"
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}"
export PATH="$JAVA_HOME/bin:$PATH"
rm -rf build && mkdir -p build/classes build/dex
javac -source 11 -target 11 -classpath "$JAR" -d build/classes $(find src -name '*.java') 2>&1 | grep -v 'bootstrap classpath' || true
"$BT/d8" --lib "$JAR" --min-api 26 --output build/dex $(find build/classes -name '*.class')
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml --min-sdk-version 26 --target-sdk-version 35 -o build/unsigned.apk
(cd build/dex && zip -q ../unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk
KS=build/debug.keystore
keytool -genkeypair -keystore "$KS" -storepass android -keypass android -alias debug \
  -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=debug" >/dev/null 2>&1
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:android --out swipetest.apk build/aligned.apk
echo "built $(pwd)/swipetest.apk"
