<#
.SYNOPSIS
  RapidPulse setup for a Windows web server (IIS), driven by one YAML file:
    1. host agent        (deploy\install-agent.ps1)
    2. .NET tracing      (OpenTelemetry .NET automatic instrumentation, registered for IIS)
    3. per IIS application: exporter, service name, application/environment, request headers
    4. request payloads  (integrations\dotnet-iis\install-request-capture.ps1)

.DESCRIPTION
  Normally started by install-webserver-windows.bat:
    .\deploy\install-webserver.ps1 -ConfigFile .\rapidpulse-webserver.yaml
    .\deploy\install-webserver.ps1 -ConfigFile .\rapidpulse-webserver.yaml -Uninstall
  Safe to run again: every setting is replaced, not added twice.
#>
[CmdletBinding()]
param([string]$ConfigFile = "", [switch]$Uninstall)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Work = Join-Path $env:SystemRoot "Temp"   # not $env:TEMP: a user profile with a short 8.3 name (C:\Users\ABC~1.DEF) breaks PowerShell 5.1 file cmdlets
function Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Note([string]$m) { Write-Host $m -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail "Run this as Administrator (right-click > Run as administrator)." }

# ================================================================== YAML (the subset the settings file uses)
function Strip-Comment([string]$s) {
  $q = $null
  for ($i = 0; $i -lt $s.Length; $i++) {
    $c = $s[$i]
    if ($q) { if ($c -eq $q) { $q = $null }; continue }
    if (($c -eq '"' -or $c -eq "'") -and ($i -eq 0 -or "$($s[$i-1])" -match '[\s:\-\[,]')) { $q = $c; continue }
    if ($c -eq '#' -and ($i -eq 0 -or "$($s[$i-1])" -match '\s')) { return $s.Substring(0, $i).TrimEnd() }
  }
  return $s.TrimEnd()
}
function Unquote([string]$v) {
  $v = $v.Trim()
  if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[-1] -eq '"') -or ($v[0] -eq "'" -and $v[-1] -eq "'"))) { return $v.Substring(1, $v.Length - 2) }
  return $v
}
function Is-Item([string]$t) { return ($t -eq "-" -or $t.StartsWith("- ")) }
function Split-Pair([string]$t) {
  $m = [regex]::Match($t, '^("[^"]*"|''[^'']*''|[^\s"''\-\[{#][^:]*?)\s*:(?:\s+(.*))?$')
  if (-not $m.Success) { return $null }
  return @((Unquote $m.Groups[1].Value), $m.Groups[2].Value)
}
function Scalar([string]$v) {
  $t = $v.Trim()
  if ($t.StartsWith("[") -and $t.EndsWith("]")) {
    $list = New-Object System.Collections.ArrayList
    foreach ($p in $t.Substring(1, $t.Length - 2).Split(",")) { if ($p.Trim()) { [void]$list.Add((Unquote $p)) } }
    Write-Output -NoEnumerate $list; return
  }
  if ($t -match '^(true|yes|on)$') { return $true }
  if ($t -match '^(false|no|off)$') { return $false }
  return (Unquote $t)
}
function Parse-Block([int]$ind) {
  if (Is-Item $script:Y[$script:YP].Text) { $r = Parse-List $ind } else { $r = Parse-Map $ind }
  Write-Output -NoEnumerate $r
}
function Parse-Value([string]$rest, [int]$ind) {
  if ($rest -ne "") { $r = Scalar $rest; Write-Output -NoEnumerate $r; return }
  if ($script:YP -lt $script:Y.Count) {
    $n = $script:Y[$script:YP]
    if ($n.Indent -gt $ind -or ($n.Indent -eq $ind -and (Is-Item $n.Text))) { $r = Parse-Block $n.Indent; Write-Output -NoEnumerate $r; return }
  }
  return ""
}
function Parse-Map([int]$ind) {
  $map = [ordered]@{}
  while ($script:YP -lt $script:Y.Count -and $script:Y[$script:YP].Indent -eq $ind -and -not (Is-Item $script:Y[$script:YP].Text)) {
    $line = $script:Y[$script:YP]
    $kv = Split-Pair $line.Text
    if (-not $kv) { throw "Line $($line.No) not understood: $($line.Text)" }
    $script:YP++
    $v = Parse-Value $kv[1] $ind
    $map[$kv[0]] = $v
  }
  if ($script:YP -lt $script:Y.Count -and $script:Y[$script:YP].Indent -gt $ind) {
    throw "Line $($script:Y[$script:YP].No): unexpected indentation"
  }
  Write-Output -NoEnumerate $map
}
function Parse-List([int]$ind) {
  $list = New-Object System.Collections.ArrayList
  while ($script:YP -lt $script:Y.Count -and $script:Y[$script:YP].Indent -eq $ind -and (Is-Item $script:Y[$script:YP].Text)) {
    $line = $script:Y[$script:YP]
    $item = if ($line.Text -eq "-") { "" } else { $line.Text.Substring(2).Trim() }
    if ($item -eq "") {
      $script:YP++
      $v = Parse-Value "" $ind
      [void]$list.Add($v)
    } elseif (Split-Pair $item) {
      # "- key: value": a map whose keys line up with the first one
      $col = $ind + 1 + ($line.Text.Length - 1 - $line.Text.Substring(1).TrimStart().Length)
      $script:Y[$script:YP] = [pscustomobject]@{ Indent = $col; Text = $item; No = $line.No }
      $m = Parse-Map $col
      [void]$list.Add($m)
    } else {
      $script:YP++
      [void]$list.Add((Scalar $item))
    }
  }
  Write-Output -NoEnumerate $list
}
function Read-Yaml([string]$path) {
  $script:Y = New-Object System.Collections.Generic.List[object]
  $no = 0
  foreach ($raw in Get-Content -LiteralPath $path -Encoding UTF8) {
    $no++
    $s = Strip-Comment ($raw -replace "`t", "  ")
    if ($s.Trim() -eq "") { continue }
    $script:Y.Add([pscustomobject]@{ Indent = ($s.Length - $s.TrimStart().Length); Text = $s.Trim(); No = $no })
  }
  $script:YP = 0
  if ($script:Y.Count -eq 0) { return [ordered]@{} }
  $r = Parse-Map $script:Y[0].Indent
  if ($script:YP -lt $script:Y.Count) { throw "Line $($script:Y[$script:YP].No) not understood: $($script:Y[$script:YP].Text)" }
  Write-Output -NoEnumerate $r
}
function Get-Setting($node, [string]$path, $default = "") {
  foreach ($k in $path.Split(".")) {
    if ($node -is [System.Collections.IDictionary] -and $node.Contains($k)) { $node = $node[$k] } else { return $default }
  }
  if ($null -eq $node -or ($node -is [string] -and $node -eq "")) { return $default }
  return $node
}

