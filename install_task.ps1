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
    [switch]$NoWake,                     # by default the task wakes the PC from sleep/hibernate
    [int]$SleepAfterMinutes = 10,        # sleep again after the round if idle this long (0 = never)
    [int]$RoundTimeoutMinutes = 5,       # watchdog: kill a round that runs longer than this
    [string]$RepeatUntil = "",           # e.g. "2026-09-26 00:00" - stop repeating after this moment
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "run_monitor.ps1"

function Get-WakeTimerState {
    # Read the "Allow wake timers" power setting (SUB_SLEEP / RTCWAKE). 1 = enabled.
    # NOTE: powercfg output is LOCALIZED (the "Current AC Power Setting Index" label is
    # translated on non-English Windows), so match on the 0x hex values instead of the
    # English labels: the two "Current ... Index" lines are the last two 0x values in this
    # query, in AC-then-DC order.
    $out = & powercfg /query SCHEME_CURRENT SUB_SLEEP RTCWAKE 2>&1 | Out-String
    $hex = [regex]::Matches($out, '0x([0-9a-fA-F]{1,8})') | ForEach-Object { [Convert]::ToInt32($_.Groups[1].Value, 16) }
    if ($hex.Count -ge 2) {
        return @{ AC = $hex[$hex.Count - 2]; DC = $hex[$hex.Count - 1] }
    }
    return @{ AC = $null; DC = $null }
}

function Enable-WakeTimers {
    # A wake-to-run task only works if wake timers are allowed by the power plan.
    # Changing the active scheme normally needs an elevated shell, so report clearly if denied.
    $before = Get-WakeTimerState
    Write-Host ("  wake timers before: AC={0} DC={1}  (1 = allowed)" -f $before.AC, $before.DC)
    if ($before.AC -eq 1) {
        Write-Host "  wake timers already enabled - nothing to change." -ForegroundColor Green
        return
    }
    $err = & powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 2>&1 | Out-String
    $err += & powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 2>&1 | Out-String
    & powercfg /setactive SCHEME_CURRENT 2>&1 | Out-Null
    $after = Get-WakeTimerState
    Write-Host ("  wake timers after : AC={0} DC={1}" -f $after.AC, $after.DC)
    if ($after.AC -ne 1) {
        Write-Host "  ! Could not enable wake timers (needs an elevated shell)." -ForegroundColor Yellow
        if ($err.Trim()) { Write-Host ("    powercfg said: " + $err.Trim()) -ForegroundColor Yellow }
        Write-Host "    Run this in an ADMIN PowerShell, then the PC can wake itself from sleep:" -ForegroundColor Yellow
        Write-Host "      powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1" -ForegroundColor Yellow
        Write-Host "      powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1" -ForegroundColor Yellow
        Write-Host "      powercfg /setactive SCHEME_CURRENT" -ForegroundColor Yellow
    } else {
        Write-Host "  wake timers enabled - the PC can now wake itself from sleep/hibernate." -ForegroundColor Green
    }
}

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
    Write-Host ("WakeToRun: {0}   (true = task wakes the PC from sleep/hibernate)" -f $t.Settings.WakeToRun)
    $w = Get-WakeTimerState
    Write-Host ("Wake timers allowed by power plan: AC={0} DC={1}   (1 = allowed)" -f $w.AC, $w.DC)
    if ($t.Actions[0].Arguments -match 'SleepAfter') {
        Write-Host "SleepAfter: enabled (sleeps again when the machine is idle)"
    } else {
        Write-Host "SleepAfter: disabled (the PC stays awake after a round)"
    }
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

$argLine = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" -Config `"$Config`""
if ($SleepAfterMinutes -gt 0) {
    $argLine += " -SleepAfter -IdleMinutes $SleepAfterMinutes"
}
if ($RoundTimeoutMinutes -gt 0) {
    $argLine += " -RoundTimeoutMinutes $RoundTimeoutMinutes"
}

$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument $argLine `
    -WorkingDirectory $root

# First run 2 minutes from now, then repeat every $IntervalMinutes.
# With -RepeatUntil the repetition stops at that moment (used for a known end of the
# monitoring window, e.g. "only until 2026-09-25"), so the task cannot keep burning
# Cloudflare publish quota after the flight is no longer worth watching.
$duration = New-TimeSpan -Days 3650
if ($RepeatUntil) {
    $until = [datetime]::Parse($RepeatUntil)
    $span = $until - (Get-Date)
    if ($span.TotalMinutes -lt 2) { throw "RepeatUntil is in the past or too close: $RepeatUntil" }
    $duration = $span
}
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration $duration

$settingArgs = @{
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
    StartWhenAvailable = $true
    RunOnlyIfNetworkAvailable = $true
    MultipleInstances = "IgnoreNew"
    ExecutionTimeLimit = (New-TimeSpan -Minutes 30)
}
if (-not $NoWake) { $settingArgs["WakeToRun"] = $true }
$settings = New-ScheduledTaskSettingsSet @settingArgs

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
if (-not $NoWake) { Enable-WakeTimers }
Show-Status
