<#
.SYNOPSIS
    Let other machines on your network reach `foley browse` (needs an elevated PowerShell).

.DESCRIPTION
    Adds an inbound Windows Firewall rule for the browse port on Domain and Private
    networks only, limited to the local subnet by default. Pair it with [browse]
    host = "0.0.0.0" and allowed_hosts in foley.toml.

    The rule is port-based, not program-based: a venv's pythonw.exe is a launcher, and the
    socket belongs to the base interpreter it starts.

.EXAMPLE
    .\scripts\windows\allow-browse-firewall.ps1 -Port 80
    .\scripts\windows\allow-browse-firewall.ps1 -Port 80 -RemoteAddress 10.0.0.0/8
    .\scripts\windows\allow-browse-firewall.ps1 -Remove
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [int]$Port = 8765,
    # 'LocalSubnet', or addresses/ranges such as '10.0.0.0/8','192.168.1.0/24'.
    [string[]]$RemoteAddress = @('LocalSubnet'),
    [string]$DisplayName = 'Foley Browser',
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'

Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Remove) {
    Write-Host "Removed firewall rule '$DisplayName'."
    return
}

New-NetFirewallRule -DisplayName $DisplayName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -Profile Domain, Private -RemoteAddress $RemoteAddress `
    -Description 'Inbound access to the Foley library browser (foley browse).' | Out-Null
Write-Host "Allowed inbound TCP $Port from $($RemoteAddress -join ', ') on Domain/Private networks."
