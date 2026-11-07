#!/usr/bin/env bash
# Build Debian, RPM, Arch Linux and Alpine packages plus a portable tree.
#
# Usage:
#   ./tools/build-packages.sh
#   NFPM=/path/to/nfpm VERSION=0.1.0 ./tools/build-packages.sh
#
# The packages are architecture-independent (arch: all / noarch / any) because
# the application is Python and all native dependencies are provided by the
# target distribution.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
VERSION="${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' pdanet_linux/__init__.py)}"
VERSION="${VERSION#v}"
NFPM="${NFPM:-nfpm}"
DIST="${DIST:-$ROOT/dist}"
PKGROOT="${PKGROOT:-$ROOT/build/pkgroot}"

if [ -z "$VERSION" ]; then
    echo "error: could not determine the project version" >&2
    exit 1
fi
if ! command -v "$NFPM" >/dev/null 2>&1; then
    echo "error: nfpm not found; install it from https://nfpm.goreleaser.com" >&2
    echo "       or set NFPM=/path/to/nfpm" >&2
    exit 1
fi

echo "Staging PdaNet Linux ${VERSION} in ${PKGROOT}"
rm -rf "$PKGROOT"
mkdir -p "$PKGROOT" "$DIST"

# install.sh supports DESTDIR and writes the final paths (/usr, /usr/libexec,
# /usr/share) into the generated launcher and helper wrapper.
DESTDIR="$PKGROOT" PREFIX=/usr "$ROOT/tools/install.sh"

# Keep the staging tree readable and executable for the package root.  nFPM
# records root:root ownership by default.
find "$PKGROOT" -type d -exec chmod 0755 {} +
find "$PKGROOT" -type f -exec chmod 0644 {} +
chmod 0755 "$PKGROOT/usr/bin/pdanet-linux" \
    "$PKGROOT/usr/libexec/pdanet-linux-helper"

export VERSION
mkdir -p "$DIST"

build_pkg() {
    local packager="$1" target="$2"
    echo "Building ${packager}: $(basename "$target")"
    "$NFPM" package \
        --packager "$packager" \
        --config "$ROOT/packaging/nfpm.yaml" \
        --target "$target"
}

build_pkg deb "$DIST/pdanet-linux_${VERSION}_all.deb"
build_pkg rpm "$DIST/pdanet-linux-${VERSION}-1.noarch.rpm"
build_pkg archlinux "$DIST/pdanet-linux-${VERSION}-1-any.pkg.tar.zst"
build_pkg apk "$DIST/pdanet-linux-${VERSION}-r1.apk"

# A portable install tree for distributions without a native package.  It is
# not a runtime bundle: the target still needs Python/PyGObject/GTK and the
# helper dependencies, which is exactly what the AppImage is for.
echo "Building portable tree: pdanet-linux-${VERSION}-linux-any.tar.xz"
tar --owner=0 --group=0 --numeric-owner \
    -C "$PKGROOT" -cJf \
    "$DIST/pdanet-linux-${VERSION}-linux-any.tar.xz" .

echo
echo "Packages written to ${DIST}:"
ls -lh "$DIST" | sed -n '1,20p'
