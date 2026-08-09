# Register ProseForge per-user autostart (launch "proseforge web" at logon).
# Mechanism: HKCU Registry Run key. This needs no elevation and works for the
# installing user only, matching the per-user data directory model. (Scheduled
# task ONLOGON was rejected: it requires privileges this app's per-user
# installer must not demand.)
# Called by the Inno Setup installer when the "autostart" task is selected,
# or manually:  powershell -ExecutionPolicy Bypass -File service_install.ps1
[CmdletBinding()]
param(
    [string]$Executable = "$env:ProgramFiles\ProseForge\proseforge.exe",
    [string]$DataDir = "$env:LOCALAPPDATA\ProseForge",
    [string]$EntryName = 'ProseForge'
)

$ErrorActionPreference = 'Stop'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

Write-Output "[1/3] Checking executable: $Executable"
if (-not (Test-Path -LiteralPath $Executable)) {
    Write-Error "Executable not found: $Executable"
    exit 1
}

Write-Output "[2/3] Ensuring per-user data directory: $DataDir"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

$command = '"' + $Executable + '" web --data-dir "' + $DataDir + '"'
Write-Output "[3/3] Writing HKCU Run entry '$EntryName': $command"
Set-ItemProperty -Path $runKey -Name $EntryName -Value $command

Write-Output "Done. '$EntryName' will start 'proseforge web' at next logon; data dir: $DataDir"
exit 0
