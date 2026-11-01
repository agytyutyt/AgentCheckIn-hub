<#
.SYNOPSIS
    把 CheckinHub 面板注册成「登录时自动启动」的计划任务（无窗口常驻）。

.DESCRIPTION
    面板的调度器跑在进程里：进程不在，当天的 08:30 签到就直接丢掉（misfire 宽限只有 6 小时）。
    双击 start.bat 适合手动看页面，长期自动跑请用这个脚本注册常驻任务。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\install-autostart.ps1
    powershell -ExecutionPolicy Bypass -File tools\install-autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [string]$TaskName = "CheckinHubPanel"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已删除计划任务 $TaskName"
    }
    else {
        Write-Host "没有找到计划任务 $TaskName，无需删除"
    }
    return
}

if (-not (Test-Path $Python)) {
    throw "没有找到 $Python。请先双击 start.bat 建好虚拟环境。"
}

$action = New-ScheduledTaskAction -Execute $Python -Argument "-m app.serve" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "CheckinHub 面板：WorkBuddy / Qoder 自动签到与积分领取" `
    -Force | Out-Null

Write-Host "已注册计划任务 $TaskName：登录后自动启动面板（无窗口）。"
Write-Host "  解释器  ：$Python"
Write-Host "  工作目录：$Root"
Write-Host "  访问    ：http://127.0.0.1:8000"
Write-Host "  取消    ：powershell -ExecutionPolicy Bypass -File tools\install-autostart.ps1 -Remove"
Write-Host "  启动日志：data\logs\panel.log；任务执行记录：面板「日志」页"
