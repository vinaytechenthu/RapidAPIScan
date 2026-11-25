<#
Installs (or removes) RapidPulse request-payload capture for an IIS application.
Run in PowerShell as Administrator on the IIS server:

  .\install-request-capture.ps1                        # lists the IIS applications
  .\install-request-capture.ps1 -App "MySite"          # installs into the application named "MySite"
                                                       # (or, if none has exactly that name, the one whose name contains it)
  .\install-request-capture.ps1 -App "MySite" -Remove  # removes it again

It detects the kind of application:
  ASP.NET (.NET Framework)  copies RapidPulse.RequestCapture.dll into the site's bin folder; ASP.NET restarts
                            that application by itself.
  ASP.NET Core              copies RapidPulse.RequestCapture.AspNetCore.dll to
                            %ProgramFiles%\RapidPulse\request-capture, adds it to DOTNET_STARTUP_HOOKS on the
                            application's app pool (keeping the OpenTelemetry agent's own hook first) and
                            recycles that app pool.
No application code or web.config changes. Request headers are a separate agent setting (guide, 6.7).
#>
param([string]$App, [switch]$Remove)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ac   = "$env:windir\system32\inetsrv\appcmd.exe"
if (-not (Test-Path $ac)) { throw "IIS is not installed on this machine (appcmd.exe not found)." }
$HookName = "RapidPulse.RequestCapture.AspNetCore.dll"

# ---------------------------------------------------------------- find the application
[xml]$vx = & $ac list vdir /xml
[xml]$ax = & $ac list app /xml
$pools = @{}
foreach ($a in $ax.appcmd.APP) { $pools[$a.'APP.NAME'] = $a.'APPPOOL.NAME' }
$apps = foreach ($v in $vx.appcmd.VDIR) {
    if ($v.'VDIR.NAME'.TrimEnd('/') -eq $v.'APP.NAME'.TrimEnd('/')) {
        [pscustomobject]@{ Name = $v.'APP.NAME'; Pool = $pools[$v.'APP.NAME']
                           Path = [Environment]::ExpandEnvironmentVariables($v.physicalPath) }
    }
}
if (-not $App) {
    Write-Host "IIS applications (use -App with the name):"
    $apps | Format-Table -AutoSize
    return
}
$want = $App.Trim().TrimEnd('/')
$hit = @($apps | Where-Object { $_.Name.TrimEnd('/') -eq $want })                     # exact name first
if ($hit.Count -eq 0) { $hit = @($apps | Where-Object { $_.Name -like "*$App*" }) }   # then part of the name
if ($hit.Count -ne 1) {
    $apps | Format-Table -AutoSize
    throw "'$App' matches $($hit.Count) applications; give the exact name (one of the above)."
}
$site = $hit[0]
$web  = Join-Path $site.Path "web.config"
$isCore = (Test-Path $web) -and (Select-String -Path $web -Pattern "<aspNetCore" -Quiet)

# ---------------------------------------------------------------- ASP.NET (.NET Framework)
if (-not $isCore) {
    $bin = Join-Path $site.Path "bin"
    if (-not (Test-Path $bin)) { throw "No bin folder in $($site.Path): is this an ASP.NET application?" }
    $target = Join-Path $bin "RapidPulse.RequestCapture.dll"
    if ($Remove) {
        if (Test-Path $target) { Remove-Item $target -Force; Write-Host "Removed from $($site.Name) ($bin)." }
        else { Write-Host "Not installed in $($site.Name)." }
        return
    }
    $dll = Join-Path $here "bin\RapidPulse.RequestCapture.dll"
    if (-not (Test-Path $dll)) { throw "Not found: $dll (run build.ps1 first)." }
    Copy-Item $dll $target -Force
    try { Unblock-File $target } catch { }
    Write-Host "ASP.NET (.NET Framework) application: installed into $($site.Name) ($bin)."
    Write-Host "The application restarts by itself. Send a request with a payload, then open its trace in RapidPulse."
    return
}

