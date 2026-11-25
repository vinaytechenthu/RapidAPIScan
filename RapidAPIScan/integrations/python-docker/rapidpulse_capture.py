"""RapidPulse request capture for OpenTelemetry zero-code instrumentation (Python).

Adds the request payload to each server span, so a trace shows what the client sent:
  http.request.body       the payload text (JSON, form, XML, text), sensitive fields masked
  http.request.body.size  the payload size in bytes
Request headers are captured by OpenTelemetry itself (OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*
variables, set by the RapidPulse Dockerfile).

Loaded at interpreter start by rapidpulse_capture.pth. It changes nothing until the web stack is
imported, and only wraps the request stream as the application reads it: the application gets
exactly the same bytes. Covers ASGI apps instrumented by OpenTelemetry (FastAPI, Starlette, ...),
Flask and Django (WSGI). Binary and multipart payloads are recorded by size only.

Settings (environment):
  RAPIDPULSE_CAPTURE_BODY_BYTES  most bytes kept per request (default 8192; 0 = off)
  RAPIDPULSE_CAPTURE_MASK        extra regex of field names to mask, e.g. "iban|account_no"
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import os
import re
import sys

try:
    MAX = int(os.environ.get("RAPIDPULSE_CAPTURE_BODY_BYTES", "8192") or 0)
except ValueError:
    MAX = 8192
TEXT_TYPES = ("json", "xml", "x-www-form-urlencoded", "text/", "graphql", "javascript", "yaml", "csv")
FIELDS = [r"pass(word|wd|phrase)?$", r"secret", r"token$", r"api[_-]?key", r"authori[sz]ation", r"credential",
          r"private[_-]?key", r"access[_-]?key", r"cookie", r"session[_-]?id", r"^cvv$|^cvc$", r"card[_-]?number", r"^pin$"]
if os.environ.get("RAPIDPULSE_CAPTURE_MASK"):
    FIELDS.append(os.environ["RAPIDPULSE_CAPTURE_MASK"])
_FIELD = re.compile("|".join(f"(?:{p})" for p in FIELDS), re.I)
_JSON_PAIR = re.compile(r'"((?:[^"\\]|\\.){1,200})"(\s*:\s*)("(?:[^"\\]|\\.)*"?|-?\d[\d.eE+-]*|true|false|null)')
_FORM_PAIR = re.compile(r"(^|&)([^=&]{1,200})=([^&]*)")
MASK = "[redacted]"


def mask(text: str) -> str:
    if text.lstrip()[:1] in "{[":
        return _JSON_PAIR.sub(lambda m: f'"{m[1]}"{m[2]}"{MASK}"' if _FIELD.search(m[1]) else m[0], text)
    if "=" in text and "\n" not in text.strip():
        return _FORM_PAIR.sub(lambda m: f"{m[1]}{m[2]}={MASK}" if _FIELD.search(m[2]) else m[0], text)
    return text


def _textual(ctype: str) -> bool:
    c = (ctype or "").lower()
    return not c or any(t in c for t in TEXT_TYPES)


def _record(buf: bytes, total: int, ctype: str, span=None) -> None:
    """Put the captured payload on the request's server span (or, if unknown, the current span)."""
    try:
        if span is None:
            from opentelemetry import trace
            span = trace.get_current_span()
        if not span.is_recording() or total <= 0:
            return
        if _textual(ctype):
            text = bytes(buf).decode("utf-8", "replace")
            if total > len(buf):
                text += f"… [truncated, {total} bytes in total]"
            span.set_attribute("http.request.body", mask(text))
        else:
            span.set_attribute("http.request.body", f"[{ctype.split(';')[0]} payload, {total} bytes, not captured]")
        span.set_attribute("http.request.body.size", total)
        if ctype:   # WSGI header capture misses Content-Type (it is not an HTTP_* variable)
            span.set_attribute("http.request.header.content-type", [ctype])
    except Exception:   # capture must never break a request
        pass


