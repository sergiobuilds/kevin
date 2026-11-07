param([Parameter(Mandatory=$true)][string]$Device,
      [Parameter(Mandatory=$true)][string]$AudioPath)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Devices.Enumeration.DeviceInformation,Windows.Devices.Enumeration,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Devices.MediaDevice,Windows.Media.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Playback.MediaPlayer,Windows.Media.Playback,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Core.MediaSource,Windows.Media.Core,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
    $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1
} | Select-Object -First 1
function Await($operation, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    if (-not $task.Wait(12000)) { throw 'Audio operation timeout' }
    $task.Result
}
function Player-State {
    if ($null -eq $script:player) {
        return @{ status='stopped'; audible_verified=$false }
    }
    return @{
        status=$script:player.PlaybackSession.PlaybackState.ToString().ToLowerInvariant()
        position_seconds=$script:player.PlaybackSession.Position.TotalSeconds
        duration_seconds=$script:player.PlaybackSession.NaturalDuration.TotalSeconds
        volume=[Math]::Round($script:player.Volume * 100)
        audible_verified=$false
    }
}
$player = $null
try {
    while ($null -ne ($line = [Console]::ReadLine())) {
        $request = $null
        try {
            $request = $line | ConvertFrom-Json
            switch ($request.command) {
                'play' {
                    if ($Device -cne 'CRF-35B') { throw 'Unsupported audio target' }
                    $volume = [double]$request.volume
                    if ([double]::IsNaN($volume) -or $volume -lt 0 -or $volume -gt 100) {
                        throw 'Volume must be 0..100'
                    }
                    if ($null -eq $player) {
                        $devices = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync(
                            [Windows.Media.Devices.MediaDevice]::GetAudioRenderSelector())) (
                            [Windows.Devices.Enumeration.DeviceInformationCollection])
                        $radio = @($devices | Where-Object { $_.Name -match 'CRF[- _]?35B' })
                        if ($radio.Count -ne 1) { throw 'Configured radio output not uniquely found' }
                        if ([IO.Path]::GetFileName($AudioPath) -cne 'jingle_bells.wav') {
                            throw 'Only the generated carol is supported'
                        }
                        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync(
                            [IO.Path]::GetFullPath($AudioPath))) ([Windows.Storage.StorageFile])
                        $player = New-Object Windows.Media.Playback.MediaPlayer
                        $player.AudioDevice = $radio[0]
                        $player.IsLoopingEnabled = $true
                        $player.Source = [Windows.Media.Core.MediaSource]::CreateFromStorageFile($file)
                    }
                    $player.Volume = $volume / 100
                    $player.Play()
                    $result = Player-State
                }
                'volume' {
                    $volume = [double]$request.volume
                    if ([double]::IsNaN($volume) -or [double]::IsInfinity($volume) -or $volume -lt 0 -or $volume -gt 100) {
                        throw 'Volume must be 0..100'
                    }
                    if ($null -ne $player) { $player.Volume = $volume / 100 }
                    $result = Player-State
                    $result.volume_applied = ($null -ne $player)
                }
                'stop' {
                    if ($null -ne $player) {
                        $player.Pause()
                        $player.PlaybackSession.Position = [TimeSpan]::Zero
                    }
                    $result = Player-State
                    $result.status = 'stopped'
                }
                'status' { $result = Player-State }
                'quit' {
                    if ($null -ne $player) { $player.Pause() }
                    $result = @{status='stopped'; audible_verified=$false}
                }
                default { throw 'Unsupported player command' }
            }
            [Console]::WriteLine((@{id=$request.id; ok=$true; result=$result} | ConvertTo-Json -Compress))
            if ($request.command -eq 'quit') { break }
        } catch {
            # Device identifiers and paths stay out of replies and browser logs.
            [Console]::WriteLine((@{id=$request.id; ok=$false; error='Radio command failed'} | ConvertTo-Json -Compress))
        }
    }
} finally {
    if ($null -ne $player) { $player.Pause(); $player.Dispose() }
}
