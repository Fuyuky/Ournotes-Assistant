$ErrorActionPreference = 'Stop'
$installRoot = $PSScriptRoot
$pointer = Get-Content -LiteralPath (Join-Path $installRoot 'current.json') -Raw | ConvertFrom-Json
if ($pointer.version -notmatch '^\d+\.\d+\.\d+$') { throw '版本指针无效。' }
if ($pointer.platform -ne 'windows-x64' -or -not [Environment]::Is64BitOperatingSystem) { throw '此包只支持 Windows x64。' }
$versionRoot = Join-Path $installRoot ('versions\' + $pointer.version)
$programRoot = Join-Path $versionRoot 'app'
$pythonPath = Join-Path $versionRoot 'runtime\python.exe'
$env:OURNOTES_DATA_ROOT = Join-Path $installRoot 'user-data'
$env:OURNOTES_INSTALL_ROOT = $installRoot
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '当前版本缺少 Python 运行时。' }
& $pythonPath -X utf8 (Join-Path $installRoot 'boot_client.py')
if ($LASTEXITCODE -ne 0) { throw '控制台未能就绪，请查看上方错误和日志路径。' }
