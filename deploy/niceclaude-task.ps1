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

    A second account -- sessions started with CLAUDE_CONFIG_DIR set -- needs
    its own daemon, and so its own task:

        powershell -ExecutionPolicy Bypass -File .\niceclaude-task.ps1 `
            -ConfigDir $env:USERPROFILE\.claude-work

    The task is then named niceclaude-watch-<slug>, the slug being the one
    `niceclaude paths slug` prints for that config dir, and its action sets
    CLAUDE_CONFIG_DIR before starting `watch`. Remove or stop it by that name.
    Without -ConfigDir the task is the plain niceclaude-watch for the default
    account, exactly as before, so the two never replace each other. A
    -ConfigDir that names the default account (~\.claude) also gets the plain
    task.

    To stop a slugged task's daemon with `niceclaude stop`, export the same
    CLAUDE_CONFIG_DIR in that shell first: `stop` reads the pidfile of the
    account its own environment names, and from a plain shell that is the
    default account's.
#>

param(
    # The Claude config dir of the account to poll. Omit for the default.
    [string]$ConfigDir
)

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

if ($ConfigDir) {
    # Absolute, because Claude resolves a relative CLAUDE_CONFIG_DIR against
    # whatever directory a session starts in, and never expands `~` -- so the
    # task would otherwise poll a directory no session uses. Resolved through
    # PowerShell, so `~` and the current location mean what they mean here.
    $ConfigDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ConfigDir)
    # cmd.exe expands %NAME% even inside double quotes, and Task Scheduler
    # expands it in Arguments too, so the daemon would key a different
    # directory from the one the slug was computed for -- silently.
    if ($ConfigDir -match '%') {
        throw "-ConfigDir may not contain '%': cmd.exe and Task Scheduler would expand it ($ConfigDir)"
    }

    # The slug comes from niceclaude itself: it is a hash of the normalized
    # path, and computing it twice is how the task and the daemon would come
    # to disagree.
    $saved = $env:CLAUDE_CONFIG_DIR
    try {
        $env:CLAUDE_CONFIG_DIR = $ConfigDir
        $Slug = & $Exe paths slug
        if ($LASTEXITCODE -ne 0) { throw "niceclaude paths slug failed for $ConfigDir" }
    } finally {
        $env:CLAUDE_CONFIG_DIR = $saved
    }
    $Slug = "$Slug".Trim()

    # An empty slug is the default account. It keeps the plain task and the
    # unchanged action: setting CLAUDE_CONFIG_DIR, even to the default dir,
    # makes Claude read ~\.claude\.claude.json instead of ~\.claude.json.
    if ($Slug) {
        $TaskName = "niceclaude-watch-$Slug"
        # A scheduled task's action carries no environment of its own, so
        # cmd.exe sets it for this one process. Without it every account's
        # task would poll the default account, and with -Force each
        # registration would replace the last.
        $Action = New-ScheduledTaskAction -Execute 'cmd.exe' `
            -Argument "/c set `"CLAUDE_CONFIG_DIR=$ConfigDir`" && `"$Exe`" watch --interval $Interval" `
            -WorkingDirectory $env:USERPROFILE
    }
}

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
if ($TaskName -ne 'niceclaude-watch') {
    Write-Host "registered and started '$TaskName'. Verify with: niceclaude status ."
    Write-Host "(run it with CLAUDE_CONFIG_DIR=$ConfigDir, or status reports the default account)"
} else {
    Write-Host "registered and started '$TaskName'. Verify with: niceclaude status ."
}
