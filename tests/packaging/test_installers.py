"""Static checks for the V15-008 native installers.

These tests never execute an installer. They assert that the packaging files
reference the contracted native-bundle layout and the real CLI subcommands,
that packaging never starts services or deletes user data, and that the
shell/PowerShell scripts parse cleanly.
"""
from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "packaging" / "windows"
MACOS = ROOT / "packaging" / "macos"
LINUX = ROOT / "packaging" / "linux"

ISS = WINDOWS / "ProseForge.iss"
SERVICE_INSTALL = WINDOWS / "service_install.ps1"
SERVICE_UNINSTALL = WINDOWS / "service_uninstall.ps1"
DISTRIBUTION = MACOS / "Distribution.xml"
PLIST = MACOS / "launchd.com.proseforge.web.plist"
BUILD_PKG = MACOS / "build-pkg.sh"
UNIT = LINUX / "proseforge.service"
DESKTOP = LINUX / "proseforge.desktop"
BUILD_DEB = LINUX / "build-deb.sh"
BUILD_RPM = LINUX / "build-rpm.sh"

INSTALLER_FILES = (
    ISS,
    SERVICE_INSTALL,
    SERVICE_UNINSTALL,
    DISTRIBUTION,
    PLIST,
    BUILD_PKG,
    UNIT,
    DESKTOP,
    BUILD_DEB,
    BUILD_RPM,
)

# Lines from the previous placeholder implementations.
PLACEHOLDER_MARKERS = (
    'echo "Build',
    'echo "Run productbuild',
    "Write-Output \"Install service command",
    "Write-Output \"Stop and remove",
)

SH_SCRIPTS = (BUILD_DEB, BUILD_RPM, BUILD_PKG)
PS1_SCRIPTS = (SERVICE_INSTALL, SERVICE_UNINSTALL)


def read(path: Path) -> str:
    # utf-8-sig: the .iss carries a BOM so Inno Setup reads its Chinese strings.
    return path.read_text(encoding="utf-8-sig")


def cli_subcommands() -> set[str]:
    source = (ROOT / "proseforge" / "cli" / "main.py").read_text(encoding="utf-8")
    return set(re.findall(r'add_parser\("([^"]+)"', source))


def test_installer_files_exist_and_are_not_placeholders():
    for path in INSTALLER_FILES:
        assert path.is_file(), f"missing {path}"
        text = read(path)
        assert len(text.strip().splitlines()) > 3, f"{path} still looks like a stub"
        for marker in PLACEHOLDER_MARKERS:
            assert marker not in text, f"{path} contains placeholder line: {marker!r}"


def test_linux_units_point_at_contract_bundle_paths():
    service = read(UNIT)
    assert "ExecStart=/opt/proseforge/proseforge web" in service
    assert "WorkingDirectory=/opt/proseforge" in service
    assert "Environment=PROSEFORGE_RUNTIME_PROFILE=native" in service
    assert "WantedBy=default.target" in service

    desktop = read(DESKTOP)
    assert "Exec=/opt/proseforge/proseforge web" in desktop
    assert "Terminal=true" in desktop


def test_packaging_references_existing_cli_subcommands():
    available = cli_subcommands()
    # Sanity: the parser scan itself found the CLI surface.
    assert {"web", "doctor", "backup", "upgrade"} <= available

    references = {
        UNIT: ["web"],
        DESKTOP: ["web"],
        PLIST: ["web"],
        ISS: ["web", "backup", "upgrade"],
        SERVICE_INSTALL: ["web"],
        BUILD_DEB: ["doctor"],
        BUILD_RPM: ["doctor"],
        BUILD_PKG: ["doctor"],
    }
    for path, subcommands in references.items():
        text = read(path)
        for name in subcommands:
            assert name in available, f"CLI subcommand {name!r} missing from main.py"
            assert re.search(rf"\b{name}\b", text), f"{path.name} does not reference `{name}`"


