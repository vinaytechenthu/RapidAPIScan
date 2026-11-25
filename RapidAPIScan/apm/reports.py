"""PDF reports: a technical developer report, a plain-language stakeholder summary, and a single-trace report.

Built with reportlab (pure Python, no browser or external service needed), so the server can produce the same
file for a click in the UI, an API call or a scheduled job.
"""
from __future__ import annotations

import html
import io
import time
from datetime import datetime, timedelta, timezone

from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (CondPageBreak, KeepTogether, PageBreak, Paragraph, Preformatted, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from . import __version__

# Same palette as the web UI (light theme); categorical colors follow the entity, never cycled.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
GOOD, WARN, CRIT, UNKNOWN = "#0ca30c", "#c98500", "#d03b3b", "#8a8984"
INK, MUTED, RULE, HEAD_BG, ZEBRA = "#1f2328", "#5b6470", "#d0d7de", "#1f4e79", "#f3f6fa"
HEALTH_COLOR = {"healthy": GOOD, "ok": GOOD, "running": GOOD, "resolved": GOOD, "warning": WARN,
                "critical": CRIT, "error": CRIT, "down": CRIT, "firing": CRIT, "unknown": UNKNOWN}
HEALTH_TEXT = {"healthy": "Healthy", "warning": "Warning", "critical": "Critical", "unknown": "No data"}

_SS = getSampleStyleSheet()
ST = {
    "title": ParagraphStyle("t", parent=_SS["Title"], fontName="Helvetica-Bold", fontSize=20, leading=24,
                            textColor=colors.HexColor(HEAD_BG), alignment=TA_LEFT, spaceAfter=2),
    "sub": ParagraphStyle("s", parent=_SS["Normal"], fontSize=10, leading=13, textColor=colors.HexColor(MUTED)),
    "h1": ParagraphStyle("h1", parent=_SS["Heading1"], fontName="Helvetica-Bold", fontSize=14, leading=17,
                         textColor=colors.HexColor(HEAD_BG), spaceBefore=12, spaceAfter=6),
    "h2": ParagraphStyle("h2", parent=_SS["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, leading=14,
                         textColor=colors.HexColor(INK), spaceBefore=8, spaceAfter=4),
    "p": ParagraphStyle("p", parent=_SS["Normal"], fontSize=9.5, leading=12.5, textColor=colors.HexColor(INK), spaceAfter=4),
    "small": ParagraphStyle("sm", parent=_SS["Normal"], fontSize=8, leading=10, textColor=colors.HexColor(MUTED)),
    "cell": ParagraphStyle("c", parent=_SS["Normal"], fontSize=7.6, leading=9.2, textColor=colors.HexColor(INK)),
    "cellh": ParagraphStyle("ch", parent=_SS["Normal"], fontName="Helvetica-Bold", fontSize=7.6, leading=9.2, textColor=colors.white),
    "mono": ParagraphStyle("m", parent=_SS["Normal"], fontName="Courier", fontSize=7.2, leading=8.8, textColor=colors.HexColor(INK)),
    "bullet": ParagraphStyle("b", parent=_SS["Normal"], fontSize=9.5, leading=12.5, leftIndent=12, bulletIndent=2,
                             textColor=colors.HexColor(INK), spaceAfter=3),
    "kpi_v": ParagraphStyle("kv", parent=_SS["Normal"], fontName="Helvetica-Bold", fontSize=15, leading=18, textColor=colors.HexColor(INK)),
    "kpi_l": ParagraphStyle("kl", parent=_SS["Normal"], fontSize=7.5, leading=9, textColor=colors.HexColor(MUTED)),
}

_ASCII = {"→": "->", "←": "<-", "▲": "+", "▼": "-", "Δ": "delta ", "≥": ">=", "≤": "<=", "✓": "OK", "✕": "x", "⛔": "",
          "◇": "", "◯": "", "△": "", " ": " "}


def t(v) -> str:
    """Text safe for a Paragraph: XML-escaped and limited to the built-in fonts' character set."""
    s = "" if v is None else str(v)
    for a, b in _ASCII.items():
        s = s.replace(a, b)
    s = s.encode("cp1252", "replace").decode("cp1252")
    return html.escape(s, quote=False)


# ------------------------------------------------------------------ formatting
def ms(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    return f"{v / 60000:.1f} min" if v >= 60000 else f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms" if v >= 10 else f"{v:.1f} ms"


def num(v, d=0) -> str:
    return "-" if v is None else f"{float(v):,.{d}f}"


def pct(v) -> str:
    return "-" if v is None else f"{float(v):.{2 if float(v) < 10 else 1}f}%"


def usd(v) -> str:
    return "-" if v is None else f"${float(v):,.{4 if float(v) < 1 else 2}f}"


def change(cur, prev, higher_is_worse=True) -> str:
    if cur is None or not prev:
        return "-"
    d = 100.0 * (cur - prev) / prev
    arrow = "up" if d > 0 else "down" if d < 0 else "flat"
    worse = (d > 10) if higher_is_worse else (d < -10)
    txt = f"{arrow} {abs(d):.0f}%"
    return f'<font color="{CRIT}"><b>{txt}</b></font>' if worse else txt


class Clock:
    """Formats epoch seconds in the viewer's time zone (offset passed by the browser)."""

    def __init__(self, tz_offset_min: int | None):
        off = -int(tz_offset_min or 0)  # JS getTimezoneOffset() is UTC minus local
        self.tz = timezone(timedelta(minutes=off))
        sign = "+" if off >= 0 else "-"
        self.label = f"UTC{sign}{abs(off) // 60:02d}:{abs(off) % 60:02d}"

    def dt(self, s: float) -> datetime:
        return datetime.fromtimestamp(s, self.tz)

    def full(self, s) -> str:
        return "-" if not s else self.dt(s).strftime("%d %b %Y %H:%M:%S")

    def short(self, s) -> str:
        return "-" if not s else self.dt(s).strftime("%d %b %H:%M:%S")


# ------------------------------------------------------------------ building blocks
def table(headers, rows, widths, *, mono_cols=(), align_right=(), max_rows=None, zebra=True, colored=None):
    """A wrapped, zebra-striped table. Cells may be pre-built Paragraph markup (strings starting with '<')."""
    if max_rows and len(rows) > max_rows:
        extra = len(rows) - max_rows
        rows = rows[:max_rows] + [[f"... {extra} more rows not shown"] + [""] * (len(headers) - 1)]
    data = [[Paragraph(t(h), ST["cellh"]) for h in headers]]
    for r in rows:
        line = []
        for i, v in enumerate(r):
            if isinstance(v, Paragraph):
                line.append(v)
                continue
            s = v if isinstance(v, str) and v.startswith("<") else t(v)
            line.append(Paragraph(s, ST["mono"] if i in mono_cols else ST["cell"]))
        data.append(line)
    tb = Table(data, colWidths=widths, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(HEAD_BG)),
             ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor(RULE)),
             ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if zebra:
        for i in range(2, len(data), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(ZEBRA)))
    for c in align_right:
        style.append(("ALIGN", (c, 1), (c, -1), "RIGHT"))
    tb.setStyle(TableStyle(style + (colored or [])))
    return tb


def status_html(h: str, label: str | None = None) -> str:
    c = HEALTH_COLOR.get(h, UNKNOWN)
    return f'<font color="{c}"><b>&#9632;</b></font> {t(label or HEALTH_TEXT.get(h, h))}'


def kpis(items, width):
    """A row of KPI tiles: [(label, value, foot)]."""
    cells = []
    for label, value, foot in items:
        cells.append([Paragraph(t(label).upper(), ST["kpi_l"]), Paragraph(value, ST["kpi_v"]),
                      Paragraph(foot or "&nbsp;", ST["kpi_l"])])
    n = len(cells)
    tb = Table([cells], colWidths=[width / n] * n)
    tb.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor(RULE)),
                            ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(RULE)),
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafbfc")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return tb


