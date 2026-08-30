[CmdletBinding()]
param(
    [switch]$RunProbe,
    [string]$PythonPath,
    [int]$PollSeconds = 20,
    [int]$ApiPort = 8765,
    [int]$FrontendPort = 5173,
    [string]$Database = "data\trade_coach\trade_coach.sqlite3"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $PSScriptRoot "start_quant_lab.ps1"
if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "Quant-Lab launcher not found: $Launcher"
}

# PowerShell 7 is the primary runtime. If an older console invokes this
# wrapper, synchronously re-enter it with pwsh.exe. Windows PowerShell 5.1 is
# retained only as a compatibility fallback when PowerShell 7 is unavailable.
if ($PSVersionTable.PSVersion.Major -lt 7) {
    $PwshCandidates = @(
        (Join-Path $env:ProgramFiles "PowerShell\7\pwsh.exe"),
        (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"),
        (Join-Path $env:LOCALAPPDATA "codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe")
    )
    $PwshExecutable = $PwshCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $PwshExecutable) {
        $PwshCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue
        if ($null -ne $PwshCommand) { $PwshExecutable = $PwshCommand.Source }
    }
    if ($PwshExecutable) {
        # Python/IDE launchers may leave both Path and PATH in the Windows
        # environment block. Windows PowerShell 5.1 Start-Process rejects that
        # duplicate even though ordinary command invocation accepts it.
        $InheritedPath = [Environment]::GetEnvironmentVariable("Path", "Process")
        Remove-Item Env:Path -ErrorAction SilentlyContinue
        Remove-Item Env:PATH -ErrorAction SilentlyContinue
        if ($InheritedPath) { $env:Path = $InheritedPath }
        $PwshArguments = @("-NoProfile", "-File", $PSCommandPath, "-ApiPort", "$ApiPort", "-FrontendPort", "$FrontendPort", "-PollSeconds", "$PollSeconds", "-Database", $Database)
        if ($RunProbe) { $PwshArguments += "-RunProbe" }
        if ($PythonPath) { $PwshArguments += @("-PythonPath", $PythonPath) }
        $PwshProcess = Start-Process -FilePath $PwshExecutable -ArgumentList $PwshArguments -NoNewWindow -Wait -PassThru
        $pwshExitCode = [int]$PwshProcess.ExitCode
        if ($pwshExitCode -ne 0) { $Host.SetShouldExit($pwshExitCode) }
        return
    }
    Write-Warning "PowerShell 7 was not found; continuing with Windows PowerShell 5.1 compatibility mode."
}

# PowerShell 7 (`pwsh.exe`) is the preferred host. Windows PowerShell 5.1
# remains a compatibility fallback; this wrapper does not install packages,
# register a task, connect to a broker, or enable automatic trading.
Set-Location -LiteralPath $ProjectRoot
$invokeParameters = @{
    TradeCoach = $true
    ApiPort = $ApiPort
    FrontendPort = $FrontendPort
    PollSeconds = $PollSeconds
    Database = $Database
}
if ($RunProbe) { $invokeParameters.RunProbe = $true }
if ($PythonPath) { $invokeParameters.PythonPath = $PythonPath }
& $Launcher @invokeParameters
# Do not use the `exit` keyword here.  Windows PowerShell 5.1 can rebind the
# nested script's invocation context and report that token as a command from
# start_quant_lab.ps1, which immediately tears down the two child services.
# The delegated launcher is intentionally synchronous and owns its own
# process-tree cleanup; SetShouldExit only propagates a non-zero code after it
# returns, while preserving the long-running Ctrl+C behavior in both shells.
$launcherExitCode = 0
if ($null -ne $LASTEXITCODE) {
    $launcherExitCode = [int]$LASTEXITCODE
}
if ($launcherExitCode -ne 0) {
    $Host.SetShouldExit($launcherExitCode)
}
