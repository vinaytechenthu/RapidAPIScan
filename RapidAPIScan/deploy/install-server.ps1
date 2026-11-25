<#
.SYNOPSIS
  Installs (or upgrades) the RapidPulse server on Windows in one step:
  Python -> files -> dependencies -> configuration -> firewall -> auto-start task -> health check.

.EXAMPLE
  .\deploy\install-server.ps1                                   # defaults; generates an ingest key and admin password
  .\deploy\install-server.ps1 -AllowFrom 10.0.0.0/16 -Webhook https://hooks.slack.com/services/...
  .\deploy\install-server.ps1 -Uninstall                         # remove task + firewall rule (data is kept)

  Re-running it upgrades in place: existing keys, password and data are kept unless you pass new ones.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = "C:\RapidPulse",
  [string]$DataDir = "",                 # default: D:\RapidPulse if a D: drive exists, else C:\RapidPulseData
  [int]$Port = 4400,
  [string]$IngestKey = "",               # default: keep existing, or generate one
  [string]$AdminUser = "admin",
  [string]$AdminPassword = "",           # default: keep existing, or generate one
  [switch]$NoUiLogin,                    # leave the dashboard without a login (not recommended)
  [string[]]$Webhook = @(),              # Slack / Teams / generic webhook URLs for alerts
  [string[]]$AllowFrom = @(),            # firewall: source IPs/CIDRs allowed to reach the port (default: any)
  [switch]$NoService,                    # trial mode: no firewall rule / scheduled task; just start the server
  [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$TaskName = "RapidPulseServer"
$RuleName = "RapidPulse $Port"
function Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
# Run an external program and judge it by exit code only. (Windows PowerShell 5.1 turns any
# stderr line - e.g. a harmless pip/venv warning - into a fatal error under "Stop".)
function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$Fail, [string]$StdIn = $null) {
  $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try {
    if ($null -ne $StdIn) { $out = $StdIn | & $Exe @Arguments 2>&1 } else { $out = & $Exe @Arguments 2>&1 }
    $code = $LASTEXITCODE
  } finally { $ErrorActionPreference = $prev }
  $text = ($out | ForEach-Object { "$_" }) -join "`n"
  if ($Fail -and $code -ne 0) { throw "$Fail (exit $code)`n$text" }
  return [pscustomobject]@{ Code = $code; Text = $text; Lines = @($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" }) }
}
function IsAdmin { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }

function Stop-RapidPulse {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*-m apm.server*' } |   # venv python.exe is a launcher: match the command, not the exe path
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if (-not $NoService -and -not (IsAdmin)) { throw "Run this from an elevated (Administrator) PowerShell, or use install-rapidpulse.bat which elevates for you." }

# ------------------------------------------------------------------ uninstall
if ($Uninstall) {
  Step "Removing RapidPulse service"
  Stop-RapidPulse
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Get-NetFirewallRule -DisplayName "RapidPulse *" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Write-Host "Removed the scheduled task and firewall rule. Files in $InstallDir and data were kept; delete them manually if no longer needed."
  return
}

$Source = (Resolve-Path "$PSScriptRoot\..").Path
if (-not $DataDir) { $DataDir = if (Test-Path "D:\") { "D:\RapidPulse" } else { "C:\RapidPulseData" } }

# ------------------------------------------------------------------ 1. Python
Step "Checking Python (3.10+)"
function Find-Python {
  foreach ($p in @("$env:ProgramFiles\Python311\python.exe", "$env:ProgramFiles\Python312\python.exe", "$env:ProgramFiles\Python313\python.exe")) {
    if (Test-Path $p) { return $p }
  }
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @("-3.11", "-3.12", "-3.13", "-3.10")) {
      $r = Invoke-Native "py" @($v, "-c", "import sys;print(sys.executable)")
      if ($r.Code -eq 0 -and $r.Lines.Count) { return $r.Lines[-1].Trim() }
    }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source -notlike "*WindowsApps*") {
    $r = Invoke-Native $cmd.Source @("-c", "import sys;print(sys.version_info[:2]>=(3,10))")
    if ($r.Code -eq 0 -and ($r.Lines -contains "True")) { return $cmd.Source }
  }
  return $null
}
$Python = Find-Python
if (-not $Python) {
  if ($NoService -and -not (IsAdmin)) { throw "Python 3.10+ not found. Install it, or run as Administrator to install it automatically." }
  Write-Host "Python not found - downloading Python 3.11.9 ..."
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $inst = Join-Path $env:SystemRoot "Temp\python-3.11.9-amd64.exe"   # not $env:TEMP: 8.3 profile paths break PS 5.1
  Invoke-WebRequest "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $inst -UseBasicParsing
  Start-Process $inst -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
  $Python = "$env:ProgramFiles\Python311\python.exe"
  if (-not (Test-Path $Python)) { throw "Python installation failed." }
}
Write-Host "Using $Python"

# ------------------------------------------------------------------ 2. Files
Step "Installing files to $InstallDir"
Stop-RapidPulse
New-Item -ItemType Directory -Force $InstallDir, $DataDir | Out-Null
if ($Source -ne (Resolve-Path $InstallDir).Path) {
  # Keep an existing configuration: never overwrite config\*.yaml on upgrade.
  $xf = @("*.pyc")
  if (Test-Path "$InstallDir\config\pulse.yaml") { $xf += "pulse.yaml" }
  $r = Invoke-Native "robocopy" (@($Source, $InstallDir, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
                                   "/XD", ".venv", "data", "__pycache__", ".pytest_cache", ".git", "/XF") + $xf)
  if ($r.Code -ge 8) { throw "Copying files failed (robocopy exit $($r.Code))`n$($r.Text)" }   # 0-7 = success
} else { Write-Host "Running from the install folder - no copy needed." }

# ------------------------------------------------------------------ 3. Dependencies
Step "Installing dependencies"
$VenvPy = "$InstallDir\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { Invoke-Native $Python @("-m", "venv", "$InstallDir\.venv") "Creating the virtual environment failed" | Out-Null }
Invoke-Native $VenvPy @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", "$InstallDir\requirements.txt") `
  "pip install failed - check internet/proxy access to pypi.org" | Out-Null

# ------------------------------------------------------------------ 4. Configuration
Step "Writing settings to config\pulse.local.yaml"
$settings = @{ local = "$InstallDir\config\pulse.local.yaml"; db = ("$DataDir\pulse.db" -replace '\\', '/'); port = $Port
               key = $IngestKey; user = $AdminUser; password = $AdminPassword; nologin = [bool]$NoUiLogin; webhooks = @($Webhook) }
$env:RP_SETTINGS = ($settings | ConvertTo-Json -Compress)
$py = @'
import json, os, secrets, yaml
s = json.loads(os.environ["RP_SETTINGS"])
path = s["local"]
cur = (yaml.safe_load(open(path, encoding="utf-8")) or {}) if os.path.exists(path) else {}
sec = cur.setdefault("security", {})
keys = sec.get("ingest_api_keys") or []
if s["key"]:
    keys = [s["key"]]
elif not keys:
    keys = [secrets.token_urlsafe(24)]
sec["ingest_api_keys"] = keys
users = sec.get("ui_users") or {}
if s["nologin"]:
    users = {}
elif s["password"]:
    users = {s["user"]: s["password"]}
elif not users:
    users = {s["user"]: secrets.token_urlsafe(12)}
sec["ui_users"] = users
cur.setdefault("storage", {})["path"] = s["db"]
cur.setdefault("server", {})["port"] = int(s["port"])
if s["webhooks"]:
    cur.setdefault("notifications", {})["webhooks"] = s["webhooks"]
with open(path, "w", encoding="utf-8") as fh:
    fh.write("# Written by deploy/install-server.ps1 - machine settings and secrets. Overrides pulse.yaml.\n")
    yaml.safe_dump(cur, fh, sort_keys=False)
print(json.dumps({"key": keys[0], "users": users}))
'@
$r = Invoke-Native $VenvPy @("-") "Writing the configuration failed" -StdIn $py
Remove-Item Env:\RP_SETTINGS
$creds = $r.Lines[-1] | ConvertFrom-Json
if (IsAdmin) {   # secrets file readable by Administrators and SYSTEM only
  Invoke-Native "icacls" @("$InstallDir\config\pulse.local.yaml", "/inheritance:r", "/grant:r", "*S-1-5-32-544:F", "*S-1-5-18:F") `
    "Restricting access to the settings file failed" | Out-Null
}

# ------------------------------------------------------------------ 5/6. Firewall + service
$Log = "$DataDir\server.log"
if ($NoService) {
  Step "Trial mode: starting the server in the background (no firewall rule, no auto-start)"
  Start-Process -FilePath $VenvPy -ArgumentList "-m", "apm.server" -WorkingDirectory $InstallDir -WindowStyle Hidden -RedirectStandardError $Log | Out-Null
} else {
  Step "Opening Windows Firewall port $Port"
  Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  $fw = @{ DisplayName = $RuleName; Direction = "Inbound"; Protocol = "TCP"; LocalPort = $Port; Action = "Allow"; Profile = "Any" }
  if ($AllowFrom.Count) { $fw.RemoteAddress = $AllowFrom }
  New-NetFirewallRule @fw | Out-Null

  Step "Registering auto-start task '$TaskName' (runs as SYSTEM, restarts on failure)"
  $action = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $InstallDir `
              -Argument "/c `"`"$VenvPy`" -m apm.server >> `"$Log`" 2>&1`""
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $set -Principal $principal -Force | Out-Null
  Start-ScheduledTask -TaskName $TaskName
}

# ------------------------------------------------------------------ 7. Health check
Step "Waiting for RapidPulse to answer on port $Port"
$ok = $false
for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
  Start-Sleep 2
  try { $ok = (Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 3).status -eq "ok" } catch {}
}
if (-not $ok) { throw "RapidPulse did not start. See the log: $Log" }

# ------------------------------------------------------------------ Summary
$ip = $null
try {
  $tok = Invoke-RestMethod -Method Put "http://169.254.169.254/latest/api/token" -Headers @{ "X-aws-ec2-metadata-token-ttl-seconds" = "60" } -TimeoutSec 1
  $ip = Invoke-RestMethod "http://169.254.169.254/latest/meta-data/local-ipv4" -Headers @{ "X-aws-ec2-metadata-token" = $tok } -TimeoutSec 1
} catch {}
if (-not $ip) { $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -First 1).IPAddress }

Write-Host ""
Write-Host "RapidPulse is running." -ForegroundColor Green
Write-Host "  Dashboard        : http://${ip}:$Port"
$u = $creds.users.PSObject.Properties | Select-Object -First 1
if ($u) { Write-Host "  Dashboard login  : $($u.Name) / $($u.Value)" } else { Write-Host "  Dashboard login  : (none - open to anyone who can reach the port)" -ForegroundColor Yellow }
Write-Host "  Ingest key       : $($creds.key)"
Write-Host "  Send traces with : OTEL_EXPORTER_OTLP_ENDPOINT=http://${ip}:$Port"
Write-Host "                     OTEL_EXPORTER_OTLP_HEADERS=x-pulse-key=$($creds.key)"
Write-Host "  Data / log       : $DataDir"
Write-Host "  Settings/secrets : $InstallDir\config\pulse.local.yaml"
if (-not $NoService) {
  Write-Host "  Restart          : Stop-ScheduledTask $TaskName; Start-ScheduledTask $TaskName"
  Write-Host "Remember: the EC2 security group must also allow TCP $Port from your app servers and your network." -ForegroundColor Yellow
}
