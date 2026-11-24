# Prints what Windows' media controls report (one JSON line every few seconds) for the Home Screen helper.
# Read-only: it never controls playback. Uses only built-in Windows APIs (no downloads).
param([int]$Every = 5, [int]$Start = 0)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Wait-Op($op, [Type]$type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $t.Wait(3000)
    $t.Result
}
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]
$propsType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]
$mgr = Wait-Op ($mgrType::RequestAsync()) $mgrType
while ($true) {
    $out = @()
    try {
        foreach ($s in $mgr.GetSessions()) {
            try {
                $p = Wait-Op ($s.TryGetMediaPropertiesAsync()) $propsType
                $tl = $s.GetTimelineProperties()
                $out += [ordered]@{
                    app      = $s.SourceAppUserModelId
                    title    = $p.Title
                    artist   = $p.Artist
                    album    = $p.AlbumTitle
                    playing  = ($s.GetPlaybackInfo().PlaybackStatus.ToString() -eq 'Playing')
                    position = [math]::Round($tl.Position.TotalSeconds)
                    duration = [math]::Round($tl.EndTime.TotalSeconds)
                }
            } catch { }
        }
    } catch { }
    [Console]::Out.WriteLine((ConvertTo-Json @{ t = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); sessions = @($out) } -Compress -Depth 4))
    [Console]::Out.Flush()
    if ($Every -le 0) { break }
    if ($Start -gt $Every) { Start-Sleep -Seconds $Start; $Start -= 2 } else { Start-Sleep -Seconds $Every }
}
