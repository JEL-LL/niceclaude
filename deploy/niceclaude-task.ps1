<#
    Register the niceclaude poller as a Windows Scheduled Task that starts at
    logon and restarts itself if it dies.

        powershell -ExecutionPolicy Bypass -File .\niceclaude-task.ps1

    Remove it again with:

        Unregister-ScheduledTask -TaskName 'niceclaude-watch' -Confirm:$false

    Stop it without unregistering:  niceclaude stop   (then Stop-ScheduledTask
    -TaskName 'niceclaude-watch' if the task itself is still marked running).

    Runs as the interactive user, not SYSTEM: the daemon shells out to
    `claude -p /usage`, which needs that user's PATH, credentials and
    %LOCALAPPDATA%\niceclaude data directory.
#>

$TaskName = 'niceclaude-watch'
$Interval = 60

# uv tool install puts console scripts in %USERPROFILE%\.local\bin.
$Exe = Join-Path $env:USERPROFILE '.local\bin\niceclaude.exe'
if (-not (Test-Path $Exe)) {
    $found = Get-Command niceclaude -ErrorAction SilentlyContinue
    if (-not $found) { throw "niceclaude.exe not found; run: uv tool install niceclaude" }
    $Exe = $found.Source
}

$Action = New-ScheduledTaskAction -Execute $Exe `
    -Argument "watch --interval $Interval" `
    -WorkingDirectory $env:USERPROFILE

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

# RestartCount/RestartInterval are the restart-on-failure setting. ExecutionTimeLimit
# of zero means "no limit" -- the default 3 days would otherwise kill the poller.
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew   # `watch` refuses a second instance anyway

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings `
    -Description 'niceclaude usage poller (niceclaude watch)' -Force

Start-ScheduledTask -TaskName $TaskName
Write-Host "registered and started '$TaskName'. Verify with: niceclaude status ."
