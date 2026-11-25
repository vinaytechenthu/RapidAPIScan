# Capture screenshots of RapidPulse pages with headless Edge (for docs / visual checks).
#   .\scripts\screenshot.ps1 -Base http://127.0.0.1:4400 -Pages overview,map,traces
param(
  [string]$Base = "http://127.0.0.1:4400",
  [string[]]$Pages = @("overview", "map", "services", "traces", "ai", "database", "infra", "alerts", "analytics"),
  [string]$Out = "$PSScriptRoot\..\docs\screenshots",
  [int]$Width = 1600, [int]$Height = 1300
)
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw "Microsoft Edge not found" }
New-Item -ItemType Directory -Force $Out | Out-Null
$profileDir = Join-Path $env:TEMP "pulse-shot-profile"
Remove-Item -Recurse -Force $profileDir -ErrorAction SilentlyContinue   # never screenshot a cached build
foreach ($p in $Pages) {
  $file = Join-Path (Resolve-Path $Out) (($p -replace '[^\w]', '_') + ".png")
  Start-Process -FilePath $edge -Wait -ArgumentList "--headless=new", "--disable-gpu", "--hide-scrollbars", "--user-data-dir=$profileDir",
    "--window-size=$Width,$Height", "--virtual-time-budget=12000", "--screenshot=$file", "$Base/#/$p"
  Write-Host "saved $file"
}
