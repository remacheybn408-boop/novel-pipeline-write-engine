from __future__ import annotations

from pathlib import Path


def test_native_packaging_tree_is_complete():
    root = Path(__file__).parents[2]
    expected = (
        "packaging/windows/ProseForge.iss",
        "packaging/windows/service_install.ps1",
        "packaging/windows/service_uninstall.ps1",
        "packaging/macos/Distribution.xml",
        "packaging/macos/launchd.com.proseforge.web.plist",
        "packaging/macos/build-pkg.sh",
        "packaging/linux/proseforge.service",
        "packaging/linux/proseforge.desktop",
        "packaging/linux/build-deb.sh",
        "packaging/linux/build-rpm.sh",
        "scripts/build_native.ps1",
        "scripts/build_native.sh",
    )
    assert all((root / item).is_file() for item in expected)
