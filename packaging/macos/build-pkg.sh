#!/usr/bin/env bash
# Build the ProseForge macOS installer package (pkgbuild + productbuild).
# Signing identity comes from PROSEFORGE_SIGN_IDENTITY (CI secrets, never
# committed); without it an unsigned development package is produced.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
IDENTIFIER="com.proseforge.web"
INSTALL_DIR="/usr/local/proseforge"
BUNDLE="$ROOT/artifacts/native/macos/ProseForge"
OUT="$ROOT/artifacts/native/macos/ProseForge-${VERSION}.pkg"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: pkgbuild/productbuild only exist on macOS; run this on a macOS CI runner." >&2
  exit 1
fi
for tool in pkgbuild productbuild; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool not found on PATH" >&2; exit 1; }
done
if [[ ! -x "$BUNDLE/proseforge" ]]; then
  echo "error: missing native bundle binary: $BUNDLE/proseforge" >&2
  echo "       build it first (scripts/build_native.sh)" >&2
  exit 1
fi

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

# Payload: binaries + LaunchAgent template (install path substituted in).
PAYLOAD="$STAGING/root"
mkdir -p "$PAYLOAD$INSTALL_DIR" "$PAYLOAD/Library/LaunchAgents"
cp -R "$BUNDLE/." "$PAYLOAD$INSTALL_DIR/"
sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  "$ROOT/packaging/macos/launchd.com.proseforge.web.plist" \
  > "$PAYLOAD/Library/LaunchAgents/$IDENTIFIER.plist"

# postinstall: launchd does not expand "~", so rewrite the tilde paths in the
# LaunchAgent to the console user's home and create the data/log directories.
SCRIPTS="$STAGING/scripts"
mkdir -p "$SCRIPTS"
cat > "$SCRIPTS/postinstall" <<'EOF'
#!/bin/sh
set -e
PLIST="/Library/LaunchAgents/com.proseforge.web.plist"
[ -f "$PLIST" ] || PLIST="$HOME/Library/LaunchAgents/com.proseforge.web.plist"
CONSOLE_USER="$(stat -f %Su /dev/console)"
USER_HOME="$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
if [ -n "$USER_HOME" ] && [ "$CONSOLE_USER" != "root" ]; then
  sed -i '' "s|~|$USER_HOME|g" "$PLIST"
  mkdir -p "$USER_HOME/Library/Application Support/ProseForge" "$USER_HOME/Library/Logs/ProseForge"
  chown -R "$CONSOLE_USER" "$USER_HOME/Library/Application Support/ProseForge" "$USER_HOME/Library/Logs/ProseForge"
fi
echo "ProseForge installed to /usr/local/proseforge."
echo "  Check: /usr/local/proseforge/proseforge doctor"
echo "User data stays in ~/Library/Application Support/ProseForge."
exit 0
EOF
chmod 0755 "$SCRIPTS/postinstall"

COMPONENT="$STAGING/proseforge-component.pkg"
pkgbuild \
  --root "$PAYLOAD" \
  --scripts "$SCRIPTS" \
  --install-location / \
  --identifier "$IDENTIFIER" \
  --version "$VERSION" \
  "$COMPONENT"

SIGN_ARGS=()
if [[ -n "${PROSEFORGE_SIGN_IDENTITY:-}" ]]; then
  SIGN_ARGS=(--sign "$PROSEFORGE_SIGN_IDENTITY")
  echo "Signing distribution package with identity: $PROSEFORGE_SIGN_IDENTITY"
else
  echo "PROSEFORGE_SIGN_IDENTITY not set: producing unsigned dev build."
fi

productbuild \
  --distribution "$ROOT/packaging/macos/Distribution.xml" \
  --package-path "$STAGING" \
  ${SIGN_ARGS[@]+"${SIGN_ARGS[@]}"} \
  "$OUT"

echo "Built $OUT"
