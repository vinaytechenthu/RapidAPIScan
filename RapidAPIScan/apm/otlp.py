"""OTLP/HTTP decoding (protobuf and JSON, optionally gzip) into one dict shape.

Both encodings are turned into the canonical OTLP/JSON structure
(`resourceSpans[].scopeSpans[].spans[]`, camelCase keys). Identifiers are
normalised to lower-case hex whether the sender used hex (OTLP/JSON spec) or
base64 (protobuf -> MessageToDict, and some non-conforming JSON senders).
"""
from __future__ import annotations

import base64
import binascii
import gzip
import json
import re
import zlib
from typing import Any

_HEX = re.compile(r"^[0-9a-fA-F]+$")

SIGNALS = {
    "traces": ("opentelemetry.proto.collector.trace.v1.trace_service_pb2", "ExportTraceServiceRequest"),
    "metrics": ("opentelemetry.proto.collector.metrics.v1.metrics_service_pb2", "ExportMetricsServiceRequest"),
    "logs": ("opentelemetry.proto.collector.logs.v1.logs_service_pb2", "ExportLogsServiceRequest"),
}


class UnsupportedPayload(ValueError):
    pass


def decompress(body: bytes, encoding: str | None) -> bytes:
    enc = (encoding or "").lower()
    if enc == "gzip" or body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    if enc == "deflate":
        return zlib.decompress(body)
    return body


def decode(signal: str, body: bytes, content_type: str | None, encoding: str | None = None) -> dict:
    body = decompress(body, encoding)
    ctype = (content_type or "").lower()
    if "json" in ctype or (not ctype and body[:1] in (b"{", b"[")):
        return json.loads(body.decode("utf-8") or "{}")
    if "protobuf" in ctype or "x-protobuf" in ctype or not ctype:
        try:
            import importlib
            from google.protobuf.json_format import MessageToDict
        except ImportError as e:  # pragma: no cover
            raise UnsupportedPayload("protobuf support not installed (pip install opentelemetry-proto)") from e
        mod_name, cls_name = SIGNALS[signal]
        msg = getattr(importlib.import_module(mod_name), cls_name)()
        msg.ParseFromString(body)
        return MessageToDict(msg)
    raise UnsupportedPayload(f"unsupported content-type {content_type!r}")


def norm_id(value: Any) -> str:
    """Hex trace/span id from hex or base64; '' for empty/all-zero."""
    if not value:
        return ""
    s = str(value)
    if _HEX.match(s) and len(s) in (16, 32):
        out = s.lower()
    else:
        try:
            out = binascii.hexlify(base64.b64decode(s + "=" * (-len(s) % 4))).decode()
        except (binascii.Error, ValueError):
            out = s.lower()
    return "" if set(out) <= {"0"} else out


def any_value(v: dict | None) -> Any:
    """OTLP AnyValue -> plain Python."""
    if not v:
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [any_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return attrs(v["kvlistValue"].get("values", []))
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def attrs(kvs: list | None) -> dict:
    return {kv.get("key"): any_value(kv.get("value")) for kv in (kvs or []) if kv.get("key")}


def iter_spans(payload: dict):
    """Yield (resource_attrs, scope_name, span_dict)."""
    for rs in payload.get("resourceSpans", []) or []:
        res = attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans", []) or rs.get("instrumentationLibrarySpans", []) or []:
            scope = (ss.get("scope") or ss.get("instrumentationLibrary") or {}).get("name", "")
            for sp in ss.get("spans", []) or []:
                yield res, scope, sp


def iter_logs(payload: dict):
    for rl in payload.get("resourceLogs", []) or []:
        res = attrs((rl.get("resource") or {}).get("attributes"))
        for sl in rl.get("scopeLogs", []) or []:
            for rec in sl.get("logRecords", []) or []:
                yield res, rec


def iter_metric_points(payload: dict):
    """Yield (resource_attrs, metric_name, unit, ts_ns, value, point_attrs).

    Gauges and sums give their value; histograms give their mean (sum/count) so
    every metric can be charted on one axis. The raw distribution of a
    histogram is not kept — request latency comes from spans, not metrics.
    """
    for rm in payload.get("resourceMetrics", []) or []:
        res = attrs((rm.get("resource") or {}).get("attributes"))
        for sm in rm.get("scopeMetrics", []) or []:
            for m in sm.get("metrics", []) or []:
                name, unit = m.get("name", ""), m.get("unit", "")
                for kind in ("gauge", "sum"):
                    for dp in (m.get(kind) or {}).get("dataPoints", []) or []:
                        val = dp.get("asDouble", dp.get("asInt"))
                        if val is None:
                            continue
                        yield res, name, unit, int(dp.get("timeUnixNano", 0)), float(val), attrs(dp.get("attributes"))
                for dp in (m.get("histogram") or {}).get("dataPoints", []) or []:
                    count = int(dp.get("count", 0) or 0)
                    if count:
                        yield (res, name, unit, int(dp.get("timeUnixNano", 0)),
                               float(dp.get("sum", 0.0)) / count, attrs(dp.get("attributes")))