# ================================================================== settings
if (-not $ConfigFile) { $ConfigFile = Join-Path $Root "rapidpulse-webserver.yaml" }
if (-not (Test-Path $ConfigFile)) { Fail "Settings file not found: $ConfigFile" }
try { $Cfg = Read-Yaml $ConfigFile } catch { Fail "$ConfigFile : $($_.Exception.Message)" }

$Server   = "$(Get-Setting $Cfg 'rapidpulse.server')".TrimEnd("/")
$Key      = "$(Get-Setting $Cfg 'rapidpulse.key')"
$AppName  = "$(Get-Setting $Cfg 'application.name')"
$EnvName  = "$(Get-Setting $Cfg 'application.environment')"
$Sites    = @(Get-Setting $Cfg 'sites' @() | Where-Object { $_ -is [System.Collections.IDictionary] })
$AgentOn  = [bool](Get-Setting $Cfg 'host_agent.enabled' $true)
$TraceOn  = [bool](Get-Setting $Cfg 'dotnet_tracing.enabled' $true)
$OtelVer  = "$(Get-Setting $Cfg 'dotnet_tracing.version' 'latest')"
$Upgrade  = [bool](Get-Setting $Cfg 'dotnet_tracing.upgrade' $false)
$Offline  = "$(Get-Setting $Cfg 'dotnet_tracing.offline_folder')"
$VerifyRelease = [bool](Get-Setting $Cfg 'dotnet_tracing.verify_release' $false)
$Headers  = @(Get-Setting $Cfg 'capture.headers' @() | ForEach-Object { "$_".Trim().ToLower() } | Where-Object { $_ })
$PayloadOn = [bool](Get-Setting $Cfg 'capture.payload' $true)
$PayloadBytes = "$(Get-Setting $Cfg 'capture.payload_bytes' '8192')"
$MaskFields = "$(Get-Setting $Cfg 'capture.mask_fields')"
$Proxy    = "$(Get-Setting $Cfg 'install.proxy')"