# ---------------------------------------------------------------- ASP.NET Core
$pool = $site.Pool
if (-not $pool) { throw "Could not find the app pool of $($site.Name)." }
$hookDir = Join-Path $env:ProgramFiles "RapidPulse\request-capture"
$hook    = Join-Path $hookDir $HookName

function Get-PoolVar([string]$name) {
    [xml]$px = & $ac list apppool "$pool" /config:* /xml
    $n = $px.SelectSingleNode("//environmentVariables/add[@name='$name']")
    if ($n) { return $n.value } else { return $null }
}
function Set-PoolVar([string]$name, [string]$value) {
    if ($null -ne (Get-PoolVar $name)) {
        & $ac set config -section:system.applicationHost/applicationPools /-"[name='$pool'].environmentVariables.[name='$name']" /commit:apphost | Out-Null
    }
    if ($value) {
        & $ac set config -section:system.applicationHost/applicationPools /+"[name='$pool'].environmentVariables.[name='$name',value='$value']" /commit:apphost | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not set $name on app pool $pool." }
    }
}
function Get-InheritedHooks {
    # What w3wp inherits when the app pool sets nothing: the IIS services' environment (where the
    # OpenTelemetry installer puts it), then the machine environment.
    foreach ($svc in "W3SVC", "WAS") {
        $envList = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$svc" -Name Environment -ErrorAction SilentlyContinue).Environment
        foreach ($line in @($envList)) { if ($line -like "DOTNET_STARTUP_HOOKS=*") { return $line.Substring(21) } }
    }
    return [Environment]::GetEnvironmentVariable("DOTNET_STARTUP_HOOKS", "Machine")
}
function Split-Hooks([string]$v) { @(($v -split ';') | ForEach-Object { $_.Trim() } | Where-Object { $_ -and ($_ -notlike "*\$HookName") }) }

$inherited = Get-InheritedHooks
$poolValue = Get-PoolVar "DOTNET_STARTUP_HOOKS"
$others    = Split-Hooks $(if ($poolValue) { $poolValue } else { $inherited })   # everything except ours, in order

if ($Remove) {
    $rest = ($others -join ';')
    if (-not $rest -or $rest -eq ((Split-Hooks $inherited) -join ';')) { Set-PoolVar "DOTNET_STARTUP_HOOKS" $null }
    else { Set-PoolVar "DOTNET_STARTUP_HOOKS" $rest }
    & $ac recycle apppool /apppool.name:"$pool" | Out-Null
    Write-Host "Removed from $($site.Name) (app pool $pool, recycled)."
    return
}

$dll = Join-Path $here "bin\$HookName"
if (-not (Test-Path $dll)) { throw "Not found: $dll (run build.ps1 first)." }
New-Item -ItemType Directory -Force $hookDir | Out-Null
Copy-Item $dll $hook -Force
try { Unblock-File $hook } catch { }
& icacls $hookDir /grant "*S-1-5-32-568:(OI)(CI)RX" /T /Q | Out-Null   # IIS_IUSRS: app pool identities can read it

if (-not ($others | Where-Object { $_ -like "*OpenTelemetry*" })) {
    Write-Warning "No OpenTelemetry startup hook found for app pool $pool. Payloads are only recorded when the OpenTelemetry .NET agent also traces this application."
}
$value = (@($others) + $hook) -join ';'
Set-PoolVar "DOTNET_STARTUP_HOOKS" $value
& $ac recycle apppool /apppool.name:"$pool" | Out-Null
Write-Host "ASP.NET Core application: installed for $($site.Name) (app pool $pool, recycled)."
Write-Host "DOTNET_STARTUP_HOOKS on the app pool is now:"
$value -split ';' | ForEach-Object { Write-Host "  $_" }
Write-Host "Send a request with a payload, then open its trace in RapidPulse."
