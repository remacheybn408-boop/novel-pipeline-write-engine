#!/bin/sh
# ProseForge Linux one-line installer.
#   curl -fsSL https://proseforge.cc/proseforge/install.sh | sh
# Downloads the compiled native bundle from the release channel, verifies its
# sha256, swaps it into ~/.local/lib/proseforge, links ~/.local/bin/proseforge
# and starts the service. User data lives in ~/.local/share/ProseForge and is
# never touched by (re)installs. macOS: no compiled artifact yet (no Mac build
# host) — the script says so honestly instead of failing mysteriously.
set -eu

BASE_URL="${PROSEFORGE_INSTALL_BASE:-https://proseforge.cc/proseforge/releases}"
APP_ROOT="$HOME/.local/lib/proseforge"
APP_DIR="$APP_ROOT/app"
BIN_DIR="$HOME/.local/bin"

step() { printf '==> %s\n' "$1"; }
fail() { printf 'install failed: %s\n' "$1" >&2; exit 1; }

os=$(uname -s)
arch=$(uname -m)
case "$os" in
    Linux) ;;
    Darwin) fail "macOS installer is not available yet (no Mac build host); it is coming soon." ;;
    *) fail "unsupported OS: $os" ;;
esac
[ "$arch" = "x86_64" ] || fail "only x86_64 Linux is supported (got $arch)"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

step "Fetching release manifest: $BASE_URL/latest.json"
curl -fsSL "$BASE_URL/latest.json" -o "$tmp/latest.json"
# Parse the manifest without jq: the release script writes a stable shape, and
# sed extraction keeps the installer dependency-free.
url=$(sed -n 's/.*"linux"[^{]*{[^}]*"url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$tmp/latest.json")
expected=$(sed -n 's/.*"linux"[^{]*{[^}]*"sha256"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$tmp/latest.json")
version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$tmp/latest.json")
[ -n "$url" ] && [ -n "$expected" ] || fail "release manifest has no linux artifact"
step "Downloading $url (version $version)"
curl -fsSL "$url" -o "$tmp/proseforge.tar.gz"

step "Verifying sha256"
actual=$(sha256sum "$tmp/proseforge.tar.gz" | cut -d' ' -f1)
[ "$actual" = "$expected" ] || fail "sha256 mismatch: expected $expected, got $actual"

if [ -x "$APP_DIR/proseforge/proseforge" ]; then
    step "Stopping running instance"
    "$APP_DIR/proseforge/proseforge" stop >/dev/null 2>&1 || true
fi

step "Installing to $APP_DIR"
mkdir -p "$APP_ROOT" "$BIN_DIR"
rm -rf "$APP_DIR.new" "$APP_DIR.rollback"
mkdir -p "$APP_DIR.new"
tar -xzf "$tmp/proseforge.tar.gz" -C "$APP_DIR.new"
[ -d "$APP_DIR" ] && mv "$APP_DIR" "$APP_DIR.rollback"
mv "$APP_DIR.new" "$APP_DIR"
exe="$APP_DIR/proseforge/proseforge"
[ -x "$exe" ] || fail "bundle executable missing after install: $exe"
ln -sf "$exe" "$BIN_DIR/proseforge"

step "Starting ProseForge"
"$exe" start >/dev/null 2>&1 || true
rm -rf "$APP_DIR.rollback"

printf '\nProseForge %s installed. Open http://127.0.0.1:8000 in your browser.\n' "$version"
printf 'Commands: proseforge start | stop | status | update | doctor\n'
printf 'Data dir: %s (untouched by updates)\n' "$HOME/.local/share/ProseForge"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'Note: add %s to your PATH (e.g. in ~/.profile).\n' "$BIN_DIR" ;;
esac
