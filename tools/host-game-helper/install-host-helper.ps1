#Requires -Version 5.1
<#
.SYNOPSIS
  Installs the Turing host game helper to start at user logon (Windows).
  Zero-config: broadcasts UDP on the LAN; the Mini-PC discovers it automatically.
#>
param(
    [string]$Python = "",
    [int]$Port = 8787,
    [string]$TaskName = "TuringHostGameHelper"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$reporter = Join-Path $scriptDir "foreground_reporter.py"

if (-not (Test-Path -LiteralPath $reporter)) {
    throw "foreground_reporter.py not found at $reporter"
}

if (-not $Python) {
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $Python = $pyCmd.Source
        $argument = "-3 `"$reporter`" --port $Port"
    } else {
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCmd) {
            throw "Python not found. Install Python 3 and ensure 'py' or 'python' is on PATH."
        }
        $Python = $pythonCmd.Source
        $argument = "`"$reporter`" --port $Port"
    }
} else {
    $argument = "`"$reporter`" --port $Port"
}

try {
    & $Python -m pip install --quiet --upgrade `
        winrt-Windows.Media.Control `
        winrt-Windows.UI.Notifications.Management `
        winrt-Windows.Foundation `
        winrt-Windows.Storage.Streams
    Write-Host "OK: pacotes WinRT instalados (media/notificacoes habilitados)."
} catch {
    Write-Warning "Pacotes WinRT opcionais falharam ($_) — deteccao de jogo continua funcionando; media/notificacoes ficam desativados. Rode este script de novo depois para tentar novamente."
}

$action = New-ScheduledTaskAction -Execute $Python -Argument $argument -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

try {
    Start-ScheduledTask -TaskName $TaskName
} catch {
    Write-Warning "Task registered but could not start now: $_"
}

Write-Host "OK: '$TaskName' starts at logon and announces games on the LAN (UDP $Port)."
Write-Host "No firewall inbound rule needed. No Mini-PC IP to configure."
Write-Host "On first run Windows may ask to allow Python on Private networks — choose Allow."
