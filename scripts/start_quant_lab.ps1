[CmdletBinding()]
param(
    [switch]$RunProbe,
    [switch]$TradeCoach,
    [string]$PythonPath,
    [int]$PollSeconds = 20,
    [int]$ApiPort = 8765,
    [int]$FrontendPort = 5173,
    [string]$DataRoot = "data\forward_probe",
    [string]$Database = "data\forward_probe\quant_lab_foundation.sqlite3"
)

$ErrorActionPreference = "Stop"
if ($PollSeconds -lt 1) { throw "PollSeconds must be at least 1." }
if ($ApiPort -lt 1 -or $ApiPort -gt 65535) { throw "ApiPort must be between 1 and 65535." }
if ($FrontendPort -lt 1 -or $FrontendPort -gt 65535) { throw "FrontendPort must be between 1 and 65535." }

# Python subprocesses on Windows can preserve both `Path` and `PATH` in the
# inherited environment block. Windows PowerShell 5.1 accepts that block until
# a child launch is requested, then Start-Process fails with a duplicate-key
# error. Normalize only this launcher process's copy before setting its own
# temporary variables; no parent process or persistent user environment is
# changed.
$InheritedPath = [Environment]::GetEnvironmentVariable("Path", "Process")
Remove-Item Env:Path -ErrorAction SilentlyContinue
Remove-Item Env:PATH -ErrorAction SilentlyContinue
if ($InheritedPath) { $env:Path = $InheritedPath }

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

# This launcher is local-only. It never registers a task, installs packages,
# contacts a remote host, or runs a probe unless -RunProbe is explicit.
if ($PythonPath) {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "PythonPath not found: $PythonPath" }
    $Python = (Resolve-Path -LiteralPath $PythonPath).Path
} else {
    $ProjectVenv = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $ProjectVenv -PathType Leaf) {
        $Python = (Resolve-Path -LiteralPath $ProjectVenv).Path
    }
    $KnownBundled = @(
        (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"),
        (Join-Path $env:LOCALAPPDATA "codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
    )
    if (-not $Python) { $Python = $KnownBundled | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1 }
    if (-not $Python) {
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $PythonCommand) { throw "Python executable not found; pass -PythonPath explicitly." }
        $Python = $PythonCommand.Source
    }
}
if ($TradeCoach) {
    # aiohttp is optional: it is only needed when QQ Bot notifications are
    # explicitly configured. The local read-only dashboard must remain usable
    # without contacting or installing anything at startup.
}
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$ViteEntry = Join-Path $FrontendRoot "node_modules\vite\bin\vite.js"
$TypeScriptEntry = Join-Path $FrontendRoot "node_modules\typescript\bin\tsc"
if (-not (Test-Path -LiteralPath $ViteEntry -PathType Leaf)) {
    throw "Frontend dependencies are missing. Run 'cd frontend; pnpm install' once, then rerun this launcher. No dependencies are installed automatically."
}
if (-not (Test-Path -LiteralPath $TypeScriptEntry -PathType Leaf)) {
    throw "Frontend TypeScript compiler is missing. Run 'cd frontend; pnpm install' once, then rerun this launcher."
}
$KnownBundledNodeBins = @(
    (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"),
    (Join-Path $env:LOCALAPPDATA "codex-runtimes\codex-primary-runtime\dependencies\node\bin")
)
$NodeBin = $KnownBundledNodeBins | Where-Object { Test-Path -LiteralPath (Join-Path $_ "node.exe") -PathType Leaf } | Select-Object -First 1
if ($NodeBin) {
    $NodeExecutable = Join-Path $NodeBin "node.exe"
    # NodeExecutable is passed as an absolute path below. Do not rewrite the
    # process PATH here: Windows PowerShell 5.1 can receive both `Path` and
    # `PATH` entries from Python/IDE launchers, and mutating either spelling
    # makes Start-Process reject the inherited environment as a duplicate key.
} else {
    $NodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($null -eq $NodeCommand) {
        throw "Node executable not found for Vite; install the frontend runtime or add node.exe to PATH."
    }
    $NodeExecutable = $NodeCommand.Source
}

function Test-LocalPortOpen([int]$Port) {
    $Client = [System.Net.Sockets.TcpClient]::new()
    try {
        $Async = $Client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $Async.AsyncWaitHandle.WaitOne(250)) { return $false }
        $Client.EndConnect($Async)
        return $true
    } catch {
        return $false
    } finally {
        $Client.Dispose()
    }
}
if ($ApiPort -eq $FrontendPort) { throw "ApiPort and FrontendPort must be different." }

