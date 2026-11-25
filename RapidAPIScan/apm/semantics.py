"""Turn one OTLP span into RapidPulse's normalised record.

This is where "generic" lives: everything is driven by OpenTelemetry semantic
conventions (HTTP, DB, GenAI, RPC/MCP) plus the OpenInference attributes many
LLM frameworks emit. No application names appear here. Any service that emits
OTLP — Python, .NET, Node, Java, Go — lands in the same model.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .otlp import attrs, norm_id

KIND_NAMES = {0: "unspecified", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}

DB_SYSTEM_LABEL = {
    "mssql": "SQL Server", "microsoft.sql_server": "SQL Server", "postgresql": "PostgreSQL",
    "mysql": "MySQL", "oracle": "Oracle", "redis": "Redis", "mongodb": "MongoDB",
    "sqlite": "SQLite", "elasticsearch": "Elasticsearch", "dynamodb": "DynamoDB",
}


def _first(a: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        v = a.get(k)
        if v not in (None, ""):
            return v
    return default


def _kind(raw: Any) -> str:
    if isinstance(raw, int):
        return KIND_NAMES.get(raw, "unspecified")
    s = str(raw or "").upper().replace("SPAN_KIND_", "")
    return s.lower() if s else "unspecified"


def _status_error(status: dict | None) -> tuple[bool, str]:
    status = status or {}
    code = status.get("code", 0)
    is_err = code in (2, "STATUS_CODE_ERROR", "ERROR")
    return is_err, status.get("message", "") or ""


_SQL_STR = re.compile(r"N?'(?:[^']|'')*'")
_SQL_NUM = re.compile(r"(?<![\w@#])-?\d+(?:\.\d+)?\b")
_SQL_INLIST = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")
_WS = re.compile(r"\s+")


def sql_fingerprint(sql: str) -> str:
    """Literal-free, whitespace-normalised statement: groups identical queries
    and keeps customer values out of the APM store."""
    s = _SQL_STR.sub("?", sql or "")
    s = _SQL_NUM.sub("?", s)
    s = _SQL_INLIST.sub("(?…)", s)
    return _WS.sub(" ", s).strip()[:2000]


URL_KEYS = {"url.full", "http.url", "http.target", "url.original"}
REQ_HEADER = "http.request.header."
REQ_BODY = "http.request.body"
# Attributes that only carry counts or RapidPulse's own notes: never secrets, even though
# their names contain "token" (gen_ai.usage.input_tokens, llm.token_count.prompt, ...).
SAFE_PREFIXES = ("gen_ai.usage.", "llm.token_count.", "rapidpulse.")
# Request headers whose values are always masked (in addition to the configured patterns).
SECRET_HEADER_PARTS = ("auth", "cookie", "key", "token", "secret", "session", "signature", "password", "credential")
DEFAULT_BODY_FIELDS = [r"pass(word|wd|phrase)?$", r"secret", r"token$", r"api[_-]?key", r"authori[sz]ation",
                       r"credential", r"private[_-]?key", r"access[_-]?key", r"cookie", r"session[_-]?id",
                       r"^cvv$|^cvc$", r"card[_-]?number", r"^pin$"]
MASK = "[redacted]"


def make_field_masker(patterns: list[str] | None):
    """Masks the values of sensitive fields inside a JSON or form-encoded request body.
    Works on truncated text too (regex, not a JSON parser), and keeps the original layout."""
    rx = re.compile("|".join(f"(?:{p})" for p in (patterns or DEFAULT_BODY_FIELDS)), re.I)
    json_pair = re.compile(r'"((?:[^"\\]|\\.){1,200})"(\s*:\s*)("(?:[^"\\]|\\.)*"?|-?\d[\d.eE+-]*|true|false|null)')
    form_pair = re.compile(r"(^|&)([^=&]{1,200})=([^&]*)")

    def mask(text: str) -> str:
        if not text:
            return text
        if text.lstrip()[:1] in "{[":
            return json_pair.sub(lambda m: f'"{m[1]}"{m[2]}"{MASK}"' if rx.search(m[1]) else m[0], text)
        if "=" in text and "\n" not in text.strip():
            return form_pair.sub(lambda m: f"{m[1]}{m[2]}={MASK}" if rx.search(m[2]) else m[0], text)
        return text
    return mask


def mask_query(url: str, masker) -> str:
    base, _, query = url.partition("?")
    return f"{base}?{masker(query)}" if query else url


def make_redactor(patterns: list[str], security: dict | None = None):
    """Attribute redaction applied to every span, event and log record before it is stored."""
    sec = security or {}
    pats = [p.lower() for p in patterns or []]
    masker = make_field_masker(sec.get("redact_body_fields"))
    max_body = int(sec.get("max_request_body_bytes", 16384))
    query_mode = str(sec.get("query_strings", "mask")).lower()     # mask | redact | keep

    def redact(a: dict) -> dict:
        out = {}
        for k, v in a.items():
            lk = k.lower()
            if lk.startswith(SAFE_PREFIXES):
                out[k] = v
            elif lk.startswith(REQ_HEADER) or lk.startswith("http.response.header."):
                name = lk.split(".header.", 1)[1]
                secret = any(p in name for p in SECRET_HEADER_PARTS) or any(p in name for p in pats)
                out[k] = MASK if secret else v
            elif lk == REQ_BODY:
                text = v if isinstance(v, str) else json.dumps(v, default=str)
                if max_body <= 0:
                    continue
                if len(text) > max_body:
                    text = text[:max_body] + f"… [truncated at {max_body} characters]"
                out[k] = masker(text)
            elif any(p in lk for p in pats):
                out[k] = MASK
            elif lk == "url.query" and isinstance(v, str):
                out[k] = v if query_mode == "keep" else MASK if query_mode == "redact" else masker(v)
            elif lk in URL_KEYS and isinstance(v, str) and "?" in v:
                # query strings carry ids and tokens: mask sensitive parameters (or all of it)
                out[k] = (v if query_mode == "keep" else v.split("?", 1)[0] + "?" + MASK if query_mode == "redact"
                          else mask_query(v, masker))
            else:
                out[k] = v
        return out
    return redact


def dump_capped(d: dict, limit: int) -> str:
    """JSON for storage, kept under `limit` characters by shortening the longest string values
    (cutting the JSON text itself would make it unreadable)."""
    s = json.dumps(d, default=str)
    if len(s) <= limit:
        return s
    d = dict(d)
    for _ in range(50):
        longest = max((k for k, v in d.items() if isinstance(v, str)), key=lambda k: len(d[k]), default=None)
        if longest is None:
            break
        over = len(s) - limit
        keep = max(200, len(d[longest]) - over - 100)
        if keep >= len(d[longest]):
            d.pop(longest)
        else:
            d[longest] = d[longest][:keep] + "… [truncated]"
        s = json.dumps(d, default=str)
        if len(s) <= limit:
            return s
    return json.dumps({k: v for k, v in d.items() if not isinstance(v, str) or len(v) < 200}, default=str)[:limit]


_REGION_PREFIX = re.compile(r"^(?:[a-z]{2,6}\.)+(?=[a-z]+\.)")       # us. eu. apac. global. (cloud inference profiles)
_VENDOR_PREFIX = re.compile(r"^(?:anthropic|openai|meta|amazon|cohere|mistral|ai21|google)[./]")
_VERSION_SUFFIX = re.compile(r"(?:[-@]\d{8})?(?:-v\d+(?::\d+)?)?$")    # -20251001, @20251001, -v1:0
_OLD_CLAUDE = re.compile(r"claude-(\d)(?:-(\d))?-(opus|sonnet|haiku)")  # claude-3-5-sonnet -> claude-sonnet-3-5


def normalise_model(model: str) -> str:
    """Provider model id -> comparable name. Handles cloud-provider ids and inference-profile ARNs
    (`arn:...:inference-profile/us.anthropic.claude-opus-5-5-v1:0`), vendor prefixes, date and
    version suffixes, and the older `claude-3-5-sonnet` word order."""
    m = (model or "").strip().lower().rsplit("/", 1)[-1]
    m = _REGION_PREFIX.sub("", m)
    m = _VENDOR_PREFIX.sub("", m)
    m = _VERSION_SUFFIX.sub("", m)
    return _OLD_CLAUDE.sub(lambda x: f"claude-{x[3]}-{x[1]}" + (f"-{x[2]}" if x[2] else ""), m)


_FAMILY_VERSION = re.compile(r"^(?:[a-z]+-)?([a-z]+)-(\d+)(?:-(\d{1,2}))?$")


def model_label(model: str) -> str:
    """Short display name: `us.anthropic.claude-opus-5-5-v1:0` -> `Opus 5.5`; unknown shapes stay as given."""
    norm = normalise_model(model)
    m = _FAMILY_VERSION.match(norm)
    if m and m[1] not in ("gpt", "v"):
        return f"{m[1].capitalize()} {m[2]}" + (f".{m[3]}" if m[3] else "")
    return norm or (model or "")


def price_row(model: str, pricing: list[dict]) -> dict | None:
    """The most specific pricing row for a model: the longest `match` text contained in the
    normalised model id (so `claude-opus-5-5` beats `claude-opus-5`, whatever the order)."""
    raw, norm = (model or "").lower(), normalise_model(model)
    best, best_len = None, -1
    for row in pricing or []:
        pat = str(row.get("match", "")).lower()
        if (pat in norm or pat in raw) and len(pat) > best_len:
            best, best_len = row, len(pat)
    return best


def price_for(model: str, pricing: list[dict]) -> tuple[float, float]:
    row = price_row(model, pricing)
    return (float(row.get("input", 0)), float(row.get("output", 0))) if row else (0.0, 0.0)


def llm_cost(model: str, tokens_in: int, tokens_out: int, cache_read: int, cache_write: int,
             pricing: list[dict], cache_in_input: str = "auto") -> tuple[float | None, int]:
    """USD cost of one LLM call from its token counts, and the total input tokens.

    Prices are per 1M tokens. Cached input is priced separately: reads at `cache_read`
    (default 0.1 x input), writes at `cache_write` (default 1.25 x input, the 5-minute cache).
    Some instrumentations include cached tokens in the input count (OpenInference, OTel GenAI),
    others report them separately (the provider APIs): `cache_in_input` is true/false, or
    "auto" = included unless the cache counts are larger than the input count.
    Returns (None, total_input) when the model has no pricing row."""
    row = price_row(model, pricing)
    cached = cache_read + cache_write
    if str(cache_in_input).lower() in ("true", "yes", "1"):
        included = True
    elif str(cache_in_input).lower() in ("false", "no", "0"):
        included = False
    else:
        included = cached > 0 and tokens_in >= cached
    uncached = max(0, tokens_in - cached) if included else tokens_in
    total_in = uncached + cached
    if row is None:
        return None, total_in
    pin, pout = float(row.get("input", 0)), float(row.get("output", 0))
    for tier in sorted(row.get("tiers") or [], key=lambda t: t.get("above_input_tokens", 0)):
        if total_in > int(tier.get("above_input_tokens", 0)):     # long-prompt pricing
            pin, pout = float(tier.get("input", pin)), float(tier.get("output", pout))
    cr = float(row["cache_read"]) if row.get("cache_read") is not None else pin * 0.1
    cw = float(row["cache_write"]) if row.get("cache_write") is not None else pin * 1.25
    cost = (uncached * pin + cache_read * cr + cache_write * cw + tokens_out * pout) / 1_000_000
    return cost, total_in


def normalise_span(res: dict, scope: str, sp: dict, *, cfg: dict, redact) -> dict:
    a = attrs(sp.get("attributes"))
    kind = _kind(sp.get("kind"))
    start = int(sp.get("startTimeUnixNano", 0) or 0)
    end = int(sp.get("endTimeUnixNano", 0) or start)
    dur_ms = max(0.0, (end - start) / 1e6)
    name = sp.get("name", "") or ""
    parent = norm_id(sp.get("parentSpanId"))
    is_err, status_msg = _status_error(sp.get("status"))

    service = str(res.get("service.name") or "unknown_service")
    rec: dict[str, Any] = {
        "trace_id": norm_id(sp.get("traceId")),
        "span_id": norm_id(sp.get("spanId")),
        "parent_id": parent,
        "app": str(_first(res, "service.namespace", "application.name", "app.name", default="default")),
        "env": str(_first(res, "deployment.environment.name", "deployment.environment", default="")),
        "service": service,
        "service_version": str(res.get("service.version") or ""),
        "host": str(_first(res, "host.name", "host.id", default="")),
        "container": str(_first(res, "container.name", "k8s.pod.name", default="")),
        "name": name,
        "kind": kind,
        "start_ns": start,
        "end_ns": end,
        "duration_ms": round(dur_ms, 3),
        "http_method": None, "http_route": None, "http_status": None,
        "db_system": None, "db_statement": None,
        "llm_model": None, "llm_provider": None,
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
        "category": "internal", "operation": name, "peer": None, "peer_type": None,
        "error_type": None, "error_message": None,
    }

    # ---- HTTP -------------------------------------------------------------
    method = _first(a, "http.request.method", "http.method")
    status = _first(a, "http.response.status_code", "http.status_code")
    route = _first(a, "http.route", "url.template")
    target = _first(a, "url.path", "http.target")
    if method or status:
        rec["category"] = "http"
        rec["http_method"] = str(method or "")
        rec["http_status"] = int(status) if status not in (None, "") else None
        rec["http_route"] = str(route or (str(target).split("?")[0] if target else "") or "")
        if kind == "server" or (not parent and kind != "client"):
            rec["operation"] = f"{rec['http_method']} {rec['http_route'] or name}".strip()
        else:
            peer = _first(a, "peer.service", "server.address", "net.peer.name", "http.host")
            if not peer and _first(a, "url.full", "http.url"):
                peer = re.sub(r"^\w+://([^/:]+).*$", r"\1", str(_first(a, "url.full", "http.url")))
            rec["peer"], rec["peer_type"] = (str(peer) if peer else None), "http"
            rec["operation"] = f"{rec['http_method']} {rec['http_route'] or peer or name}".strip()
        sc = rec["http_status"] or 0
        if sc >= 500 or (kind == "client" and sc >= 400):
            is_err = True

    # ---- Database ---------------------------------------------------------
    db_system = _first(a, "db.system", "db.system.name")
    if db_system:
        stmt = str(_first(a, "db.query.text", "db.statement", default="") or "")
        if cfg["security"].get("sanitize_sql", True):
            stmt = sql_fingerprint(stmt)
        db_name = _first(a, "db.namespace", "db.name", default="")
        rec.update(category="db", db_system=str(db_system), db_statement=stmt or None)
        label = DB_SYSTEM_LABEL.get(str(db_system).lower(), str(db_system))
        target_host = _first(a, "peer.service", "server.address", "net.peer.name", default="")
        rec["peer"] = f"{label}" + (f" ({db_name})" if db_name else (f" ({target_host})" if target_host else ""))
        rec["peer_type"] = "database"
        op = _first(a, "db.operation.name", "db.operation", default="")
        rec["operation"] = (stmt[:160] if stmt else f"{op} {db_name}".strip()) or name

    # ---- MCP / tool execution (checked before LLM: a tool span may carry gen_ai.*)
    oi_kind = str(a.get("openinference.span.kind", "") or "").upper()
    mcp_method = _first(a, "mcp.method.name", "mcp.method")
    tool = _first(a, "gen_ai.tool.name", "mcp.tool.name", "tool.name")
    if (mcp_method or str(a.get("rpc.system", "")).lower() == "mcp" or oi_kind == "TOOL"
            or name.startswith(("tools/call", "mcp.", "mcp "))):
        rec["category"] = "mcp"
        if kind != "server":  # the server side of a tool call *is* the MCP service, not a peer of it
            rec["peer"] = str(_first(a, "mcp.server.name", "peer.service", "server.address", default="MCP"))
            rec["peer_type"] = "mcp"
        else:
            rec["peer"] = rec["peer_type"] = None
        rec["operation"] = str(tool or mcp_method or name)

    # ---- LLM / GenAI --------------------------------------------------------
    # Standard names first (OTel GenAI, then OpenInference), then any application-specific
    # names declared under `attribute_aliases:` in the config.
    al = cfg.get("attribute_aliases") or {}
    model = _first(a, "gen_ai.response.model", "gen_ai.request.model", "llm.model_name", "ai.model.id",
                   *al.get("llm_model", []))
    provider = _first(a, "gen_ai.system", "gen_ai.provider.name", "llm.provider", "llm.system",
                      *al.get("llm_provider", []))
    in_keys = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt",
               *al.get("llm_input_tokens", []))
    out_keys = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.token_count.completion",
                *al.get("llm_output_tokens", []))
    read_keys = ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read_input_tokens",
                 "llm.token_count.prompt_details.cache_read", *al.get("llm_cache_read_tokens", []))
    write_keys = ("gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.cache_creation_input_tokens",
                  "gen_ai.usage.cache_write_input_tokens", "llm.token_count.prompt_details.cache_write",
                  *al.get("llm_cache_write_tokens", []))
    if rec["category"] != "mcp" and (oi_kind == "LLM" or provider or (model and _first(a, *in_keys) is not None)):
        num = lambda keys: int(float(_first(a, *keys, default=0) or 0))
        tin, tout, cread, cwrite = num(in_keys), num(out_keys), num(read_keys), num(write_keys)
        model = str(model or "unknown-model")
        computed, tin = llm_cost(model, tin, tout, cread, cwrite, cfg.get("llm_pricing", []),
                                 cfg.get("llm_cache_tokens_in_input", "auto"))
        reported = _first(a, "gen_ai.usage.cost", "llm.cost.total", *al.get("llm_cost", []))
        try:
            reported = float(reported) if reported not in (None, "") else None
        except (TypeError, ValueError):
            reported = None
        # Which cost wins: RapidPulse's price table ("pricing", default) or a cost the span reports ("span").
        prefer_span = str(cfg.get("llm_cost_source", "pricing")).lower() == "span"
        if reported is not None and (prefer_span or computed is None):
            cost, source = reported, "span"
        elif computed is not None:
            cost, source = computed, "pricing"
        else:
            cost, source = 0.0, "unpriced"
        a["rapidpulse.cost_source"] = source
        if cread or cwrite:
            a["rapidpulse.cache_tokens"] = f"{cread} read / {cwrite} written"
        rec.update(category="llm", llm_model=model, llm_provider=str(provider or "llm"),
                   tokens_in=tin, tokens_out=tout, cost_usd=round(float(cost), 6),
                   peer=f"LLM ({provider})" if provider else "LLM", peer_type="llm",
                   operation=model)

    # ---- Errors -----------------------------------------------------------
    events = []
    for ev in sp.get("events", []) or []:
        ea = attrs(ev.get("attributes"))
        if ev.get("name") == "exception":
            rec["error_type"] = rec["error_type"] or ea.get("exception.type")
            rec["error_message"] = rec["error_message"] or ea.get("exception.message")
            is_err = True
        events.append({"name": ev.get("name"), "time_ns": int(ev.get("timeUnixNano", 0) or 0),
                       "attributes": redact(ea)})
    if a.get("error.type") and is_err:
        rec["error_type"] = rec["error_type"] or str(a["error.type"])
    if is_err and not rec["error_message"]:
        rec["error_message"] = status_msg or (f"HTTP {rec['http_status']}" if rec["http_status"] else "error")
    rec["status"] = "error" if is_err else "ok"
    rec["is_entry"] = 1 if (kind in ("server", "consumer") or not parent) else 0
    rec["attributes"] = dump_capped(redact(a), 60000)
    rec["resource"] = json.dumps(res, default=str)[:5000]
    rec["events"] = json.dumps(events, default=str)[:20000] if events else None
    rec["scope"] = scope
    return rec


def latency_bucket(ms: float) -> int:
    """Log-scale histogram bucket index (~25% resolution, 0.5 ms .. ~2 h)."""
    import math
    if ms <= 0.5:
        return 0
    return min(79, int(math.log(ms / 0.5, 1.25)) + 1)


def bucket_upper(i: int) -> float:
    return 0.5 * (1.25 ** i)
