<#
.SYNOPSIS
    Start `foley browse` in the background every time you log on to Windows.

.DESCRIPTION
    Registers a Task Scheduler task for the current user that runs `foley tray` with
    the repo's own interpreter (pythonw.exe, so no console window) at logon. The tray
    icon runs the browse server and has a right-click menu to open, restart or stop it
    and to start a scan. With -Headless the task runs `foley browse` directly, with no
    icon. The task runs under your normal logon, so it reaches a NAS share with your
    own credentials: no stored password and no admin rights needed.

    It also adds "Foley" to the Start Menu, to start Foley again after Quit; choosing it
    while Foley runs opens Foley in the browser. Only one tray runs at a time.

    The tray restarts itself on the base Python interpreter, so that it and the server
    are one process each, and the task's own run then ends: Task Scheduler shows the
    task as "Ready", not "Running", while Foley runs. Look for the tray icon, or for
    `-m foley.cli tray` in Task Manager's command lines, instead. Installing or
    uninstalling stops a running tray for this log file (its server stops with it).

    Host, port and allowed host names come from the [browse] table of foley.toml, and
    the daily library scan from [schedule] (or the Scans dialog in the UI).

    To serve other machines on your network, also run allow-browse-firewall.ps1 from an
    elevated PowerShell.

.EXAMPLE
    .\scripts\windows\install-browse-task.ps1              # install (or update) and start now
    .\scripts\windows\install-browse-task.ps1 -Headless    # no tray icon (and no shortcut)
    .\scripts\windows\install-browse-task.ps1 -Uninstall   # stop and remove both
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'Foley Browser',
    # Server log (size-rotated). Defaults next to the catalog in the default data dir.
    [string]$LogFile = (Join-Path $env:LOCALAPPDATA 'Foley\logs\browse.log'),
    # Explicit foley.toml; by default the server uses its normal lookup
    # (FOLEY_CONFIG, then <data dir>\foley.toml).
    [string]$Config,
    [switch]$Headless,
    [switch]$NoStart,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'

$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Foley.lnk'

# The tray runs outside the task once it has restarted itself (see above), so stopping
# the task does not stop it. Stopping the tray closes the server's standard input, and
# the server then stops cleanly.
function Stop-FoleyTray {
    $trays = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains('foley.cli') -and
                       $_.CommandLine.Contains(' tray ') -and $_.CommandLine.Contains($LogFile) })
    foreach ($p in $trays) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped foley tray (pid $($p.ProcessId))."
    }
    if ($trays) { Start-Sleep -Seconds 3 }  # let the server release its port
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName
        Stop-FoleyTray
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed task '$TaskName'."
    } else {
        Write-Host "No task named '$TaskName'."
    }
    if (Test-Path $shortcut) {
        Remove-Item $shortcut
        Write-Host "Removed the Start Menu shortcut."
    }
    return
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonw = Join-Path $repo '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    throw "$pythonw not found. Run 'uv sync' in $repo first."
}
New-Item -ItemType Directory -Force (Split-Path $LogFile) | Out-Null

$arguments = @('-m', 'foley.cli')
if ($Config) { $arguments += @('--config', "`"$((Resolve-Path $Config).Path)`"") }
if ($Headless) {
    $arguments += @('browse', '--no-open', '--log-file', "`"$LogFile`"")
} else {
    $arguments += @('tray', '--log-file', "`"$LogFile`"")
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ($arguments -join ' ') `
    -WorkingDirectory $env:LOCALAPPDATA  # not the repo: a stray ./foley.toml there must not win
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Foley library browser ($(if ($Headless) { 'foley browse' } else { 'foley tray' })) from $repo. Log: $LogFile" | Out-Null
Write-Host "Registered task '$TaskName' (at logon of $user)."

if (-not $Headless) {
    # Same command as the task, plus --open: start Foley, or show the running one.
    $icon = Join-Path (Split-Path $LogFile) 'foley.ico'
    $iconArgs = @('-m', 'foley.cli')
    if ($Config) { $iconArgs += @('--config', (Resolve-Path $Config).Path) }
    & (Join-Path $repo '.venv\Scripts\python.exe') @iconArgs tray --save-icon $icon | Out-Null
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = (($arguments + '--open') -join ' ')
    $lnk.WorkingDirectory = $env:LOCALAPPDATA
    $lnk.Description = 'Foley sound library browser'
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Save()
    Write-Host "Added 'Foley' to the Start Menu."
} elseif (Test-Path $shortcut) {
    Remove-Item $shortcut  # a headless server has no tray to start
}

if (-not $NoStart) {
    Stop-ScheduledTask -TaskName $TaskName  # a headless server still runs inside the task
    Stop-FoleyTray  # so the new run starts the current code
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started. Log: $LogFile"
}
