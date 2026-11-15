# Install dayloom for the current user (no admin rights needed).
#
#   pwsh -File packaging\install.ps1
#   pwsh -File packaging\install.ps1 -Desktop
#
# What it does:
#   * copies dist\dayloom to %LOCALAPPDATA%\Programs\dayloom
#   * creates a Start Menu shortcut (and optionally a desktop one)
#   * registers an entry in "Apps & features" (HKCU Uninstall key)
#   * does NOT touch %USERPROFILE%\dayloom (your data)
#
param(
    [string]$Source = "",
    [switch]$Desktop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Source) { $Source = Join-Path $root "dist\dayloom" }
$Source = (Resolve-Path $Source).Path

$gui = Join-Path $Source "dayloom-gui.exe"
$cli = Join-Path $Source "dayloom.exe"
if (-not (Test-Path $gui)) { throw "先构建：找不到 $gui（运行 packaging\build.ps1）" }

$target = Join-Path $env:LOCALAPPDATA "Programs\dayloom"
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$desktopDir = Join-Path $env:USERPROFILE "Desktop"

Write-Host "installing to $target" -ForegroundColor Cyan
Get-Process -Name dayloom, dayloom-gui -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 400
if (Test-Path $target) { Remove-Item -Recurse -Force $target }
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Path (Join-Path $Source "*") -Destination $target -Recurse -Force
Copy-Item -Path (Join-Path $PSScriptRoot "uninstall.ps1") -Destination $target -Force

function New-DayloomShortcut([string]$link, [string]$exe, [string]$icon) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = $target
    $shortcut.Description = "dayloom — 屏幕活动日记"
    $shortcut.IconLocation = "$icon,0"
    $shortcut.WindowStyle = 1
    $shortcut.Save()
}

$installedGui = Join-Path $target "dayloom-gui.exe"
$icon = Join-Path $target "dayloom.ico"
if (-not (Test-Path $icon)) { $icon = $installedGui }

New-DayloomShortcut (Join-Path $startMenu "dayloom.lnk") $installedGui $icon
Write-Host "  start menu shortcut created"
if ($Desktop) {
    New-DayloomShortcut (Join-Path $desktopDir "dayloom.lnk") $installedGui $icon
    Write-Host "  desktop shortcut created"
}

# Apps & features entry (per user, no admin)
$version = "0.1.0"
try { $version = (& $cli --version 2>$null | Select-Object -First 1) -replace '^dayloom\s+', '' } catch { }
$sizeMb = [math]::Round(((Get-ChildItem $target -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))
$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\dayloom"
New-Item -Path $uninstallKey -Force | Out-Null
Set-ItemProperty -Path $uninstallKey -Name "DisplayName" -Value "dayloom"
Set-ItemProperty -Path $uninstallKey -Name "DisplayVersion" -Value $version
Set-ItemProperty -Path $uninstallKey -Name "Publisher" -Value "dayloom contributors"
Set-ItemProperty -Path $uninstallKey -Name "DisplayIcon" -Value "$icon,0"
Set-ItemProperty -Path $uninstallKey -Name "InstallLocation" -Value $target
Set-ItemProperty -Path $uninstallKey -Name "EstimatedSize" -Value ([int]($sizeMb * 1024)) -Type DWord
Set-ItemProperty -Path $uninstallKey -Name "NoModify" -Value 1 -Type DWord
Set-ItemProperty -Path $uninstallKey -Name "NoRepair" -Value 1 -Type DWord
Set-ItemProperty -Path $uninstallKey -Name "UninstallString" `
    -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$target\uninstall.ps1`""

Write-Host ""
Write-Host "installed dayloom $version" -ForegroundColor Green
Write-Host "  app      : $installedGui"
Write-Host "  cli      : $(Join-Path $target 'dayloom.exe')"
Write-Host "  data     : $env:USERPROFILE\dayloom (untouched)"
Write-Host "  uninstall: $target\uninstall.ps1"
Write-Host ""
Write-Host "从开始菜单打开 dayloom；第一次运行会问你三个问题。" -ForegroundColor Green
