"""Console / markdown reporting."""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from .corners import GripEnvelope, analyse_lap
from .insights import (LapAnalysis, consistency_insights, limit_usage,
                       theoretical_best)
from .laps import Session


def fmt_time(sec: float) -> str:
    if sec is None or not np.isfinite(sec):
        return "--:--.---"
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}:{s:06.3f}" if m else f"{s:.3f}"


def fmt_delta(sec: float) -> str:
    return f"{sec:+.3f}"


def session_summary(session: Session) -> str:
    lines: List[str] = []
    best = session.best_lap()
    lines.append(f"File          : {session.vbo.path}")
    lines.append(f"Samples       : {session.vbo.n_samples} @ "
                 f"{session.vbo.sample_rate:.0f} Hz")
    lines.append(f"Channels      : {', '.join(sorted(session.vbo.channels))}")
    lines.append(f"Start/finish  : {session.sf_source}")
    lines.append(f"Laps detected : {len(session.laps)} "
                 f"({len(session.valid_laps)} clean)")
    lines.append("")
    lines.append(f"{'Lap':>4}  {'Time':>10}  {'Delta':>8}  {'Len m':>7}  Note")
    lines.append("-" * 58)
    for lap in session.laps:
        d = lap.lap_time - best.lap_time if best else 0.0
        flag = "" if lap.valid else f"  [{lap.note}]"
        lines.append(f"{lap.number:>4}  {fmt_time(lap.lap_time):>10}  "
                     f"{fmt_delta(d):>8}  {lap.length:>7.0f}{flag}")
    return "\n".join(lines)


def corner_table(analysis: LapAnalysis) -> str:
    hdr = (f"{'Corner':<15}{'R m':>6}{'Brake m':>9}{'Brk km/h':>10}"
           f"{'Dec g':>7}{'Min km/h':>10}{'Apex off':>10}{'Lat g':>7}"
           f"{'Coast s':>9}{'Exit km/h':>11}")
    lines = [hdr, "-" * len(hdr)]
    for c, m in zip(analysis.corners, analysis.metrics):
        lines.append(
            f"{corner_label(c):<15}{c.min_radius:>6.0f}"
            f"{(m.s_brake if m.s_brake is not None else float('nan')):>9.0f}"
            f"{(m.v_brake if m.v_brake is not None else float('nan')):>10.1f}"
            f"{abs(m.peak_decel_g):>7.2f}{m.v_min:>10.1f}"
            f"{m.apex_offset:>+10.0f}{m.peak_lat_g:>7.2f}"
            f"{m.coast_time:>9.2f}{m.v_exit_plus:>11.1f}")
    return "\n".join(lines)


def delta_table(analysis: LapAnalysis) -> str:
    if not analysis.comparisons:
        return ""
    hdr = (f"{'Cnr':<5}{'dT s':>8}{'dBrake m':>10}{'dMin km/h':>11}"
           f"{'dExit km/h':>12}{'dCoast s':>10}")
    lines = [hdr, "-" * len(hdr)]
    for comp in analysis.comparisons:
        r, c = comp.ref, comp.cmp
        db = (c.s_brake - r.s_brake) if (r.s_brake is not None
                                         and c.s_brake is not None) else float("nan")
        lines.append(
            f"{comp.corner.name:<5}{comp.dt:>+8.3f}{db:>+10.1f}"
            f"{c.v_min - r.v_min:>+11.1f}"
            f"{c.v_exit_plus - r.v_exit_plus:>+12.1f}"
            f"{c.coast_time - r.coast_time:>+10.2f}")
    return "\n".join(lines)


