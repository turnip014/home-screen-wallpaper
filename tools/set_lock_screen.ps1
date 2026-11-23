# Sets an image as the Windows lock screen (and sign-in screen) background, and turns off
# Windows Spotlight's rotating pictures and the "fun facts, tips and tricks" overlay.
# Usage: powershell -File set_lock_screen.ps1 <image path>
param([Parameter(Mandatory)][string]$Path)
$ErrorActionPreference = 'Stop'
$Path = (Resolve-Path $Path).Path

# Spotlight off, tips overlay off (Settings > Personalization > Lock screen)
$cdm = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager'
if (-not (Test-Path $cdm)) { New-Item $cdm -Force | Out-Null }
Set-ItemProperty $cdm RotatingLockScreenEnabled 0 -Type DWord
Set-ItemProperty $cdm RotatingLockScreenOverlayEnabled 0 -Type DWord
Set-ItemProperty $cdm 'SubscribedContent-338387Enabled' 0 -Type DWord

# Windows.System.UserProfile.LockScreen (WinRT) works on Windows Home, unlike the policy settings
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.System.UserProfile.LockScreen, Windows.System.UserProfile, ContentType = WindowsRuntime]
$ext = [System.WindowsRuntimeSystemExtensions].GetMethods()
$asTaskOp = $ext | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$asTaskAction = $ext | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1

$task = $asTaskOp.MakeGenericMethod([Windows.Storage.StorageFile]).Invoke($null, @([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)))
$task.Wait()
$file = $task.Result
$set = $asTaskAction.Invoke($null, @([Windows.System.UserProfile.LockScreen]::SetImageFileAsync($file)))
$set.Wait()
Write-Output "Lock screen set to $Path"
