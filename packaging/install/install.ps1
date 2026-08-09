# ProseForge Windows one-line installer.
#   irm https://proseforge.cc/proseforge/install.ps1 | iex
# Downloads the compiled native bundle from the release channel, verifies its
# sha256, swaps it into %LOCALAPPDATA%\Programs\ProseForge, puts `proseforge`
# on the user PATH and starts the service. User data lives in
# %LOCALAPPDATA%\ProseForge and is never touched by (re)installs.
[CmdletBinding()]
param(
    [string]$BaseUrl = "https://proseforge.cc/proseforge/releases",
    [switch]$NoAutostart
)

$ErrorActionPreference = 'Stop'
$appDir = Join-Path $env:LOCALAPPDATA 'Programs\ProseForge\app'
$stageDir = Join-Path $env:LOCALAPPDATA 'Programs\ProseForge'
$tmpDir = Join-Path $env:TEMP ("proseforge-install-" + [guid]::NewGuid().ToString('N'))

function Step([string]$msg) { Write-Output "==> $msg" }

try {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        throw "Only x64 Windows is supported (got $env:PROCESSOR_ARCHITECTURE)."
    }

    Step "Fetching release manifest: $BaseUrl/latest.json"
    $manifest = Invoke-RestMethod -Uri "$BaseUrl/latest.json" -UseBasicParsing
    $artifact = $manifest.artifacts.windows
    if (-not $artifact -or -not $artifact.url -or -not $artifact.sha256) {
        throw "Release manifest has no windows artifact."
    }
    Write-Output "    version $($manifest.version)"

    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    $zipPath = Join-Path $tmpDir 'proseforge.zip'
    Step "Downloading $($artifact.url)"
    Invoke-WebRequest -Uri $artifact.url -OutFile $zipPath -UseBasicParsing

    Step "Verifying sha256"
    $actual = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $artifact.sha256.ToLowerInvariant()) {
        throw "sha256 mismatch: expected $($artifact.sha256), got $actual"
    }

    # Stop a running instance before swapping the app directory (ignore errors:
    # the CLI may not exist yet on first install).
    $existingExe = Join-Path $appDir 'proseforge\proseforge.exe'
    if (Test-Path -LiteralPath $existingExe) {
        Step "Stopping running instance"
        & $existingExe stop 2>$null | Out-Null
    }

    Step "Installing to $appDir"
    $newDir = "$appDir.new"
    $oldDir = "$appDir.rollback"
    foreach ($d in @($newDir, $oldDir)) {
        if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Recurse -Force }
    }
    New-Item -ItemType Directory -Force -Path $newDir | Out-Null
    Expand-Archive -Path $zipPath -DestinationPath $newDir -Force
    if (Test-Path -LiteralPath $appDir) { Rename-Item -LiteralPath $appDir -NewName $oldDir }
    Rename-Item -LiteralPath $newDir -NewName $appDir
    $exe = Join-Path $appDir 'proseforge\proseforge.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw "Bundle executable missing after install: $exe" }

    Step "Adding $appDir\proseforge to user PATH"
    $binDir = Join-Path $appDir 'proseforge'
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (($userPath -split ';') -notcontains $binDir) {
        [Environment]::SetEnvironmentVariable('Path', "$userPath;$binDir", 'User')
    }

    if (-not $NoAutostart) {
        Step "Registering logon autostart (HKCU Run)"
        $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
        $dataDir = Join-Path $env:LOCALAPPDATA 'ProseForge'
        Set-ItemProperty -Path $runKey -Name 'ProseForge' -Value ('"' + $exe + '" start')
    }

    Step "Starting ProseForge"
    & $exe start | Out-Null

    Remove-Item -LiteralPath $oldDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Output ""
    Write-Output "ProseForge $($manifest.version) installed. Open http://127.0.0.1:8000 in your browser."
    Write-Output "Commands: proseforge start | stop | status | update | doctor"
    Write-Output "Data dir: $(Join-Path $env:LOCALAPPDATA 'ProseForge') (untouched by updates)"
} catch {
    Write-Error "Install failed: $($_.Exception.Message)"
    exit 1
} finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
