[CmdletBinding()]
param(
    [ValidatePattern('^\d{8}$')]
    [string]$EmployeeNo = '00000000',

    [ValidateNotNullOrEmpty()]
    [string]$DisplayName = '平台超级管理员',

    [ValidateRange(1, 900)]
    [int]$CloseAfterSeconds = 180
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$escapedRoot = $repositoryRoot.Replace("'", "''")
$escapedEmployeeNo = $EmployeeNo.Replace("'", "''")
$escapedDisplayName = $DisplayName.Replace("'", "''")
$childCommand = @"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Set-Location -LiteralPath '$escapedRoot'
`$Host.UI.RawUI.WindowTitle = '本地超级管理员初始化'
Write-Host '正在创建本地超级管理员 $escapedEmployeeNo……' -ForegroundColor Cyan
Write-Host '临时密码只显示一次；成功后窗口将在 $CloseAfterSeconds 秒内自动关闭。' -ForegroundColor Yellow
`$uv = Get-Command uv -ErrorAction SilentlyContinue
if (`$uv) {
    & `$uv.Source run python -m control_plane.tools.bootstrap_admin_window --employee-no '$escapedEmployeeNo' --display-name '$escapedDisplayName' --close-after-seconds $CloseAfterSeconds
} else {
    `$codexPython = Join-Path `$env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (-not (Test-Path -LiteralPath `$codexPython)) {
        Write-Error '未找到 uv 或可用的 Codex Python，无法启动本地初始化。'
        exit 4
    }
    `$env:PYTHONPATH = '$escapedRoot;' + (Join-Path '$escapedRoot' '.venv\Lib\site-packages')
    & `$codexPython -m control_plane.tools.bootstrap_admin_window --employee-no '$escapedEmployeeNo' --display-name '$escapedDisplayName' --close-after-seconds $CloseAfterSeconds
}
`$bootstrapExitCode = `$LASTEXITCODE
if (`$bootstrapExitCode -ne 0) {
    Write-Host '初始化失败；窗口将在 30 秒后自动关闭。' -ForegroundColor Red
    Start-Sleep -Seconds 30
}
exit `$bootstrapExitCode
"@
$encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childCommand))

Start-Process `
    -FilePath 'powershell.exe' `
    -ArgumentList @('-NoLogo', '-NoProfile', '-EncodedCommand', $encodedCommand) `
    -WorkingDirectory $repositoryRoot `
    -WindowStyle Normal