def insight_report(analysis: LapAnalysis, session: Optional[Session] = None,
                   max_items: int = 14, units=None) -> str:
    lines: List[str] = []
    lap = analysis.lap
    ref = analysis.reference

    lines.append("=" * 70)
    if ref is not None and ref is not lap:
        lines.append(f"LAP {lap.number}  {fmt_time(lap.lap_time)}   vs reference "
                     f"lap {ref.number}  {fmt_time(ref.lap_time)}   "
                     f"({fmt_delta(lap.lap_time - ref.lap_time)})")
    else:
        lines.append(f"LAP {lap.number}  {fmt_time(lap.lap_time)}   "
                     f"(reference lap — absolute analysis)")
    lines.append(f"{len(analysis.corners)} corners detected over "
                 f"{lap.length:.0f} m")
    lines.append("=" * 70)

    if analysis.comparisons:
        top = analysis.top_losses(6)
        if top:
            lines.append("")
            lines.append("WHERE THE TIME WENT  (measured from the delta trace)")
            total = sum(c.dt for c in analysis.comparisons if c.dt > 0)
            for comp in top:
                pct = 100 * comp.dt / total if total > 0 else 0
                bar = "#" * max(1, int(round(pct / 4)))
                lines.append(f"  {comp.corner.name:<4} {comp.dt:+.3f}s  "
                             f"{bar:<25} {pct:.0f}% of losses")
            gained = [c for c in analysis.comparisons if c.dt < -0.02]
            if gained:
                g = ", ".join(f"{c.corner.name} {c.dt:+.3f}s"
                              for c in sorted(gained, key=lambda c: c.dt)[:4])
                lines.append(f"  gained: {g}")

    if analysis.comparative:
        lines.append("")
        lines.append("WHAT CAUSED IT  (ordered by measured loss)")
        _emit(lines, analysis.comparative, max_items)
    elif not analysis.comparisons:
        pass

    if analysis.absolute:
        lines.append("")
        lines.append("TECHNIQUE NOTES  (hold regardless of reference lap)")
        _emit(lines, analysis.absolute, max_items)

    if not analysis.comparative and not analysis.absolute:
        lines.append("")
        lines.append("  Nothing significant flagged on this lap.")

    lines.append("")
    lines.append("CORNER DETAIL")
    lines.append(corner_table(analysis))

    dt = delta_table(analysis)
    if dt:
        lines.append("")
        lines.append("DELTA vs REFERENCE  (positive = slower / later / more)")
        lines.append(dt)

    if session is not None:
        pool = session.valid_laps or session.laps
        lines.append("")
        block = elevation_block(analysis, units)
        if block:
            lines.append(block)
            lines.append("")
        lines.append(corner_summary_block(session, analysis, units))
        lines.append("")
        lines.append(sector_block(session, analysis, units))
        tb, owner = theoretical_best(pool, analysis.corners)
        best = session.best_lap()
        lines.append("")
        lines.append("SESSION POTENTIAL")
        lines.append(f"  Best actual lap    : {fmt_time(best.lap_time)} "
                     f"(lap {best.number})")
        lines.append(f"  Theoretical best   : {fmt_time(tb)}  "
                     f"({fmt_delta(tb - best.lap_time)} vs best)")
        contributors = sorted(set(owner.values()))
        lines.append(f"  Built from laps    : "
                     f"{', '.join(str(n) for n in contributors)}")

        env = GripEnvelope.from_laps(pool)
        use = limit_usage(analysis.lap, env)
        lines.append("")
        lines.append("HOW THE LAP IS SPENT")
        lines.append(f"  Cornering          : {use['turning_pct']:.0f}% of the "
                     f"lap above a quarter of your lateral limit")
        lines.append(f"  Braking            : {use['braking_pct']:.0f}%")
        lines.append(f"  Near the limit     : {use['at_limit_pct']:.0f}% of the "
                     f"lap, {use['cornering_at_limit_pct']:.0f}% of the "
                     f"cornering")
        lines.append(f"  Peak combined      : {use['peak_combined_g']:.2f} g "
                     f"on this lap; {env.max_combined_g:.2f} g is the level "
                     f"you sustain\n                       across the session "
                     f"(97th percentile, so one spike cannot set it)")

        cons = consistency_insights(pool, analysis.corners)
        if cons:
            lines.append("")
            lines.append("CONSISTENCY")
            for ins in cons[:8]:
                lines.append(f"  [{ins.corner:<4}] {ins.message}")

    return "\n".join(lines)


