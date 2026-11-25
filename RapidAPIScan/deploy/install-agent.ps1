<#
.SYNOPSIS
  Installs (or upgrades) the RapidPulse agent on a Windows server in one step:
  settings -> connection test -> Python -> files -> test send -> auto-start task.

.DESCRIPTION
  Normally started by install-agent-windows.bat, which passes rapidpulse-agent.conf:
    .\deploy\install-agent.ps1 -ConfigFile .\rapidpulse-agent.conf
  Parameters given on the command line override the file:
    .\deploy\install-agent.ps1 -Server http://10.0.0.5:4400 -Key <key> -App my-application -Role web `
        -Checks "Web login=https://localhost/login@web-app"
    .\deploy\install-agent.ps1 -Uninstall

  The agent runs as the scheduled task "RapidPulseAgent" (SYSTEM, starts at boot, restarts if it stops)
  and reads its settings from <InstallDir>\agent.conf (Administrators and SYSTEM only).
#>
[CmdletBinding()]
param(
  [string]$ConfigFile = "",
  [string]$Server = "", [string]$Key = "", [string]$App = "", [string]$Role = "", [string]$HostName = "",
  [string[]]$Checks = @(), [string]$Mssql = "", [int]$Interval = 0,
  [string]$InstallDir = "", [string]$Python = "",
  [switch]$NoDocker, [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$TaskName = "RapidPulseAgent"
function Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Fail([string]$m) { Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }
# Run an external program and judge it by exit code only (PowerShell 5.1 turns stderr lines into errors).
function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$FailMsg) {
  $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { $out = & $Exe @Arguments 2>&1; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
  $text = ($out | ForEach-Object { "$_" }) -join "`n"
  if ($FailMsg -and $code -ne 0) { Fail "$FailMsg (exit $code)`n$text" }
  return [pscustomobject]@{ Code = $code; Text = $text; Lines = @($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" }) }
}
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail "Run this as Administrator (right-click > Run as administrator)." }

# ------------------------------------------------------------------ 0. settings
$cfg = @{}; $cfgChecks = New-Object System.Collections.Generic.List[string]
if ($ConfigFile) {
  if (-not (Test-Path $ConfigFile)) { Fail "Settings file not found: $ConfigFile" }
  foreach ($raw in Get-Content -LiteralPath $ConfigFile -Encoding UTF8) {
    $line = $raw.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
    $i = $line.IndexOf("="); $k = $line.Substring(0, $i).Trim().ToUpper(); $v = $line.Substring($i + 1).Trim()
    if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[-1] -eq '"') -or ($v[0] -eq "'" -and $v[-1] -eq "'"))) { $v = $v.Substring(1, $v.Length - 2) }
    if ($k -eq "CHECK") { if ($v) { $cfgChecks.Add($v) } } else { $cfg[$k] = $v }
  }
}
function Pick([string]$cli, [string]$name, [string]$default = "") { if ($cli) { $cli } elseif ($cfg[$name]) { $cfg[$name] } else { $default } }
$Server     = (Pick $Server "SERVER").TrimEnd("/")
$Key        = Pick $Key "KEY"
$App        = Pick $App "APP"
$Role       = Pick $Role "ROLE"
$HostName   = Pick $HostName "HOST_NAME"
$Mssql      = Pick $Mssql "MSSQL"
$Interval   = [int](Pick ($(if ($Interval) { "$Interval" } else { "" })) "INTERVAL" "15")
$InstallDir = Pick $InstallDir "INSTALL_DIR" "$env:ProgramFiles\RapidPulseAgent"
$Python     = Pick $Python "PYTHON"
$Docker     = if ($NoDocker) { "off" } else { Pick "" "DOCKER" "auto" }
$AllChecks  = @($cfgChecks) + @($Checks | Where-Object { $_ })
$PipIndex   = Pick "" "PIP_INDEX_URL"
$Proxy      = Pick "" "PROXY"

