# 注册逆光365开机自启计划任务（当前用户登录时运行）
# 管理员确认后执行：powershell -ExecutionPolicy Bypass -File deploy\register-autostart.ps1
$ErrorActionPreference = 'Stop'

$root = 'D:\Code\VSCode\BKLT365'
$taskName = 'Niguang365-Stack'
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

$webAction = New-ScheduledTaskAction -Execute $powershell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\deploy\start-web.ps1`""
$chromeAction = New-ScheduledTaskAction -Execute $powershell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\deploy\start-chrome.ps1`""

$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action @($webAction, $chromeAction) `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description '逆光365 v2：登录时启动 Web(8501) 与项目 Chrome CDP(9222)' `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
Write-Output ("registered: {0} state={1}" -f $task.TaskName, $task.State)
