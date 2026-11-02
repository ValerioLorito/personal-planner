#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
APP_NAME="Planner"
BUILD_DIR="$SCRIPT_DIR/build"
APP_PATH="$BUILD_DIR/$APP_NAME.app"
BINARY="$BUILD_DIR/PlannerLauncher"
SOURCE="$SCRIPT_DIR/app/Launcher.swift"
PLIST_SOURCE="$SCRIPT_DIR/app/Info.plist"
ICON_SOURCE="$SCRIPT_DIR/app/icon/AppIcon.icns"
SWIFTC="/usr/bin/swiftc"

mkdir -p "$BUILD_DIR" /tmp/swift_cache

echo "==> Compiling native helper plannerkit..."
"$SWIFTC" -O -swift-version 5 -target arm64-apple-macosx14.0 \
    -module-cache-path /tmp/swift_cache \
    -framework EventKit -framework PDFKit \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$SCRIPT_DIR/helper/Info.plist" \
    "$SCRIPT_DIR/helper/main.swift" \
    -o "$BUILD_DIR/plannerkit"
codesign --force --sign - --identifier dev.local.planner.kit "$BUILD_DIR/plannerkit"

echo "==> Compiling $APP_NAME launcher..."
"$SWIFTC" -O -swift-version 5 -target arm64-apple-macosx14.0 \
    -module-cache-path /tmp/swift_cache \
    -framework AppKit -framework WebKit \
    -module-name PlannerLauncher \
    "$SOURCE" \
    -o "$BINARY"

echo "==> Assembling $APP_PATH..."
rm -rf "$APP_PATH"
mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"
cp "$BINARY" "$APP_PATH/Contents/MacOS/Planner"
chmod 755 "$APP_PATH/Contents/MacOS/Planner"
cp "$PLIST_SOURCE" "$APP_PATH/Contents/Info.plist"
printf 'APPL????' > "$APP_PATH/Contents/PkgInfo"

if [ -f "$ICON_SOURCE" ]; then
    cp "$ICON_SOURCE" "$APP_PATH/Contents/Resources/AppIcon.icns"
fi

/usr/bin/xattr -cr "$APP_PATH" 2>/dev/null || true
/usr/bin/codesign --force --deep --sign - "$APP_PATH"

echo "==> Installing to ~/Applications/Planner.app..."
/usr/bin/pkill -f "planner.cli web" 2>/dev/null || true
/usr/bin/pkill -x "Planner" 2>/dev/null || true
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/$APP_NAME.app"
cp -R "$APP_PATH" "$HOME/Applications/$APP_NAME.app"
/usr/bin/open "$HOME/Applications/$APP_NAME.app" || true

echo "==> Successfully built and installed Planner.app!"