# ------------------------------------------------------------------ uninstall
if ($Uninstall) {
  Step "Removing the RapidPulse agent"
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  }
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*pulse_agent.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
  Write-Host "Removed the scheduled task and $InstallDir." -ForegroundColor Green
  exit 0
}

if (-not $Server -or $Server -notmatch '^https?://[^/]+') { Fail "SERVER is not set. Put the RapidPulse address in the settings file, e.g. SERVER=http://10.0.0.5:4400" }
if (-not $App) { Write-Host "Note: APP is empty - this server will not be grouped under an application." -ForegroundColor Yellow }
if ($Proxy) { $env:HTTP_PROXY = $Proxy; $env:HTTPS_PROXY = $Proxy }
if ($PipIndex) { $env:PIP_INDEX_URL = $PipIndex }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ------------------------------------------------------------------ 1. connection test
Step "Testing the connection to RapidPulse at $Server"
try { Invoke-RestMethod "$Server/api/health" -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop | Out-Null }
catch { Fail "Cannot reach $Server/api/health ($($_.Exception.Message)).`nCheck SERVER, and that TCP port $(([uri]$Server).Port) is allowed from this server in the RapidPulse security group and Windows Firewall." }
try {
  Invoke-WebRequest "$Server/v1/traces" -Method Post -Body '{"resourceSpans":[]}' -ContentType "application/json" `
    -Headers @{ "x-pulse-key" = $Key } -TimeoutSec 10 -UseBasicParsing | Out-Null
  Write-Host "Reachable; ingest key accepted."
} catch {
  if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) {
    Fail "RapidPulse rejected the key (HTTP 401). Set KEY to the ingest key from C:\RapidPulse\config\pulse.local.yaml on the RapidPulse server." }
  Write-Host "Warning: key test inconclusive ($($_.Exception.Message)); continuing." -ForegroundColor Yellow
}

# ------------------------------------------------------------------ 2. Python
Step "Checking Python (3.8+)"
function Find-Python {
  foreach ($p in @("$env:ProgramFiles\Python313\python.exe", "$env:ProgramFiles\Python312\python.exe", "$env:ProgramFiles\Python311\python.exe", "$env:ProgramFiles\Python310\python.exe")) {
    if (Test-Path $p) { return $p }
  }
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $r = Invoke-Native "py" @("-3", "-c", "import sys;print(sys.executable if sys.version_info[:2]>=(3,8) else '')")
    if ($r.Code -eq 0 -and $r.Lines.Count -and $r.Lines[-1].Trim()) { return $r.Lines[-1].Trim() }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source -notlike "*WindowsApps*") {
    $r = Invoke-Native $cmd.Source @("-c", "import sys;print(sys.version_info[:2]>=(3,8))")
    if ($r.Code -eq 0 -and ($r.Lines -contains "True")) { return $cmd.Source }
  }
  return $null
}
if ($Python -and -not (Test-Path $Python)) { Fail "PYTHON is set to '$Python' but that file does not exist." }
if (-not $Python) { $Python = Find-Python }
if (-not $Python) {
  Write-Host "Python not found - downloading Python 3.11.9 ..."
  $inst = Join-Path $env:SystemRoot "Temp\python-3.11.9-amd64.exe"   # not $env:TEMP: 8.3 profile paths break PS 5.1
  try { Invoke-WebRequest "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $inst -UseBasicParsing }
  catch { Fail "Could not download Python ($($_.Exception.Message)). Install Python 3.11 for all users, or set PYTHON / PROXY in the settings file." }
  Start-Process $inst -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
  $Python = "$env:ProgramFiles\Python311\python.exe"
  if (-not (Test-Path $Python)) { Fail "Python installation failed." }
}
Write-Host "Using $Python"

# ------------------------------------------------------------------ 3. files
Step "Installing to $InstallDir"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like "*pulse_agent.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$src = Join-Path $PSScriptRoot "..\agent\pulse_agent.py"
if (-not (Test-Path $src)) { Fail "agent\pulse_agent.py not found next to the installer. Copy the 'agent' and 'deploy' folders together with the .bat." }
New-Item -ItemType Directory -Force $InstallDir | Out-Null
Copy-Item $src $InstallDir -Force
$venvPy = Join-Path $InstallDir "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Invoke-Native $Python @("-m", "venv", "$InstallDir\venv") "Could not create the Python environment" | Out-Null }
$pkgs = @("psutil==6.1.0"); if ($Mssql) { $pkgs += "pyodbc==5.2.0" }
Invoke-Native $venvPy (@("-m", "pip", "install", "--quiet", "--disable-pip-version-check") + $pkgs) "Package install failed (needs pypi.org, or PIP_INDEX_URL / PROXY in the settings file)" | Out-Null
if ($Mssql -and -not (Get-OdbcDriver -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "ODBC Driver 1* for SQL Server" })) {
  Write-Host "Warning: MSSQL is set but no 'ODBC Driver 17/18 for SQL Server' is installed - SQL metrics will fail until it is." -ForegroundColor Yellow
}

# The installed settings file: only what the agent needs, readable by Administrators and SYSTEM only.
$conf = Join-Path $InstallDir "agent.conf"
$lines = @("# RapidPulse agent settings (written by the installer $(Get-Date -Format s)). Edit, then restart:",
           "#   Stop-ScheduledTask $TaskName; Start-ScheduledTask $TaskName",
           "SERVER=$Server", "KEY=$Key", "APP=$App", "ROLE=$Role", "HOST_NAME=$HostName", "INTERVAL=$Interval", "DOCKER=$Docker", "MSSQL=$Mssql") +
         ($AllChecks | ForEach-Object { "CHECK=$_" })
Set-Content -LiteralPath $conf -Value $lines -Encoding UTF8
Invoke-Native "icacls.exe" @($conf, "/inheritance:r", "/grant:r", "*S-1-5-32-544:F", "*S-1-5-18:F") "Could not protect $conf" | Out-Null

# ------------------------------------------------------------------ 4. test send
Step "Sending a test sample"
$t = Invoke-Native $venvPy @("$InstallDir\pulse_agent.py", "--config", $conf, "--once")
if ($t.Code -ne 0) { Fail "The test sample was not accepted:`n$($t.Text)" }
$first = [Array]::FindIndex([string[]]$t.Lines, [Predicate[string]]{ param($l) $l.StartsWith("{") })
$summary = $null
if ($first -ge 0) { try { $summary = ($t.Lines[$first..($t.Lines.Count - 1)] -join "`n") | ConvertFrom-Json } catch { } }
if ($summary) {
  Write-Host ("Host '{0}': CPU {1}%, memory {2}%, {3} container(s), {4} check(s)" -f $summary.host, $summary.metrics.cpu, $summary.metrics.mem_pct, $summary.containers, @($summary.checks).Count)
  foreach ($c in @($summary.checks)) { if ($c) { Write-Host ("  check {0}: {1}" -f $c[0], $(if ($c[1]) { "OK" } else { "FAILING" })) } }
}

# ------------------------------------------------------------------ 5. auto-start task
Step "Registering the auto-start task '$TaskName'"
$log = Join-Path $InstallDir "agent.log"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $InstallDir `
  -Argument "/c `"`"$venvPy`" `"$InstallDir\pulse_agent.py`" --config `"$conf`" >> `"$log`" 2>&1`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4
$state = (Get-ScheduledTask -TaskName $TaskName).State
if ("$state" -ne "Running") { Fail "The task did not stay running (state: $state). See $log" }

Write-Host ""
Write-Host "RapidPulse agent is running." -ForegroundColor Green
Write-Host "  Reporting to : $Server  (application '$App', role '$Role')"
Write-Host "  Settings     : $conf"
Write-Host "  Log          : $log"
Write-Host "  Restart      : Stop-ScheduledTask $TaskName; Start-ScheduledTask $TaskName"
Write-Host "  Uninstall    : install-agent-windows.bat -Uninstall"
Write-Host "This server appears on the RapidPulse Infrastructure page within about $Interval seconds."
