# 逆光365 项目 Chrome 启动脚本（Windows PowerShell 5.1 兼容）
# 由计划任务 Niguang365-Stack 在用户登录时调用；使用独立 profile，不占用日常 Chrome。
$ErrorActionPreference = 'Stop'

$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile = Join-Path $env:LOCALAPPDATA 'Google\Chrome\XiaohongshuProfiles\default'
$logDir = 'D:\逆光365图片下载\.niguang365\logs'
$cdpUrl = 'http://127.0.0.1:9222/json/version'

if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

function Test-Cdp {
    try {
        $response = Invoke-WebRequest -Uri $cdpUrl -UseBasicParsing -TimeoutSec 3
        return [bool]($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (Test-Cdp) {
    exit 0
}

if (-not (Test-Path -LiteralPath $profile)) {
    New-Item -ItemType Directory -Force -Path $profile | Out-Null
}

Start-Process -FilePath $chrome -ArgumentList @(
    "--user-data-dir=$profile",
    '--remote-debugging-port=9222',
    '--remote-debugging-address=127.0.0.1',
    '--no-first-run',
    '--no-default-browser-check'
) -WindowStyle Normal | Out-Null

for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Cdp) {
        Add-Content -LiteralPath (Join-Path $logDir 'autostart.log') -Value "$(Get-Date -Format s) chrome cdp started"
        exit 0
    }
}

Add-Content -LiteralPath (Join-Path $logDir 'autostart.log') -Value "$(Get-Date -Format s) chrome cdp not reachable"
exit 1
