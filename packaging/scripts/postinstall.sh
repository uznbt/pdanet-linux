#!/bin/sh
# Refresh desktop and icon caches after a distro package installation.
set -eu

refresh_icon_cache() {
    directory="$1"
    [ -d "$directory" ] || return 0
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f "$directory" >/dev/null 2>&1 \
            || rm -f "$directory/icon-theme.cache"
    fi
}

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi

# A previous source-tree install may have left an icon-theme.cache in
# /usr/local that still points at a removed icon.  Refresh it so the icon
# from /usr/share is used instead.
refresh_icon_cache /usr/share/icons/hicolor
refresh_icon_cache /usr/local/share/icons/hicolor

exit 0