# ------------------------------------------------------------------ ASGI (FastAPI, Starlette, ...)
def _asgi_header(scope: dict, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


SCOPE_SPAN = "rapidpulse.server_span"


def _tee_receive(receive, ctype: str, scope: dict):
    buf, seen = bytearray(), [0]

    async def wrapped():
        msg = await receive()
        try:
            if msg.get("type") == "http.request":
                chunk = msg.get("body") or b""
                seen[0] += len(chunk)
                if len(buf) < MAX:
                    buf.extend(chunk[:MAX - len(buf)])
                if not msg.get("more_body", False):
                    _record(buf, seen[0], ctype, scope.get(SCOPE_SPAN))
        except Exception:
            pass
        return msg
    return wrapped


def _patch_asgi(mod) -> None:
    cls = getattr(mod, "OpenTelemetryMiddleware", None)
    if cls is None or getattr(cls, "_rapidpulse", False):
        return
    original_init, original_call = cls.__init__, cls.__call__

    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        user_hook = getattr(self, "server_request_hook", None)

        def hook(span, scope):           # called with the server span itself: remember it for the payload
            try:
                scope[SCOPE_SPAN] = span
            except Exception:
                pass
            if callable(user_hook):
                user_hook(span, scope)
        self.server_request_hook = hook

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            receive = _tee_receive(receive, _asgi_header(scope, b"content-type"), scope)
        return await original_call(self, scope, receive, send)

    cls.__init__ = __init__
    cls.__call__ = __call__
    cls._rapidpulse = True

    # Second way to find the server span, for versions where the hook above is not reached:
    # the middleware creates it with this module-level function, passing the ASGI scope.
    start = getattr(mod, "_start_internal_or_server_span", None)
    if callable(start) and not getattr(start, "_rapidpulse", False):
        def _start(*args, **kwargs):
            result = start(*args, **kwargs)
            try:
                carrier = kwargs.get("context_carrier")
                if isinstance(carrier, dict) and carrier.get("type") == "http":
                    carrier.setdefault(SCOPE_SPAN, result[0])
            except Exception:
                pass
            return result
        _start._rapidpulse = True
        mod._start_internal_or_server_span = _start


# ------------------------------------------------------------------ WSGI (Flask, Django)
class _TeeInput:
    """wsgi.input stand-in that records what the application reads."""

    def __init__(self, stream, ctype: str, length: int, environ: dict | None = None):
        self._s, self._ctype, self._len = stream, ctype, length
        self._buf, self._seen = bytearray(), 0
        self._env = environ if environ is not None else {}

    def _span(self):
        # Flask and Django instrumentation keep the server span in the WSGI environ
        for k, v in self._env.items():
            if isinstance(k, str) and k.endswith("span_key") and hasattr(v, "set_attribute"):
                return v
        return None

    def _keep(self, data: bytes) -> bytes:
        try:
            self._seen += len(data)
            if len(self._buf) < MAX:
                self._buf.extend(data[:MAX - len(self._buf)])
            _record(self._buf, max(self._seen, self._len), self._ctype, self._span())
        except Exception:
            pass
        return data

    def read(self, *a):
        return self._keep(self._s.read(*a))

    def readinto(self, b):
        if hasattr(self._s, "readinto"):
            n = self._s.readinto(b)
            if n:
                self._keep(bytes(memoryview(b)[:n]))
            return n
        data = self._s.read(len(b))
        b[:len(data)] = data
        self._keep(data)
        return len(data)

    def readline(self, *a):
        return self._keep(self._s.readline(*a))

    def readlines(self, *a):
        lines = self._s.readlines(*a)
        self._keep(b"".join(lines))
        return lines

    def __iter__(self):
        for line in self._s:
            yield self._keep(line)

    def __getattr__(self, name):
        return getattr(self._s, name)


def _wrap_environ(environ: dict) -> None:
    try:
        if environ.get("wsgi.input") is not None and not isinstance(environ["wsgi.input"], _TeeInput):
            length = int(environ.get("CONTENT_LENGTH") or 0)
            environ["wsgi.input"] = _TeeInput(environ["wsgi.input"], environ.get("CONTENT_TYPE", ""), length, environ)
    except Exception:
        pass


def _patch_wsgi_call(cls) -> None:
    if cls is None or getattr(cls, "_rapidpulse", False):
        return
    original = cls.__call__

    def __call__(self, environ, start_response):
        _wrap_environ(environ)
        return original(self, environ, start_response)

    cls.__call__ = __call__
    cls._rapidpulse = True


PATCHES = {
    "opentelemetry.instrumentation.asgi": _patch_asgi,
    "flask.app": lambda m: _patch_wsgi_call(getattr(m, "Flask", None)),
    "django.core.handlers.wsgi": lambda m: _patch_wsgi_call(getattr(m, "WSGIHandler", None)),
}


class _AfterImport(importlib.abc.MetaPathFinder):
    """Applies a patch right after one of the modules above is imported."""

    def __init__(self):
        self._busy = False

    def find_spec(self, name, path=None, target=None):
        if name not in PATCHES or self._busy:
            return None
        self._busy = True
        try:
            spec = importlib.util.find_spec(name)
        except Exception:
            spec = None
        finally:
            self._busy = False
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        run = spec.loader.exec_module

        def exec_module(module, _run=run, _name=name):
            _run(module)
            try:
                PATCHES[_name](module)
            except Exception:
                pass
        spec.loader.exec_module = exec_module
        return spec


def install() -> None:
    if MAX <= 0 or os.environ.get("RAPIDPULSE_CAPTURE_DISABLED"):
        return
    for name, patch in PATCHES.items():
        if name in sys.modules:
            try:
                patch(sys.modules[name])
            except Exception:
                pass
    if not any(isinstance(f, _AfterImport) for f in sys.meta_path):
        sys.meta_path.insert(0, _AfterImport())


try:
    install()
except Exception:
    pass
