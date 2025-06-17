#!/bin/bash
set -e

TEMPLATE="."
PKG_NAME="dispatch-dashboard"
VERSION="1.0.0"
ARCH="amd64"
WORKDIR="pkg"

# Clean up
rm -rf "$WORKDIR" "${PKG_NAME}_${VERSION}.deb"

# Copy template into package directory
cp -r "$TEMPLATE" "$WORKDIR"

# Build the Debian package
dpkg-deb --build "$WORKDIR" "${PKG_NAME}_${VERSION}.deb"

echo "Created: ${PKG_NAME}_${VERSION}.deb"
