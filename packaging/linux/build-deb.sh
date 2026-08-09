#!/usr/bin/env bash
# Build the unsigned ProseForge .deb from the native bundle.
# Never starts services: postinst only prints next-step hints.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
BUNDLE="$ROOT/artifacts/native/linux/ProseForge"
OUT="$ROOT/artifacts/native/linux/proseforge_${VERSION}_amd64.deb"

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "error: dpkg-deb not found. Run inside a Debian container:" >&2
  echo "  podman run --rm -v $ROOT:/src -w /src debian:bookworm-slim bash packaging/linux/build-deb.sh" >&2
  exit 1
fi

if [[ ! -x "$BUNDLE/proseforge" ]]; then
  echo "error: missing native bundle binary: $BUNDLE/proseforge" >&2
  echo "       build it first (scripts/build_native.sh)" >&2
  exit 1
fi

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
DEB="$STAGING/deb"
mkdir -p "$DEB/DEBIAN" "$DEB/opt/proseforge" "$DEB/usr/bin" \
  "$DEB/usr/share/applications" "$DEB/usr/lib/systemd/user"

cp -a "$BUNDLE/." "$DEB/opt/proseforge/"
ln -s /opt/proseforge/proseforge "$DEB/usr/bin/proseforge"
cp "$ROOT/packaging/linux/proseforge.desktop" "$DEB/usr/share/applications/proseforge.desktop"
cp "$ROOT/packaging/linux/proseforge.service" "$DEB/usr/lib/systemd/user/proseforge.service"

cat > "$DEB/DEBIAN/control" <<EOF
Package: proseforge
Version: $VERSION
Architecture: amd64
Maintainer: ProseForge <ops@proseforge.local>
Depends:
Section: utils
Priority: optional
Description: ProseForge native web runtime
 Installs the ProseForge CLI and web runtime under /opt/proseforge.
 User data lives in the per-user XDG data directory and survives removal.
EOF

cat > "$DEB/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
echo "ProseForge installed to /opt/proseforge."
echo "  Check:  proseforge doctor"
echo "  Enable: systemctl --user enable --now proseforge"
echo 'User data stays in ${XDG_DATA_HOME:-~/.local/share}/ProseForge.'
exit 0
EOF
chmod 0755 "$DEB/DEBIAN" "$DEB/DEBIAN/postinst"

dpkg-deb --build "$DEB" "$OUT"
echo "Built $OUT"
