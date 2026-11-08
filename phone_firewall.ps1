# Lets the Good ol Tracker phone app reach this PC - on PRIVATE networks only
# (your phone's hotspot or home Wi-Fi, once you've set them to "Private" in Windows).
# Public networks (campus, cafe) stay closed.
#
# Run once, as administrator:  right-click PowerShell -> Run as administrator, then
#   powershell -ExecutionPolicy Bypass -File "C:\Users\shihaz shaheem\Desktop\Good ol Tracker\phone_firewall.ps1"
# Undo:  Remove-NetFirewallRule -Group "Good ol Tracker"

$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Please run this as administrator (right-click PowerShell -> Run as administrator)." -ForegroundColor Yellow
    exit 1
}

Get-NetFirewallRule -Group "Good ol Tracker" -ErrorAction SilentlyContinue | Remove-NetFirewallRule

New-NetFirewallRule -DisplayName "Good ol Tracker - phone sync (TCP 47323)" -Group "Good ol Tracker" `
    -Direction Inbound -Protocol TCP -LocalPort 47323 -Profile Private -Action Allow | Out-Null
New-NetFirewallRule -DisplayName "Good ol Tracker - phone discovery (UDP 47324)" -Group "Good ol Tracker" `
    -Direction Inbound -Protocol UDP -LocalPort 47324 -Profile Private -Action Allow | Out-Null

Write-Host "Done: the phone app can reach this PC on Private networks." -ForegroundColor Green
Write-Host ""
Write-Host "Your networks right now:"
Get-NetConnectionProfile | Format-Table Name, InterfaceAlias, NetworkCategory -AutoSize
Write-Host "If your phone's hotspot shows 'Public' above, set it to Private:"
Write-Host "  Settings -> Network & internet -> Wi-Fi -> (the hotspot) -> Network profile type -> Private"
