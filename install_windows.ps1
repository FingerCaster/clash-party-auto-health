[CmdletBinding()]
param(
    [switch]$Install,
    [string]$AppDir,
    [string]$DataDir,
    [string]$PythonPath,
    [string]$NodePath,
    [ValidateSet('mihomo', 'mihomo-alpha', 'mihomo-smart')][string]$Core
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
if (-not $PythonPath) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $PythonPath = (& $launcher.Source -3 -c 'import sys; print(sys.executable)').Trim()
    } else {
        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($python -and $python.Source -notlike '*WindowsApps*') { $PythonPath = $python.Source }
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Python was not found. Install Python, reopen PowerShell, or pass -PythonPath.'
}
if (-not $NodePath) {
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($node) { $NodePath = $node.Source }
}
if (-not $NodePath -or -not (Test-Path -LiteralPath $NodePath)) {
    throw 'Node.js was not found. Install Node.js LTS, reopen PowerShell, or pass -NodePath.'
}
$arguments = @((Join-Path $PSScriptRoot 'install_windows.py'), '--node', $NodePath)
if ($Install) { $arguments += '--install' }
if ($AppDir) { $arguments += @('--app-dir', $AppDir) }
if ($DataDir) { $arguments += @('--data-dir', $DataDir) }
if ($Core) { $arguments += @('--core', $Core) }
& $PythonPath @arguments
if ($LASTEXITCODE -ne 0) { throw 'Auto Health setup did not complete. See the error above.' }
