"""
Self-contained HTML session report.

One file, no external assets, no JavaScript: the plots are inline SVG generated
from the same arrays the UI draws. It opens in any browser, prints sensibly,
and survives being emailed to an instructor who does not have this tool.

Light theme on purpose. The application is dark because it is looked at for an
hour at a time in a dim garage; a report is read once and often printed.
"""

from __future__ import annotations

import html
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .corners import GripEnvelope
from .insights import (LapAnalysis, consistency_insights, limit_usage,
                       theoretical_best)
from .laps import LapTrack, Session
from .report import fmt_time
from .units import METRIC, UnitSystem

REF_COLOUR = "#1f7a8c"
CMP_COLOUR = "#c47f1a"
LOSS_COLOUR = "#c0392b"
GAIN_COLOUR = "#1e8449"
INK = "#1a1d21"
MUTED = "#6b7280"
RULE = "#d8dce1"

SPEED_STOPS = [(0.0, (52, 84, 168)), (0.45, (60, 160, 175)),
               (0.75, (222, 170, 40)), (1.0, (200, 60, 50))]


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _lerp(stops, t: float) -> str:
    t = float(np.clip(t, 0.0, 1.0))
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p0 <= t <= p1:
            f = (t - p0) / max(p1 - p0, 1e-9)
            r, g, b = (int(round(a + (b_ - a) * f)) for a, b_ in zip(c0, c1))
            return f"#{r:02x}{g:02x}{b:02x}"
    r, g, b = stops[-1][1]
    return f"#{r:02x}{g:02x}{b:02x}"


# --------------------------------------------------------------------------
# SVG plots
# --------------------------------------------------------------------------


