# Remove the ProseForge per-user autostart entry.
# This script never touches user data: everything under
# %LOCALAPPDATA%\ProseForge (database, backups, logs) is preserved.
[CmdletBinding()]
param([string]$EntryName = 'ProseForge')

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

Write-Output "Removing HKCU Run entry '$EntryName' (no-op if absent)..."
Remove-ItemProperty -Path $runKey -Name $EntryName -ErrorAction SilentlyContinue

Write-Output "Autostart removed. User data is preserved at $env:LOCALAPPDATA\ProseForge"
exit 0
