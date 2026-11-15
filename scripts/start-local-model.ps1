# 启动本地视觉模型（llama.cpp + GGUF），供 dayloom 使用。
#
#   powershell -ExecutionPolicy Bypass -File scripts\start-local-model.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\start-local-model.ps1 -Status
#
# 模型目录默认 %USERPROFILE%\dayloom\models，里面应有：
#   llama-bXXXXX\llama-server.exe
#   Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf
#   mmproj-F16.gguf
param(
    [string]$Models = "",
    [string]$ModelFile = "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf",
    [string]$Mmproj = "mmproj-F16.gguf",
    [int]$Port = 8080,
    [int]$Threads = 8,
    [int]$Context = 16384,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
if (-not $Models) { $Models = Join-Path $env:USERPROFILE "dayloom\models" }

$health = "http://127.0.0.1:$Port/health"

function Test-Server {
    try { return (Invoke-WebRequest $health -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 }
    catch { return $false }
}

if ($Status) {
    if (Test-Server) { Write-Host "本地模型服务：运行中（$health）" -ForegroundColor Green }
    else { Write-Host "本地模型服务：未运行" -ForegroundColor Yellow }
    exit 0
}

if (Test-Server) {
    Write-Host "已经在跑了：$health" -ForegroundColor Green
    exit 0
}

$server = Get-ChildItem -Path $Models -Filter "llama-server.exe" -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $server) { throw "找不到 llama-server.exe，检查 $Models" }

$modelPath = Join-Path $Models $ModelFile
$mmprojPath = Join-Path $Models $Mmproj
if (-not (Test-Path $modelPath)) { throw "找不到模型：$modelPath" }
if (-not (Test-Path $mmprojPath)) { throw "找不到视觉投影文件：$mmprojPath（没有它模型看不见图片）" }

Write-Host "启动 $($server.FullName)" -ForegroundColor Cyan
Write-Host "  模型   : $ModelFile"
Write-Host "  投影   : $Mmproj"
Write-Host "  端点   : http://127.0.0.1:$Port/v1"

Start-Process -FilePath $server.FullName -WindowStyle Hidden `
    -WorkingDirectory $server.DirectoryName `
    -RedirectStandardOutput (Join-Path $Models "server.out.log") `
    -RedirectStandardError (Join-Path $Models "server.err.log") `
    -ArgumentList @(
    "-m", $modelPath,
    "--mmproj", $mmprojPath,
    "--host", "127.0.0.1",
    "--port", "$Port",
    "-c", "$Context",
    "--threads", "$Threads",
    "-ngl", "0")

Write-Host "等待模型加载（3B Q4 在 CPU 上约 10~30 秒）…" -ForegroundColor Cyan
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Server) {
        Write-Host "就绪：http://127.0.0.1:$Port/v1" -ForegroundColor Green
        Write-Host "下一步：dayloom use-local --model qwen2.5vl-3b   （只需一次）"
        exit 0
    }
    if ($i -eq 15) { Write-Host "（还在加载，首次会慢一些）" -ForegroundColor DarkGray }
}
Write-Host "超时：看 $Models\server.err.log" -ForegroundColor Red
exit 1
