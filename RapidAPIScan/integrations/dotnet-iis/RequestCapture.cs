// RapidPulse request capture for ASP.NET (.NET Framework 4.5+) on IIS.
// (ASP.NET Core applications use AspNetCoreCapture.cs instead; install-request-capture.ps1 picks the right one.)
//
// Adds the request payload to each server span created by the OpenTelemetry .NET agent:
//   http.request.body       the payload text (JSON, form, XML, text), sensitive fields masked
//   http.request.body.size  the payload size in bytes
// Request headers are captured by the agent itself
// (OTEL_DOTNET_AUTO_TRACES_ASPNET_INSTRUMENTATION_CAPTURE_REQUEST_HEADERS).
//
// Install: copy RapidPulse.RequestCapture.dll into the site's bin folder. ASP.NET loads it on start
// (PreApplicationStartMethod), so neither the application code nor web.config changes.
// It reads the payload only AFTER the application has handled the request, from ASP.NET's own
// buffer, and puts the read position back: the application sees exactly what it saw before.
// Payloads the application streamed without buffering, and binary/multipart payloads, are
// recorded by size only. Any failure is swallowed: capture never breaks a request.
//
// Settings (web.config <appSettings> first, then the environment, e.g. the app pool):
//   RAPIDPULSE_CAPTURE_BODY_BYTES  most bytes kept per request (default 8192; 0 = off)
//   RAPIDPULSE_CAPTURE_MASK        extra regex of field names to mask, e.g. "iban|account_no"
//
// Build: build.ps1 (uses the C# compiler that ships with Windows; C# 5 syntax on purpose).
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text.RegularExpressions;
using System.Web;
using System.Web.Configuration;

[assembly: PreApplicationStartMethod(typeof(RapidPulse.RequestCapture.Startup), "Start")]
[assembly: AssemblyTitle("RapidPulse request capture for ASP.NET")]
[assembly: AssemblyVersion("1.1.0.0")]
[assembly: AssemblyFileVersion("1.1.0.0")]

namespace RapidPulse.RequestCapture
{
    public static class Startup
    {
        static bool started;

        public static void Start()
        {
            if (started) return;
            started = true;
            try
            {
                if (Settings.MaxBytes > 0) HttpApplication.RegisterModule(typeof(CaptureModule));
            }
            catch { }
        }
    }

    public static class Settings
    {
        static int maxBytes = -1;
        static Regex fields;

        static string Get(string name)
        {
            string v = null;
            try { v = WebConfigurationManager.AppSettings[name]; } catch { }
            if (string.IsNullOrEmpty(v)) v = Environment.GetEnvironmentVariable(name);
            return v;
        }

        public static int MaxBytes
        {
            get { if (maxBytes < 0) maxBytes = Payload.ParseMax(Get("RAPIDPULSE_CAPTURE_BODY_BYTES")); return maxBytes; }
            set { maxBytes = value; }
        }

        public static Regex Fields
        {
            get { if (fields == null) fields = Payload.Fields(Get("RAPIDPULSE_CAPTURE_MASK")); return fields; }
        }
    }

    public sealed class CaptureModule : IHttpModule
    {
        const string DoneKey = "rapidpulse.capture.done";

        public void Init(HttpApplication app)
        {
            app.PostRequestHandlerExecute += OnStep;   // normal end of the application's work
            try { app.LogRequest += OnStep; } catch { } // also reached after errors (integrated pipeline)
            app.EndRequest += OnStep;                    // last chance
        }

        static void OnStep(object sender, EventArgs e)
        {
            try
            {
                HttpContext ctx = ((HttpApplication)sender).Context;
                if (ctx == null || ctx.Items[DoneKey] != null) return;
                if (Capture.Run(ctx)) ctx.Items[DoneKey] = true;
            }
            catch { }
        }

        public void Dispose() { }
    }

    public static class Capture
    {
        const long UnreadLimit = 1024 * 1024;   // payloads the application never read: only small ones are read

        /// <summary>Records the payload on the request's server span. False = span not found yet, try later.</summary>
        public static bool Run(HttpContext ctx)
        {
            HttpRequest req = ctx.Request;
            int max = Settings.MaxBytes;
            if (max <= 0) return true;
            long length = req.ContentLength;
            bool chunked = (req.Headers["Transfer-Encoding"] ?? "").IndexOf("chunked", StringComparison.OrdinalIgnoreCase) >= 0;
            if (length <= 0 && !chunked) return true;

            object span = Server(ctx);
            if (span == null) return false;
            if (!Spans.Recording(span)) return true;

            string ctype = req.ContentType ?? "";
            string body;
            long total = length;
            if (!Payload.Textual(ctype))
            {
                body = Payload.SizeOnly(ctype, length, null);
            }
            else
            {
                byte[] data;
                string why = Read(req, max, out data, out total);
                if (why != null) { body = Payload.SizeOnly(ctype, length, why); total = length; }
                else body = Payload.Describe(ctype, data, data.Length, total, Settings.Fields);
            }
            Spans.Tag(span, "http.request.body", body);
            if (total > 0) Spans.Tag(span, "http.request.body.size", total);
            return true;
        }

        static object Server(HttpContext ctx)
        {
            var candidates = new List<object>(Spans.CurrentActivities());
            // The ASP.NET instrumentation also keeps its activity in HttpContext.Items.
            try
            {
                foreach (DictionaryEntry e in ctx.Items)
                {
                    if (e.Value == null) continue;
                    candidates.Add(Spans.IsActivity(e.Value) ? e.Value : Spans.Prop(e.Value, "Activity"));
                }
            }
            catch { }
            return Spans.Pick(candidates);
        }

        /// <summary>Reads up to max bytes from ASP.NET's request buffer and restores the position. Returns why not, or null.</summary>
        static string Read(HttpRequest req, int max, out byte[] data, out long total)
        {
            data = null;
            total = 0;
            ReadEntityBodyMode mode = req.ReadEntityBodyMode;
            if (mode == ReadEntityBodyMode.Bufferless) return "the application streamed it unbuffered";
            if (mode == ReadEntityBodyMode.None && (req.ContentLength <= 0 || req.ContentLength > UnreadLimit))
                return "not read by the application";
            Stream s;
            try { s = req.InputStream; }
            catch { return "the application did not finish reading it"; }
            if (s == null || !s.CanSeek) return "not buffered";
            long pos = s.Position;
            try
            {
                s.Position = 0;
                var buf = new byte[(int)Math.Min(max, s.Length)];
                int n = 0, r;
                while (n < buf.Length && (r = s.Read(buf, n, buf.Length - n)) > 0) n += r;
                if (n < buf.Length) Array.Resize(ref buf, n);
                data = buf;
                total = s.Length;
                return null;
            }
            finally
            {
                try { s.Position = pos; } catch { }
            }
        }
    }
}
