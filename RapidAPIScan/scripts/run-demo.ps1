# Start RapidPulse locally with simulated traffic for an example application.
#   .\scripts\run-demo.ps1                          # demo-shop (default)
#   .\scripts\run-demo.ps1 -Example <folder>        # any folder under examples\ with topology.yaml (+ optional pulse.yaml)
#   .\scripts\run-demo.ps1 -Topology C:\my\app.yaml # your own topology
# then open http://127.0.0.1:4400
param([string]$Example = "demo-shop", [string]$Topology = "", [int]$BackfillMinutes = 120, [double]$Rate = 0.5)
$root = Resolve-Path "$PSScriptRoot\.."
$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  python -m venv "$root\.venv"
  & $py -m pip install -q -r "$root\requirements.txt"
}
if (-not $Topology) { $Topology = "$root\examples\$Example\topology.yaml" }
if (-not (Test-Path $Topology)) { throw "Topology not found: $Topology" }
$cfg = Join-Path (Split-Path $Topology) "pulse.yaml"
if (Test-Path $cfg) { $env:PULSE_CONFIG = $cfg; Write-Host "config: $cfg" }
New-Item -ItemType Directory -Force "$root\data" | Out-Null
$server = Start-Process -FilePath $py -ArgumentList "-m", "apm.server" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardError "$root\data\server.log"
Start-Sleep 3
Write-Host "RapidPulse: http://127.0.0.1:4400  (server pid $($server.Id); stop with Stop-Process -Id $($server.Id))"
& $py -u "$root\simulator\simulate.py" --topology $Topology --url http://127.0.0.1:4400 --backfill-minutes $BackfillMinutes --rate $Rate
