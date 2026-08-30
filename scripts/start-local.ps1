[CmdletBinding()]
param(
    [int]$ApiPort = 8765,
    [int]$FrontendPort = 5173,
    [string]$PythonPath = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
if ($ApiPort -eq $FrontendPort) { throw "ApiPort and FrontendPort must be different." }
$db = Join-Path $root "data\trade_coach\trade_coach.sqlite3"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $db) | Out-Null
Push-Location (Join-Path $root "frontend")
try {
    pnpm exec tsc -b
    pnpm exec vite build --configLoader runner
} finally { Pop-Location }
$api = Start-Process -FilePath $PythonPath -ArgumentList @("-m","quant_lab.cli","trade-coach","--db",$db,"--project-root",$root,"--serve","--host","127.0.0.1","--port","$ApiPort") -WorkingDirectory $root -PassThru
$frontend = Start-Process -FilePath "pnpm" -ArgumentList @("exec","vite","preview","--host","127.0.0.1","--port","$FrontendPort","--configLoader","runner") -WorkingDirectory (Join-Path $root "frontend") -PassThru
Write-Host "API: http://127.0.0.1:$ApiPort (PID $($api.Id))"
Write-Host "Frontend: http://127.0.0.1:$FrontendPort (PID $($frontend.Id))"
try { Wait-Process -Id $api.Id } finally {
    if (-not $api.HasExited) { Stop-Process -Id $api.Id -Force }
    if (-not $frontend.HasExited) { Stop-Process -Id $frontend.Id -Force }
}

