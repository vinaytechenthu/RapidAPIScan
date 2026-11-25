// Shared by the ASP.NET (.NET Framework) module and the ASP.NET Core startup hook:
// payload formatting and masking, and finding the OpenTelemetry server span through reflection
// (so both work with whichever System.Diagnostics.DiagnosticSource version the agent loaded).
// C# 5 syntax and mscorlib-only references on purpose: built with the compiler that ships with Windows.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;

namespace RapidPulse.RequestCapture
{
    internal static class Payload
    {
        public const string Mask = "[redacted]";
        static readonly string[] TextTypes = { "json", "xml", "x-www-form-urlencoded", "text/", "graphql", "javascript", "yaml", "csv" };
        static readonly string[] DefaultFields = {
            @"pass(word|wd|phrase)?$", "secret", "token$", @"api[_-]?key", "authori[sz]ation", "credential",
            @"private[_-]?key", @"access[_-]?key", "cookie", @"session[_-]?id", "^cvv$|^cvc$", @"card[_-]?number", "^pin$" };
        static readonly Regex JsonPair = new Regex("\"((?:[^\"\\\\]|\\\\.){1,200})\"(\\s*:\\s*)(\"(?:[^\"\\\\]|\\\\.)*\"?|-?\\d[\\d.eE+-]*|true|false|null)");
        static readonly Regex FormPair = new Regex("(^|&)([^=&]{1,200})=([^&]*)");

        public static int ParseMax(string v)
        {
            int n;
            if (string.IsNullOrEmpty(v)) return 8192;
            return int.TryParse(v.Trim(), out n) ? Math.Max(n, 0) : 8192;
        }

        public static Regex Fields(string extra)
        {
            var sb = new StringBuilder();
            var parts = new List<string>(DefaultFields);
            if (!string.IsNullOrEmpty(extra)) parts.Add(extra);
            foreach (string p in parts) sb.Append(sb.Length > 0 ? "|" : "").Append("(?:").Append(p).Append(")");
            return new Regex(sb.ToString(), RegexOptions.IgnoreCase);
        }

        public static bool Textual(string ctype)
        {
            string c = (ctype ?? "").ToLowerInvariant();
            if (c.Length == 0) return true;
            foreach (string t in TextTypes) if (c.Contains(t)) return true;
            return false;
        }

        public static string SizeOnly(string ctype, long total, string why)
        {
            string kind = string.IsNullOrEmpty(ctype) ? "payload" : ctype.Split(';')[0].Trim() + " payload";
            return "[" + kind + ", " + total + " bytes, not captured" + (why == null ? "" : ": " + why) + "]";
        }

        /// <summary>The text recorded for a payload: masked text (truncated if longer than kept), or a size-only note.</summary>
        public static string Describe(string ctype, byte[] data, int kept, long total, Regex fields)
        {
            if (!Textual(ctype)) return SizeOnly(ctype, total, null);
            string text = Encoding.UTF8.GetString(data, 0, kept);
            if (total > kept) text += "… [truncated, " + total + " bytes in total]";
            return MaskFields(text, fields);
        }

        public static string MaskFields(string text, Regex f)
        {
            string t = text.TrimStart();
            if (t.StartsWith("{") || t.StartsWith("["))
                return JsonPair.Replace(text, m => f.IsMatch(m.Groups[1].Value)
                    ? "\"" + m.Groups[1].Value + "\"" + m.Groups[2].Value + "\"" + Mask + "\"" : m.Value);
            if (text.Contains("=") && !text.Trim().Contains("\n"))
                return FormPair.Replace(text, m => f.IsMatch(Uri.UnescapeDataString(m.Groups[2].Value.Replace('+', ' ')))
                    ? m.Groups[1].Value + m.Groups[2].Value + "=" + Mask : m.Value);
            return text;
        }
    }

    internal static class Spans
    {
        static PropertyInfo[] currents;

        /// <summary>Activity.Current of every loaded System.Diagnostics.DiagnosticSource (normally one).</summary>
        public static IEnumerable<object> CurrentActivities()
        {
            if (currents == null || currents.Length == 0)
            {
                var found = new List<PropertyInfo>();
                foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try
                    {
                        if (a.GetName().Name != "System.Diagnostics.DiagnosticSource") continue;
                        Type t = a.GetType("System.Diagnostics.Activity", false);
                        PropertyInfo p = t == null ? null : t.GetProperty("Current", BindingFlags.Public | BindingFlags.Static);
                        if (p != null) found.Add(p);
                    }
                    catch { }
                }
                currents = found.ToArray();
            }
            var list = new List<object>();
            foreach (PropertyInfo p in currents)
            {
                try { object v = p.GetValue(null, null); if (v != null) list.Add(v); } catch { }
            }
            return list;
        }

        public static object Prop(object o, string name)
        {
            try
            {
                PropertyInfo p = o.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.Instance);
                return p == null ? null : p.GetValue(o, null);
            }
            catch { return null; }
        }

        public static bool IsActivity(object o)
        {
            return o != null && o.GetType().GetMethod("SetTag", new[] { typeof(string), typeof(object) }) != null
                && o.GetType().GetProperty("Kind") != null;
        }

        static object ServerOf(object activity)
        {
            for (object a = activity; a != null; a = Prop(a, "Parent"))
            {
                object kind = Prop(a, "Kind");
                if (kind != null && kind.ToString() == "Server") return a;
            }
            return null;
        }

        /// <summary>The server span among the candidates (or an ancestor of one); else the first activity seen.
        /// RapidPulse moves a child span's payload onto the server span when it shows the trace.</summary>
        public static object Pick(IEnumerable<object> candidates)
        {
            object fallback = null;
            foreach (object c in candidates)
            {
                if (!IsActivity(c)) continue;
                object s = ServerOf(c);
                if (s != null) return s;
                if (fallback == null) fallback = c;
            }
            return fallback;
        }

        public static bool Recording(object activity)
        {
            object r = Prop(activity, "IsAllDataRequested");
            return !(r is bool) || (bool)r;
        }

        public static void Tag(object activity, string key, object value)
        {
            try
            {
                MethodInfo m = activity.GetType().GetMethod("SetTag", new[] { typeof(string), typeof(object) });
                if (m != null) { m.Invoke(activity, new[] { key, value }); return; }
                m = activity.GetType().GetMethod("AddTag", new[] { typeof(string), typeof(string) });
                if (m != null) m.Invoke(activity, new object[] { key, Convert.ToString(value) });
            }
            catch { }
        }
    }
}
