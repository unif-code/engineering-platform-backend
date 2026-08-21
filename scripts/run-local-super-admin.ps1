[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function ConvertFrom-Utf8Base64 {
    param([Parameter(Mandatory)][string]$Value)

    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

$employeeNo = $env:LOCAL_BOOTSTRAP_EMPLOYEE_NO
if ($employeeNo -notmatch '^\d{8}$') {
    throw "LOCAL_BOOTSTRAP_EMPLOYEE_NO must contain exactly eight digits."
}

$displayName = ConvertFrom-Utf8Base64 $env:LOCAL_BOOTSTRAP_DISPLAY_NAME_BASE64
$closeAfterSeconds = 0
if (
    -not [int]::TryParse(
        $env:LOCAL_BOOTSTRAP_CLOSE_AFTER_SECONDS,
        [ref]$closeAfterSeconds
    ) -or $closeAfterSeconds -lt 1 -or $closeAfterSeconds -gt 900
) {
    throw "LOCAL_BOOTSTRAP_CLOSE_AFTER_SECONDS must be between 1 and 900."
}

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repositoryRoot
$Host.UI.RawUI.WindowTitle = ConvertFrom-Utf8Base64 '5pys5Zyw6LaF57qn566h55CG5ZGY5Yid5aeL5YyW'
$creatingMessage = (ConvertFrom-Utf8Base64 '5q2j5Zyo5Yib5bu65pys5Zyw6LaF57qn566h55CG5ZGYIHswfeKApuKApg==') -f $employeeNo
$closeMessage = (ConvertFrom-Utf8Base64 '5Li05pe25a+G56CB5Y+q5pi+56S65LiA5qyh77yb5oiQ5Yqf5ZCO56qX5Y+j5bCG5ZyoIHswfSDnp5LlhoXoh6rliqjlhbPpl63jgII=') -f $closeAfterSeconds
$missingRuntimeMessage = ConvertFrom-Utf8Base64 '5pyq5om+5YiwIHV2IOaIluWPr+eUqOeahCBDb2RleCBQeXRob27vvIzml6Dms5XlkK/liqjmnKzlnLDliJ3lp4vljJbjgII='
$failureMessage = ConvertFrom-Utf8Base64 '5Yid5aeL5YyW5aSx6LSl77yb56qX5Y+j5bCG5ZyoIDMwIOenkuWQjuiHquWKqOWFs+mXreOAgg=='

Write-Host $creatingMessage -ForegroundColor Cyan
Write-Host $closeMessage -ForegroundColor Yellow
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    & $uv.Source run python -m control_plane.tools.bootstrap_admin_window --employee-no $employeeNo --display-name $displayName --close-after-seconds $closeAfterSeconds
} else {
    $codexPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (-not (Test-Path -LiteralPath $codexPython)) {
        Write-Error $missingRuntimeMessage
        exit 4
    }
    $env:PYTHONPATH = $repositoryRoot + ';' + (Join-Path $repositoryRoot '.venv\Lib\site-packages')
    & $codexPython -m control_plane.tools.bootstrap_admin_window --employee-no $employeeNo --display-name $displayName --close-after-seconds $closeAfterSeconds
}

$bootstrapExitCode = $LASTEXITCODE
if ($bootstrapExitCode -ne 0) {
    Write-Host $failureMessage -ForegroundColor Red
    Start-Sleep -Seconds 30
}
exit $bootstrapExitCode