def callout(text: str, color: str = SERIES[0], width=None):
    tb = Table([[Paragraph(text, ST["p"])]], colWidths=[width] if width else None)
    tb.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(color)),
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f6f8fa")),
                            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return tb


def line_chart(times, series, width, height, clock: Clock, yfmt=lambda v: f"{v:,.0f}", title=""):
    """series: [(name, points, color)] aligned with `times`. Returns a Drawing or a 'no data' note."""
    data, used = [], []
    for name, pts, color in series:
        xy = [(x, float(v)) for x, v in zip(times, pts) if v is not None]
        if len(xy) >= 2:
            data.append(xy); used.append((name, color))
    if not data:
        return Paragraph(f"{t(title)}: no data in this period.", ST["small"])
    d = Drawing(width, height)
    if title:
        d.add(String(0, height - 10, title, fontName="Helvetica-Bold", fontSize=8.5, fillColor=colors.HexColor(INK)))
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = 44, 22, width - 54, height - (48 if len(used) > 1 else 34)
    lp.data = data
    for i, (_, color) in enumerate(used):
        lp.lines[i].strokeColor = colors.HexColor(color)
        lp.lines[i].strokeWidth = 1.4
    x0, x1 = times[0], times[-1] if times[-1] > times[0] else times[0] + 60
    span = x1 - x0
    fmt = "%d %b" if span > 3 * 86400 else "%d %b %H:%M" if span > 86400 else "%H:%M"
    lp.xValueAxis.valueMin, lp.xValueAxis.valueMax = x0, x1
    lp.xValueAxis.valueSteps = [x0 + span * k / 5 for k in range(6)]
    lp.xValueAxis.labelTextFormat = lambda v: clock.dt(v).strftime(fmt)
    lp.xValueAxis.labels.fontSize = 6.5
    lp.xValueAxis.labels.fontName = "Helvetica"
    lp.xValueAxis.strokeColor = colors.HexColor(RULE)
    ymax = max(max(v for _, v in s) for s in data)
    lp.yValueAxis.valueMin, lp.yValueAxis.valueMax = 0, (ymax * 1.15) or 1
    lp.yValueAxis.labelTextFormat = yfmt
    lp.yValueAxis.labels.fontSize = 6.5
    lp.yValueAxis.labels.fontName = "Helvetica"
    lp.yValueAxis.strokeColor = colors.HexColor(RULE)
    lp.yValueAxis.visibleGrid = True
    lp.yValueAxis.gridStrokeColor = colors.HexColor("#eaeef2")
    lp.yValueAxis.gridStrokeWidth = 0.4
    d.add(lp)
    if len(used) > 1:
        lg = Legend()
        lg.x, lg.y = 44, height - 18
        lg.alignment = "right"; lg.columnMaximum = 1; lg.fontSize = 6.8; lg.fontName = "Helvetica"
        lg.dx = lg.dy = 6; lg.deltax = 4
        lg.colorNamePairs = [(colors.HexColor(c), n[:28]) for n, c in used]
        lg.dxTextSpace = 3; lg.deltax = max(60, (width - 60) / max(1, len(used)))
        d.add(lg)
    return d


# ------------------------------------------------------------------ page decoration
def _decorate(title: str, clock: Clock, landscape_mode=False):
    def draw(canvas, doc):
        w, h = doc.pagesize
        canvas.saveState()
        canvas.setFillColor(colors.HexColor(HEAD_BG))
        canvas.rect(0, h - 6 * mm, w, 6 * mm, stroke=0, fill=1)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(doc.leftMargin, 9 * mm, f"RapidPulse {__version__}  |  {title}")
        canvas.drawRightString(w - doc.rightMargin, 9 * mm,
                               f"Generated {clock.full(time.time())} {clock.label}  |  Page {doc.page}")
        canvas.restoreState()
    return draw


def _doc(buf, title, land=False):
    size = landscape(A4) if land else A4
    return SimpleDocTemplate(buf, pagesize=size, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm,
                             bottomMargin=16 * mm, title=title, author="RapidPulse", creator="RapidPulse")


