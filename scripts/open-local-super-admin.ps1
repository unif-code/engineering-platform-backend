[CmdletBinding()]
param(
    [ValidatePattern('^\d{8}$')]
    [string]$EmployeeNo = '00000000',

    [string]$DisplayName,

    [ValidateRange(1, 900)]
    [int]$CloseAfterSeconds = 180
)

$ErrorActionPreference = 'Stop'
function ConvertFrom-Utf8Base64 {
    param([Parameter(Mandatory)][string]$Value)

    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

if ([string]::IsNullOrWhiteSpace($DisplayName)) {
    $DisplayName = ConvertFrom-Utf8Base64 '5bmz5Y+w6LaF57qn566h55CG5ZGY'
}

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$windowTitle = ConvertFrom-Utf8Base64 '5pys5Zyw6LaF57qn566h55CG5ZGY5Yid5aeL5YyW'
$creatingMessage = (ConvertFrom-Utf8Base64 '5q2j5Zyo5Yib5bu65pys5Zyw6LaF57qn566h55CG5ZGYIHswfeKApuKApg==') -f $EmployeeNo
$closeMessage = (ConvertFrom-Utf8Base64 '5Li05pe25a+G56CB5Y+q5pi+56S65LiA5qyh77yb5oiQ5Yqf5ZCO56qX5Y+j5bCG5ZyoIHswfSDnp5LlhoXoh6rliqjlhbPpl63jgII=') -f $CloseAfterSeconds
$missingRuntimeMessage = ConvertFrom-Utf8Base64 '5pyq5om+5YiwIHV2IOaIluWPr+eUqOeahCBDb2RleCBQeXRob27vvIzml6Dms5XlkK/liqjmnKzlnLDliJ3lp4vljJbjgII='
$failureMessage = ConvertFrom-Utf8Base64 '5Yid5aeL5YyW5aSx6LSl77yb56qX5Y+j5bCG5ZyoIDMwIOenkuWQjuiHquWKqOWFs+mXreOAgg=='
$escapedRoot = $repositoryRoot.Replace("'", "''")
$escapedEmployeeNo = $EmployeeNo.Replace("'", "''")
$escapedDisplayName = $DisplayName.Replace("'", "''")
$escapedWindowTitle = $windowTitle.Replace("'", "''")
$escapedCreatingMessage = $creatingMessage.Replace("'", "''")
$escapedCloseMessage = $closeMessage.Replace("'", "''")
$escapedMissingRuntimeMessage = $missingRuntimeMessage.Replace("'", "''")
$escapedFailureMessage = $failureMessage.Replace("'", "''")
$childCommand = @"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Set-Location -LiteralPath '$escapedRoot'
`$Host.UI.RawUI.WindowTitle = '$escapedWindowTitle'
Write-Host '$escapedCreatingMessage' -ForegroundColor Cyan
Write-Host '$escapedCloseMessage' -ForegroundColor Yellow
`$uv = Get-Command uv -ErrorAction SilentlyContinue
if (`$uv) {
    & `$uv.Source run python -m control_plane.tools.bootstrap_admin_window --employee-no '$escapedEmployeeNo' --display-name '$escapedDisplayName' --close-after-seconds $CloseAfterSeconds
} else {
    `$codexPython = Join-Path `$env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (-not (Test-Path -LiteralPath `$codexPython)) {
        Write-Error '$escapedMissingRuntimeMessage'
        exit 4
    }
    `$env:PYTHONPATH = '$escapedRoot;' + (Join-Path '$escapedRoot' '.venv\Lib\site-packages')
    & `$codexPython -m control_plane.tools.bootstrap_admin_window --employee-no '$escapedEmployeeNo' --display-name '$escapedDisplayName' --close-after-seconds $CloseAfterSeconds
}
`$bootstrapExitCode = `$LASTEXITCODE
if (`$bootstrapExitCode -ne 0) {
    Write-Host '$escapedFailureMessage' -ForegroundColor Red
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