SERVICE_CONTROL_RE = re.compile(
    r"\bsystemctl\b[^\n]*\b(start|enable)\b"
    r"|\blaunchctl\b[^\n]*\b(load|bootstrap|start)\b"
    r"|\bservice\b[^\n]*\bstart\b"
)


@pytest.mark.parametrize("script", (BUILD_DEB, BUILD_RPM))
def test_linux_packaging_never_starts_services(script):
    text = read(script)
    assert "set -euo pipefail" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "echo")):
            continue  # printed hints and comments are not executed commands
        assert not SERVICE_CONTROL_RE.search(line), f"{script.name} starts a service: {line!r}"
        # The post-install hint mentions systemctl only as printed output.
        assert "systemctl" not in line, f"{script.name}: systemctl outside an echo hint: {line!r}"


def test_linux_build_scripts_require_native_bundle():
    for script in (BUILD_DEB, BUILD_RPM):
        text = read(script)
        assert "artifacts/native/linux/ProseForge" in text
        assert re.search(r"missing native bundle", text, re.IGNORECASE)


def test_uninstall_preserves_user_data():
    ps1 = read(SERVICE_UNINSTALL)
    # Remove-ItemProperty only deletes the HKCU Run registry value (autostart);
    # the forbidden thing is deleting files/directories (Remove-Item with a path).
    assert "Remove-Item " not in ps1
    assert re.search(r"Remove-Item(?!Property)", ps1) is None
    assert "LOCALAPPDATA" in ps1  # tells the user where preserved data lives

    iss = read(ISS)
    assert "[UninstallDelete]" in iss
    section = iss.split("[UninstallDelete]", 1)[1].split("\n[", 1)[0]
    names = re.findall(r'Name:\s*"([^"]+)"', section)
    assert names, "expected at least one [UninstallDelete] entry (logs)"
    for name in names:
        assert name.startswith("{localappdata}\\ProseForge\\"), name
        assert name != "{localappdata}\\ProseForge", "must never delete the data root"


def test_plist_required_keys():
    plist = plistlib.loads(PLIST.read_bytes())
    assert plist["Label"] == "com.proseforge.web"
    args = plist["ProgramArguments"]
    assert args[0] == "/usr/local/proseforge/proseforge"
    assert args[1] == "web"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["WorkingDirectory"].endswith("Library/Application Support/ProseForge")
    assert "StandardOutPath" in plist
    assert "StandardErrorPath" in plist


def test_distribution_xml_matches_component_package():
    root = ET.parse(DISTRIBUTION).getroot()
    assert root.tag == "installer-gui-script"
    domains = root.find("domains")
    assert domains is not None and domains.get("enable_currentUserHome") == "true"
    refs = {el.get("id"): el for el in root.iter("pkg-ref")}
    assert "com.proseforge.web" in refs
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert refs["com.proseforge.web"].get("version") == version


def test_iss_required_directives():
    iss = read(ISS)
    assert "AppVersion=1.5.0" in iss or 'MyAppVersion "1.5.0"' in iss
    assert re.search(r"^AppId=\{\{[0-9A-Fa-f-]{36}\}", iss, re.MULTILINE)
    assert "{app}\\proseforge.exe" in iss
    assert "{localappdata}\\ProseForge" in iss
    for section in ("[Dirs]", "[Tasks]", "[Run]", "[UninstallRun]", "[UninstallDelete]", "[Code]"):
        assert section in iss, f"iss missing {section}"
    assert "PrepareToInstall" in iss
    assert "CurUninstallStepChanged" in iss
    assert "service_install.ps1" in iss and "service_uninstall.ps1" in iss


@pytest.mark.parametrize("script", SH_SCRIPTS)
def test_shell_scripts_pass_bash_syntax_check(script):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this host")
    subprocess.run([bash, "-n", str(script)], check=True)


@pytest.mark.parametrize("script", PS1_SCRIPTS)
def test_powershell_scripts_parse(script):
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell not available on this host")
    command = f"[scriptblock]::Create((Get-Content -LiteralPath '{script}' -Raw)) | Out-Null"
    subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", command], check=True)
