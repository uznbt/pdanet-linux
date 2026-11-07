#!/bin/sh
# Install the stable, root-owned TUN helper from an AppImage.
#
# This script is copied into the AppImage and invoked once by AppRun through
# pkexec.  It can also be run manually:
#
#   sudo ./install-helper.sh /path/to/squashfs-root
#   sudo ./install-helper.sh /path/to/squashfs-root --passwordless
#
# The helper itself is a small PyInstaller onedir bundle, so it does not
# depend on the host Python or on PyGObject.
set -eu

APPDIR="${1:-${APPDIR:-}}"
if [ "$(id -u)" -ne 0 ]; then
    echo "error: install-helper.sh must run as root (use pkexec or sudo)" >&2
    exit 1
fi
if [ -z "$APPDIR" ] || [ ! -x "$APPDIR/usr/lib/pdanet-linux-helper/pdanet-linux-helper" ]; then
    echo "error: AppImage directory not found: ${APPDIR:-<empty>}" >&2
    exit 1
fi

PASSWORDLESS=0
if [ "${2:-}" = "--passwordless" ]; then
    PASSWORDLESS=1
fi

LIBDIR=/usr/local/lib/pdanet-linux-helper
LIBEXECDIR=/usr/local/libexec
HELPER="$LIBEXECDIR/pdanet-linux-helper"
POLICYDIR=/usr/share/polkit-1/actions
POLICY="$POLICYDIR/io.github.pdanet.PdaNetLinux.policy"
ICONDIR=/usr/local/share/icons/hicolor/scalable/apps
ICON="$ICONDIR/io.github.pdanet.PdaNetLinux.svg"

install -d -m 0755 "$LIBDIR" "$LIBEXECDIR" "$POLICYDIR" "$ICONDIR"

# Replace an older helper atomically enough for a one-shot installer.
rm -rf "$LIBDIR.new"
cp -a "$APPDIR/usr/lib/pdanet-linux-helper" "$LIBDIR.new"
chown -R root:root "$LIBDIR.new"
chmod -R go-w "$LIBDIR.new"
rm -rf "$LIBDIR"
mv "$LIBDIR.new" "$LIBDIR"

helper_tmp="$(mktemp)"
cat >"$helper_tmp" <<EOF
#!/bin/sh
exec "$LIBDIR/pdanet-linux-helper" "\$@"
EOF
install -m 0755 "$helper_tmp" "$HELPER"
rm -f "$helper_tmp"

policy_tmp="$(mktemp)"
sed "s|@HELPER@|$HELPER|g" \
    "$APPDIR/usr/share/pdanet-linux/policy.in" >"$policy_tmp"
install -m 0644 "$policy_tmp" "$POLICY"
rm -f "$policy_tmp"

if [ -f "$APPDIR/usr/share/icons/hicolor/scalable/apps/io.github.pdanet.PdaNetLinux.svg" ]; then
    install -m 0644 \
        "$APPDIR/usr/share/icons/hicolor/scalable/apps/io.github.pdanet.PdaNetLinux.svg" \
        "$ICON"
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/local/share/icons/hicolor || true
    fi
fi

if [ "$PASSWORDLESS" -eq 1 ]; then
    target_user="${SUDO_USER:-}"
    if [ -z "$target_user" ] && [ -n "${PKEXEC_UID:-}" ]; then
        target_user="$(getent passwd "$PKEXEC_UID" 2>/dev/null | cut -d: -f1 || true)"
    fi
    case "$target_user" in
        ""|*[!A-Za-z0-9_.-]*)
            echo "error: cannot determine a valid desktop user for --passwordless" >&2
            exit 1
            ;;
    esac
    visudo_bin="$(command -v visudo 2>/dev/null || true)"
    if [ -z "$visudo_bin" ]; then
        echo "error: visudo is required for --passwordless" >&2
        exit 1
    fi
    sudoers_tmp="$(mktemp)"
    chmod 0600 "$sudoers_tmp"
    {
        echo "# Installed by PdaNet Linux AppImage install-helper.sh --passwordless."
        echo "# Allows only the PdaNet TUN helper, not general sudo."
        printf '%s ALL=(root) NOPASSWD: %s\n' "$target_user" "$HELPER"
    } >"$sudoers_tmp"
    if ! "$visudo_bin" -cf "$sudoers_tmp" >/dev/null; then
        rm -f "$sudoers_tmp"
        echo "error: generated sudoers rule is invalid; nothing changed" >&2
        exit 1
    fi
    install -m 0440 "$sudoers_tmp" /etc/sudoers.d/pdanet-linux
    rm -f "$sudoers_tmp"
    echo "Passwordless helper enabled for $target_user"
fi

echo "PdaNet root helper installed at $HELPER"