if ($Server -notmatch '^https?://[^/]+') { Fail "rapidpulse.server is not set, e.g.  server: http://10.0.0.5:4400" }
if ($Proxy) { $env:HTTP_PROXY = $Proxy; $env:HTTPS_PROXY = $Proxy; $global:PSDefaultParameterValues["Invoke-WebRequest:Proxy"] = $Proxy }
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$ac = "$env:windir\system32\inetsrv\appcmd.exe"
$HasIIS = Test-Path $ac
$Managed = @("OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_PROTOCOL",
             "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_TRACES_EXPORTER", "OTEL_LOGS_EXPORTER", "OTEL_METRICS_EXPORTER",
             "OTEL_DOTNET_AUTO_TRACES_ASPNET_INSTRUMENTATION_CAPTURE_REQUEST_HEADERS",
             "OTEL_DOTNET_AUTO_TRACES_ASPNETCORE_INSTRUMENTATION_CAPTURE_REQUEST_HEADERS",
             "RAPIDPULSE_CAPTURE_BODY_BYTES", "RAPIDPULSE_CAPTURE_MASK")

# Run another installer script in its own PowerShell, so its exit/throw cannot end this one.
# Its output goes straight to the screen (not into the return value); only the exit code is returned.
function Run-Script([string]$script, [string[]]$arguments) {
  $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"   # PS 5.1: child stderr must not stop us
  try { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script @arguments 2>&1 | Out-Host; $code = $LASTEXITCODE }
  finally { $ErrorActionPreference = $prev }
  return [int]$code
}

# ================================================================== IIS helpers
function Get-IisApps {
  [xml]$vx = & $ac list vdir /xml
  [xml]$ax = & $ac list app /xml
  $pools = @{}
  foreach ($a in $ax.appcmd.APP) { $pools[$a.'APP.NAME'] = $a.'APPPOOL.NAME' }
  foreach ($v in $vx.appcmd.VDIR) {
    if ($v.'VDIR.NAME'.TrimEnd('/') -eq $v.'APP.NAME'.TrimEnd('/')) {
      [pscustomobject]@{ Name = $v.'APP.NAME'; Pool = $pools[$v.'APP.NAME']; Path = [Environment]::ExpandEnvironmentVariables($v.physicalPath) }
    }
  }
}
function Find-IisApp($apps, [string]$name) {
  $want = $name.Trim().TrimEnd('/')
  $hit = @($apps | Where-Object { $_.Name.TrimEnd('/') -eq $want })
  if ($hit.Count -eq 0) { $hit = @($apps | Where-Object { $_.Name -like "*$want*" }) }
  if ($hit.Count -eq 1) { return $hit[0] }
  return $null
}
function Get-PoolVar([string]$pool, [string]$name) {
  [xml]$px = & $ac list apppool "$pool" /config:* /xml
  $n = $px.SelectSingleNode("//environmentVariables/add[@name='$name']")
  if ($n) { return $n.value } else { return $null }
}
function Set-PoolVar([string]$pool, [string]$name, [string]$value) {
  if ($null -ne (Get-PoolVar $pool $name)) {
    & $ac set config -section:system.applicationHost/applicationPools /-"[name='$pool'].environmentVariables.[name='$name']" /commit:apphost | Out-Null
  }
  if ($value) {
    if ($value.Contains("'")) { Fail "The value of $name contains a ' character, which IIS settings cannot hold." }
    & $ac set config -section:system.applicationHost/applicationPools /+"[name='$pool'].environmentVariables.[name='$name',value='$value']" /commit:apphost | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Could not set $name on app pool $pool." }
  }
}

