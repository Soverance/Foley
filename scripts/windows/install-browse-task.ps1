<#
.SYNOPSIS
    Start `foley browse` in the background every time you log on to Windows.

.DESCRIPTION
    Registers a Task Scheduler task for the current user that runs the browse server
    with the repo's own interpreter (pythonw.exe, so no console window) at logon. The
    task runs under your normal logon, so it reaches a NAS share with your own
    credentials: no stored password and no admin rights needed.

    Host, port and allowed host names come from the [browse] table of foley.toml, and
    the daily library scan from [schedule] (or the Scans dialog in the UI).

    To serve other machines on your network, also run allow-browse-firewall.ps1 from an
    elevated PowerShell.

.EXAMPLE
    .\scripts\windows\install-browse-task.ps1              # install (or update) and start now
    .\scripts\windows\install-browse-task.ps1 -Uninstall   # stop and remove
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'Foley Browser',
    # Server log (size-rotated). Defaults next to the catalog in the default data dir.
    [string]$LogFile = (Join-Path $env:LOCALAPPDATA 'Foley\logs\browse.log'),
    # Explicit foley.toml; by default the server uses its normal lookup
    # (FOLEY_CONFIG, then <data dir>\foley.toml).
    [string]$Config,
    [switch]$NoStart,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed task '$TaskName'."
    } else {
        Write-Host "No task named '$TaskName'."
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
$arguments += @('browse', '--no-open', '--log-file', "`"$LogFile`"")

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
    -Description "Foley library browser (foley browse) from $repo. Log: $LogFile" | Out-Null
Write-Host "Registered task '$TaskName' (at logon of $user)."

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started. Log: $LogFile"
}
