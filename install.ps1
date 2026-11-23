<#
  Installs (or updates) the Home Screen wallpaper and its helper. Safe to re-run
  any time, e.g. after editing the wallpaper files.

  1. Creates a secret token so only the wallpaper can talk to the helper.
  2. Copies the wallpaper into Wallpaper Engine's "myprojects" folder.
  3. Starts the helper now and at every login (Startup folder shortcut).
#>
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$appDir = Join-Path $env:LOCALAPPDATA 'HomeScreen'
New-Item -ItemType Directory -Force $appDir | Out-Null

# 1. token + user config
$tokenFile = Join-Path $appDir 'token.txt'
if (-not (Test-Path $tokenFile)) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $hex = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
    [IO.File]::WriteAllText($tokenFile, $hex)
}
$token = ([IO.File]::ReadAllText($tokenFile)).Trim()
$config = Join-Path $appDir 'config.json'
if (-not (Test-Path $config)) { Copy-Item (Join-Path $root 'helper\config.default.json') $config }
[IO.File]::WriteAllText((Join-Path $root 'wallpaper\token.js'), "window.HOMESCREEN = { token: '$token', port: 48650 };`n")

# 2. Wallpaper Engine project
$weRoots = @(
    'C:\Program Files (x86)\Steam\steamapps\common\wallpaper_engine',
    'E:\SteamLibrary\steamapps\common\wallpaper_engine'
)
$we = $weRoots | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($we) {
    $dest = Join-Path $we 'projects\myprojects\home-screen'
    New-Item -ItemType Directory -Force $dest | Out-Null
    Copy-Item (Join-Path $root 'wallpaper\*') $dest -Recurse -Force
    Write-Host "Wallpaper copied to $dest"
} else {
    Write-Host 'Wallpaper Engine not found yet. Install it, then run this script again.'
}

# 3. helper: autostart + (re)start
$pythonw = (Get-Command pythonw -ErrorAction Stop).Source
$helper = Join-Path $root 'helper\homescreen_helper.py'
$lnkPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'Home Screen Helper.lnk'
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$helper`""
$lnk.WorkingDirectory = Split-Path $helper
$lnk.Description = 'Home Screen wallpaper helper'
$lnk.Save()

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object CommandLine -like '*homescreen_helper.py*' |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process $pythonw -ArgumentList "`"$helper`"" -WorkingDirectory (Split-Path $helper)
Write-Host 'Helper started (and set to start at login).'

# 4. Wallpaper Engine keeps running the page it loaded earlier, so tell it to reload the new files
$weExe = if ($we) { Join-Path $we 'wallpaper64.exe' }
if ($weExe -and (Test-Path $weExe) -and (Get-Process wallpaper64 -ErrorAction SilentlyContinue)) {
    # stop + play unloads and reloads the page ("openWallpaper" on the current wallpaper is a no-op)
    & $weExe -control stop
    Start-Sleep -Seconds 2
    & $weExe -control play
    Write-Host 'Wallpaper reloaded in Wallpaper Engine.'
}
