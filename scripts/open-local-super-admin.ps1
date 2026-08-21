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

$childScript = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'run-local-super-admin.ps1')).Path
$env:LOCAL_BOOTSTRAP_EMPLOYEE_NO = $EmployeeNo
$env:LOCAL_BOOTSTRAP_DISPLAY_NAME_BASE64 = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($DisplayName)
)
$env:LOCAL_BOOTSTRAP_CLOSE_AFTER_SECONDS = [string]$CloseAfterSeconds
$escapedChildScript = $childScript.Replace('"', '""')
$launchCommand = 'start "Local Super Admin Bootstrap" powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $escapedChildScript
& $env:ComSpec /d /s /c $launchCommand
if ($LASTEXITCODE -ne 0) {
    throw "Unable to open the local Super Admin bootstrap console."
}
