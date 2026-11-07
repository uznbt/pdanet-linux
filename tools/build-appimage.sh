#!/usr/bin/env bash
# Build a self-contained x86_64 AppImage with PyInstaller.
#
# The GUI is frozen with its Python, PyGObject, GTK4 and libadwaita payload.
# The TUN helper is a separate small PyInstaller bundle because it must run as
# root; AppRun installs it once through pkexec into a stable host path.
#
# Requirements: python3 with PyInstaller, PyGObject, GTK4, libadwaita,
# gir1.2-adw-1, rsvg-convert (optional), curl, and appimagetool/runtime.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
VERSION="${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' pdanet_linux/__init__.py)}"
VERSION="${VERSION#v}"
PYTHON="${PYTHON:-python3}"
DIST="${DIST:-$ROOT/dist}"
BUILD="${BUILD:-$ROOT/build/appimage}"
APPIMAGETOOL="${APPIMAGETOOL:-$BUILD/appimagetool-x86_64.AppImage}"
APPIMAGE_RUNTIME="${APPIMAGE_RUNTIME:-$BUILD/runtime-x86_64}"

if [ -z "$VERSION" ]; then
    echo "error: could not determine the project version" >&2
    exit 1
fi
if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "error: PyInstaller is not installed for $PYTHON" >&2
    echo "       python3 -m pip install pyinstaller" >&2
    exit 1
fi
if ! "$PYTHON" -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1')" >/dev/null 2>&1; then
    echo "error: PyGObject with GTK4 and libadwaita is required" >&2
    exit 1
fi

mkdir -p "$BUILD" "$DIST"
rm -rf "$BUILD/AppDir" "$BUILD/pyinstaller-dist" "$BUILD/pyinstaller-work" "$BUILD/spec"
mkdir -p "$BUILD/AppDir" "$BUILD/pyinstaller-dist" "$BUILD/pyinstaller-work" "$BUILD/spec"

pyinstaller() {
    "$PYTHON" -m PyInstaller \
        --noconfirm --clean --onedir \
        --paths "$ROOT" \
        --distpath "$BUILD/pyinstaller-dist" \
        --workpath "$BUILD/pyinstaller-work" \
        --specpath "$BUILD/spec" \
        --exclude-module tkinter \
        --exclude-module unittest \
        --exclude-module test \
        --exclude-module pydoc \
        "$@"
}

echo "Building frozen GUI"
pyinstaller --name pdanet-linux packaging/appimage/app_entry.py
echo "Building frozen root helper"
pyinstaller --name pdanet-linux-helper packaging/appimage/helper_entry.py

# PyInstaller collects the whole host /usr/share tree through the Gtk hook on
# some distributions.  Keep the schemas and the two icon themes that GTK and
# libadwaita actually need; the host remains the fallback for everything else.
APP_PAYLOAD="$BUILD/pyinstaller-dist/pdanet-linux"
for unwanted in icons locale themes mime fontconfig; do
    rm -rf "$APP_PAYLOAD/_internal/share/$unwanted"
done
rm -f "$APP_PAYLOAD"/_internal/libgtk-3.so* \
    "$APP_PAYLOAD/_internal/gi_typelibs/Gdk-3.0.typelib"

APPDIR="$BUILD/AppDir"
mkdir -p \
    "$APPDIR/usr/bin" \
    "$APPDIR/usr/lib" \
    "$APPDIR/usr/libexec" \
    "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps" \
    "$APPDIR/usr/share/icons/hicolor/512x512/apps" \
    "$APPDIR/usr/share/pdanet-linux"
cp -a "$APP_PAYLOAD" "$APPDIR/usr/lib/pdanet-linux"
cp -a "$BUILD/pyinstaller-dist/pdanet-linux-helper" \
    "$APPDIR/usr/lib/pdanet-linux-helper"

cat >"$APPDIR/usr/bin/pdanet-linux" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/../lib/pdanet-linux/pdanet-linux" "$@"
EOF
chmod 0755 "$APPDIR/usr/bin/pdanet-linux"

cp packaging/appimage/AppRun "$APPDIR/AppRun"
cp packaging/appimage/io.github.pdanet.PdaNetLinux.desktop \
    "$APPDIR/io.github.pdanet.PdaNetLinux.desktop"
cp data/io.github.pdanet.PdaNetLinux.svg \
    "$APPDIR/io.github.pdanet.PdaNetLinux.svg"
cp data/io.github.pdanet.PdaNetLinux.svg \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/"
cp packaging/appimage/io.github.pdanet.PdaNetLinux.desktop \
    "$APPDIR/usr/share/applications/"
cp data/io.github.pdanet.PdaNetLinux.policy.in \
    "$APPDIR/usr/share/pdanet-linux/policy.in"
cp packaging/appimage/install-helper.sh \
    "$APPDIR/usr/share/pdanet-linux/install-helper.sh"
mkdir -p "$APPDIR/usr/share/doc/pdanet-linux"
cp LICENSE "$APPDIR/usr/share/doc/pdanet-linux/LICENSE"
chmod 0755 "$APPDIR/AppRun" "$APPDIR/usr/share/pdanet-linux/install-helper.sh"

# A PNG is the most portable form for the AppImage root icon and for
# desktops that do not render SVG application icons.
if command -v rsvg-convert >/dev/null 2>&1; then
    rsvg-convert -w 512 -h 512 data/io.github.pdanet.PdaNetLinux.svg \
        -o "$APPDIR/io.github.pdanet.PdaNetLinux.png"
    cp "$APPDIR/io.github.pdanet.PdaNetLinux.png" \
        "$APPDIR/usr/share/icons/hicolor/512x512/apps/"
fi

# Provide the symbolic icons even on systems with a minimal icon theme.
for theme in Adwaita hicolor; do
    if [ -d "/usr/share/icons/$theme" ]; then
        cp -a "/usr/share/icons/$theme" "$APPDIR/usr/share/icons/"
    fi
done

if [ ! -x "$APPIMAGETOOL" ]; then
    echo "Downloading appimagetool"
    curl -fL --retry 3 --connect-timeout 20 \
        -o "$APPIMAGETOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod 0755 "$APPIMAGETOOL"
fi
if [ ! -f "$APPIMAGE_RUNTIME" ]; then
    echo "Downloading AppImage runtime"
    curl -fL --retry 3 --connect-timeout 20 \
        -o "$APPIMAGE_RUNTIME" \
        https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64
    chmod 0755 "$APPIMAGE_RUNTIME"
fi

OUT="$DIST/pdanet-linux-${VERSION}-x86_64.AppImage"
echo "Packaging ${OUT}"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGETOOL" \
    --runtime-file "$APPIMAGE_RUNTIME" \
    "$APPDIR" "$OUT"

echo
echo "AppImage written:"
ls -lh "$OUT"