# ================================================================== period report
def _collect(q, store, frm, to, app):
    store.flush_rollups()
    ov = q.overview(frm, to, app)
    cur = q.grouped(frm, to, ["role"], app=app, role="entry")
    prev = q.grouped(frm - (to - frm), frm, ["role"], app=app, role="entry")
    alerts = store.q("SELECT * FROM alert_events WHERE started < ? AND (resolved IS NULL OR resolved >= ?) "
                     "ORDER BY started DESC LIMIT 300", (to + 1, frm))
    return {
        "ov": ov, "cur": cur[0] if cur else {}, "prev": prev[0] if prev else {},
        "endpoints": q.grouped(frm, to, ["service", "operation"], app=app, role="entry"),
        "errors": q.errors(frm, to, app, limit=60)["groups"],
        "slow": q.traces(frm, to, app, sort="slowest", limit=20),
        "exits": q.grouped(frm, to, ["service", "category", "peer"], app=app, role="exit"),
        "db": q.database(frm, to, app), "ai": q.ai(frm, to, app), "hosts": q.hosts(), "checks": q.checks(frm, to),
        "alerts": alerts,
        "svc_p95": q.series(frm, to, app=app, role="entry", metrics=("p95_ms",), group_by="service"),
    }


def _findings(d, cfg) -> tuple[str, list[str]]:
    """Overall verdict and plain-language findings, most important first."""
    ov, cur, prev = d["ov"], d["cur"], d["prev"]
    svcs = [s for s in ov["services"] if s.get("calls") or s.get("health") == "critical"]
    crit = [s for s in svcs if s["health"] == "critical"]
    warn = [s for s in svcs if s["health"] == "warning"]
    crit_alerts = [a for a in d["alerts"] if a["severity"] == "critical"]
    verdict = "critical" if crit or any(a["state"] == "firing" for a in crit_alerts) else "warning" if warn or d["alerts"] else "healthy"
    out = []
    if cur.get("calls"):
        avail = 100 - (cur.get("error_rate") or 0)
        out.append(f"<b>{num(cur['calls'])}</b> requests were handled; <b>{avail:.2f}%</b> succeeded. "
                   f"Half of all requests completed within <b>{ms(cur.get('p50_ms'))}</b> and 95% within <b>{ms(cur.get('p95_ms'))}</b>.")
    else:
        out.append("No requests were recorded in this period.")
    if prev.get("calls") and cur.get("calls"):
        dc = 100 * (cur["calls"] - prev["calls"]) / prev["calls"]
        out.append(f"Compared with the previous period of equal length: traffic {'rose' if dc >= 0 else 'fell'} "
                   f"<b>{abs(dc):.0f}%</b>, failure rate went from {pct(prev.get('error_rate'))} to {pct(cur.get('error_rate'))}, "
                   f"and the 95th-percentile response time from {ms(prev.get('p95_ms'))} to {ms(cur.get('p95_ms'))}.")
    slow_default = cfg["thresholds"]["slow_request_ms"]
    for s in crit + warn:
        target = (cfg.get("services", {}).get(s["service"], {}) or {}).get("slow_request_ms", slow_default)
        why = []
        if not s.get("calls"):
            why.append("its health check is failing (service down)")
        if (s.get("error_rate") or 0) >= 2:
            why.append(f"{pct(s['error_rate'])} of its requests failed")
        if (s.get("p95_ms") or 0) >= target:
            why.append(f"it is slow: 95% of requests took up to {ms(s['p95_ms'])} against a target of {ms(target)}")
        out.append(f"{status_html(s['health'])} <b>{t(s.get('display') or s['service'])}</b>: {t('; '.join(why) or 'degraded')}.")
    top_err = [e for e in d["ov"]["top_errors"]][:1]
    if top_err:
        e = top_err[0]
        out.append(f"Most failures came from <b>{t(e['service'])}</b> <font face='Courier'>{t(e['operation'])}</font> "
                   f"({num(e['errors'])} failed calls).")
    if d["alerts"]:
        firing = sum(1 for a in d["alerts"] if a["state"] == "firing")
        out.append(f"<b>{len(d['alerts'])}</b> alert(s) were active in the period ({len(crit_alerts)} critical); "
                   f"{firing} still firing at the end of the period.")
    k = ov["kpi"]
    if k["hosts"]:
        bad = [h["host"] for h in d["hosts"] if h["status"] in ("down", "critical")]
        conts = [c["container"] for h in d["hosts"] for c in h["containers"]
                 if (c.get("status") or "") != "running" or (c.get("health") or "") == "unhealthy"]
        out.append(f"Infrastructure: {k['hosts']} server(s) monitored"
                   + (f"; needing attention: {t(', '.join(bad))}" if bad else "; all reporting normally")
                   + (f"; containers not healthy: {t(', '.join(conts[:6]))}" if conts else "") + ".")
    if k["llm_calls"]:
        out.append(f"AI usage: {num(k['llm_calls'])} LLM calls, {num(k['llm_tokens'])} tokens, estimated cost <b>{usd(k['llm_cost'])}</b>.")
    return verdict, out


