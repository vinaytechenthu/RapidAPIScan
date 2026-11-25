# Request payload capture for ASP.NET and ASP.NET Core on IIS

The OpenTelemetry .NET agent records request **headers** (when listed in
`OTEL_DOTNET_AUTO_TRACES_ASPNET_INSTRUMENTATION_CAPTURE_REQUEST_HEADERS`) but never the request **payload**.
This add-on adds the payload to each server span, so the RapidPulse trace page shows it in the Request card:

| Attribute | Content |
|---|---|
| `http.request.body` | the payload text (JSON, form, XML, text), sensitive fields shown as `[redacted]` |
| `http.request.body.size` | the payload size in bytes |

## Install (IIS server, PowerShell as Administrator)

```powershell
.\install-request-capture.ps1                 # lists the IIS applications
.\install-request-capture.ps1 -App "<name>"   # installs for that application (exact name wins)
.\install-request-capture.ps1 -App "<name>" -Remove
```

The script detects the kind of application from its `web.config`:

| Application | What the script does |
|---|---|
| ASP.NET (.NET Framework 4.5+) | copies `RapidPulse.RequestCapture.dll` into the site's `bin` folder. ASP.NET loads it on start (`PreApplicationStartMethod`) and restarts that application by itself. |
| ASP.NET Core (.NET 6+) | copies `RapidPulse.RequestCapture.AspNetCore.dll` to `%ProgramFiles%\RapidPulse\request-capture`, adds it to `DOTNET_STARTUP_HOOKS` on the application's app pool after the OpenTelemetry agent's own hook, and recycles that app pool. |

The application code and `web.config` don't change.

## How it behaves

- ASP.NET: it reads the payload **after** the application has handled the request, from ASP.NET's own
  buffer, and puts the read position back. The application sees exactly the bytes it saw before.
- ASP.NET Core: it keeps a copy of the first bytes **as the application reads them** (nothing is read ahead)
  and puts it on the span when the request ends. A payload the application never reads is recorded by size.
- Binary and multipart payloads (file uploads) are recorded by size only. So are payloads the application
  streamed without buffering.
- Field names such as `password`, `token`, `api_key`, `secret` and `cookie` are masked before the span leaves
  the server. RapidPulse masks the payload again when it stores the trace.
- Any failure is swallowed, so capture never breaks a request.

## Settings

Set these as environment variables on the app pool (ASP.NET also reads them from `web.config` `<appSettings>` first):

| Setting | Default | Meaning |
|---|---|---|
| `RAPIDPULSE_CAPTURE_BODY_BYTES` | `8192` | most bytes kept per request; `0` = off |
| `RAPIDPULSE_CAPTURE_MASK` | (none) | extra regex of field names to mask, e.g. `iban\|account_no` |

## Build

Both DLLs are included in `bin\`. To rebuild them, run `build.ps1`. It uses the C# compiler that ships with
Windows, so no Visual Studio or .NET SDK is needed. The ASP.NET Core DLL references only `mscorlib` and reaches
ASP.NET Core through reflection, so the same file works on .NET 6, 7, 8, 9 and later.

ASP.NET needs .NET Framework 4.5 or later and the IIS integrated pipeline (the default).