def corner_label(corner, width: int = 13) -> str:
    """`name + direction`, trimmed so a long name cannot break the columns."""
    name = corner.name
    if len(name) > width - 2:
        name = name[:width - 3].rstrip() + "\u2026"
    return f"{name} {corner.direction}"


def elevation_block(analysis, units=None) -> str:
    """Gradient and load through each corner, where the log supports it."""
    from . import elevation as el
    from .units import METRIC
    u = units or METRIC

    lap = analysis.reference or analysis.lap
    if not el.usable(lap):
        return ""
    rows = {e.corner_index: e
            for e in el.analyse_lap(lap, analysis.corners, analysis.metrics)}
    if not rows:
        return ""

    lines = [f"ELEVATION  ({u.d_s(el.total_relief(lap))} of relief over the lap)"]
    hdr = (f"{'Corner':<15}{'Brake %':>9}{'Exit %':>8}{'Change':>9}"
           f"{'Load':>8}{'Brake dist':>12}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for c in analysis.corners:
        e = rows.get(c.index)
        if e is None:
            continue
        penalty = (f"{u.d(e.braking_penalty_m):+.0f} {u.dist_label}"
                   if abs(e.braking_penalty_m) > 1.0 else "—")
        lines.append(
            f"{corner_label(c):<15}{e.grade_braking * 100:>+8.1f}%"
            f"{e.grade_exit * 100:>+7.1f}%{u.d(e.change_m):>+9.0f}"
            f"{e.min_load_factor:>8.2f}{penalty:>12}")
    lines.append("")
    lines.append("  Load is the lightest the tires get through the corner, "
                 "1.00 being static:")
    lines.append("  a crest costs grip, a compression lends it. Brake dist is "
                 "what the slope adds")
    lines.append("  to or takes off your stopping distance versus flat ground.")
    lines.append("  GPS altitude is noisy in meters, so this is smoothed hard "
                 "and small figures")
    lines.append("  mean little.")
    return "\n".join(lines)


def corner_summary_block(session, analysis, units=None) -> str:
    """Every corner across the whole session, rather than lap against lap."""
    from .insights import corner_summaries
    from .units import METRIC
    u = units or METRIC

    pool = session.valid_laps or session.laps
    rows = corner_summaries(pool, analysis.corners)
    if not rows or len(pool) < 2:
        return ""

    lines = [f"CORNER SUMMARY  (all {len(pool)} clean laps)"]
    hdr = (f"{'Corner':<15}{'Best':>8}{'Avg':>8}{'Worst':>8}{'Consist':>9}"
           f"{'To gain':>9}{'Best on':>9}{'Min ' + u.speed_label:>11}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for c in sorted(rows, key=lambda r: -r.potential_gain_s):
        lines.append(
            f"{corner_label(c.corner):<15}{c.best_s:>8.3f}{c.mean_s:>8.3f}"
            f"{c.worst_s:>8.3f}{c.consistency_pct:>8.0f}%"
            f"{c.potential_gain_s:>+9.3f}{('lap ' + str(c.best_lap)):>9}"
            f"{u.spd(c.v_min_best_kmh):>11.1f}")

    total = sum(c.potential_gain_s for c in rows)
    mean_consistency = sum(c.consistency_pct for c in rows) / len(rows)
    lines.append("")
    lines.append(f"  Matching your own best in every corner: {total:+.3f}s")
    lines.append(f"  Average consistency: {mean_consistency:.0f}% "
                 f"across {len(rows)} corners")
    lines.append("")
    lines.append("  Timed over each corner's whole window — braking zone, "
                 "corner, and the run to")
    lines.append("  the exit — so it covers what you do at the corner, not "
                 "the geometry alone.")
    lines.append("  Consistency is 100 x (1 - deviation/average): 100% means "
                 "every lap took the")
    lines.append("  same time through here, whether or not that time was any "
                 "good.")
    lines.append("  'To gain' is average minus best, so it is what "
                 "repeatability alone would")
    lines.append("  return. It is optimistic: the theoretical best above is "
                 "the stricter figure,")
    lines.append("  because it stitches segments you actually drove rather "
                 "than averaging.")
    return "\n".join(lines)


def sector_block(session, analysis, units=None) -> str:
    """Sector times for every lap, and the best of each."""
    from .sectors import (best_sectors, build_sectors, comment_sectors,
                          sector_times)
    from .units import METRIC
    u = units or METRIC

    ref = analysis.reference or analysis.lap
    sec = build_sectors(session, ref)
    pool = session.valid_laps or session.laps
    if sec.count < 2 or not pool:
        return ""

    origin = ("split lines declared in the file" if sec.source == "file"
              else f"{sec.count} equal sectors (the file declares no splits)")
    lines = [f"SECTORS  ({origin})"]
    hdr = f"{'Lap':>4}  " + "  ".join(f"{sec.label(i):>9}"
                                      for i in range(sec.count))
    lines.append(hdr + f"  {'Lap':>10}")
    lines.append("-" * len(hdr + "  " + " " * 10))

    best, owner = best_sectors(pool, sec)
    for lap in session.laps:
        times = sector_times(lap, sec)
        cells = []
        for i, t in enumerate(times):
            mark = "*" if i < len(owner) and owner[i] == lap.number else " "
            cells.append(f"{fmt_time(t):>8}{mark}")
        lines.append(f"{lap.number:>4}  " + "  ".join(cells)
                     + f"  {fmt_time(lap.lap_time):>10}")
    lines.append("")
    lines.append(f"{'best':>4}  "
                 + "  ".join(f"{fmt_time(t):>8} " for t in best)
                 + f"  {fmt_time(sum(best)):>10}  "
                 + f"(from laps {', '.join(str(o) for o in owner)})")

    embedded = comment_sectors(session)
    if embedded:
        lines.append("")
        lines.append("  The file also records its own sector times:")
        for lap_no in sorted(embedded)[:6]:
            vals = "  ".join(f"{v:6.2f}" for v in embedded[lap_no])
            lines.append(f"    lap {lap_no}: {vals}")
        if sec.source != "file":
            lines.append("  Those came from the logger and use its own "
                         "boundaries, which are not recorded")
            lines.append("  in the file — so they will not match the sectors "
                         "above and cannot be")
            lines.append("  compared lap to lap here.")
    return "\n".join(lines)


def quality_block(session, corners=(), units=None) -> str:
    """What the analysis is standing on."""
    from .quality import assess, significance_note
    q = assess(session, corners, units)
    lines = [f"DATA QUALITY  ({q.actionable_grade}: {q.headline})"]
    for c in q.checks:
        tag = " (device limit)" if c.inherent and c.grade != "good" else ""
        lines.append(f"  [{c.grade:<4}] {c.name:<17} {c.summary}{tag}")
    poor = [c for c in q.checks if c.grade != "good"]
    if poor:
        lines.append("")
        for c in poor[:4]:
            for chunk in _wrap(f"{c.name}: {c.detail}", 66):
                lines.append(f"    {chunk}")
            lines.append("")
    note = significance_note(q, units)
    if note:
        lines.append("  " + note)
    return "\n".join(lines).rstrip()


def _emit(lines: List[str], items, max_items: int) -> None:
    tags = {"high": "!!", "medium": "! ", "low": "  "}
    for ins in items[:max_items]:
        head = f"  {tags[ins.severity]} [{ins.corner or '--':<4}] {ins.message}"
        if ins.time_cost >= 0.01:
            head += f"  (~{ins.time_cost:.2f}s)"
        lines.append(head)
        if ins.detail:
            for chunk in _wrap(ins.detail, 64):
                lines.append(f"          {chunk}")


def _wrap(text: str, width: int) -> List[str]:
    words = text.split()
    out, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def trend_report(trend, units=None) -> str:
    """Progress at one circuit, across recorded sessions."""
    from .units import METRIC
    u = units or METRIC
    import time as _time

    def when(ts):
        return _time.strftime("%d %b %Y", _time.localtime(ts)) if ts else "?"

    faster = trend.improvement_s
    verdict = ("no change" if abs(faster) < 0.005 else
               f"{abs(faster):.3f}s {'faster' if faster > 0 else 'slower'} "
               f"than the first visit")
    lines = ["=" * 70,
             f"{trend.label}",
             f"{trend.sessions} sessions, {when(trend.first_at)} "
             f"to {when(trend.last_at)}",
             "=" * 70,
             "",
             f"  First session : {when(trend.first_at)}  "
             f"{fmt_time(trend.first_best_s)}   {trend.first_source}",
             f"  Latest        : {when(trend.last_at)}  "
             f"{fmt_time(trend.last_best_s)}   {trend.last_source}",
             f"  Best ever     : {when(trend.best_at)}  "
             f"{fmt_time(trend.best_ever_s)}   {trend.best_source}",
             "",
             f"  {verdict.capitalize()}."]

    if not trend.corners:
        lines.append("")
        lines.append("  No corners could be matched between sessions. Corner "
                     "numbering is not")
        lines.append("  stable, so matching is by apex position — if the "
                     "sessions are at different")
        lines.append("  circuits, or the layout was different, nothing will "
                     "line up.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"CORNER BY CORNER  ({trend.last_source} versus "
                 f"{trend.first_source})")
    hdr = (f"{'Corner':<15}{'Brake':>10}{'Min spd':>10}{'Exit spd':>10}"
           f"{'Coast':>9}{'Repeatability':>16}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    flagged = False
    for c in sorted(trend.corners, key=lambda c: -abs(c.d_v_exit_kmh)):
        # A brake point that appears to have moved further than this between
        # sessions is far more likely to be the detector picking a different
        # start to a gradual deceleration than the driver actually doing it.
        # Say so rather than presenting it as a finding.
        suspect = c.d_brake_m is not None and abs(c.d_brake_m) > 60.0
        flagged = flagged or suspect
        brake = ("     —" if c.d_brake_m is None
                 else f"{u.d(c.d_brake_m):+8.0f}?" if suspect
                 else f"{u.d(c.d_brake_m):+9.0f}")
        rep = "—"
        if c.scatter_now_m is not None and c.scatter_then_m is not None:
            rep = (f"{u.d(c.scatter_then_m):.0f}->"
                   f"{u.d(c.scatter_now_m):.0f} {u.dist_label}")
        lines.append(
            f"{c.name + ' ' + c.direction:<7}{brake}{u.spd(c.d_v_min_kmh):+10.1f}"
            f"{u.spd(c.d_v_exit_kmh):+10.1f}{c.d_coast_s:+9.2f}{rep:>16}")

    lines.append("")
    lines.append(f"  Brake is later ({u.dist_label} closer to the apex); "
                 f"speeds are {u.speed_label}.")
    lines.append("  Repeatability is the spread of your brake point within a "
                 "session, then and now.")
    if flagged:
        lines.append("  ? marks a brake-point change too large to trust: on a "
                     "gradual deceleration the")
        lines.append("    detector can pick a different start to the same "
                     "braking event. Read those as")
        lines.append("    'something changed here', not as a measurement.")
    if trend.sessions < 4:
        lines.append(f"  Only {trend.sessions} sessions recorded — treat "
                     "corner-level trends as provisional")
        lines.append("    until there are more.")
    return "\n".join(lines)