def build_report(q, store, cfg, frm: int, to: int, app: str | None, audience: str, tz_offset: int | None) -> bytes:
    dev = audience != "stakeholder"
    clock = Clock(tz_offset)
    d = _collect(q, store, frm, to, app)
    title = "Developer performance report" if dev else "Service performance summary"
    buf = io.BytesIO()
    doc = _doc(buf, title, land=dev)
    W = doc.width
    story = []
    ov, k, cur, prev = d["ov"], d["ov"]["kpi"], d["cur"], d["prev"]
    verdict, findings = _findings(d, cfg)

    # -------------------------------------------------- header
    story += [Paragraph(t(title), ST["title"]),
              Paragraph(f"Application: <b>{t(app or 'All applications')}</b> &nbsp;|&nbsp; Period: <b>{clock.full(frm)}</b> to "
                        f"<b>{clock.full(to)}</b> ({clock.label}, {ms((to - frm) * 1000)})", ST["sub"]),
              Spacer(1, 8)]
    vtext = {"healthy": "All services healthy", "warning": "Attention needed", "critical": "Action required"}[verdict]
    story.append(callout(f"<b>Overall status:</b> {status_html(verdict, vtext)}", HEALTH_COLOR[verdict], W))
    story.append(Spacer(1, 6))
    avail = None if not cur.get("calls") else 100 - (cur.get("error_rate") or 0)
    tiles = [("Requests", num(cur.get("calls")), f"{num(cur.get('rpm'), 1)} per minute"),
             ("Success rate", pct(avail) if avail is not None else "-", f"{num(cur.get('errors'))} failed"),
             ("Typical response (p50)", ms(cur.get("p50_ms")), f"avg {ms(cur.get('avg_ms'))}"),
             ("95% of requests within", ms(cur.get("p95_ms")), f"p99 {ms(cur.get('p99_ms'))}"),
             ("User satisfaction (Apdex)", "-" if cur.get("apdex") is None else f"{cur['apdex']:.2f}", "1.00 = all fast"),
             ("Alerts in period", num(len(d["alerts"])), f"{sum(1 for a in d['alerts'] if a['state'] == 'firing')} still firing")]
    if k["llm_calls"]:
        tiles.append(("LLM cost", usd(k["llm_cost"]), f"{num(k['llm_calls'])} calls"))
    story.append(kpis(tiles, W))
    story.append(Spacer(1, 4))

    story.append(Paragraph("Key findings", ST["h1"]))
    for f in findings:
        story.append(Paragraph(f, ST["bullet"], bulletText="•"))

    # -------------------------------------------------- trend charts
    s = ov["series"]
    def pts(metric):
        return next((x["points"] for x in s["series"] if x["metric"] == metric), [])
    cw = (W - 10) / 2 if dev else W
    traffic = line_chart(s["times"], [("Requests / min", pts("rpm"), SERIES[0])], cw, 150, clock,
                         lambda v: f"{v:,.1f}", "Traffic (requests per minute)")
    errs = line_chart(s["times"], [("Failure rate %", pts("error_rate"), SERIES[7])], cw, 150, clock,
                      lambda v: f"{v:.1f}%", "Failure rate (%)")
    story.append(Paragraph("Trends", ST["h1"]))
    if dev:
        story.append(Table([[traffic, errs]], colWidths=[cw + 5, cw + 5], style=[("VALIGN", (0, 0), (-1, -1), "TOP")]))
        sp = d["svc_p95"]
        lat = line_chart(sp["times"], [(x["group"], x["points"], SERIES[i] if i < 8 else UNKNOWN)
                                       for i, x in enumerate(sp["series"])], W, 175, clock, lambda v: ms(v),
                         "95th-percentile response time per service")
        story += [Spacer(1, 4), lat]
    else:
        story += [traffic, Spacer(1, 4), errs]

    # -------------------------------------------------- comparison
    story.append(KeepTogether([Paragraph("Compared with the previous period", ST["h1"]), table(
        ["Measure", "This period", "Previous period", "Change"],
        [["Requests", num(cur.get("calls")), num(prev.get("calls")), change(cur.get("calls"), prev.get("calls"), False)],
         ["Failure rate", pct(cur.get("error_rate")), pct(prev.get("error_rate")), change(cur.get("error_rate"), prev.get("error_rate"))],
         ["Typical response (p50)", ms(cur.get("p50_ms")), ms(prev.get("p50_ms")), change(cur.get("p50_ms"), prev.get("p50_ms"))],
         ["95% of requests within (p95)", ms(cur.get("p95_ms")), ms(prev.get("p95_ms")), change(cur.get("p95_ms"), prev.get("p95_ms"))],
         ["Apdex", "-" if cur.get("apdex") is None else f"{cur['apdex']:.2f}", "-" if prev.get("apdex") is None else f"{prev['apdex']:.2f}", ""]],
        [W * 0.34, W * 0.22, W * 0.22, W * 0.22], align_right=(1, 2, 3))]))

    # -------------------------------------------------- services
    svcs = [x for x in ov["services"] if x.get("calls") or x.get("health") == "critical"]
    story.append(CondPageBreak(60 * mm))
    story.append(Paragraph("Service health", ST["h1"]))
    if dev:
        story.append(table(
            ["Service", "Health", "Calls", "Req/min", "Errors", "Error %", "Avg", "p50", "p95", "p99", "Max", "Apdex", "Hosts / containers"],
            [[x.get("display") or x["service"], status_html(x["health"]), num(x.get("calls")), num(x.get("rpm"), 1),
              num(x.get("errors")), pct(x.get("error_rate")), ms(x.get("avg_ms")), ms(x.get("p50_ms")), ms(x.get("p95_ms")),
              ms(x.get("p99_ms")), ms(x.get("max_ms")), "-" if x.get("apdex") is None else f"{x['apdex']:.2f}",
              ", ".join(filter(None, [x.get("hosts"), x.get("containers")]))] for x in svcs],
            [W * w for w in (.13, .07, .06, .055, .05, .055, .06, .06, .06, .06, .06, .045, .19)], align_right=tuple(range(2, 12))))
    else:
        story.append(table(
            ["Service", "Status", "Requests", "Success rate", "Typical response", "95% within"],
            [[x.get("display") or x["service"], status_html(x["health"]), num(x.get("calls")),
              pct(100 - (x.get("error_rate") or 0)) if x.get("calls") else "-", ms(x.get("p50_ms")), ms(x.get("p95_ms"))] for x in svcs],
            [W * w for w in (.30, .14, .14, .14, .14, .14)], align_right=(2, 3, 4, 5)))
        story.append(Paragraph("Status: Healthy = within targets; Warning = 2% or more requests failing, or slower than the "
                               "service's response-time target; Critical = 10% or more failing, twice the target, or down.", ST["small"]))

    if not dev:
        # Stakeholders: the few problems worth knowing about, then stop.
        top = [e for e in ov["top_errors"]][:5]
        if top:
            story.append(Paragraph("Where failures happened", ST["h1"]))
            story.append(table(["Service", "Function (API)", "Failed calls", "Failure rate"],
                               [[e["service"], e["operation"], num(e["errors"]), pct(e["error_rate"])] for e in top],
                               [W * .25, W * .45, W * .15, W * .15], align_right=(2, 3)))
        if d["alerts"]:
            story.append(Paragraph("Alerts", ST["h1"]))
            story.append(table(["Started", "Alert", "Severity", "Status"],
                               [[clock.short(a["started"]), a["message"] or a["rule_name"], a["severity"].title(),
                                 "Resolved " + clock.short(a["resolved"]) if a["resolved"] else "Still active"] for a in d["alerts"][:10]],
                               [W * .2, W * .5, W * .12, W * .18], max_rows=10))
        story.append(Spacer(1, 10))
        story.append(Paragraph("A full technical report with endpoints, errors, traces, dependencies and infrastructure "
                               "detail is available from the RapidPulse dashboard (Export PDF > Developer report).", ST["small"]))
        doc.build(story, onFirstPage=_decorate(title, clock), onLaterPages=_decorate(title, clock))
        return buf.getvalue()

    # ================================================== developer-only sections
    story.append(PageBreak())
    story.append(Paragraph("API endpoints", ST["h1"]))
    story.append(Paragraph("Every server endpoint that received requests, busiest first. Status codes follow the HTTP semantic conventions.", ST["small"]))
    story.append(table(
        ["Service", "Endpoint", "Calls", "Req/min", "Error %", "Avg", "p50", "p95", "p99", "Max", "Status codes"],
        [[e["service"], e["operation"], num(e["calls"]), num(e["rpm"], 1), pct(e["error_rate"]), ms(e["avg_ms"]),
          ms(e["p50_ms"]), ms(e["p95_ms"]), ms(e["p99_ms"]), ms(e["max_ms"]),
          "  ".join(f"{c}: {n}" for c, n in sorted((e.get("codes") or {}).items()))] for e in d["endpoints"]],
        [W * w for w in (.12, .27, .06, .055, .055, .06, .06, .06, .06, .06, .11)], mono_cols=(1,),
        align_right=tuple(range(2, 10)), max_rows=200,
        colored=[("TEXTCOLOR", (4, 1), (4, -1), colors.HexColor(INK))]))

    story.append(Paragraph("Errors", ST["h1"]))
    story.append(table(
        ["Service", "Operation", "Type / HTTP", "Count", "Last seen", "Message", "Sample trace"],
        [[e["service"], e["operation"], " ".join(filter(None, [e.get("error_type"), str(e["http_status"] or "")])) or "-",
          num(e["n"]), clock.short(e["last_ns"] / 1e9), (e.get("message") or "")[:220], e["sample_trace"]] for e in d["errors"]],
        [W * w for w in (.1, .19, .11, .05, .1, .24, .21)], mono_cols=(1, 6), align_right=(3,)) if d["errors"]
        else Paragraph("No errors in this period.", ST["p"]))

    story.append(Paragraph("Slowest traces", ST["h1"]))
    story.append(table(
        ["Started", "Entry point", "Duration", "Spans", "Errors", "Services", "Trace ID"],
        [[clock.short(x["start_ns"] / 1e9), f"{x['root_service']}  {x['root_name']}", ms(x["duration_ms"]), num(x["span_count"]),
          num(x["error_count"]), ", ".join(x["services"]), x["trace_id"]] for x in d["slow"]],
        [W * w for w in (.11, .26, .07, .05, .05, .25, .21)], mono_cols=(6,), align_right=(2, 3, 4)) if d["slow"]
        else Paragraph("No traces in this period.", ST["p"]))

    story.append(Paragraph("Downstream dependencies", ST["h1"]))
    story.append(Paragraph("Outgoing calls from each service to other APIs, databases, LLMs and MCP tools.", ST["small"]))
    story.append(table(
        ["Service", "Type", "Target", "Calls", "Avg", "p95", "p99", "Error %"],
        [[e["service"], e["category"], e["peer"] or "-", num(e["calls"]), ms(e["avg_ms"]), ms(e["p95_ms"]), ms(e["p99_ms"]),
          pct(e["error_rate"])] for e in d["exits"]],
        [W * w for w in (.18, .08, .3, .08, .09, .09, .09, .09)], align_right=(3, 4, 5, 6, 7), max_rows=120) if d["exits"]
        else Paragraph("No outgoing calls recorded.", ST["p"]))

    db = d["db"]
    if db["databases"] or db["slow"]:
        story.append(Paragraph("Database", ST["h1"]))
        story.append(table(["Database", "Calls", "Avg", "p95", "p99", "Error %"],
                           [[x["peer"], num(x["calls"]), ms(x["avg_ms"]), ms(x["p95_ms"]), ms(x["p99_ms"]), pct(x["error_rate"])]
                            for x in db["databases"]], [W * .4] + [W * .12] * 5, align_right=(1, 2, 3, 4, 5)))
        story.append(Paragraph(f"Heaviest queries (by total time)", ST["h2"]))
        story.append(table(["Database", "Caller", "Statement", "Calls", "Avg", "p95"],
                           [[x["peer"], x["service"], x["operation"], num(x["calls"]), ms(x["avg_ms"]), ms(x["p95_ms"])]
                            for x in db["queries"][:25]], [W * w for w in (.13, .12, .5, .07, .09, .09)], mono_cols=(2,),
                           align_right=(3, 4, 5)))
        if db["slow"]:
            story.append(Paragraph(f"Slow queries (over {ms(db['slow_threshold_ms'])})", ST["h2"]))
            story.append(table(["Time", "Service", "Statement", "Duration", "Trace ID"],
                               [[clock.short(x["start_ns"] / 1e9), x["service"], (x["db_statement"] or "")[:300], ms(x["duration_ms"]), x["trace_id"]]
                                for x in db["slow"][:20]], [W * w for w in (.11, .11, .49, .08, .21)], mono_cols=(2, 4), align_right=(3,)))

    ai = d["ai"]
    if ai["models"] or ai["tools"]:
        story.append(Paragraph("AI: LLM and MCP", ST["h1"]))
        if ai["models"]:
            story.append(table(["Model / operation", "Provider", "Calls", "Tokens in", "Tokens out", "Cost", "Avg", "p95", "Error %"],
                               [[x["operation"], x["peer"], num(x["calls"]), num(x["tokens_in"]), num(x["tokens_out"]), usd(x["cost"]),
                                 ms(x["avg_ms"]), ms(x["p95_ms"]), pct(x["error_rate"])] for x in ai["models"]],
                               [W * w for w in (.24, .14, .07, .09, .09, .09, .09, .09, .1)], align_right=tuple(range(2, 9))))
        if ai["tools"]:
            story.append(Paragraph("MCP tools", ST["h2"]))
            story.append(table(["Service", "Tool", "Calls", "Avg", "p95", "Error %"],
                               [[x["service"], x["operation"], num(x["calls"]), ms(x["avg_ms"]), ms(x["p95_ms"]), pct(x["error_rate"])]
                                for x in ai["tools"]], [W * w for w in (.2, .4, .1, .1, .1, .1)], align_right=(2, 3, 4, 5)))
        if ai["failures"]:
            story.append(Paragraph("AI failures", ST["h2"]))
            story.append(table(["Time", "Service", "Operation", "Error", "Trace ID"],
                               [[clock.short(x["start_ns"] / 1e9), x["service"], x["operation"],
                                 f"{x['error_type'] or ''} {x['error_message'] or ''}"[:200], x["trace_id"]] for x in ai["failures"][:20]],
                               [W * w for w in (.11, .12, .2, .36, .21)], mono_cols=(4,)))

    story.append(Paragraph("Infrastructure", ST["h1"]))
    if d["hosts"]:
        story.append(table(["Host", "Role", "OS", "Status", "CPU", "Memory", "Disk", "Last seen", "Services"],
                           [[h["host"], h.get("role") or "", h.get("os") or "", status_html(h["status"]),
                             pct((h["metrics"] or {}).get("cpu")), pct((h["metrics"] or {}).get("mem_pct")),
                             pct((h["metrics"] or {}).get("disk_pct")), clock.short(h.get("last_seen")), ", ".join(h["services"])]
                            for h in d["hosts"]], [W * w for w in (.15, .07, .1, .08, .06, .07, .06, .11, .3)], align_right=(4, 5, 6)))
        conts = [(h["host"], c) for h in d["hosts"] for c in h["containers"]]
        if conts:
            story.append(Paragraph("Containers", ST["h2"]))
            story.append(table(["Host", "Container", "Image", "Status", "Health", "CPU", "Memory", "Restarts"],
                               [[h, c["container"], c.get("image") or "", c.get("status") or "", c.get("health") or "-",
                                 pct(c.get("cpu")), pct(c.get("mem_pct")), num(c.get("restarts"))] for h, c in conts],
                               [W * w for w in (.13, .2, .25, .08, .08, .08, .09, .09)], align_right=(5, 6, 7), max_rows=150))
    else:
        story.append(Paragraph("No agents are reporting. Install the RapidPulse agent on each server.", ST["p"]))
    if d["checks"]:
        story.append(Paragraph("Health checks", ST["h2"]))
        story.append(table(["Check", "Type", "Target", "Service", "Uptime", "Avg latency", "Last result"],
                           [[c["name"], c["type"], c["target"], c["service"] or "", pct(c["uptime"]), ms(c["avg_ms"]),
                             ("OK" if c["last_ok"] else "FAILED " + (c["last_message"] or ""))[:120]] for c in d["checks"]],
                           [W * w for w in (.16, .06, .26, .12, .08, .09, .23)], mono_cols=(2,), align_right=(4, 5)))

    story.append(Paragraph("Alerts in the period", ST["h1"]))
    story.append(table(["Started", "Resolved", "Severity", "Rule", "Scope", "Value / threshold", "Message"],
                       [[clock.short(a["started"]), clock.short(a["resolved"]) if a["resolved"] else "firing", a["severity"],
                         a["rule_name"], a["scope_key"] or "", f"{num(a['value'], 2)} / {num(a['threshold'], 2)}", a["message"] or ""]
                        for a in d["alerts"]], [W * w for w in (.1, .1, .07, .15, .14, .1, .34)], max_rows=150) if d["alerts"]
                 else Paragraph("No alerts were active in this period.", ST["p"]))

    story.append(Paragraph("Definitions", ST["h1"]))
    for term, desc in (("p50 / p95 / p99", "The response time within which 50%, 95% or 99% of requests completed (from log-scale histograms, about +/-12%)."),
                       ("Apdex", f"Share of satisfied requests (under {ms(cfg['thresholds']['apdex_t_ms'])}) plus half the tolerating ones (under 4x that); 1.00 is best."),
                       ("Error %", "Requests marked as failed by the service: HTTP 5xx, exceptions, or an explicit error status."),
                       ("Health", "Critical: error rate 10%+ or p95 twice the service's target, or a failing health check. Warning: error rate 2%+ or p95 above target.")):
        story.append(Paragraph(f"<b>{t(term)}</b>: {t(desc)}", ST["bullet"], bulletText="•"))
    doc.build(story, onFirstPage=_decorate(title, clock, True), onLaterPages=_decorate(title, clock, True))
    return buf.getvalue()


