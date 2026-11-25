# Builds both capture DLLs with the C# compiler that ships with Windows (.NET Framework 4.x).
# No Visual Studio or .NET SDK needed.   powershell -ExecutionPolicy Bypass -File build.ps1
#   bin\RapidPulse.RequestCapture.dll             ASP.NET (.NET Framework): goes into the site's bin folder
#   bin\RapidPulse.RequestCapture.AspNetCore.dll  ASP.NET Core (.NET 6+): loaded via DOTNET_STARTUP_HOOKS
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$fw   = "$env:windir\Microsoft.NET\Framework64\v4.0.30319"
if (-not (Test-Path "$fw\csc.exe")) { $fw = "$env:windir\Microsoft.NET\Framework\v4.0.30319" }
New-Item -ItemType Directory -Force "$here\bin" | Out-Null

& "$fw\csc.exe" /nologo /target:library /optimize+ /out:"$here\bin\RapidPulse.RequestCapture.dll" `
    /reference:"$fw\System.Web.dll" /reference:"$fw\System.Configuration.dll" "$here\RequestCapture.cs" "$here\Common.cs"
if ($LASTEXITCODE -ne 0) { throw "Build failed (ASP.NET)" }

# mscorlib only: .NET 6+ maps it onto its own core library, so one DLL fits every .NET version.
& "$fw\csc.exe" /nologo /target:library /optimize+ /out:"$here\bin\RapidPulse.RequestCapture.AspNetCore.dll" `
    "$here\AspNetCoreCapture.cs" "$here\Common.cs"
if ($LASTEXITCODE -ne 0) { throw "Build failed (ASP.NET Core)" }
Write-Host "Built $here\bin\RapidPulse.RequestCapture.dll and RapidPulse.RequestCapture.AspNetCore.dll"
