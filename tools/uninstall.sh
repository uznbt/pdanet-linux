#!/usr/bin/env bash
# Remove the PdaNet Linux client.  The tunnel itself is cleaned up by the
# helper when the application exits; this also removes stale routing state.
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
DESTDIR="${DESTDIR:-}"
BINDIR="$DESTDIR$PREFIX/bin"
LIBDIR="$DESTDIR$PREFIX/lib/pdanet-linux"
LIBEXECDIR="$DESTDIR$PREFIX/libexec"
DATADIR="$DESTDIR$PREFIX/share"

if [ -n "$DESTDIR" ]; then
    SUDO=()
elif [ "$(id -u)" -ne 0 ]; then
    SUDO=(sudo)
else
    SUDO=()
fi

"${SUDO[@]}" rm -f \
    "$BINDIR/pdanet-linux" \
    "$LIBEXECDIR/pdanet-linux-helper" \
    "$DATADIR/applications/io.github.pdanet.PdaNetLinux.desktop" \
    "$DATADIR/polkit-1/actions/io.github.pdanet.PdaNetLinux.policy" \
    "$DATADIR/icons/hicolor/scalable/apps/io.github.pdanet.PdaNetLinux.svg"
"${SUDO[@]}" rm -rf "$LIBDIR"

# Remove leftover routing state from a crashed instance.  There is one rule
# per private range, so deleting a preference once is not enough; loop until
# ip reports that no matching rule is left.  Include the preferences used by
# earlier releases so upgrades do not leave dead rules behind.
for pref in 15010 15000 51820 51819; do
    for _ in $(seq 1 64); do
        "${SUDO[@]}" ip rule del pref "$pref" >/dev/null 2>&1 || break
    done
    for _ in $(seq 1 64); do
        "${SUDO[@]}" ip -6 rule del pref "$pref" >/dev/null 2>&1 || break
    done
done
"${SUDO[@]}" ip route flush table 51820 >/dev/null 2>&1 || true
"${SUDO[@]}" ip -6 route flush table 51820 >/dev/null 2>&1 || true
"${SUDO[@]}" ip link del pdanet0 >/dev/null 2>&1 || true
"${SUDO[@]}" rm -f /run/pdanet-linux.lock

# Remove the marked Microsoft block and the WiFi Share profile if present.
if [ -f /etc/hosts ] && grep -q '^# BEGIN pdanet-linux microsoft block$' /etc/hosts; then
    "${SUDO[@]}" sed -i \
        '/^# BEGIN pdanet-linux microsoft block$/,/^# END pdanet-linux microsoft block$/d' \
        /etc/hosts
fi
if command -v nmcli >/dev/null 2>&1; then
    "${SUDO[@]}" nmcli connection delete pdanet-share >/dev/null 2>&1 || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    "${SUDO[@]}" update-desktop-database "$DATADIR/applications" \
        >/dev/null 2>&1 || true
fi
printf 'PdaNet Linux removed.\n'
