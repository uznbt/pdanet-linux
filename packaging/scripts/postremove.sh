#!/bin/sh
# Clean up the optional passwordless rule only when the package is really
# being removed, not while it is being upgraded.
#
# Debian passes "remove" or "purge", RPM passes 0.  Arch and Alpine run the
# removal script without arguments (their upgrades use a separate upgrade
# script), so an empty argument also means a real removal.
#
# Leave user configuration alone; only remove a rule that points at the
# helper path owned by this package.
set -u

refresh_icon_cache() {
    directory="$1"
    [ -d "$directory" ] || return 0
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f "$directory" >/dev/null 2>&1 \
            || rm -f "$directory/icon-theme.cache"
    fi
}

case "${1:-}" in
    remove|purge|0|"")
        if [ -f /etc/sudoers.d/pdanet-linux ] \
            && grep -q '/usr/libexec/pdanet-linux-helper' \
                /etc/sudoers.d/pdanet-linux 2>/dev/null; then
            rm -f /etc/sudoers.d/pdanet-linux
        fi
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q /usr/share/applications || true
        fi
        refresh_icon_cache /usr/share/icons/hicolor
        refresh_icon_cache /usr/local/share/icons/hicolor
        ;;
esac

exit 0
