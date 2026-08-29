# Windows Notification Utility for Quant Pipeline
param(
    [string]$Title = "Quant Pipeline",
    [string]$Message = "A task is done or awaiting your response."
)

[void][System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms")
$objNotifyIcon = New-Object System.Windows.Forms.NotifyIcon
$objNotifyIcon.Icon = [System.Drawing.SystemIcons]::Information
$objNotifyIcon.BalloonTipIcon = "Info"
$objNotifyIcon.BalloonTipText = $Message
$objNotifyIcon.BalloonTipTitle = $Title
$objNotifyIcon.Visible = $True
$objNotifyIcon.ShowBalloonTip(10000)
# Keep it visible long enough for tip to display
Start-Sleep -Seconds 2
$objNotifyIcon.Dispose()