$ApiDatabase = Join-Path $ProjectRoot $Database
if ($TradeCoach) {
    # Consume the operator-reviewed local export by default.  This does not
    # contact the VPS: the file is append-only evidence synchronized by a
    # separate reviewed job.  An explicit environment value still wins.
    if (-not $env:QUANT_LAB_VPS_FACT_PATH) {
        $DefaultVpsFactPath = Join-Path $ProjectRoot "data\forward\vps_macro_risk_points.jsonl"
        if (Test-Path -LiteralPath $DefaultVpsFactPath -PathType Leaf) {
            $env:QUANT_LAB_VPS_FACT_PATH = $DefaultVpsFactPath
        }
    }
    # The Personal Trade Coach is a separate local product database. Keep the
    # legacy Sprint 1 dashboard path below unchanged for its existing users.
    $ApiArguments = @("-m", "quant_lab.cli", "trade-coach", "--db", $ApiDatabase, "--project-root", $ProjectRoot, "--serve", "--host", "127.0.0.1", "--port", "$ApiPort")
} else {
    $ApiArguments = @("-m", "quant_lab.cli", "dashboard", "--db", $ApiDatabase, "--host", "127.0.0.1", "--port", "$ApiPort")
}
$TypeScriptArguments = @($TypeScriptEntry, "-b")
$ViteBuildArguments = @($ViteEntry, "build", "--configLoader", "runner")
# Serve the compiled bundle. The dev server's noDiscovery safety setting is
# intentionally not used for the user-facing launcher: React's CommonJS
# client entry must be bundled before a browser loads it.
$FrontendArguments = @($ViteEntry, "preview", "--host", "127.0.0.1", "--port", "$FrontendPort", "--configLoader", "runner")
$ApiProcess = $null
$FrontendProcess = $null
$RuntimeRoot = Join-Path $ProjectRoot "data\trade_coach\runtime"
New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
$StatePath = Join-Path $RuntimeRoot "quantlab-$ApiPort.state.json"
$RunStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$ApiStdoutLog = Join-Path $RuntimeRoot "api-$RunStamp.out.log"
$ApiStderrLog = Join-Path $RuntimeRoot "api-$RunStamp.err.log"
$FrontendStdoutLog = Join-Path $RuntimeRoot "frontend-$RunStamp.out.log"
$FrontendStderrLog = Join-Path $RuntimeRoot "frontend-$RunStamp.err.log"
$FactSyncWrapper = Join-Path $PSScriptRoot "sync_trade_coach_facts.ps1"
$FactSyncExecuted = @{}

function Stop-StaleQuantLab {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return }
    try { $old = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json } catch { return }
    $oldPid = [int]$old.api_pid
    if ($oldPid -le 0) { return }
    try { $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid" -ErrorAction Stop } catch { return }
    $samePath = $proc.ExecutablePath -and ((Resolve-Path -LiteralPath $Python).Path -eq $proc.ExecutablePath)
    $sameCommand = [string]$proc.CommandLine -like "*quant_lab.cli*trade-coach*"
    $portOpen = Test-LocalPortOpen $ApiPort
    if ($samePath -and $sameCommand -and $portOpen) {
        try { Stop-Process -Id $oldPid -Force -ErrorAction Stop } catch { return }
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    }
}
Stop-StaleQuantLab
if (Test-LocalPortOpen $ApiPort) { throw "API port $ApiPort is already in use at 127.0.0.1; no process was changed." }
if (Test-LocalPortOpen $FrontendPort) { throw "Frontend port $FrontendPort is already in use at 127.0.0.1; no process was changed." }

function Invoke-TradeCoachFactSync([string]$Reason) {
    if (-not $TradeCoach) { return }
    if (-not (Test-Path -LiteralPath $FactSyncWrapper -PathType Leaf)) {
        Write-Warning "事实同步脚本不存在；保留已有本地事实：$FactSyncWrapper"
        return
    }
    Write-Host "正在执行只读事实同步（$Reason）..."
    & $FactSyncWrapper -PythonPath $Python
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "事实同步未完成；已有风险与锡文件保持不变，并将按各自有效期自然转为 STALE/MISSING。"
    }
}

