# 把 dayloom 切到本地视觉模型（Ollama）。
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup-local-model.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup-local-model.ps1 -CheckOnly
#
# 默认从 gh-proxy 镜像下载 Ollama（国内直连 github 只有 0.02 MB/s，镜像约 0.5 MB/s）。
# 如果你能直连官方，加 -Installer "https://ollama.com/download/OllamaSetup.exe"。
#
# 这一步只在网络好的时候跑一次；跑完 dayloom 就用本地模型看图，不再花 API 费用。
param(
    [string]$Model = "qwen2.5vl:3b",
    [string]$Installer = "https://gh-proxy.com/https://github.com/ollama/ollama/releases/latest/download/OllamaSetup.exe",
    [string]$Dayloom = "",
    [switch]$SkipInstall,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Find-Dayloom {
    if ($Dayloom) { return $Dayloom }
    foreach ($candidate in @(
            (Join-Path $root "dist\dayloom\dayloom.exe"),
            (Join-Path $root ".venv\Scripts\dayloom.exe"))) {
        if (Test-Path $candidate) { return $candidate }
    }
    $cmd = Get-Command dayloom -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "找不到 dayloom.exe，用 -Dayloom 指定路径"
}

$cli = Find-Dayloom
$ollama = Get-Command ollama -ErrorAction SilentlyContinue

Write-Host "dayloom : $cli"
Write-Host "模型    : $Model"
Write-Host ("ollama  : " + $(if ($ollama) { $ollama.Source } else { "未安装（将下载 $Installer）" }))
Write-Host ""

if ($CheckOnly) {
    Write-Host "检查模式：什么都没做。" -ForegroundColor Yellow
    Write-Host "去掉 -CheckOnly 就会执行："
    Write-Host "  1. 下载并安装 Ollama（约 1.5 GB，镜像下约 50 分钟）"
    Write-Host "  2. ollama pull $Model（约 3 GB，取决于 registry 速度）"
    Write-Host "  3. $cli use-local --model $Model"
    Write-Host "  4. $cli probe-llm"
    exit 0
}

# 1. 安装 Ollama
if (-not $ollama -and -not $SkipInstall) {
    $setup = Join-Path $env:TEMP "OllamaSetup.exe"
    Write-Host "下载 Ollama（支持断点续传，中断了重跑这条命令就行）…" -ForegroundColor Cyan
    & curl.exe -L -C - --progress-bar -o $setup $Installer
    if ($LASTEXITCODE -ne 0) { throw "下载失败，退出码 $LASTEXITCODE" }
    Write-Host "安装…" -ForegroundColor Cyan
    Start-Process -FilePath $setup -ArgumentList "/SILENT" -Wait
    Start-Sleep -Seconds 3
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) { throw "安装完成但找不到 ollama 命令，请重开一个终端再试" }
}

# 2. 确保服务在跑
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-WebRequest "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 3 | Out-Null
        $ready = $true
        break
    }
    catch {
        if ($i -eq 0) { Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden }
        Start-Sleep -Seconds 3
    }
}
if (-not $ready) { throw "Ollama 服务没起来（127.0.0.1:11434 无响应）" }
Write-Host "Ollama 已就绪" -ForegroundColor Green

# 3. 拉模型
Write-Host "拉取 $Model …" -ForegroundColor Cyan
& ollama pull $Model
if ($LASTEXITCODE -ne 0) { throw "拉取模型失败" }

# 4. 切配置
Write-Host "切换 dayloom 配置…" -ForegroundColor Cyan
& $cli use-local --model $Model
if ($LASTEXITCODE -ne 0) { throw "切换配置失败" }

# 5. 验证视觉能力
Write-Host "验证模型能否读图…" -ForegroundColor Cyan
& $cli probe-llm
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "模型装好了，但视觉校验没过 —— 可能是这个 tag 不带视觉能力。" -ForegroundColor Yellow
    Write-Host "换一个再试：ollama pull qwen2.5vl:7b 然后 $cli use-local --model qwen2.5vl:7b"
    exit 1
}

Write-Host ""
Write-Host "完成。现在 dayloom 用本地模型分析，不再产生 API 费用。" -ForegroundColor Green
Write-Host "待处理批次：$cli analyze"
