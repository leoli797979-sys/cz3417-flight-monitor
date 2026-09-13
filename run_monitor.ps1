<#
  Scheduled scraper wrapper: run ONE scraping round, then refresh the HTML report.
  Invoked by the Windows scheduled task (see install_task.ps1) or manually.

  Usage:
    powershell -ExecutionPolicy Bypass -File run_monitor.ps1          # headless (default)
    powershell -ExecutionPolicy Bypass -File run_monitor.ps1 -Show    # visible browser window

  IMPORTANT - keep this file ASCII-only.
  Windows PowerShell 5.1 reads a .ps1 without a UTF-8 BOM using the system ANSI
  codepage; non-ASCII text then gets mis-decoded and can even break parsing
  (that is exactly what happened once here: a stray token made the script fail
  with exit code 1 before it could write any log).
#>
[CmdletBinding()]
param(
    [string]$Config = "config.yaml",
    [switch]$Show,                      # default is headless; -Show pops a browser window
    [int]$MaxLogKB = 2048
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# Make the child Python process emit UTF-8 regardless of system locale.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "task.log"

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding UTF8
}

# Rotate the log so it cannot grow without bound.
if ((Test-Path $log) -and ((Get-Item $log).Length -gt ($MaxLogKB * 1KB))) {
    Move-Item -Path $log -Destination "$log.1" -Force
    Write-Log "log rotated to task.log.1"
}

# Re-entrancy guard: skip this trigger if the previous round is still running.
# The scraping target rate-limits aggressively, so overlapping runs are harmful.
$mutex = New-Object System.Threading.Mutex($false, "cz3417_flight_monitor")
if (-not $mutex.WaitOne(0)) {
    Write-Log "previous round still running - skip this trigger"
    exit 0
}

function Publish-Snapshot {
    # Commit + push data/prices.db. That push triggers publish.yml on GitHub,
    # which renders the report and deploys it to GitHub Pages.
    # Best effort: a network problem must never fail the scraping round.
    $git = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
    if (-not $git) { Write-Log "git not found - skip publish"; return }

    & $git add -f data/prices.db 2>&1 | Out-Null
    & $git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Log "no DB change - skip publish"
        return
    }
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    & $git -c user.name=cz3417-monitor -c user.email=cz3417-monitor@users.noreply.github.com `
        commit -q -m ("data: price snapshot " + $stamp) 2>&1 | ForEach-Object { Write-Log ("  " + $_) }

    # Remote may have moved (cloud workflow commits the DB too) - rebase first.
    & $git pull --rebase --autostash -q mine main 2>&1 | ForEach-Object { Write-Log ("  " + $_) }
    & $git push -q mine main 2>&1 | ForEach-Object { Write-Log ("  " + $_) }
    if ($LASTEXITCODE -eq 0) {
        Write-Log "pushed snapshot - GitHub Pages will republish"
    } else {
        Write-Log "push failed (network?) - will retry next round"
    }
}

try {
    Write-Log ("===== start round (headless={0}) =====" -f (-not $Show))
    $pyArgs = @("main.py", "--once", "-c", $Config)
    if (-not $Show) { $pyArgs += "--headless" }

    # Do NOT pipe the child output into PowerShell: PS 5.1 decodes pipe bytes with the
    # system ANSI codepage and mangles UTF-8 (Chinese log lines), and inside a scheduled
    # task there is no console so [Console]::OutputEncoding cannot fix it.
    # Redirect straight to files and read them back as UTF-8 instead.
    $tmpOut = Join-Path $logDir "task.stdout.tmp"
    $tmpErr = Join-Path $logDir "task.stderr.tmp"
    Remove-Item $tmpOut, $tmpErr -ErrorAction SilentlyContinue

    $proc = Start-Process -FilePath $py -ArgumentList $pyArgs `
        -WorkingDirectory $root -NoNewWindow -PassThru -Wait `
        -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
    $code = $proc.ExitCode

    foreach ($f in @($tmpOut, $tmpErr)) {
        if (Test-Path $f) {
            Get-Content -Path $f -Encoding UTF8 | ForEach-Object { Write-Log ("  " + $_) }
        }
    }
    Remove-Item $tmpOut, $tmpErr -ErrorAction SilentlyContinue
    Write-Log ("===== round done, exit code {0} =====" -f $code)

    # Push the fresh snapshot so the cloud can render + publish it.
    # Publishing itself is done by .github/workflows/publish.yml (triggered by this push):
    # cloud-side scraping is impossible because qunar redirects datacenter IPs to a login page.
    if ($code -eq 0) { Publish-Snapshot }
}
catch {
    Write-Log ("FATAL: " + $_.Exception.Message)
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
