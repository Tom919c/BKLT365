# 逆光365 v2 Web 启动脚本（Windows PowerShell 5.1 兼容）
# 由计划任务 Niguang365-Stack 在用户登录时调用。
$ErrorActionPreference = 'Stop'

$root = 'D:\Code\VSCode\BKLT365'
$python = 'C:\Users\chenquan\.workbuddy-ai\binaries\python\envs\xhs\Scripts\python.exe'
$config = Join-Path $root 'niguang365\config.json'
$logDir = 'D:\逆光365图片下载\.niguang365\logs'
$healthUrl = 'http://127.0.0.1:8501/api/health'
$cliEnvFile = 'D:\逆光365图片下载\.niguang365\tools\cli-env.json'

if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

# lark-cli 需要豆包 Office 运行环境中的 token/proxy 变量；登录自启进程不会继承这些变量。
if (Test-Path -LiteralPath $cliEnvFile) {
    try {
        $cliEnv = Get-Content -LiteralPath $cliEnvFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($property in $cliEnv.PSObject.Properties) {
            if ($property.Value) {
                Set-Item -Path "Env:$($property.Name)" -Value ([string]$property.Value)
            }
        }
    } catch {
        Add-Content -LiteralPath (Join-Path $logDir 'autostart.log') -Value "$(Get-Date -Format s) cli env load failed: $($_.Exception.Message)"
    }
}

function Test-WebHealth {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
        return [bool]($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (Test-WebHealth) {
    exit 0
}

$listener = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Add-Content -LiteralPath (Join-Path $logDir 'autostart.log') -Value "$(Get-Date -Format s) port 8501 occupied but unhealthy"
    exit 1
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outLog = Join-Path $logDir "web-$stamp.out.log"
$errLog = Join-Path $logDir "web-$stamp.err.log"

$process = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'niguang365.webapp', '--host', '127.0.0.1', '--port', '8501', '--config', $config) `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru

for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    if (Test-WebHealth) {
        Add-Content -LiteralPath (Join-Path $logDir 'autostart.log') -Value "$(Get-Date -Format s) web started pid=$($process.Id)"
        exit 0
    }
}

Add-Content -LiteralPath (Join-Path $logDir 'autostart.log') -Value "$(Get-Date -Format s) web failed pid=$($process.Id) see $errLog"
exit 1