function Invoke-DueTradeCoachFactSync {
    if (-not $TradeCoach) { return }
    $now = Get-Date
    if ($now.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) { return }
    $target = [DateTime]::Today.AddHours(16).AddMinutes(45)
    $key = $now.ToString("yyyy-MM-dd")
    $minutesAfter = ($now - $target).TotalMinutes
    if (-not $FactSyncExecuted.ContainsKey($key) -and $minutesAfter -ge 0 -and $minutesAfter -le 5) {
        $FactSyncExecuted[$key] = $true
        Invoke-TradeCoachFactSync "16:45 收盘后"
    }
}

$Checkpoints = @(
    @{ Label = "09:31"; Hour = 9; Minute = 31 },
    @{ Label = "10:00"; Hour = 10; Minute = 0 },
    @{ Label = "13:30"; Hour = 13; Minute = 30 },
    @{ Label = "14:50"; Hour = 14; Minute = 50 },
    @{ Label = "15:05"; Hour = 15; Minute = 5 }
)
$ExecutedCheckpoints = @{}
function Get-CurrentCheckpoint {
    $now = Get-Date
    foreach ($target in $Checkpoints) {
        $targetTime = [DateTime]::Today.AddHours($target.Hour).AddMinutes($target.Minute)
        $key = "{0}/{1}" -f $now.ToString("yyyy-MM-dd"), $target.Label
        $minutesAfterTarget = ($now - $targetTime).TotalMinutes
        # Never sample before the checkpoint: 09:26 is not 09:31, and 13:25
        # precedes the futures afternoon session. A late sample is allowed for
        # at most five minutes; missed windows are not backfilled.
        if (-not $ExecutedCheckpoints.ContainsKey($key) -and $minutesAfterTarget -ge 0 -and $minutesAfterTarget -le 5) {
            return @{ Label = $target.Label; Key = $key }
        }
    }
    return $null
}

