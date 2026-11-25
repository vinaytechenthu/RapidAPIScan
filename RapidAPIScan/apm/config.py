"""Configuration: one YAML file (config/pulse.yaml) plus PULSE_* environment overrides.

Nothing in here is specific to one application. Application-specific knowledge
(display names, tiers, which host runs what) is *data* in the config file.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 4400},
    "storage": {
        "path": str(ROOT / "data" / "pulse.db"),
        "raw_retention_hours": 72,        # spans, logs
        "rollup_retention_days": 90,      # 1-minute aggregates (history)
        "infra_retention_days": 30,
    },
    "security": {
        # Empty list = ingest is open (fine on a private VPC). Otherwise every
        # ingest call must send `Authorization: Bearer <key>` or `X-Pulse-Key`.
        "ingest_api_keys": [],
        # Empty = UI/API open. Otherwise HTTP basic auth, user -> password.
        "ui_users": {},
        # Attribute keys whose values are always replaced by "[redacted]".
        "redact_attribute_patterns": ["authorization", "password", "secret", "token", "cookie", "api_key", "apikey"],
        # Strip literals from SQL before storing (keeps PII out of the APM).
        "sanitize_sql": True,
        # Request payloads (attribute http.request.body): longest body kept, in characters (0 = never store).
        "max_request_body_bytes": 16384,
        # Body / query-string fields whose values are masked (regular expressions, case-insensitive).
        # Empty = the built-in list (password, secret, *token, api_key, authorization, credential, ...).
        "redact_body_fields": [],
        # URL query strings: mask = mask sensitive parameters only, redact = hide the whole query, keep.
        "query_strings": "mask",
    },
    "thresholds": {
        "slow_query_ms": 1000,
        "slow_request_ms": 3000,
        "apdex_t_ms": 500,
    },
    # LLM prices in USD per 1M tokens. The most specific row wins: the longest `match` text found in
    # the model id after removing cloud-provider region/vendor prefixes and version suffixes
    # (us.anthropic.claude-opus-5-5-v1:0 -> claude-opus-5-5). Optional per row:
    #   cache_read  (default 0.1 x input)   cache_write (default 1.25 x input, 5-minute cache)
    #   tiers: [{above_input_tokens: N, input: .., output: ..}]   prices for long prompts
    # Models without a row are shown as "not priced" (cost 0) rather than guessed.
    # Anthropic first-party API list prices as of November 2026; cloud providers (AWS, Google Cloud)
    # set their own prices, so check them if you call Claude through a cloud provider.
    "llm_pricing_as_of": "2026-11",
    "llm_pricing": [
        {"match": "claude-fable-5-2", "input": 8.0, "output": 40.0, "cache_read": 0.20},
        {"match": "claude-mythos-5-2", "input": 8.0, "output": 40.0, "cache_read": 0.20},
        {"match": "claude-fable-5-1", "input": 10.0, "output": 50.0, "cache_read": 0.25},
        {"match": "claude-mythos-5-1", "input": 10.0, "output": 50.0, "cache_read": 0.25},
        {"match": "claude-fable-5", "input": 10.0, "output": 50.0, "cache_read": 1.0},
        {"match": "claude-mythos-5", "input": 10.0, "output": 50.0, "cache_read": 1.0},
        {"match": "claude-opus-5-5", "input": 4.0, "output": 20.0, "cache_read": 0.20},
        {"match": "claude-opus-5", "input": 5.0, "output": 25.0},
        {"match": "claude-opus-4-8", "input": 5.0, "output": 25.0},
        {"match": "claude-opus-4-7", "input": 5.0, "output": 25.0},
        {"match": "claude-opus-4-6", "input": 5.0, "output": 25.0},
        {"match": "claude-opus-4-5", "input": 5.0, "output": 25.0},
        {"match": "claude-opus-4", "input": 15.0, "output": 75.0},        # Opus 4 and 4.1
        {"match": "claude-opus-3", "input": 15.0, "output": 75.0},
        {"match": "claude-sonnet-5-5", "input": 2.0, "output": 10.0, "cache_read": 0.10},
        {"match": "claude-sonnet-5", "input": 2.0, "output": 10.0},
        {"match": "claude-sonnet-4", "input": 3.0, "output": 15.0},       # Sonnet 4, 4.5, 4.6
        {"match": "claude-sonnet-3", "input": 3.0, "output": 15.0},
        {"match": "claude-haiku-5-5", "input": 0.10, "output": 0.50,
         "tiers": [{"above_input_tokens": 100000, "input": 0.50, "output": 2.50}]},
        {"match": "claude-haiku-4-5", "input": 1.0, "output": 5.0},
        {"match": "claude-haiku-3-5", "input": 0.80, "output": 4.0},
        {"match": "claude-haiku-3", "input": 0.25, "output": 1.25},
        # Other providers: not verified against current price lists; adjust to your contract.
        {"match": "gpt-4o-mini", "input": 0.15, "output": 0.6},
        {"match": "gpt-4o", "input": 2.5, "output": 10.0},
        {"match": "titan-embed", "input": 0.02, "output": 0.0},
    ],
    # Which cost is shown when a span also reports its own cost (gen_ai.usage.cost or an llm_cost alias):
    #   pricing = RapidPulse's table above (default; the span's own figure is used only for unpriced models)
    #   span    = the span's own figure
    "llm_cost_source": "pricing",
    # Whether the input-token count already includes cached tokens: auto | true | false.
    "llm_cache_tokens_in_input": "auto",
    # Optional cosmetic metadata per service (keyed by service.name).
    "services": {},
    # Extra attribute names to read when an application uses its own names instead of the
    # OpenTelemetry GenAI / OpenInference conventions. Keys: llm_model, llm_provider,
    # llm_input_tokens, llm_output_tokens, llm_cache_read_tokens, llm_cache_write_tokens, llm_cost
    # — each a list of attribute names.
    "attribute_aliases": {},
    # Central synthetic monitors (availability / connectivity).
    "monitors": [],
    "monitor_interval_seconds": 30,
    # Built-in alert rules seeded on first start (editable in the UI).
    "default_alert_rules": True,
    "notifications": {
        "webhooks": [],          # generic JSON / Slack / Teams incoming webhooks
        "smtp": None,            # {host, port, user, password, from, to: [..], starttls}
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def merge_pricing(builtin: list[dict], user: list[dict]) -> list[dict]:
    """Rows from the config file add to the built-in price table; a row with the same `match`
    replaces the built-in one. A catch-all row (match "") is ignored: an unknown model should
    show as "not priced", not as a guess."""
    rows = {str(r.get("match", "")).lower(): dict(r) for r in builtin}
    for r in user or []:
        if isinstance(r, dict) and str(r.get("match", "")).strip():
            rows[str(r["match"]).lower()] = dict(r)
    return list(rows.values())


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("PULSE_CONFIG") or str(ROOT / "config" / "pulse.yaml")
    data: dict = {}
    if Path(path).exists():
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    cfg = _merge(DEFAULTS, data)
    user_prices = list(data.get("llm_pricing") or [])
    # Machine-specific settings and secrets (written by the installer) live next to the
    # main file in pulse.local.yaml and override it, so pulse.yaml is never rewritten.
    local = Path(path).with_name(Path(path).stem + ".local.yaml")
    if local.exists():
        with open(local, "r", encoding="utf-8") as fh:
            local_data = yaml.safe_load(fh) or {}
        cfg = _merge(cfg, local_data)
        user_prices += list(local_data.get("llm_pricing") or [])
    cfg["llm_pricing"] = merge_pricing(DEFAULTS["llm_pricing"], user_prices)
    # Environment overrides for the things ops most often change per box.
    if os.environ.get("PULSE_DB"):
        cfg["storage"]["path"] = os.environ["PULSE_DB"]
    if os.environ.get("PULSE_PORT"):
        cfg["server"]["port"] = int(os.environ["PULSE_PORT"])
    if os.environ.get("PULSE_INGEST_KEYS"):
        cfg["security"]["ingest_api_keys"] = [k.strip() for k in os.environ["PULSE_INGEST_KEYS"].split(",") if k.strip()]
    return cfg
