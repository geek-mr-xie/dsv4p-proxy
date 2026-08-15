# dsh-proxy management script (PowerShell 5.1+ compatible)
# Usage:
#   powershell -File manage_proxy.ps1 -Action start    # start (idempotent)
#   powershell -File manage_proxy.ps1 -Action stop     # stop
#   powershell -File manage_proxy.ps1 -Action restart  # restart
#   powershell -File manage_proxy.ps1 -Action status   # port + /status summary
#   powershell -File manage_proxy.ps1 -Action logs     # tail proxy.log
# Also usable from schtasks / Task Scheduler with -Action start.

param(
    [ValidateSet("start", "stop", "restart", "status", "logs")]
    [string]$Action = "status"
)

$Port = 8787
# 自动推导代理目录（脚本位置），迁移无需改路径；$PSScriptRoot 在 -File 模式可靠
$ProxyDir = $PSScriptRoot
if (-not $ProxyDir) { $ProxyDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$LogFile = Join-Path $ProxyDir "proxy.log"

function Get-ProxyPid {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        return $conns[0].OwningProcess
    }
    return $null
}

function Ensure-EnvKey {
    # 首次启动：.env 不存在时从模板生成；没有可用 key 时交互询问。
    $EnvFile = Join-Path $ProxyDir ".env"
    $EnvExample = Join-Path $ProxyDir ".env.example"

    if (-not (Test-Path $EnvFile)) {
        if (Test-Path $EnvExample) {
            Copy-Item $EnvExample $EnvFile
        } else {
            New-Item -Path $EnvFile -ItemType File -Force | Out-Null
        }
    }

    $content = Get-Content -Path $EnvFile -Raw -Encoding UTF8
    if (-not $content) { $content = "" }
    # key 行存在且后面有实际内容（如 sk-...）才算已配置；空值/纯引号都算未配置
    $hasValue = $content -match '(?m)^\s*DEEPSEEK_API_KEY\s*=\s*["'']?\s*[A-Za-z0-9]'
    if ($hasValue) { return }

    try {
        $apiKey = Read-Host "请输入 DeepSeek API Key（sk-...）"
    } catch {
        Write-Host "非交互环境无法询问；请手动复制 .env.example 为 .env 并填入 DEEPSEEK_API_KEY=sk-xxx。"
        exit 1
    }
    $apiKey = $apiKey.Trim()
    if (-not $apiKey) {
        Write-Host "API Key 不能为空。"
        exit 1
    }

    $hasKeyLine = $content -match '(?m)^\s*DEEPSEEK_API_KEY\s*='
    if ($hasKeyLine) {
        $content = [regex]::Replace($content, '(?m)^\s*DEEPSEEK_API_KEY\s*=.*$', "DEEPSEEK_API_KEY=$apiKey")
    } else {
        if (-not $content.EndsWith("`n")) { $content += "`r`n" }
        $content += "DEEPSEEK_API_KEY=$apiKey`r`n"
    }
    [System.IO.File]::WriteAllText($EnvFile, $content, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "已写入 DEEPSEEK_API_KEY 到 $EnvFile"
}

function Start-Proxy {
    $proxyPid = Get-ProxyPid
    if ($proxyPid) {
        Write-Host "dsh-proxy already running (PID $proxyPid on port $Port) - nothing to do"
        return
    }
    # 首次启动：config.yaml 不存在时从 example 自动生成
    $ConfigFile = Join-Path $ProxyDir "config.yaml"
    if (-not (Test-Path $ConfigFile)) {
        Copy-Item (Join-Path $ProxyDir "config.yaml.example") $ConfigFile
        Write-Host "created config.yaml from example (edit it if needed)"
    }
    # 首次启动：检查/询问 DeepSeek API Key
    Ensure-EnvKey
    Write-Host "Starting dsh-proxy on 127.0.0.1:$Port ..."
    Start-Process -FilePath "python" `
        -ArgumentList "proxy.py --config config.yaml" `
        -WorkingDirectory $ProxyDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $ProxyDir "proxy_svc.log") `
        -RedirectStandardError (Join-Path $ProxyDir "proxy_svc.err.log")
    Start-Sleep -Seconds 2
    $proxyPid = Get-ProxyPid
    if ($proxyPid) {
        Write-Host "dsh-proxy started (PID $proxyPid)"
    } else {
        Write-Host "FAILED to start - check proxy_svc.err.log"
        exit 1
    }
}

function Stop-Proxy {
    $proxyPid = Get-ProxyPid
    if (-not $proxyPid) {
        Write-Host "dsh-proxy not running"
        return
    }
    Write-Host "Stopping dsh-proxy (PID $proxyPid) ..."
    Stop-Process -Id $proxyPid -Force
    Start-Sleep -Seconds 1
    Write-Host "Stopped"
}

function Show-Status {
    $proxyPid = Get-ProxyPid
    if (-not $proxyPid) {
        Write-Host "dsh-proxy NOT RUNNING"
        return
    }
    Write-Host "dsh-proxy running (PID $proxyPid)"
    try {
        $st = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/status" -TimeoutSec 5
        Write-Host ("stats: total={0} masked={1} anchored={2} translated={3} errors={4}" -f `
            $st.stats.total, $st.stats.masked, $st.stats.anchored, $st.stats.translated_calls, $st.stats.errors)
        Write-Host ("mask_models: {0}" -f ($st.config.mask_models -join ", "))
    } catch {
        Write-Host "port open but /status failed: $_"
    }
}

function Show-Logs {
    if (Test-Path $LogFile) {
        Get-Content $LogFile -Tail 20
    } else {
        Write-Host "no log file at $LogFile"
    }
}

switch ($Action) {
    "start"   { Start-Proxy }
    "stop"    { Stop-Proxy }
    "restart" { Stop-Proxy; Start-Sleep -Seconds 1; Start-Proxy }
    "status"  { Show-Status }
    "logs"    { Show-Logs }
}
