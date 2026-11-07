#!/usr/bin/env bash
# Install the PdaNet Linux client and its root-owned TUN helper.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PREFIX="${PREFIX:-/usr/local}"
DESTDIR="${DESTDIR:-}"

PASSWORDLESS=0

usage() {
    cat <<'EOF'
Usage: sudo ./tools/install.sh [--passwordless]

  --passwordless  install /etc/sudoers.d/pdanet-linux so the desktop user
                  can start the TUN helper without a password prompt
  -h, --help      show this help

Environment:
  PREFIX       installation prefix (default: /usr/local)
  DESTDIR      staging directory (does not run sudo; --passwordless writes
               the sudoers file into the staging tree)
  PDANET_USER  desktop user for the sudoers rule (default: $SUDO_USER or
               the current user)
EOF
}

for arg in "$@"; do
    case "$arg" in
        --passwordless) PASSWORDLESS=1 ;;
        -h|--help) usage; exit 0 ;;
        *)
            printf 'error: unknown option: %s\n\n' "$arg" >&2
            usage >&2
            exit 2
            ;;
    esac
done

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
SUDOERSDIR="/etc/sudoers.d"
SUDOERSFILE="$SUDOERSDIR/pdanet-linux"

DEST_LIBDIR="$DESTDIR$LIBDIR"
DEST_BINDIR="$DESTDIR$BINDIR"
DEST_LIBEXECDIR="$DESTDIR$LIBEXECDIR"
DEST_POLICYDIR="$DESTDIR$POLICYDIR"
DEST_APPDIR="$DESTDIR$APPDIR"
DEST_ICONDIR="$DESTDIR$ICONDIR"
DEST_SUDOERSDIR="$DESTDIR$SUDOERSDIR"
DEST_SUDOERSFILE="$DESTDIR$SUDOERSFILE"

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

# Optional one-time setup: allow the desktop user to start only the
# installed helper without a password.  This is the only sudoers change the
# project makes; uninstall.sh removes the file again.
if [ "$PASSWORDLESS" -eq 1 ]; then
    target_user="${PDANET_USER:-${SUDO_USER:-}}"
    if [ -z "$target_user" ] && [ "$(id -u)" -ne 0 ]; then
        target_user="$(id -un)"
    fi
    if [ -z "$target_user" ]; then
        printf 'error: --passwordless cannot determine the desktop user ' >&2
        printf 'when run as root.\n' >&2
        printf '       Run it with sudo as that user, or set PDANET_USER=name.\n' >&2
        exit 2
    fi
    if ! id -u -- "$target_user" >/dev/null 2>&1; then
        printf 'error: unknown user %s for --passwordless.\n' "$target_user" >&2
        exit 2
    fi
    case "$target_user" in
        -*|*[!A-Za-z0-9_.-]*)
            printf 'error: unsupported characters in user name %s.\n' \
                "$target_user" >&2
            exit 2
            ;;
    esac

    helper_path="$LIBEXECDIR/pdanet-linux-helper"
    case "$helper_path" in
        *[!A-Za-z0-9/._+-]*)
            printf 'error: unsupported characters in helper path: %s\n' \
                "$helper_path" >&2
            exit 2
            ;;
    esac

    visudo_bin="${VISUDO:-}"
    if [ -z "$visudo_bin" ]; then
        visudo_bin="$(command -v visudo 2>/dev/null || true)"
    fi
    if [ -z "$visudo_bin" ]; then
        for candidate in /usr/sbin/visudo /usr/local/sbin/visudo /usr/bin/visudo; do
            if [ -x "$candidate" ]; then
                visudo_bin="$candidate"
                break
            fi
        done
    fi
    if [ -z "$visudo_bin" ]; then
        printf 'error: visudo was not found; cannot validate the sudoers rule.\n' >&2
        exit 2
    fi

    tmp_sudoers="$(mktemp)"
    trap 'rm -f "$tmp_sudoers"' EXIT
    chmod 0600 "$tmp_sudoers"
    {
        printf '# Installed by pdanet-linux tools/install.sh --passwordless.\n'
        printf '# Allows only the PdaNet TUN helper, not general sudo.\n'
        printf '%s ALL=(root) NOPASSWD: %s\n' \
            "$target_user" "$helper_path"
    } >"$tmp_sudoers"
    if ! "$visudo_bin" -cf "$tmp_sudoers" >/dev/null; then
        printf 'error: the generated sudoers rule is invalid; ' >&2
        printf 'nothing was installed.\n' >&2
        exit 2
    fi
    "${SUDO[@]}" install -d -m 0755 "$DEST_SUDOERSDIR"
    if [ -n "$DESTDIR" ]; then
        install -m 0440 "$tmp_sudoers" "$DEST_SUDOERSFILE"
    else
        "${SUDO[@]}" install -m 0440 -o root -g root "$tmp_sudoers" \
            "$DEST_SUDOERSFILE"
    fi
    rm -f "$tmp_sudoers"
    trap - EXIT
    say "Passwordless helper enabled for user: $target_user"
fi

# Distro packaging stages the tree with DESTDIR and runs these caches from
# the package post-install script on the target system instead.
if [ -z "$DESTDIR" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        "${SUDO[@]}" update-desktop-database "$DEST_APPDIR" || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        "${SUDO[@]}" gtk-update-icon-cache -f -t \
            "$DESTDIR$DATADIR/icons/hicolor" >/dev/null 2>&1 || true
    fi
fi

say
say "Installed."
say "  CLI : pdanet-linux doctor"
say "  GUI : pdanet-linux gui   (or launch 'PdaNet Linux' from the app menu)"
if [ "$PASSWORDLESS" -eq 1 ]; then
    say "  Sudo: passwordless helper for $target_user (no daily prompt)"
else
    say "  Sudo: re-run with --passwordless to avoid daily authentication prompts"
fi
say
say "The helper is only used for CAP_NET_ADMIN (TUN/routes); adb still runs as your user."