function Stop-LocalProcessTree([System.Diagnostics.Process]$Process) {
    if ($null -eq $Process -or $Process.HasExited) { return }
    try {
        $Process.Kill($true)
        $Process.WaitForExit(3000)
    } catch {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
}

function Read-LauncherLog([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "(log not created yet: $Path)" }
    $lines = @(Get-Content -LiteralPath $Path -Tail 30 -ErrorAction SilentlyContinue)
    if ($lines.Count -eq 0) { return "(log is empty: $Path)" }
    return ($lines -join [Environment]::NewLine)
}

function Wait-LocalService([string]$Label, [int]$Port, [System.Diagnostics.Process]$Process, [string]$ErrorLog) {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($Process.HasExited) {
            throw ("{0} stopped before becoming ready (status code {1}). Log: {2}`n{3}" -f $Label, $Process.ExitCode, $ErrorLog, (Read-LauncherLog $ErrorLog))
        }
        if (Test-LocalPortOpen $Port) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "$Label did not open 127.0.0.1:$Port within 15 seconds. Log: $ErrorLog`n$(Read-LauncherLog $ErrorLog)"
}

try {
    # Startup synchronization is fail-safe: SSH/Tushare failure only appends a
    # local audit row and never blocks the UI or replaces last-known facts.
    Invoke-TradeCoachFactSync "PowerShell 7 主启动"
    # Keep the React preview proxy aligned with a non-default API port too. The
    # value is inherited only by the build/preview child processes and restored
    # below.
    $PreviousCoachApiPort = $env:QUANT_LAB_API_PORT
    $PreviousCoachAiDiscovery = $env:TRADE_COACH_AI_DISCOVER_MODELS
    $env:QUANT_LAB_API_PORT = "$ApiPort"
    # Model discovery is diagnostics only. Startup never makes a remote AI
    # request; the frozen model is called directly only after an explicit UI
    # action and the actual response remains fail-closed.
    $env:TRADE_COACH_AI_DISCOVER_MODELS = "0"
    Push-Location -LiteralPath $FrontendRoot
    try {
        Write-Host "正在构建 React 生产前端..."
        & $NodeExecutable @TypeScriptArguments
        if ($LASTEXITCODE -ne 0) { throw "TypeScript build stopped with code $LASTEXITCODE." }
        & $NodeExecutable @ViteBuildArguments
        if ($LASTEXITCODE -ne 0) { throw "Vite production build stopped with code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
    $ApiProcess = Start-Process -FilePath $Python -ArgumentList $ApiArguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $ApiStdoutLog -RedirectStandardError $ApiStderrLog -PassThru
    $FrontendProcess = Start-Process -FilePath $NodeExecutable -ArgumentList $FrontendArguments -WorkingDirectory $FrontendRoot -WindowStyle Hidden -RedirectStandardOutput $FrontendStdoutLog -RedirectStandardError $FrontendStderrLog -PassThru
    @{ api_pid = $ApiProcess.Id; api_port = $ApiPort; python_path = $Python; started_at = (Get-Date).ToString("o") } | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
    if ($null -eq $PreviousCoachApiPort) { Remove-Item Env:QUANT_LAB_API_PORT -ErrorAction SilentlyContinue } else { $env:QUANT_LAB_API_PORT = $PreviousCoachApiPort }
    if ($null -eq $PreviousCoachAiDiscovery) { Remove-Item Env:TRADE_COACH_AI_DISCOVER_MODELS -ErrorAction SilentlyContinue } else { $env:TRADE_COACH_AI_DISCOVER_MODELS = $PreviousCoachAiDiscovery }
    Wait-LocalService "Quant-Lab API" $ApiPort $ApiProcess $ApiStderrLog
    Wait-LocalService "Quant-Lab frontend" $FrontendPort $FrontendProcess $FrontendStderrLog
    Write-Host "Quant-Lab API: http://127.0.0.1:$ApiPort/ (PID $($ApiProcess.Id))"
    Write-Host "Quant-Lab frontend: http://127.0.0.1:$FrontendPort/ (PID $($FrontendProcess.Id))"
    Write-Host "启动日志：$RuntimeRoot (本次 $RunStamp)"
    if (-not $RunProbe) {
        # Keep an ASCII terminator before the closing quote. Windows PowerShell
        # 5.1 reads UTF-8 scripts without a BOM using the active code page; a
        # quote immediately after a CJK byte sequence can otherwise be parsed
        # as part of the expandable string and swallow the health loop below.
        Write-Host "只读模式：未启用 probe。按 Ctrl+C 停止并清理子进程."
        while ($true) {
            $ApiProcess.Refresh()
            $FrontendProcess.Refresh()
            if ($ApiProcess.HasExited) { throw ("Quant-Lab API stopped unexpectedly (status code {0}). Log: {1}`n{2}" -f $ApiProcess.ExitCode, $ApiStderrLog, (Read-LauncherLog $ApiStderrLog)) }
            if ($FrontendProcess.HasExited) { throw ("Quant-Lab frontend stopped unexpectedly (status code {0}). Log: {1}`n{2}" -f $FrontendProcess.ExitCode, $FrontendStderrLog, (Read-LauncherLog $FrontendStderrLog)) }
            Invoke-DueTradeCoachFactSync
            Start-Sleep -Seconds 2
        }
    } else {
        Write-Host "已显式启用 probe：固定检查点、无离线回补。PollSeconds=$PollSeconds。按 Ctrl+C 停止."
        while ($true) {
            $ApiProcess.Refresh()
            $FrontendProcess.Refresh()
            if ($ApiProcess.HasExited) { throw ("Quant-Lab API stopped unexpectedly (status code {0}). Log: {1}`n{2}" -f $ApiProcess.ExitCode, $ApiStderrLog, (Read-LauncherLog $ApiStderrLog)) }
            if ($FrontendProcess.HasExited) { throw ("Quant-Lab frontend stopped unexpectedly (status code {0}). Log: {1}`n{2}" -f $FrontendProcess.ExitCode, $FrontendStderrLog, (Read-LauncherLog $FrontendStderrLog)) }
            Invoke-DueTradeCoachFactSync
            $slot = Get-CurrentCheckpoint
            if ($null -ne $slot) {
                $ExecutedCheckpoints[$slot.Key] = $true
                $ProbeDatabase = if ($TradeCoach) { Join-Path $ProjectRoot "data\forward_probe\quant_lab_foundation.sqlite3" } else { $ApiDatabase }
                $ProbeArguments = @("-m", "quant_lab.cli", "probe", "--data-root", (Join-Path $ProjectRoot $DataRoot), "--db", $ProbeDatabase, "--check-point", $slot.Label)
                & $Python @ProbeArguments
                if ($LASTEXITCODE -ne 0) { Write-Warning "Probe stopped with code $LASTEXITCODE; checkpoint will not be retried or backfilled." }
            }
            Start-Sleep -Seconds $PollSeconds
        }
    }
} finally {
    foreach ($Process in @($FrontendProcess, $ApiProcess)) {
        Stop-LocalProcessTree $Process
    }
    Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
}