# ================================================================== single trace
def _waterfall(spans, total_ms, width, color_of):
    """Waterfall drawings in page-sized chunks (a Drawing cannot split across pages)."""
    label_w, dur_w, row_h = width * 0.40, 46, 11
    bar_w = width - label_w - dur_w
    out = []
    for start in range(0, len(spans), 48):
        chunk = spans[start:start + 48]
        h = row_h * len(chunk) + (14 if start == 0 else 2)
        d = Drawing(width, h)
        y = h - 2
        if start == 0:
            for f in (0, .25, .5, .75, 1):
                x = label_w + bar_w * f
                d.add(String(x, h - 9, ms(total_ms * f), fontName="Helvetica", fontSize=6,
                             textAnchor="middle" if 0 < f < 1 else "start" if f == 0 else "end", fillColor=colors.HexColor(MUTED)))
                d.add(Line(x, 0, x, h - 12, strokeColor=colors.HexColor("#eaeef2"), strokeWidth=0.4))
            y = h - 13
        for s in chunk:
            y -= row_h
            name = s["db_statement"] if s["category"] == "db" and s.get("db_statement") else s["name"]
            label = f"{'  ' * min(s['depth'], 12)}{s['service']}: {name}"
            label = t(label).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            maxc = int(label_w / 3.3)
            d.add(String(2, y + 3, label[:maxc] + ("..." if len(label) > maxc else ""), fontName="Helvetica", fontSize=6.3,
                         fillColor=colors.HexColor(CRIT if s["status"] == "error" else INK)))
            x = label_w + bar_w * (s["offset_ms"] / total_ms if total_ms else 0)
            w = max(0.8, bar_w * ((s["duration_ms"] or 0) / total_ms if total_ms else 0))
            d.add(Rect(x, y + 1.5, min(w, label_w + bar_w - x), row_h - 4, fillColor=colors.HexColor(color_of(s)),
                       strokeColor=colors.HexColor(CRIT) if s["status"] == "error" else (colors.HexColor(INK) if s.get("critical") else None),
                       strokeWidth=1 if s["status"] == "error" else 0.5))
            d.add(String(width - 2, y + 3, ms(s["duration_ms"]), fontName="Helvetica", fontSize=6.3, textAnchor="end",
                         fillColor=colors.HexColor(MUTED)))
        out.append(d)
    return out


