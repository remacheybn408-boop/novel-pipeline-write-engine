param(
  [ValidateSet('windows','linux','macos')][string]$Target = 'windows',
  [switch]$SkipSign
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "artifacts/native/$Target"
New-Item -ItemType Directory -Force $out | Out-Null
# 模块本身只做轻量编排，可直接跑在 py -3.12 下；真正的 PyInstaller
# 构建在模块自建的 .venv-native-<target>（Python 3.12）venv 中执行。
$env:PYTHONPATH = $root
Push-Location $root
try {
  py -3.12 -m packaging.native_bundle --root $root --output $out --target $Target --format zip
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
Write-Output $out
