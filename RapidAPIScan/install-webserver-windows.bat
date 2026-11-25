@echo off
rem RapidPulse setup for a Windows web server (IIS): host agent + .NET tracing + request capture.
rem   1. Fill in rapidpulse-webserver.yaml (next to this file).
rem   2. Double-click this file and accept the Administrator prompt.
rem Remove everything:          install-webserver-windows.bat -Uninstall
rem Use another settings file:  install-webserver-windows.bat -ConfigFile D:\path\my-server.yaml
rem Needs these folders next to it: deploy, agent, integrations\dotnet-iis

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting Administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  exit /b
)

set "CONF=%~dp0rapidpulse-webserver.yaml"
echo %* | findstr /i /c:"-ConfigFile" >nul && set "CONF="
if defined CONF (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\install-webserver.ps1" -ConfigFile "%CONF%" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\install-webserver.ps1" %*
)
echo.
pause
