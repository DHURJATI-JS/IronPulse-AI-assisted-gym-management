<#
.SYNOPSIS
Allow other computers on your local network to reach a TCP port on this PC (Windows Defender Firewall).

.DESCRIPTION
Use it on the PC that runs the Gemma gateway (default port 8000, e.g. http://192.168.0.101:8000/v1) so the
IronPulse server and other LAN machines can reach it - or for IronPulse itself when HOST=0.0.0.0.
The rule only accepts connections from the local subnet. Needs an Administrator PowerShell.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\windows\open_firewall_port.ps1 -Port 8000
.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\windows\open_firewall_port.ps1 -Port 8000 -Remove
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [string]$Name = "",
    [ValidateSet("Private", "Domain", "Public", "Any")]
    [string]$NetworkProfile = "Private",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

try {
    if (-not $Name) { $Name = "IronPulse LAN access (TCP $Port)" }
    if (-not (Test-Admin)) {
        throw "Run this script in an Administrator PowerShell (right-click PowerShell > Run as administrator)."
    }
    $existing = Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
    if ($Remove) {
        if ($existing) {
            Remove-NetFirewallRule -DisplayName $Name
            Write-Ok "Removed the firewall rule '$Name'"
        } else {
            Write-Info "There is no firewall rule named '$Name'"
        }
        exit 0
    }
    if ($existing) {
        Write-Ok "The firewall rule '$Name' already exists"
        exit 0
    }
    New-NetFirewallRule -DisplayName $Name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile $NetworkProfile | Out-Null
    Write-Ok "Inbound TCP $Port is now allowed from your local network ($NetworkProfile networks)"
    Write-Info "Test it from another PC:  curl http://<this-pc-ip>:$Port/v1/models"
    exit 0
} catch {
    Write-Host "Could not change the firewall: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
