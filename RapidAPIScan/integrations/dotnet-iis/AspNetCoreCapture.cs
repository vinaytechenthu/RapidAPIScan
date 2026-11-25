// RapidPulse request capture for ASP.NET Core (.NET 6 or later), on IIS or anywhere else.
//
// Adds the request payload to each server span created by the OpenTelemetry .NET agent:
//   http.request.body       the payload text (JSON, form, XML, text), sensitive fields masked
//   http.request.body.size  the payload size in bytes
// Request headers are captured by the agent itself
// (OTEL_DOTNET_AUTO_TRACES_ASPNETCORE_INSTRUMENTATION_CAPTURE_REQUEST_HEADERS).
//
// Loaded as a .NET startup hook: DOTNET_STARTUP_HOOKS=<path of this DLL> (after the OpenTelemetry
// hook, separated by ';'). install-request-capture.ps1 sets that on the IIS app pool. No change to the
// application or its web.config.
//
// How: it listens to ASP.NET Core's own "Microsoft.AspNetCore" diagnostic events. When a request with
// a payload starts, the request stream is wrapped so it keeps a copy of the first bytes AS THE
// APPLICATION READS THEM (the application gets exactly the same bytes, nothing is read ahead). When
// the request ends, just before its span closes, the copy is put on the span. Payloads the application
// never reads are recorded by size only; binary/multipart payloads by size only. Any failure is
// swallowed: capture never breaks a request.
//
// Settings (environment, e.g. on the app pool):
//   RAPIDPULSE_CAPTURE_BODY_BYTES  most bytes kept per request (default 8192; 0 = off)
//   RAPIDPULSE_CAPTURE_MASK        extra regex of field names to mask, e.g. "iban|account_no"
//
// Compiled against mscorlib only (C# 5, the compiler that ships with Windows); everything ASP.NET Core
// is reached through reflection, so one DLL works on .NET 6, 7, 8, 9 and later.
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

[assembly: AssemblyTitle("RapidPulse request capture for ASP.NET Core (startup hook)")]
[assembly: AssemblyVersion("1.1.0.0")]
[assembly: AssemblyFileVersion("1.1.0.0")]

/// <summary>Entry point required by DOTNET_STARTUP_HOOKS: a class named StartupHook, outside any namespace.</summary>
public static class StartupHook
{
    public static void Initialize()
    {
        try { RapidPulse.RequestCapture.AspNetCoreCapture.Start(); } catch { }
    }
}

namespace RapidPulse.RequestCapture
{
    public static class AspNetCoreCapture
    {
        const string StartKey = "Microsoft.AspNetCore.Hosting.HttpRequestIn.Start";
        const string StopKey = "Microsoft.AspNetCore.Hosting.HttpRequestIn.Stop";
        internal static int Max;
        internal static Regex Fields;
        static readonly ConditionalWeakTable<object, TeeStream> Open = new ConditionalWeakTable<object, TeeStream>();
        static readonly List<object> Keep = new List<object>();   // subscriptions stay alive for the process
        static bool started;

        public static void Start()
        {
            if (started) return;
            started = true;
            Max = Payload.ParseMax(Environment.GetEnvironmentVariable("RAPIDPULSE_CAPTURE_BODY_BYTES"));
            if (Max <= 0 || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("RAPIDPULSE_CAPTURE_DISABLED"))) return;
            Fields = Payload.Fields(Environment.GetEnvironmentVariable("RAPIDPULSE_CAPTURE_MASK"));
            Type dl = Type.GetType("System.Diagnostics.DiagnosticListener, System.Diagnostics.DiagnosticSource", false);
            if (dl == null) return;
            object all = dl.GetProperty("AllListeners", BindingFlags.Public | BindingFlags.Static).GetValue(null, null);
            object observer = Activator.CreateInstance(typeof(ListenerObserver<>).MakeGenericType(dl));
            object sub = typeof(IObservable<>).MakeGenericType(dl).GetMethod("Subscribe").Invoke(all, new[] { observer });
            lock (Keep) Keep.Add(sub);
        }

        /// <summary>Called for every DiagnosticListener in the process; subscribes to ASP.NET Core's.</summary>
        internal static void OnListener(object listener)
        {
            try
            {
                if (!"Microsoft.AspNetCore".Equals(Spans.Prop(listener, "Name"))) return;
                MethodInfo subscribe = listener.GetType().GetMethod("Subscribe",
                    new[] { typeof(IObserver<KeyValuePair<string, object>>), typeof(Predicate<string>) });
                if (subscribe == null) return;
                Predicate<string> wanted = n => n == StartKey || n == StopKey;
                object sub = subscribe.Invoke(listener, new object[] { new EventObserver(), wanted });
                lock (Keep) Keep.Add(sub);
            }
            catch { }
        }

        internal static void OnEvent(string name, object httpContext)
        {
            try
            {
                if (httpContext == null) return;
                if (name == StartKey) OnStart(httpContext);
                else if (name == StopKey) OnStop(httpContext);
            }
            catch { }
        }

