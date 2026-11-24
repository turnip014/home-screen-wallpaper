# Adds a half-refresh mode (e.g. 120 Hz next to 240 Hz) at native resolution through a Windows EDID
# override, so the Home Screen can rest the display at that rate instead of 60 Hz. CRU does the same.
#   apply:  powershell -ExecutionPolicy Bypass -File edid_120hz.ps1 [-Monitor ABC1234]
#   undo:   powershell -ExecutionPolicy Bypass -File edid_120hz.ps1 -Remove
# -Monitor is the monitor's hardware ID (first connected monitor if left out). The GPU driver is
# restarted afterwards (screen goes black for a few seconds) so the new mode shows up.
# If the screen ever stays black: reboot into Safe Mode and run with -Remove (or delete the
# EDID_OVERRIDE key under the monitor's "Device Parameters" in regedit).
param([switch]$Remove, [string]$Monitor = '')
$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators')) {
    $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($Monitor) { $argv += @('-Monitor', $Monitor) }
    if ($Remove) { $argv += '-Remove' }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argv -Wait
    exit
}
Start-Transcript -Path (Join-Path $PSScriptRoot 'edid_120hz.log') -Force | Out-Null
try {
    $inst = (Get-CimInstance -Namespace root\wmi WmiMonitorID | Where-Object { -not $Monitor -or $_.InstanceName -like "DISPLAY\$Monitor\*" } |
        Select-Object -First 1).InstanceName -replace '_0$', ''
    if (-not $inst) { throw "Monitor not found" }
    $id = $inst.Split('\')[1]
    $params = "HKLM:\SYSTEM\CurrentControlSet\Enum\$inst\Device Parameters"
    $key = "$params\EDID_OVERRIDE"
    if ($Remove) {
        if (Test-Path $key) { Remove-Item $key -Recurse -Force; "Removed the override for $id" }
    } else {
        $backup = Join-Path $PSScriptRoot "edid_backup_$id.bin"
        $out = Join-Path $PSScriptRoot "edid_120hz_$id.bin"
        if (-not (Test-Path $backup)) { [IO.File]::WriteAllBytes($backup, (Get-ItemProperty $params).EDID) }
        & python (Join-Path $PSScriptRoot 'make_edid_halfhz.py') $backup $out
        if ($LASTEXITCODE -ne 0) { throw "No override made (see above)" }
        $edid = [IO.File]::ReadAllBytes($out)
        New-Item -Path $key -Force | Out-Null
        for ($i = 0; $i -lt $edid.Length / 128; $i++) {
            New-ItemProperty -Path $key -Name "$i" -PropertyType Binary -Value $edid[($i * 128)..($i * 128 + 127)] -Force | Out-Null
        }
        "Wrote the override for $id"
    }
    $gpu = Get-PnpDevice -Class Display -PresentOnly | Select-Object -First 1
    pnputil /restart-device "$($gpu.InstanceId)"
} finally { Stop-Transcript | Out-Null }