REQUEST_ATTR = ("http.request.header.", "http.request.body")
MAX_PDF_BODY = 12000


def _plain(v) -> str:
    """Text for a Preformatted block (no markup): limited to the built-in fonts' character set."""
    s = "" if v is None else str(v)
    for a, b in _ASCII.items():
        s = s.replace(a, b)
    return s.encode("cp1252", "replace").decode("cp1252")


def _pretty_body(body) -> str:
    import json
    try:
        return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(body)


def _request_section(req: dict | None, W) -> list:
    """Method, URL, headers and payload of a captured HTTP request."""
    if not req:
        return [Paragraph("No headers or payload were captured for this request. Request capture is set up per "
                          "service (Installation Guide, section 6).", ST["small"])]
    line = f"<b>{t(req.get('method') or '')}</b> <font face='Courier'>{t(req.get('url') or '')}</font>"
    if req.get("status"):
        line += f" &nbsp; HTTP {t(req['status'])}"
    out = [Paragraph(line, ST["p"]), Spacer(1, 3)]
    hdrs = req.get("headers") or {}
    if hdrs:
        out.append(table(["Request header", "Value"], [[Paragraph(t(k), ST["mono"]), Paragraph(t(v)[:600], ST["mono"])]
                                                        for k, v in hdrs.items()], [W * .3, W * .7], max_rows=80))
    if req.get("body") is not None:
        body = _pretty_body(req["body"])
        ctype = req.get("content_type") or ""
        note = f" ({t(ctype)}{', ' if ctype else ''}{num(req['body_size'])} bytes)" if req.get("body_size") else ""
        if len(body) > MAX_PDF_BODY:
            body = body[:MAX_PDF_BODY] + f"\n... [{len(body) - MAX_PDF_BODY} more characters in the dashboard]"
        out += [Spacer(1, 4), Paragraph(f"<b>Request payload</b>{note}", ST["p"]),
                Preformatted(_plain(body), ST["mono"], maxLineLength=int(W / 4.4), newLineChars=""), Spacer(1, 4)]
    elif not hdrs:
        out.append(Paragraph("No payload.", ST["small"]))
    out.append(Paragraph("Secrets in headers and payload fields (authorization, cookies, keys, tokens, passwords) are masked "
                         "before they are stored.", ST["small"]))
    return out