        static void OnStart(object ctx)
        {
            object req = Spans.Prop(ctx, "Request");
            if (req == null) return;
            object len = Spans.Prop(req, "ContentLength");            // long? boxed: null or long
            string method = (Spans.Prop(req, "Method") as string) ?? "";
            bool hasBody = len is long ? (long)len > 0
                : (method == "POST" || method == "PUT" || method == "PATCH");   // chunked: length unknown
            if (!hasBody) return;
            PropertyInfo bodyProp = req.GetType().GetProperty("Body");
            Stream inner = bodyProp == null ? null : bodyProp.GetValue(req, null) as Stream;
            if (inner == null || inner is TeeStream) return;
            var tee = new TeeStream(inner, Max, Spans.Prop(req, "ContentType") as string, len is long ? (long)len : -1);
            bodyProp.SetValue(req, tee, null);
            Open.Remove(ctx);   // contexts are pooled and reused: replace any leftover entry
            Open.Add(ctx, tee);
        }

        static void OnStop(object ctx)
        {
            TeeStream tee;
            if (!Open.TryGetValue(ctx, out tee)) return;
            Open.Remove(ctx);
            object req = Spans.Prop(ctx, "Request");
            try
            {   // give the request its own stream back before the context is reused
                PropertyInfo bodyProp = req == null ? null : req.GetType().GetProperty("Body");
                if (bodyProp != null && ReferenceEquals(bodyProp.GetValue(req, null), tee)) bodyProp.SetValue(req, tee.Inner, null);
            }
            catch { }

            object span = Spans.Pick(Candidates(ctx));
            if (span == null || !Spans.Recording(span)) return;
            long total = Math.Max(tee.Seen, tee.Declared);
            if (total <= 0) return;
            string body = tee.Seen == 0
                ? Payload.SizeOnly(tee.ContentType, total, "not read by the application")
                : Payload.Describe(tee.ContentType, tee.Buffer, tee.Kept, total, Fields);
            Spans.Tag(span, "http.request.body", body);
            Spans.Tag(span, "http.request.body.size", total);
        }

        /// <summary>The request's activity (IHttpActivityFeature, .NET 6+), then Activity.Current.</summary>
        static IEnumerable<object> Candidates(object ctx)
        {
            var list = new List<object>();
            try
            {
                var features = Spans.Prop(ctx, "Features") as IEnumerable;
                if (features != null)
                    foreach (object kv in features)
                    {
                        var key = Spans.Prop(kv, "Key") as Type;
                        if (key != null && key.Name == "IHttpActivityFeature")
                        {
                            object a = Spans.Prop(Spans.Prop(kv, "Value"), "Activity");
                            if (a != null) list.Add(a);
                        }
                    }
            }
            catch { }
            list.AddRange(Spans.CurrentActivities());
            return list;
        }
    }

    public sealed class ListenerObserver<T> : IObserver<T>
    {
        public void OnNext(T value) { AspNetCoreCapture.OnListener(value); }
        public void OnError(Exception error) { }
        public void OnCompleted() { }
    }

    public sealed class EventObserver : IObserver<KeyValuePair<string, object>>
    {
        public void OnNext(KeyValuePair<string, object> e) { AspNetCoreCapture.OnEvent(e.Key, e.Value); }
        public void OnError(Exception error) { }
        public void OnCompleted() { }
    }

    /// <summary>Request stream stand-in: passes every read through and keeps a copy of the first bytes.</summary>
    public sealed class TeeStream : Stream
    {
        internal readonly Stream Inner;
        internal readonly byte[] Buffer;
        internal readonly string ContentType;
        internal readonly long Declared;
        internal int Kept;
        internal long Seen;

        public TeeStream(Stream inner, int max, string contentType, long declared)
        {
            Inner = inner;
            Buffer = new byte[max];
            ContentType = contentType ?? "";
            Declared = declared;
        }

        void Record(byte[] b, int offset, int n)
        {
            if (n <= 0) return;
            Seen += n;
            int c = Math.Min(n, Buffer.Length - Kept);
            if (c > 0) { System.Buffer.BlockCopy(b, offset, Buffer, Kept, c); Kept += c; }
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            int n = Inner.Read(buffer, offset, count);
            try { Record(buffer, offset, n); } catch { }
            return n;
        }

        public override async Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken)
        {
            int n = await Inner.ReadAsync(buffer, offset, count, cancellationToken).ConfigureAwait(false);
            try { Record(buffer, offset, n); } catch { }
            return n;
        }

        public override bool CanRead { get { return Inner.CanRead; } }
        public override bool CanSeek { get { return false; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { throw new NotSupportedException(); } }
        public override long Position { get { throw new NotSupportedException(); } set { throw new NotSupportedException(); } }
        public override void Flush() { }
        public override long Seek(long offset, SeekOrigin origin) { throw new NotSupportedException(); }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
        protected override void Dispose(bool disposing) { }   // the server owns the real request stream
    }
}