def track_svg(lap: LapTrack, corners, width: int = 520,
              height: int = 380, pad: int = 26) -> str:
    """Plan view colored by speed, with corner labels and brake points."""
    x, y = np.asarray(lap.x), np.asarray(lap.y)
    if x.size < 2:
        return ""
    x0, x1 = float(x.min()), float(x.max())
    y0, y1 = float(y.min()), float(y.max())
    scale = min((width - 2 * pad) / max(x1 - x0, 1e-6),
                (height - 2 * pad) / max(y1 - y0, 1e-6))
    ox = pad + ((width - 2 * pad) - (x1 - x0) * scale) / 2
    oy = pad + ((height - 2 * pad) - (y1 - y0) * scale) / 2

    def px(i):
        # SVG y grows downward; north should be up
        return (ox + (x[i] - x0) * scale,
                height - (oy + (y[i] - y0) * scale))

    v = lap.speed_kmh
    lo, hi = float(v.min()), float(v.max())
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" '
             f'height="{height}" xmlns="http://www.w3.org/2000/svg" '
             'role="img" aria-label="track map colored by speed">']
    step = max(1, len(x) // 900)
    for i in range(0, len(x) - step, step):
        ax, ay = px(i)
        bx, by = px(min(i + step, len(x) - 1))
        t = (float(v[i]) - lo) / max(hi - lo, 1e-6)
        parts.append(f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" '
                     f'y2="{by:.1f}" stroke="{_lerp(SPEED_STOPS, t)}" '
                     'stroke-width="3.4" stroke-linecap="round"/>')
    for c in corners:
        i = lap.idx(c.s_geo_apex)
        cx, cy = px(i)
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="8.5" '
                     f'fill="#ffffff" stroke="{RULE}"/>')
        parts.append(f'<text x="{cx:.1f}" y="{cy + 3.2:.1f}" font-size="9" '
                     f'text-anchor="middle" fill="{INK}" '
                     f'font-family="sans-serif">{_esc(c.name)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def trace_svg(series: Sequence[Tuple[np.ndarray, np.ndarray, str, str]],
              width: int = 900, height: int = 200, pad_l: int = 52,
              pad_b: int = 26, pad_t: int = 22, xlabel: str = "",
              ylabel: str = "",
              zero_line: bool = False,
              fill_sign: bool = False, shade=None) -> str:
    """A line chart. `series` is (x, y, color, label) tuples."""
    xs = np.concatenate([s[0] for s in series if len(s[0])]) if series else None
    ys = np.concatenate([s[1] for s in series if len(s[1])]) if series else None
    if xs is None or ys is None or xs.size == 0:
        return ""
    x0, x1 = float(xs.min()), float(xs.max())
    lo = float(np.percentile(ys, 0.2))
    hi = float(np.percentile(ys, 99.8))
    if hi - lo < 1e-9:
        lo, hi = lo - 1.0, hi + 1.0
    margin = (hi - lo) * 0.08
    lo, hi = lo - margin, hi + margin

    def sx(v):
        return pad_l + (v - x0) / max(x1 - x0, 1e-9) * (width - pad_l - 12)

    def sy(v):
        # pad_t keeps the axis labels clear of the top gridline; without it the
        # y label and the topmost tick value print on top of one another
        return (height - pad_b) - (v - lo) / max(hi - lo, 1e-9) * (
            height - pad_b - pad_t)

    out = [f'<svg viewBox="0 0 {width} {height}" width="{width}" '
           f'height="{height}" xmlns="http://www.w3.org/2000/svg">']

    for band in (shade or []):
        a, b = sx(band[0]), sx(band[1])
        out.append(f'<rect x="{a:.1f}" y="{pad_t}" '
                   f'width="{max(b - a, 0.5):.1f}" '
                   f'height="{height - pad_b - pad_t:.1f}" fill="#000" '
                   'opacity="0.045"/>')

    for frac in (0.0, 0.5, 1.0):
        gy = sy(lo + (hi - lo) * frac)
        out.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - 12}" '
                   f'y2="{gy:.1f}" stroke="{RULE}" stroke-width="1"/>')
        out.append(f'<text x="{pad_l - 6}" y="{gy + 3.5:.1f}" font-size="9" '
                   f'text-anchor="end" fill="{MUTED}" font-family="sans-serif">'
                   f'{lo + (hi - lo) * frac:.1f}</text>')

    if zero_line and lo < 0 < hi:
        zy = sy(0.0)
        out.append(f'<line x1="{pad_l}" y1="{zy:.1f}" x2="{width - 12}" '
                   f'y2="{zy:.1f}" stroke="{MUTED}" stroke-width="1" '
                   'stroke-dasharray="3 3"/>')

    for gx, gv in _ticks(x0, x1):
        px_ = sx(gx)
        out.append(f'<text x="{px_:.1f}" y="{height - 8}" font-size="9" '
                   f'text-anchor="middle" fill="{MUTED}" '
                   f'font-family="sans-serif">{gv}</text>')

    for xarr, yarr, colour, _label in series:
        if len(xarr) == 0:
            continue
        step = max(1, len(xarr) // 1400)
        pts = " ".join(f"{sx(float(xarr[i])):.1f},{sy(float(yarr[i])):.1f}"
                       for i in range(0, len(xarr), step))
        if fill_sign:
            base = sy(0.0)
            up = " ".join(
                f"{sx(float(xarr[i])):.1f},{sy(max(float(yarr[i]), 0.0)):.1f}"
                for i in range(0, len(xarr), step))
            dn = " ".join(
                f"{sx(float(xarr[i])):.1f},{sy(min(float(yarr[i]), 0.0)):.1f}"
                for i in range(0, len(xarr), step))
            out.append(f'<polygon points="{sx(float(xarr[0])):.1f},{base:.1f} '
                       f'{up} {sx(float(xarr[-1])):.1f},{base:.1f}" '
                       f'fill="{LOSS_COLOUR}" opacity="0.18"/>')
            out.append(f'<polygon points="{sx(float(xarr[0])):.1f},{base:.1f} '
                       f'{dn} {sx(float(xarr[-1])):.1f},{base:.1f}" '
                       f'fill="{GAIN_COLOUR}" opacity="0.18"/>')
        out.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                   'stroke-width="1.7"/>')

    # both captions live on the top line, clear of the tick values
    if ylabel:
        out.append(f'<text x="6" y="12" font-size="9" fill="{MUTED}" '
                   f'font-family="sans-serif">{_esc(ylabel)}</text>')
    if xlabel:
        out.append(f'<text x="{width - 12}" y="12" font-size="9" '
                   f'text-anchor="end" fill="{MUTED}" '
                   f'font-family="sans-serif">{_esc(xlabel)}</text>')
    out.append("</svg>")
    return "".join(out)


