#!/usr/bin/env bash
# Install the PdaNet Linux client and its root-owned TUN helper.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PREFIX="${PREFIX:-/usr/local}"
DESTDIR="${DESTDIR:-}"

# Final paths written into generated files (launcher, helper wrapper, polkit
# action, desktop entry).  DESTDIR is only used for the actual installation
# locations, so packaging with DESTDIR does not bake a staging prefix into
# the installed files.
LIBDIR="$PREFIX/lib/pdanet-linux"
BINDIR="$PREFIX/bin"
LIBEXECDIR="$PREFIX/libexec"
DATADIR="$PREFIX/share"
POLICYDIR="$DATADIR/polkit-1/actions"
APPDIR="$DATADIR/applications"
ICONDIR="$DATADIR/icons/hicolor/scalable/apps"

DEST_LIBDIR="$DESTDIR$LIBDIR"
DEST_BINDIR="$DESTDIR$BINDIR"
DEST_LIBEXECDIR="$DESTDIR$LIBEXECDIR"
DEST_POLICYDIR="$DESTDIR$POLICYDIR"
DEST_APPDIR="$DESTDIR$APPDIR"
DEST_ICONDIR="$DESTDIR$ICONDIR"

# A staged install is owned by the invoking user; only a real system
# installation needs privilege escalation.
if [ -n "$DESTDIR" ]; then
    SUDO=()
elif [ "$(id -u)" -ne 0 ]; then
    SUDO=(sudo)
else
    SUDO=()
fi

say() { printf '%s\n' "$*"; }

say "Installing PdaNet Linux to ${DESTDIR}${PREFIX}"
"${SUDO[@]}" install -d "$DEST_LIBDIR" "$DEST_BINDIR" "$DEST_LIBEXECDIR" \
    "$DEST_POLICYDIR" "$DEST_APPDIR" "$DEST_ICONDIR"

# Python package.
"${SUDO[@]}" rm -rf "$DEST_LIBDIR/pdanet_linux"
"${SUDO[@]}" cp -a "$ROOT/pdanet_linux" "$DEST_LIBDIR/pdanet_linux"
"${SUDO[@]}" find "$DEST_LIBDIR/pdanet_linux" -type d -name __pycache__ \
    -prune -exec rm -rf {} +
# The root helper imports this package.  cp -a preserves the invoking user's
# ownership, which would let that user replace code that later runs as root.
# A real system install must be root-owned and not group/world writable.
if [ -z "$DESTDIR" ]; then
    "${SUDO[@]}" chown -R root:root "$DEST_LIBDIR/pdanet_linux"
    "${SUDO[@]}" chmod -R go-w "$DEST_LIBDIR/pdanet_linux"
fi

# CLI launcher.
tmp_launcher="$(mktemp)"
cat >"$tmp_launcher" <<EOF
#!/bin/sh
export PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 -m pdanet_linux.cli "\$@"
EOF
"${SUDO[@]}" install -m 0755 "$tmp_launcher" "$DEST_BINDIR/pdanet-linux"
rm -f "$tmp_launcher"

# Root helper (wrapper) and polkit action.
tmp_helper="$(mktemp)"
sed "s|@LIBDIR@|$LIBDIR|g" "$ROOT/tools/pdanet-linux-helper.in" >"$tmp_helper"
"${SUDO[@]}" install -m 0755 "$tmp_helper" "$DEST_LIBEXECDIR/pdanet-linux-helper"
rm -f "$tmp_helper"

tmp_policy="$(mktemp)"
sed "s|@HELPER@|$LIBEXECDIR/pdanet-linux-helper|g" \
    "$ROOT/data/io.github.pdanet.PdaNetLinux.policy.in" >"$tmp_policy"
"${SUDO[@]}" install -m 0644 "$tmp_policy" \
    "$DEST_POLICYDIR/io.github.pdanet.PdaNetLinux.policy"
rm -f "$tmp_policy"

# Desktop entry and icon.
tmp_desktop="$(mktemp)"
sed "s|@BINDIR@|$BINDIR|g" \
    "$ROOT/data/io.github.pdanet.PdaNetLinux.desktop.in" >"$tmp_desktop"
"${SUDO[@]}" install -m 0644 "$tmp_desktop" \
    "$DEST_APPDIR/io.github.pdanet.PdaNetLinux.desktop"
rm -f "$tmp_desktop"
"${SUDO[@]}" install -m 0644 "$ROOT/data/io.github.pdanet.PdaNetLinux.svg" \
    "$DEST_ICONDIR/io.github.pdanet.PdaNetLinux.svg"

if command -v update-desktop-database >/dev/null 2>&1; then
    "${SUDO[@]}" update-desktop-database "$DEST_APPDIR" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    "${SUDO[@]}" gtk-update-icon-cache -f -t \
        "$DESTDIR$DATADIR/icons/hicolor" >/dev/null 2>&1 || true
fi

say
say "Installed."
say "  CLI : pdanet-linux doctor"
say "  GUI : pdanet-linux gui   (or launch 'PdaNet Linux' from the app menu)"
say
say "The helper is only used for CAP_NET_ADMIN (TUN/routes); adb still runs as your user."
