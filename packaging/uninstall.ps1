# 卸载 dayloom（仅当前用户）。数据默认保留在 %USERPROFILE%\dayloom。
#
#   powershell -File uninstall.ps1
#   powershell -File uninstall.ps1 -RemoveData     # 连截图和数据库一起删
#   powershell -File uninstall.ps1 -Quiet          # 不弹最后的提示框（自动化用）
#
param(
    [switch]$RemoveData,
    [switch]$Quiet
)

$ErrorActionPreference = "SilentlyContinue"
$target = $PSScriptRoot
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$desktopDir = Join-Path $env:USERPROFILE "Desktop"
$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$dataDir = Join-Path $env:USERPROFILE "dayloom"

Write-Host "uninstalling dayloom from $target" -ForegroundColor Cyan

Get-Process -Name dayloom, dayloom-gui -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

foreach ($link in @(
        (Join-Path $startMenu "dayloom.lnk"),
        (Join-Path $desktopDir "dayloom.lnk"),
        (Join-Path $startup "dayloom.lnk"),
        (Join-Path $startup "dayloom.cmd"))) {
    if (Test-Path $link) {
        Remove-Item $link -Force
        Write-Host "  removed $link"
    }
}

schtasks /Delete /TN dayloom /F 2>$null | Out-Null

Remove-Item -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\dayloom" -Recurse -Force
Write-Host "  removed Apps & features entry"

# 先记下数据状态，再删程序目录（下面会把这个脚本自己留在原地直到父进程退出）
$dataExists = Test-Path $dataDir
$dataSizeMb = 0
if ($dataExists) {
    $dataSizeMb = [math]::Round(((Get-ChildItem $dataDir -Recurse -File -ErrorAction SilentlyContinue |
                Measure-Object Length -Sum).Sum / 1MB), 1)
}

if ($RemoveData -and $dataExists) {
    Remove-Item -Recurse -Force $dataDir
    Write-Host "  removed data dir $dataDir"
    $dataExists = $false
}
elseif ($dataExists) {
    Write-Host ""
    Write-Host "你的数据还在：$dataDir" -ForegroundColor Yellow
    Write-Host "  （截图、数据库、日报；不想留就手动删，或重跑时加 -RemoveData）"
}

# Remove everything except this script, then let a detached shell delete the folder.
Get-ChildItem -Path $target -Force | Where-Object { $_.Name -ne "uninstall.ps1" } | Remove-Item -Recurse -Force
Start-Process -WindowStyle Hidden -FilePath "cmd.exe" `
    -ArgumentList "/c timeout /t 2 /nobreak >nul & rmdir /s /q `"$target`""

Write-Host ""
Write-Host "dayloom 已卸载。" -ForegroundColor Green

# 从「设置 → 应用」卸载时没有可见的控制台，所以把结论弹出来。
if (-not $Quiet) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        if ($dataExists) {
            $text = "程序已卸载。`n`n你的数据仍然保留在：`n$dataDir`n（约 $dataSizeMb MB）`n`n" +
            "截图、数据库、日报都在这里；想彻底删除就手动删掉这个文件夹。"
        }
        else {
            $text = "程序已卸载，数据目录也已删除（-RemoveData）。"
        }
        [System.Windows.Forms.MessageBox]::Show(
            $text, "dayloom 已卸载",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    }
    catch {
        # 没有交互式桌面（CI、远程会话）时忽略弹窗
    }
}