# ================================================================== uninstall
if ($Uninstall) {
  if ($HasIIS -and $Sites.Count) {
    $apps = Get-IisApps
    foreach ($s in $Sites) {
      $site = Find-IisApp $apps "$($s['iis_app'])"
      if (-not $site) { Note "IIS application '$($s['iis_app'])' not found - skipped."; continue }
      Step "Removing RapidPulse settings from $($site.Name) (app pool $($site.Pool))"
      Run-Script (Join-Path $Root "integrations\dotnet-iis\install-request-capture.ps1") @("-App", $site.Name, "-Remove") | Out-Null
      foreach ($n in $Managed) { Set-PoolVar $site.Pool $n $null }
      & $ac recycle apppool /apppool.name:"$($site.Pool)" | Out-Null
    }
  }
  if ($AgentOn) { Step "Removing the host agent"; Run-Script (Join-Path $Root "deploy\install-agent.ps1") @("-Uninstall") | Out-Null }
  Write-Host "`nDone. The OpenTelemetry .NET agent itself is left installed (other sites may use it). To remove it:" -ForegroundColor Green
  Write-Host "  Import-Module `"<folder>\OpenTelemetry.DotNet.Auto.psm1`"; Unregister-OpenTelemetryForIIS; Uninstall-OpenTelemetryCore"
  exit 0
}

# ================================================================== 1. connection
Step "Testing the connection to RapidPulse at $Server"
try { Invoke-RestMethod "$Server/api/health" -TimeoutSec 10 -UseBasicParsing | Out-Null }
catch { Fail "Cannot reach $Server/api/health ($($_.Exception.Message)).`nCheck rapidpulse.server, and that TCP port $(([uri]$Server).Port) is open from this server (security group and Windows Firewall)." }
try {
  Invoke-WebRequest "$Server/v1/traces" -Method Post -Body '{"resourceSpans":[]}' -ContentType "application/json" -Headers @{ "x-pulse-key" = $Key } -TimeoutSec 10 -UseBasicParsing | Out-Null
  Write-Host "Reachable; ingest key accepted."
} catch {
  if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) { Fail "RapidPulse rejected rapidpulse.key (HTTP 401). Copy the key from C:\RapidPulse\config\pulse.local.yaml on the RapidPulse server." }
  Note "Key test inconclusive ($($_.Exception.Message)); continuing."
}

# ================================================================== 2. host agent
if ($AgentOn) {
  Step "Host agent"
  $tmp = Join-Path $Work ("rapidpulse-agent-" + [guid]::NewGuid().ToString("N") + ".conf")
  $lines = @("SERVER=$Server", "KEY=$Key", "APP=$AppName",
             "ROLE=$(Get-Setting $Cfg 'host_agent.role' 'web')", "HOST_NAME=$(Get-Setting $Cfg 'host_agent.host_name')",
             "INTERVAL=$(Get-Setting $Cfg 'host_agent.interval' '15')", "DOCKER=off",
             "PYTHON=$(Get-Setting $Cfg 'install.python')", "PIP_INDEX_URL=$(Get-Setting $Cfg 'install.pip_index_url')", "PROXY=$Proxy")
  foreach ($c in @(Get-Setting $Cfg 'host_agent.checks' @())) {
    if ($c -is [System.Collections.IDictionary] -and $c['url']) { $lines += "CHECK=$($c['name'])=$($c['url'])@$($c['service'])" }
  }
  try {
    [IO.File]::WriteAllLines($tmp, [string[]]$lines)
    $code = Run-Script (Join-Path $Root "deploy\install-agent.ps1") @("-ConfigFile", $tmp)
  } finally { try { [IO.File]::Delete($tmp) } catch { } }   # it holds the key
  if ($code -ne 0) { Fail "The host agent install failed (see above)." }
}

