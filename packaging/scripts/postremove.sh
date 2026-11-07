#!/bin/sh
# Clean up the optional passwordless rule only when the package is really
# being removed, not while it is being upgraded.
#
# Debian passes "remove" or "purge", RPM passes 0, and Arch passes 0 or 1.
# Leave user configuration alone; only remove a rule that points at the
# helper path owned by this package.
set -u

case "${1:-}" in
    remove|purge|0)
        if [ -f /etc/sudoers.d/pdanet-linux ] \
            && grep -q '/usr/libexec/pdanet-linux-helper' \
                /etc/sudoers.d/pdanet-linux 2>/dev/null; then
            rm -f /etc/sudoers.d/pdanet-linux
        fi
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q /usr/share/applications || true
        fi
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
        fi
        ;;
esac

exit 0