def _ticks(x0: float, x1: float, count: int = 6) -> List[Tuple[float, str]]:
    span = x1 - x0
    if span <= 0:
        return []
    raw = span / count
    mag = 10 ** int(np.floor(np.log10(raw)))
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if step >= raw:
            break
    first = np.ceil(x0 / step) * step
    return [(t, f"{t:,.0f}") for t in np.arange(first, x1 + step * 0.5, step)]


# --------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------

CSS = f"""
  *{{box-sizing:border-box}}
  body{{margin:0;padding:28px 32px;background:#fff;color:{INK};
       font:14px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif;max-width:1000px}}
  h1{{font-size:21px;margin:0 0 2px}}
  h2{{font-size:11px;letter-spacing:1.6px;text-transform:uppercase;
      color:{MUTED};margin:30px 0 8px;border-bottom:1px solid {RULE};
      padding-bottom:5px;font-weight:600}}
  .sub{{color:{MUTED};font-size:12px;margin-bottom:4px}}
  table{{border-collapse:collapse;width:100%;font-size:12.5px}}
  th{{text-align:right;color:{MUTED};font-weight:600;padding:5px 8px;
      border-bottom:1px solid {RULE};font-size:10.5px;letter-spacing:.6px;
      text-transform:uppercase}}
  th:first-child,td:first-child{{text-align:left}}
  td{{text-align:right;padding:5px 8px;border-bottom:1px solid #eef0f3;
      font-variant-numeric:tabular-nums}}
  .loss{{color:{LOSS_COLOUR}}} .gain{{color:{GAIN_COLOUR}}}
  .bar{{background:#eef0f3;height:6px;border-radius:3px;overflow:hidden;
        margin-top:3px}}
  .bar>i{{display:block;height:6px;background:{LOSS_COLOUR}}}
  .find{{border-left:3px solid {RULE};padding:2px 0 2px 11px;margin:11px 0}}
  .find.high{{border-color:{LOSS_COLOUR}}} .find.medium{{border-color:#d68910}}
  .find b{{font-weight:600}}
  .find .why{{color:{MUTED};font-size:12.5px;margin-top:2px}}
  .cost{{color:{MUTED};font-size:12px}}
  .row{{display:flex;gap:26px;flex-wrap:wrap;align-items:flex-start}}
  .stat{{min-width:82px}}
  .stat .k{{color:{MUTED};font-size:10px;letter-spacing:1px;
            text-transform:uppercase}}
  .stat .v{{font-size:19px;font-variant-numeric:tabular-nums}}
  .legend{{color:{MUTED};font-size:11.5px;margin:4px 0 0}}
  .sw{{display:inline-block;width:22px;height:3px;vertical-align:middle;
       margin-right:5px}}
  footer{{margin-top:34px;color:{MUTED};font-size:11px;
          border-top:1px solid {RULE};padding-top:9px}}
  @media print{{body{{padding:0;max-width:none}} h2{{page-break-after:avoid}}
                .find{{page-break-inside:avoid}}}}
"""


def _findings(items, limit: int = 12) -> str:
    out = []
    for ins in items[:limit]:
        cost = (f' <span class="cost">~{ins.time_cost:.2f}s</span>'
                if ins.time_cost >= 0.01 else "")
        why = (f'<div class="why">{_esc(ins.detail)}</div>'
               if ins.detail else "")
        out.append(f'<div class="find {ins.severity}"><b>{_esc(ins.corner or "--")}'
                   f'</b> {_esc(ins.message)}{cost}{why}</div>')
    return "".join(out) or f'<p class="sub">Nothing flagged.</p>'