if (-not $HasIIS) {
  Note "`nIIS is not installed on this server: .NET tracing and request capture were skipped."
  exit 0
}
if (-not $Sites.Count) { Note "`nNo sites listed under 'sites:' - nothing traced. Add your IIS applications and run again."; exit 0 }

# ================================================================== 3. OpenTelemetry .NET agent
if ($TraceOn) {
  Step "OpenTelemetry .NET automatic instrumentation"
  $otelHome = Join-Path $env:ProgramFiles "OpenTelemetry .NET AutoInstrumentation"
  $installed = Test-Path (Join-Path $otelHome "net")
  $w3env = @((Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\W3SVC" -Name Environment -ErrorAction SilentlyContinue).Environment)
  $registered = [bool]($w3env | Where-Object { $_ -like "OTEL_DOTNET_AUTO_HOME=*" })
  if ($installed -and $registered -and -not $Upgrade) {
    Write-Host "Already installed and registered for IIS ($otelHome) - kept."
  } else {
    if ($Offline) {
      $psm1 = Join-Path $Offline "OpenTelemetry.DotNet.Auto.psm1"
      $zip  = Join-Path $Offline "opentelemetry-dotnet-instrumentation-windows.zip"
      if (-not (Test-Path $psm1) -or -not (Test-Path $zip)) { Fail "dotnet_tracing.offline_folder must contain OpenTelemetry.DotNet.Auto.psm1 and opentelemetry-dotnet-instrumentation-windows.zip" }
    } else {
      $base = "https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation/releases"
      $url = if ($OtelVer -eq "latest") { "$base/latest/download/OpenTelemetry.DotNet.Auto.psm1" } else { "$base/download/$OtelVer/OpenTelemetry.DotNet.Auto.psm1" }
      $psm1 = Join-Path $Work "OpenTelemetry.DotNet.Auto.psm1"
      try { Invoke-WebRequest $url -OutFile $psm1 -UseBasicParsing }
      catch { Fail "Could not download $url ($($_.Exception.Message)).`nNo internet here? Use dotnet_tracing.offline_folder (see the settings file), or set install.proxy." }
    }
    Import-Module $psm1 -Force
    # Newer module versions verify the download with the GitHub CLI ('gh'). Use it when this server has it;
    # otherwise skip that check (the files still come from GitHub's release page over HTTPS) unless
    # dotnet_tracing.verify_release is true.
    function Verify-Args([string]$cmd) {
      $p = (Get-Command $cmd).Parameters
      if (-not $p.ContainsKey("SkipReleaseVerification")) { return @{} }
      if (Get-Command gh -ErrorAction SilentlyContinue) { Write-Host "Verifying the release with the GitHub CLI."; return @{} }
      if ($VerifyRelease) { Fail "dotnet_tracing.verify_release is true but the GitHub CLI (gh) is not installed: install it from https://cli.github.com/ or set verify_release: false." }
      Note "GitHub CLI (gh) not installed: skipping the release signature check (download is over HTTPS from GitHub)."
      return @{ SkipReleaseVerification = $true }
    }
    if (-not $installed) {
      Write-Host "Installing to $otelHome ..."
      if ($Offline) { Install-OpenTelemetryCore -LocalPath $zip }
      else { $va = Verify-Args "Install-OpenTelemetryCore"; Install-OpenTelemetryCore @va }
    } elseif ($Upgrade) {
      Write-Host "Upgrading $otelHome ..."
      $va = Verify-Args "Update-OpenTelemetryCore"; Update-OpenTelemetryCore @va
    }
    Write-Host "Registering for IIS (this restarts IIS once: all sites on this server pause for a few seconds) ..."
    Register-OpenTelemetryForIIS
  }
}

# ================================================================== 4. each IIS application
$apps = Get-IisApps
$poolService = @{}
$done = @()
foreach ($s in $Sites) {
  $name = "$($s['iis_app'])"
  $site = Find-IisApp $apps $name
  if (-not $site) {
    Write-Host "`nIIS applications on this server:"; $apps | Format-Table Name, Pool, Path -AutoSize | Out-Host
    Fail "sites: iis_app '$name' matches no single IIS application (use one of the names above)."
  }
  $service = "$($s['service'])"; if (-not $service) { $service = $site.Name.TrimEnd('/').Split('/')[-1] }
  $pool = $site.Pool
  Step "$($site.Name)  ->  service '$service'  (app pool $pool)"
  if ($poolService.ContainsKey($pool) -and $poolService[$pool] -ne $service) {
    Note "App pool $pool is shared with service '$($poolService[$pool])': IIS settings are per app pool, so both report as '$service'. Give each application its own app pool to tell them apart."
  }
  $poolService[$pool] = $service

  $attrs = @("service.namespace=$AppName", "deployment.environment.name=$EnvName") | Where-Object { $_ -notmatch '=$' }
  $vars = [ordered]@{
    "OTEL_SERVICE_NAME" = $service
    "OTEL_RESOURCE_ATTRIBUTES" = ($attrs -join ",")
    "OTEL_EXPORTER_OTLP_ENDPOINT" = $Server
    "OTEL_EXPORTER_OTLP_PROTOCOL" = "http/protobuf"
    "OTEL_EXPORTER_OTLP_HEADERS" = $(if ($Key) { "x-pulse-key=$Key" } else { "" })
    "OTEL_TRACES_EXPORTER" = "otlp"
    "OTEL_LOGS_EXPORTER" = "otlp"
    "OTEL_METRICS_EXPORTER" = "none"
    "OTEL_DOTNET_AUTO_TRACES_ASPNET_INSTRUMENTATION_CAPTURE_REQUEST_HEADERS" = ($Headers -join ",")
    "OTEL_DOTNET_AUTO_TRACES_ASPNETCORE_INSTRUMENTATION_CAPTURE_REQUEST_HEADERS" = ($Headers -join ",")
    "RAPIDPULSE_CAPTURE_BODY_BYTES" = $(if ($PayloadOn) { $PayloadBytes } else { "0" })
    "RAPIDPULSE_CAPTURE_MASK" = $MaskFields
  }
  foreach ($k in $vars.Keys) { Set-PoolVar $pool $k $vars[$k] }
  Write-Host "Tracing settings, headers ($($Headers.Count)) set on app pool $pool."

  $capture = Join-Path $Root "integrations\dotnet-iis\install-request-capture.ps1"
  if ($PayloadOn) {
    if ((Run-Script $capture @("-App", $site.Name)) -ne 0) { Fail "Payload capture install failed for $($site.Name) (see above)." }
  } else {
    Run-Script $capture @("-App", $site.Name, "-Remove") | Out-Null
  }
  & $ac recycle apppool /apppool.name:"$pool" | Out-Null
  Write-Host "App pool $pool recycled."
  $done += "$($site.Name) -> $service"
}

# ================================================================== summary
Write-Host "`nRapidPulse web server setup complete." -ForegroundColor Green
Write-Host "  RapidPulse   : $Server   (application '$AppName', environment '$EnvName')"
if ($AgentOn) { Write-Host "  Host agent   : scheduled task RapidPulseAgent  -> Infrastructure page" }
foreach ($d in $done) { Write-Host "  Traced       : $d" }
Write-Host "  Request card : headers $(if ($Headers.Count) { 'on' } else { 'off' }), payload $(if ($PayloadOn) { 'on' } else { 'off' })"
Write-Host "Open each site once in a browser, then look for the service under Traces in RapidPulse."
Write-Host "Change a setting: edit $ConfigFile and run install-webserver-windows.bat again."
