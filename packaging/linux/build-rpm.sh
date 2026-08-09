#!/usr/bin/env bash
# Build the unsigned ProseForge .rpm from the native bundle.
# Never starts services: %post only prints next-step hints.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
BUNDLE="$ROOT/artifacts/native/linux/ProseForge"
OUT_DIR="$ROOT/artifacts/native/linux"

if ! command -v rpmbuild >/dev/null 2>&1; then
  echo "error: rpmbuild not found. Run inside a Fedora container:" >&2
  echo "  podman run --rm -v $ROOT:/src -w /src fedora:40 bash -lc \"dnf install -y rpm-build && bash packaging/linux/build-rpm.sh\"" >&2
  exit 1
fi

if [[ ! -x "$BUNDLE/proseforge" ]]; then
  echo "error: missing native bundle binary: $BUNDLE/proseforge" >&2
  echo "       build it first (scripts/build_native.sh)" >&2
  exit 1
fi

TOPDIR="$(mktemp -d)"
trap 'rm -rf "$TOPDIR"' EXIT
mkdir -p "$TOPDIR"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

cat > "$TOPDIR/SPECS/proseforge.spec" <<'EOF'
Name:           proseforge
Version:        %{pf_version}
Release:        1%{?dist}
Summary:        ProseForge native web runtime
License:        Proprietary
BuildArch:      x86_64
AutoReqProv:    no

%description
Installs the ProseForge CLI and web runtime under /opt/proseforge.
User data lives in the per-user XDG data directory and survives removal.

%prep
# Payload is staged directly from the native bundle; nothing to unpack.

%build
# Prebuilt native bundle; nothing to compile.

%install
mkdir -p %{buildroot}/opt/proseforge %{buildroot}/usr/bin \
  %{buildroot}/usr/share/applications %{buildroot}/usr/lib/systemd/user
cp -a %{bundle_path}/. %{buildroot}/opt/proseforge/
ln -s /opt/proseforge/proseforge %{buildroot}/usr/bin/proseforge
cp %{packaging_dir}/proseforge.desktop %{buildroot}/usr/share/applications/proseforge.desktop
cp %{packaging_dir}/proseforge.service %{buildroot}/usr/lib/systemd/user/proseforge.service

%post
echo "ProseForge installed to /opt/proseforge."
echo "  Check:  proseforge doctor"
echo "  Enable: systemctl --user enable --now proseforge"
echo 'User data stays in ${XDG_DATA_HOME:-~/.local/share}/ProseForge.'
exit 0

%files
/opt/proseforge
/usr/bin/proseforge
/usr/share/applications/proseforge.desktop
/usr/lib/systemd/user/proseforge.service

%changelog
* Wed Jan 01 2025 ProseForge <ops@proseforge.local> - 1.5.0-1
- First native installer build.
EOF

rpmbuild -bb "$TOPDIR/SPECS/proseforge.spec" \
  --define "_topdir $TOPDIR" \
  --define "pf_version $VERSION" \
  --define "bundle_path $BUNDLE" \
  --define "packaging_dir $ROOT/packaging/linux"

find "$TOPDIR/RPMS" -name '*.rpm' -exec cp {} "$OUT_DIR/" \;
echo "Built rpm(s) into $OUT_DIR"