def session_html(analysis: LapAnalysis, session: Session,
                 units: UnitSystem = METRIC) -> str:
    """Render a complete session report as one standalone HTML document."""
    u = units
    lap, ref = analysis.lap, analysis.reference
    pool = session.valid_laps or session.laps
    best = session.best_lap()
    env = GripEnvelope.from_laps(pool)
    use = limit_usage(lap, env)
    tb, _owner = theoretical_best(pool, analysis.corners)

    doc: List[str] = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Lap {lap.number} — {_esc(fmt_time(lap.lap_time))}</title>",
        f"<style>{CSS}</style></head><body>",
        f"<h1>Lap {lap.number} &mdash; {_esc(fmt_time(lap.lap_time))}</h1>",
    ]

    if ref is not None and ref is not lap:
        delta = lap.lap_time - ref.lap_time
        cls = "loss" if delta > 0 else "gain"
        doc.append(f"<div class='sub'>versus reference lap {ref.number} "
                   f"({_esc(fmt_time(ref.lap_time))}) "
                   f"<span class='{cls}'>{delta:+.3f}s</span> &middot; "
                   f"{len(analysis.corners)} corners &middot; "
                   f"{_esc(u.length_s(lap.length))}</div>")
    else:
        doc.append(f"<div class='sub'>{len(analysis.corners)} corners &middot; "
                   f"{_esc(u.length_s(lap.length))}</div>")
    doc.append(f"<div class='sub'>{_esc(session.vbo.path)}</div>")

    # ---- data quality: state what the findings are standing on
    from .quality import assess, significance_note
    q = assess(session, analysis.corners, u)
    tone = {"good": GAIN_COLOUR, "fair": "#b7791f", "poor": LOSS_COLOUR}[q.grade]
    doc.append(f"<h2>Data quality</h2>")
    doc.append(f"<p><b style='color:{tone}'>{_esc(q.grade)}</b> &mdash; "
               f"{_esc(q.headline)}</p>")
    doc.append("<table><tr><th>Check</th><th>Grade</th><th>Detail</th></tr>")
    for c in q.checks:
        col = {"good": GAIN_COLOUR, "fair": "#b7791f",
               "poor": LOSS_COLOUR}[c.grade]
        doc.append(f"<tr><td>{_esc(c.name)}</td>"
                   f"<td style='color:{col}'>{_esc(c.grade)}</td>"
                   f"<td style='text-align:left'>{_esc(c.summary)}</td></tr>")
    doc.append("</table>")
    weak = [c for c in q.checks if c.grade != "good"]
    if weak:
        for c in weak[:4]:
            doc.append(f"<p class='legend'><b>{_esc(c.name)}:</b> "
                       f"{_esc(c.detail)}</p>")
    note = significance_note(q, u)
    if note:
        doc.append(f"<p class='legend'>{_esc(note)}</p>")

    # ---- headline numbers
    doc.append("<h2>How the lap is spent</h2><div class='row'>")
    for key, val in (("cornering", f"{use['turning_pct']:.0f}%"),
                     ("braking", f"{use['braking_pct']:.0f}%"),
                     ("at limit", f"{use['at_limit_pct']:.0f}%"),
                     ("peak g", f"{use['peak_combined_g']:.2f}"),
                     ("theoretical best", fmt_time(tb))):
        doc.append(f"<div class='stat'><div class='k'>{key}</div>"
                   f"<div class='v'>{_esc(val)}</div></div>")
    doc.append("</div>")
    doc.append(f"<p class='legend'>Cornering and braking are measured against a "
               f"quarter of your own demonstrated limits; &ldquo;at limit&rdquo; "
               f"is within 10% of the {env.max_combined_g:.2f} g you sustain "
               f"across this session. Theoretical best stitches the fastest "
               f"version of each segment across {len(pool)} laps.</p>")

    # ---- map and traces
    shade = [(c.s_start, c.s_end) for c in analysis.corners]
    doc.append("<h2>Track</h2>")
    doc.append(track_svg(lap, analysis.corners))

    doc.append("<h2>Speed</h2>")
    series = []
    if ref is not None:
        series.append((u.d(ref.s), u.spd(ref.speed_kmh), REF_COLOUR, "reference"))
    if ref is not lap:
        series.append((u.d(lap.s), u.spd(lap.speed_kmh), CMP_COLOUR, "this lap"))
    doc.append(trace_svg(series, ylabel=f"speed ({u.speed_label})",
                         xlabel=f"distance ({u.dist_label})",
                         shade=[(u.d(a), u.d(b)) for a, b in shade]))
    if ref is not None and ref is not lap:
        doc.append(f"<p class='legend'>"
                   f"<span class='sw' style='background:{REF_COLOUR}'></span>"
                   f"lap {ref.number} (reference) &nbsp;&nbsp;"
                   f"<span class='sw' style='background:{CMP_COLOUR}'></span>"
                   f"lap {lap.number}</p>")

    if analysis.delta is not None:
        doc.append("<h2>Time delta</h2>")
        doc.append(trace_svg([(u.d(analysis.s_delta), analysis.delta,
                               "#333", "delta")],
                             ylabel="delta (s)",
                             xlabel=f"distance ({u.dist_label})",
                             zero_line=True, fill_sign=True,
                             shade=[(u.d(a), u.d(b)) for a, b in shade]))
        doc.append("<p class='legend'>Read the slope, not the height: rising "
                   "means time is being lost right there. Red is behind the "
                   "reference, green ahead.</p>")

    # ---- where the time went
    top = analysis.top_losses(8)
    if top:
        total = sum(c.dt for c in analysis.comparisons if c.dt > 0)
        doc.append("<h2>Where the time went</h2><table><tr><th>Corner</th>"
                   "<th>Lost</th><th>Share</th></tr>")
        for comp in top:
            pct = 100 * comp.dt / total if total else 0
            doc.append(f"<tr><td>{_esc(comp.corner.name)}</td>"
                       f"<td class='loss'>{comp.dt:+.3f}s</td>"
                       f"<td style='width:45%'><div class='bar'>"
                       f"<i style='width:{pct:.0f}%'></i></div></td></tr>")
        doc.append("</table>")

    if analysis.comparative:
        doc.append("<h2>What caused it</h2>")
        doc.append(_findings(analysis.comparative))
    if analysis.absolute:
        doc.append("<h2>Technique notes</h2>")
        doc.append(_findings(analysis.absolute))

    cons = consistency_insights(pool, analysis.corners, u=u)
    if cons:
        doc.append("<h2>Consistency</h2>")
        doc.append(_findings(cons, limit=8))

    # ---- corner summary across the session
    from .insights import corner_summaries
    summaries = corner_summaries(pool, analysis.corners)
    if summaries and len(pool) >= 2:
        doc.append(f"<h2>Corner summary</h2>")
        doc.append(f"<table><tr><th>Corner</th><th>Best</th><th>Avg</th>"
                   f"<th>Worst</th><th>Consistency</th><th>To gain</th>"
                   f"<th>Best on</th><th>Min {_esc(u.speed_label)}</th></tr>")
        for c in sorted(summaries, key=lambda r: -r.potential_gain_s):
            tone = ("#1e8449" if c.consistency_pct >= 95
                    else "#b7791f" if c.consistency_pct >= 88 else LOSS_COLOUR)
            doc.append(
                f"<tr><td>{_esc(c.name)} {c.corner.direction}</td>"
                f"<td>{c.best_s:.3f}</td><td>{c.mean_s:.3f}</td>"
                f"<td>{c.worst_s:.3f}</td>"
                f"<td style='color:{tone}'>{c.consistency_pct:.0f}%</td>"
                f"<td class='loss'>{c.potential_gain_s:+.3f}</td>"
                f"<td>lap {c.best_lap}</td>"
                f"<td>{u.spd(c.v_min_best_kmh):.1f}</td></tr>")
        doc.append("</table>")
        total = sum(c.potential_gain_s for c in summaries)
        avg = sum(c.consistency_pct for c in summaries) / len(summaries)
        doc.append("<div class='row' style='margin-top:12px'>")
        for k, v in (("matching your own best", f"{total:+.3f}s"),
                     ("corners", str(len(summaries))),
                     ("average consistency", f"{avg:.0f}%")):
            doc.append(f"<div class='stat'><div class='k'>{k}</div>"
                       f"<div class='v'>{_esc(v)}</div></div>")
        doc.append("</div>")
        doc.append("<p class='legend'>Timed over each corner's whole window — "
                   "braking zone, corner, and the run to the exit. Consistency "
                   "is 100 &times; (1 &minus; deviation/average): 100% means "
                   "every lap took the same time through here, whether or not "
                   "that time was good. &ldquo;To gain&rdquo; is average minus "
                   "best, so it is what repeatability alone would return; the "
                   "theoretical best above is the stricter figure, because it "
                   "stitches segments actually driven rather than averaging.</p>")

    # ---- laps and sectors
    from .sectors import best_sectors, build_sectors, comment_sectors, sector_times
    sec = build_sectors(session, ref or lap)
    show_sectors = sec.count >= 2
    owner: List[int] = []
    if show_sectors:
        _bt, owner = best_sectors(pool, sec)

    doc.append("<h2>Laps</h2><table><tr><th>Lap</th><th>Time</th><th>Delta</th>")
    if show_sectors:
        for i in range(sec.count):
            doc.append(f"<th>{_esc(sec.label(i))}</th>")
    doc.append("<th>Length</th><th>Note</th></tr>")
    for l in session.laps:
        d = l.lap_time - best.lap_time if best else 0.0
        note = "" if l.valid else _esc(l.note)
        doc.append(f"<tr><td>{l.number}</td><td>{_esc(fmt_time(l.lap_time))}</td>"
                   f"<td>{'&mdash;' if l is best else f'{d:+.3f}'}</td>")
        if show_sectors:
            for i, t in enumerate(sector_times(l, sec)):
                purple = i < len(owner) and owner[i] == l.number
                style = " style='color:#7c3aed;font-weight:600'" if purple else ""
                doc.append(f"<td{style}>{_esc(fmt_time(t))}</td>")
        doc.append(f"<td>{_esc(u.length_s(l.length))}</td><td>{note}</td></tr>")
    doc.append("</table>")
    if show_sectors:
        origin = ("split lines declared in the file" if sec.source == "file"
                  else f"{sec.count} equal sectors — the file declares no splits")
        doc.append(f"<p class='legend'>Sectors: {origin}. Purple is the "
                   f"fastest time in that sector across the session.</p>")
        embedded = comment_sectors(session)
        if embedded and sec.source != "file":
            doc.append("<p class='legend'>The file records its own sector "
                       "times, but not the boundaries they were measured "
                       "between, so they cannot be reconciled with these.</p>")

    # ---- corner detail
    d, sp = u.dist_label, u.speed_label
    doc.append(f"<h2>Corner detail</h2><table><tr><th>Corner</th><th>dT</th>"
               f"<th>R {d}</th><th>Brake {d}</th><th>Brake {sp}</th>"
               f"<th>Dec g</th><th>Min {sp}</th><th>Apex {d}</th>"
               f"<th>Lat g</th><th>Coast s</th><th>Exit {sp}</th></tr>")
    dts = {c.corner.index: c.dt for c in analysis.comparisons}
    for c, m in zip(analysis.corners, analysis.metrics):
        dt = dts.get(c.index)
        cls = "" if dt is None else (" class='loss'" if dt > 0.04
                                     else " class='gain'" if dt < -0.04 else "")
        doc.append(
            f"<tr><td>{_esc(c.name)}{c.direction}</td>"
            f"<td{cls}>{'&mdash;' if dt is None else f'{dt:+.3f}'}</td>"
            f"<td>{u.d(c.min_radius):.0f}</td>"
            f"<td>{'&mdash;' if m.s_brake is None else f'{u.d(m.s_brake):.0f}'}</td>"
            f"<td>{'&mdash;' if m.v_brake is None else f'{u.spd(m.v_brake):.0f}'}</td>"
            f"<td>{abs(m.peak_decel_g):.2f}</td>"
            f"<td>{u.spd(m.v_min):.1f}</td>"
            f"<td>{u.d(m.apex_offset):+.0f}</td>"
            f"<td>{m.peak_lat_g:.2f}</td>"
            f"<td>{m.coast_time:.2f}</td>"
            f"<td>{u.spd(m.v_exit_plus):.0f}</td></tr>")
    doc.append("</table>")

    doc.append("<footer>Generated by GARW Genie Lap Analysis. Corner numbering follows the "
               "reference lap. Time costs attributed to individual inputs are "
               "apportionments of a measured corner delta &mdash; the corner "
               "total is measured, the split within it is a heuristic."
               "</footer></body></html>")
    return "".join(doc)
