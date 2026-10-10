#!/bin/bash
# Builds "Segue.app" in this folder. The app is a small launcher: the code
# stays here, so editing the Python files takes effect the next time the app opens.
set -euo pipefail
cd "$(dirname "$0")"
PROJECT="$(pwd)"
APP="$PROJECT/Segue.app"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f assets/Segue.icns ]; then
  .venv/bin/python make_icon.py
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp assets/Segue.icns "$APP/Contents/Resources/Segue.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Segue</string>
  <key>CFBundleDisplayName</key><string>Segue</string>
  <key>CFBundleIdentifier</key><string>local.segue.app</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>Segue</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSArchitecturePriority</key><array><string>arm64</string><string>x86_64</string></array>
  <key>NSAppleEventsUsageDescription</key><string>Segue controls rekordbox to load tracks for analysis.</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/launcher" <<LAUNCH
#!/bin/bash
cd "$PROJECT"
# Finder can start script launchers under Rosetta; force the Mac's native architecture
# so Python matches the libraries installed in .venv.
NATIVE=""
if [ "\$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then NATIVE="/usr/bin/arch -arm64"; fi
exec \$NATIVE "$PROJECT/.venv/bin/python" app.py >> "\$HOME/Library/Logs/Segue.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/launcher"
touch "$APP"  # so Finder picks up the icon
echo "Built $APP"
