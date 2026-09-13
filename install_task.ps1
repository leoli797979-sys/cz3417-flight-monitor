<#
  Register / inspect / trigger / remove the Windows scheduled task that
  periodically scrapes CZ3417 (Guangzhou -> Chengdu, 2026-09-22) flight prices
  and refreshes the HTML report.

  Usage:
    powershell -ExecutionPolicy Bypass -File install_task.ps1
    powershell -ExecutionPolicy Bypass -File install_task.ps1 -IntervalMinutes 60
    powershell -ExecutionPolicy Bypass -File install_task.ps1 -Status
    powershell -ExecutionPolicy Bypass -File install_task.ps1 -RunNow
    powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall

  The task is registered as "run only when the user is logged on": scraping needs a
  real desktop session for the browser handshake, which is far more reliable than
  session 0 ("run whether user is logged on or not").

  IMPORTANT - keep this file ASCII-only (see the note in run_monitor.ps1).
#>
[CmdletBinding()]
param(
    [int]$IntervalMinutes = 90,
    [string]$TaskName = "CZ3417-Flight-Monitor",
    [string]$Config = "config.yaml",
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "run_monitor.ps1"

function Show-Status {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "Task not found: $TaskName" -ForegroundColor Yellow; return }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    [pscustomobject]@{
        TaskName     = $t.TaskName
        State        = $t.State
        LastRunTime  = $info.LastRunTime
        LastResult   = $info.LastTaskResult
        NextRunTime  = $info.NextRunTime
        LogFile      = (Join-Path $root "logs\task.log")
        ReportFile   = (Join-Path $root "report.html")
    } | Format-List
    $trig = $t.Triggers | Select-Object -First 1
    Write-Host ("Repetition interval: {0}   duration: {1}" -f $trig.Repetition.Interval, $trig.Repetition.Duration)
    Write-Host "LastResult 0 = success, 1 = script error, 267011 = never run yet."
}

if ($Status) { Show-Status; return }

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Triggered once. Check logs\task.log or run -Status shortly." -ForegroundColor Green
    return
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered scheduled task: $TaskName" -ForegroundColor Green
    } else {
        Write-Host "Task does not exist, nothing to remove." -ForegroundColor Yellow
    }
    return
}

if (-not (Test-Path $script)) { throw "wrapper script not found: $script" }

$psExe = (Get-Command powershell.exe -ErrorAction Stop).Source

$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" -Config `"$Config`"") `
    -WorkingDirectory $root

# First run 2 minutes from now, then repeat every $IntervalMinutes for ~10 years.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$principal = New-ScheduledTaskPrincipal `
    -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description ("Scrape CZ3417 CAN-CTU 2026-09-22 flight price every $IntervalMinutes minutes, store to SQLite and refresh report.html") `
    -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName" -ForegroundColor Green
Write-Host ("  interval : every {0} minutes, first run at {1}" -f $IntervalMinutes, (Get-Date).AddMinutes(2).ToString("HH:mm:ss"))
Write-Host ("  command  : `"$psExe`" -File `"$script`"")
Write-Host ("  log      : " + (Join-Path $root "logs\task.log"))
Write-Host ("  report   : " + (Join-Path $root "report.html"))
Show-Status