def build_trace_report(q, trace_id: str, tz_offset: int | None) -> bytes | None:
    tr = q.trace(trace_id)
    if not tr:
        return None
    clock = Clock(tz_offset)
    title = f"Trace report {tr['trace_id'][:16]}"
    buf = io.BytesIO()
    doc = _doc(buf, title)
    W = doc.width
    spans = tr["spans"]
    svc_order = {s: i for i, s in enumerate(tr["services"])}
    comp_color = {"llm": SERIES[6], "database": SERIES[3], "mcp": SERIES[4]}

    def color_of(s):
        if s.get("peer") and s.get("peer_type") in comp_color and not s.get("child_count"):
            return comp_color[s["peer_type"]]
        i = svc_order.get(s["service"], 99)
        return SERIES[i] if i < 8 else UNKNOWN

    root = spans[0] if spans else {}
    entry = f"{root.get('http_method') or ''} {root.get('http_route') or root.get('name') or ''}".strip()
    story = [Paragraph("Trace report", ST["title"]),
             Paragraph(f"Trace ID <font face='Courier'>{t(tr['trace_id'])}</font> &nbsp;|&nbsp; started {clock.full(tr['start_ns'] / 1e9)} {clock.label}",
                       ST["sub"]), Spacer(1, 8)]
    ok = not tr["error_count"]
    story.append(callout(f"<b>Request:</b> {t(root.get('service', ''))} <font face='Courier'>{t(entry)}</font>"
                         + (f" &nbsp; HTTP {root['http_status']}" if root.get("http_status") else "")
                         + f" &nbsp; {status_html('ok' if ok else 'error', 'Succeeded' if ok else 'Failed')}", GOOD if ok else CRIT, W))
    story.append(Spacer(1, 6))
    story.append(kpis([("End-to-end duration", ms(tr["duration_ms"]), ""), ("Spans", num(tr["span_count"]), ""),
                       ("Services", num(len(tr["services"])), t(", ".join(tr["services"]))[:80]),
                       ("Errors", num(tr["error_count"]), "failed spans"),
                       ("LLM calls", num(tr["ai"]["llm_calls"]), f"{num(tr['ai']['tokens_in'])} in / {num(tr['ai']['tokens_out'])} out"),
                       ("LLM cost", usd(tr["ai"]["cost"]), f"{tr['ai']['mcp_calls']} MCP calls")], W))
    story.append(Spacer(1, 6))
    rc, bn = tr["root_cause"], tr["bottleneck"]
    if rc:
        story.append(callout(f"<b>Root cause:</b> {t(rc['service'])} <font face='Courier'>{t(rc['name'])}</font>"
                             + (f" <b>{t(rc['error_type'])}</b>" if rc.get("error_type") else "") + f": {t(rc.get('error_message') or '')}", CRIT, W))
        story.append(Spacer(1, 4))
    story.append(callout(f"<b>Biggest time consumer:</b> {t(bn['service'])} <font face='Courier'>{t(bn['name'])}</font> spent "
                         f"<b>{ms(bn['self_ms'])}</b> of its own time ({pct(100 * bn['self_ms'] / (tr['duration_ms'] or 1))} of the request).", SERIES[0], W))

    req = tr.get("request")
    story.append(Paragraph("Request" + (f" received by {t(req['service'])}" if req else ""), ST["h1"]))
    story += _request_section(req, W)

    story.append(Paragraph("Where the time went", ST["h1"]))
    story.append(Paragraph("Self time per component: time not spent waiting on a child call. Concurrent work can overlap.", ST["small"]))
    bd = [c for c in tr["breakdown"]]
    bar = Drawing(W, 16)
    x, tot = 0.0, sum(c["self_ms"] for c in bd) or 1
    for c in bd:
        w = W * c["self_ms"] / tot
        col = comp_color.get(c["type"]) or (SERIES[svc_order[c["component"]]] if svc_order.get(c["component"], 99) < 8 else UNKNOWN)
        bar.add(Rect(x, 2, max(w - 1, 0.5), 12, fillColor=colors.HexColor(col), strokeColor=None))
        c["_color"] = col
        x += w
    story.append(bar)
    story.append(table(["Component", "Type", "Self time", "% of request", "Spans", "Errors"],
                       [[f'<font color="{c["_color"]}"><b>&#9632;</b></font> {t(c["component"])}', c["type"], ms(c["self_ms"]),
                         pct(c["pct"]), num(c["spans"]), num(c["errors"])] for c in bd],
                       [W * w for w in (.38, .14, .13, .13, .11, .11)], align_right=(2, 3, 4, 5)))

    story.append(Paragraph("Execution journey", ST["h1"]))
    shown = spans[:400]
    story.append(Paragraph("Every span in start order, indented by call depth. Red = error; dark outline = critical path."
                           + (f" Showing the first 400 of {len(spans)} spans." if len(spans) > 400 else ""), ST["small"]))
    story += _waterfall(shown, tr["duration_ms"] or 1, W, color_of)

    story.append(CondPageBreak(45 * mm))
    story.append(Paragraph("Span details", ST["h1"]))
    story.append(table(["#", "Service", "Operation", "Type", "Start", "Duration", "Self", "Status", "Target / HTTP"],
                       [[str(i + 1), s["service"], (s["db_statement"] if s["category"] == "db" and s.get("db_statement") else s["name"])[:160],
                         s["category"], ms(s["offset_ms"]), ms(s["duration_ms"]), ms(s["self_ms"]),
                         f'<font color="{CRIT}"><b>error</b></font>' if s["status"] == "error" else "ok",
                         " ".join(filter(None, [s.get("peer") or "", str(s["http_status"]) if s.get("http_status") else ""]))]
                        for i, s in enumerate(shown)],
                       [W * w for w in (.04, .12, .31, .07, .08, .08, .08, .07, .15)], mono_cols=(2,), align_right=(4, 5, 6), max_rows=400))

    errs = [s for s in spans if s["status"] == "error"]
    if errs:
        story.append(Paragraph("Errors", ST["h1"]))
        for s in errs[:30]:
            # never print model input/output text, whatever the attribute is called
            attrs = {k: v for k, v in (s.get("attributes") or {}).items()
                     if not any(w in str(k).lower() for w in ("prompt", "completion", "input.value", "output.value"))
                     and not str(k).lower().startswith(REQUEST_ATTR)}
            body = [Paragraph(f"<b>{t(s['service'])}</b> <font face='Courier'>{t(s['name'])}</font> &nbsp; "
                              f"{t(s.get('error_type') or '')} at +{ms(s['offset_ms'])}", ST["p"]),
                    Paragraph(t(s.get("error_message") or "(no message)"), ST["mono"])]
            if attrs:
                body.append(Paragraph("<br/>".join(f"{t(k)} = {t(str(v)[:160])}" for k, v in list(attrs.items())[:25]), ST["mono"]))
            sreq = s.get("request")
            if sreq and sreq.get("body") is not None and s["span_id"] != (req or {}).get("span_id"):
                body.append(Paragraph(f"<b>Request payload:</b> <font face='Courier'>{t(str(sreq['body'])[:600])}</font>", ST["small"]))
            story.append(KeepTogether(body))
            story.append(Spacer(1, 5))

    llm = [s for s in spans if s["category"] == "llm"]
    if llm:
        story.append(Paragraph("LLM calls", ST["h1"]))
        story.append(table(["Service", "Model", "Duration", "Tokens in", "Tokens out", "Cost", "Status"],
                           [[s["service"], s.get("llm_model") or s["operation"], ms(s["duration_ms"]), num(s["tokens_in"]),
                             num(s["tokens_out"]), usd(s["cost_usd"]), s["status"]] for s in llm],
                           [W * w for w in (.18, .3, .1, .1, .1, .1, .12)], align_right=(2, 3, 4, 5)))
    dbs = [s for s in spans if s["category"] == "db"]
    if dbs:
        story.append(Paragraph("Database calls", ST["h1"]))
        story.append(table(["Service", "Database", "Statement", "Duration", "Status"],
                           [[s["service"], s.get("peer") or s.get("db_system") or "", (s.get("db_statement") or s["name"])[:400],
                             ms(s["duration_ms"]), s["status"]] for s in dbs],
                           [W * w for w in (.14, .14, .52, .1, .1)], mono_cols=(2,), align_right=(3,), max_rows=150))
    if tr["logs"]:
        story.append(Paragraph("Correlated logs", ST["h1"]))
        story.append(table(["Time", "Service", "Level", "Message"],
                           [[clock.short(lg["ts_ns"] / 1e9), lg["service"], lg["severity"], (lg["body"] or "")[:400]] for lg in tr["logs"]],
                           [W * w for w in (.15, .14, .08, .63)], mono_cols=(3,), max_rows=200))
    if tr["infra"]:
        story.append(Paragraph("Infrastructure at the time", ST["h1"]))
        story.append(table(["Host / container", "OS", "CPU", "Memory", "Status"],
                           [[i["host"], i.get("os") or "", pct(i.get("cpu")), pct(i.get("mem_pct")), i.get("status") or ""] for i in tr["infra"]],
                           [W * w for w in (.4, .15, .15, .15, .15)], align_right=(2, 3)))
    root_attrs = {k: v for k, v in (root.get("attributes") or {}).items() if not str(k).lower().startswith(REQUEST_ATTR)}
    if root_attrs:
        story.append(Paragraph("Entry span attributes", ST["h1"]))
        story.append(table(["Attribute", "Value"], [[k, str(v)[:300]] for k, v in sorted(root_attrs.items())],
                           [W * .35, W * .65], mono_cols=(0, 1), max_rows=80))
    doc.build(story, onFirstPage=_decorate(title, clock), onLaterPages=_decorate(title, clock))
    return buf.getvalue()
