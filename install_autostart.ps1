# Good ol Tracker - run the desktop tracker at login and keep it running.
#
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1              # install / update
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall   # remove
#
# Creates a per-user scheduled task (no admin needed) that:
#   * starts the tracker when you log in,
#   * checks every 5 minutes and re-launches it if it isn't running (a second copy
#     exits immediately, so this is a watchdog, never a duplicate),
#   * never stops it on battery or after a time limit.
# Replaces the older Startup-folder shortcut.

param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$TaskName = "Good ol Tracker"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Project "desktop\tracker.py"
$OldShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Good ol Tracker.lnk"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed scheduled task '$TaskName'. (The tracker keeps running until you end pythonw.exe or log out.)"
    exit 0
}

# The real pythonw.exe (not the Microsoft Store alias, which scheduled tasks can't always start).
$python = (& python -c "import sys; print(sys.executable)").Trim()
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$Script`"" -WorkingDirectory (Join-Path $Project "desktop")

$atLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Watchdog: a time-based trigger repeating every 5 minutes, indefinitely. (A repetition on
# the logon trigger alone only starts after the next login, and "restart on failure" doesn't
# fire when the process is killed, so this is what actually brings it back.)
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $watchdog) -Settings $settings `
    -Principal $principal -Description "Good ol Tracker desktop logger + local server (127.0.0.1:47321)" -Force | Out-Null

if (Test-Path $OldShortcut) {
    Remove-Item $OldShortcut -Confirm:$false
    Write-Output "Removed old Startup shortcut (replaced by the scheduled task)."
}
Write-Output "Installed scheduled task '$TaskName' -> $pythonw `"$Script`""
