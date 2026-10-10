"""
The coaching layer.

Three independent sources of insight, because they answer different questions:

1. Comparative  — "where is this lap losing time against the reference, and
                   which input caused it?"
2. Absolute     — "even on the best lap, where is technique costing time?"
                   Judged against the driver's own demonstrated grip envelope,
                   not an assumed friction coefficient.
3. Consistency  — "which corners can't you repeat?" Scatter in a brake point
                   across a session is usually worth more lap time than any
                   single-corner refinement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from . import geometry as geo
from .corners import (Corner, CornerMetrics, GripEnvelope, analyse_lap,
                      detect_corners)
from .laps import LapTrack, Session, delta_time
from .units import METRIC, UnitSystem

SEV_HIGH, SEV_MED, SEV_LOW = "high", "medium", "low"

# --------------------------------------------------------------------------
# Corner priority and grip potential
#
# These implement long-standing coaching principles rather than anything novel:
# that a corner opening onto a long straight is worth more than one that does
# not, that the limit is set by radius and available grip, and that abrupt
# inputs cost grip. What the data adds is the arithmetic — which corner, how
# much, and how many tenths.
# --------------------------------------------------------------------------


def straight_after(corner: Corner, corners: Sequence[Corner],
                   lap_length: float) -> float:
    """Meters of open track between this corner's exit and the next turn-in."""
    later = [c for c in corners if c.s_start > corner.s_end]
    nxt = min(later, key=lambda c: c.s_start).s_start if later else lap_length
    return float(max(0.0, nxt - corner.s_end))


def exit_leverage(corners: Sequence[Corner], lap_length: float) -> Dict[int, float]:
    """Each corner's following straight, as a fraction of the longest one.

    A tenth found at the exit of the corner onto the back straight is carried
    all the way down it; the same tenth at the exit of a corner that leads
    straight into another braking zone is given back almost immediately. This
    is the ratio that decides where practice time is worth spending.
    """
    lengths = {c.index: straight_after(c, corners, lap_length) for c in corners}
    longest = max(lengths.values()) if lengths else 0.0
    if longest <= 0:
        return {k: 0.0 for k in lengths}
    return {k: v / longest for k, v in lengths.items()}


def grip_limited_speed(radius_m: float, lat_g: float) -> float:
    """Steady-state cornering speed in km/h for a radius and a lateral g.

    v = sqrt(a_lat * r). Deliberately fed with the driver's *own* demonstrated
    lateral g rather than an assumed coefficient, so the answer is "the grip
    you have already used elsewhere allows this", not "a physics textbook says
    this".
    """
    if radius_m <= 0 or lat_g <= 0:
        return 0.0
    return float(np.sqrt(lat_g * 9.80665 * radius_m) * 3.6)


def abruptness(lap: LapTrack, s0: float, s1: float) -> float:
    """Peak rate of change of total grip demand, in g per second.

    Smoothness is not an aesthetic preference: the tire needs time to build
    slip angle, and a snatched input asks for grip that has not arrived yet.
    """
    i0, i1 = lap.idx(s0), lap.idx(s1)
    if i1 <= i0 + 2:
        return 0.0
    g = lap.combined_g()[i0:i1 + 1]
    t = lap.t[i0:i1 + 1]
    dt = np.diff(t)
    dt[dt <= 0] = 1e-3
    return float(np.max(np.abs(np.diff(g) / dt)))


@dataclass
class Insight:
    corner: Optional[str]
    kind: str                 # 'braking' | 'entry' | 'apex' | 'exit' | 'grip' | ...
    severity: str
    time_cost: float          # seconds, best estimate (0 if unquantified)
    message: str
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        where = f"{self.corner}: " if self.corner else ""
        cost = f" (~{self.time_cost:.2f}s)" if self.time_cost >= 0.01 else ""
        return f"{where}{self.message}{cost}"


@dataclass
class LineDeviation:
    """How far off the reference line a corner was driven, in meters.

    Signed so that positive is to the left of the reference's direction of
    travel; `wide_*` restates that as wide/tight relative to the corner's own
    handedness, which is what a driver actually thinks in.
    """
    at_turnin: float
    at_apex: float
    at_exit: float
    max_abs: float
    mean_abs: float
    direction: str                  # 'L' or 'R', the corner's handedness

    def _wide(self, offset: float) -> float:
        # in a left-hander, being to the *right* of the reference is wider
        return -offset if self.direction == "L" else offset

    @property
    def wide_turnin(self) -> float:
        return self._wide(self.at_turnin)

    @property
    def wide_apex(self) -> float:
        return self._wide(self.at_apex)

    @property
    def wide_exit(self) -> float:
        return self._wide(self.at_exit)


@dataclass
class CornerComparison:
    corner: Corner
    ref: CornerMetrics
    cmp: CornerMetrics
    dt: float                       # time lost in this corner's window (+ = slower)
    line: Optional[LineDeviation] = None
    insights: List[Insight] = field(default_factory=list)


@dataclass
class LapAnalysis:
    lap: LapTrack
    corners: List[Corner]
    metrics: List[CornerMetrics]
    reference: Optional[LapTrack] = None
    comparisons: List[CornerComparison] = field(default_factory=list)
    #: everything, comparative first — kept for convenience
    insights: List[Insight] = field(default_factory=list)
    #: findings that explain a *measured* delta against the reference lap
    comparative: List[Insight] = field(default_factory=list)
    #: technique observations that hold with no reference lap at all
    absolute: List[Insight] = field(default_factory=list)
    s_delta: Optional[np.ndarray] = None
    delta: Optional[np.ndarray] = None

    def top_losses(self, n: int = 5, floor: float = 0.01) -> List[CornerComparison]:
        return sorted([c for c in self.comparisons if c.dt > floor],
                      key=lambda c: -c.dt)[:n]

    def metrics_for(self, corner_index: int) -> Optional[CornerMetrics]:
        for c, m in zip(self.corners, self.metrics):
            if c.index == corner_index:
                return m
        return None


# --------------------------------------------------------------------------
# Thresholds — tuned so a normal amount of driver noise stays quiet
# --------------------------------------------------------------------------

T_BRAKE_M = 6.0        # meters of brake-point difference worth mentioning
T_SPEED_KMH = 1.5      # km/h difference worth mentioning
T_COAST_S = 0.12       # seconds of coasting difference
T_TIME_S = 0.04        # seconds of corner time worth reporting
T_APEX_M = 12.0        # meters of apex-position shift


def _sev(cost: float) -> str:
    if cost >= 0.15:
        return SEV_HIGH
    if cost >= 0.06:
        return SEV_MED
    return SEV_LOW


def _fmt_m(v: float, u: UnitSystem = METRIC) -> str:
    """Magnitude of a distance, in the display units."""
    return u.d_abs_s(v)


# --------------------------------------------------------------------------
# Comparative analysis
# --------------------------------------------------------------------------


T_LINE_M = 1.2         # meters of line difference worth mentioning


def compare_corner(corner: Corner, ref: CornerMetrics, cmp_: CornerMetrics,
                   dt: float, u: UnitSystem = METRIC,
                   line: "Optional[LineDeviation]" = None) -> List[Insight]:
    """Explain a corner-time delta in terms of driver inputs."""
    out: List[Insight] = []
    sev = _sev(abs(dt))
    name = corner.name

    d_min = cmp_.v_min - ref.v_min
    d_exit = cmp_.v_exit_plus - ref.v_exit_plus
    d_coast = cmp_.coast_time - ref.coast_time
    d_apex = cmp_.s_vmin - ref.s_vmin

    # --- braking point ----------------------------------------------------
    if ref.s_brake is not None and cmp_.s_brake is not None:
        d_brake = cmp_.s_brake - ref.s_brake
        if abs(d_brake) > T_BRAKE_M:
            if d_brake < 0:
                out.append(Insight(
                    name, "braking", sev, max(dt, 0.0) * 0.6,
                    f"braking {_fmt_m(d_brake, u)} earlier than the reference",
                    f"brake at {u.d_s(cmp_.s_brake)} vs {u.d_s(ref.s_brake)}; "
                    f"entry speed {u.spd(cmp_.v_brake):.1f} vs "
                    f"{u.spd_s(ref.v_brake)}. "
                    f"Total braking distance {u.d_s(cmp_.brake_dist)} vs "
                    f"{u.d_s(ref.brake_dist)}."))
            else:
                kind = "braking"
                if d_min < -T_SPEED_KMH:
                    msg = (f"braked {_fmt_m(d_brake, u)} later but lost "
                           f"{u.spd_s(abs(d_min))} at the apex — "
                           "overshooting entry")
                else:
                    msg = f"braking {_fmt_m(d_brake, u)} later than the reference"
                out.append(Insight(name, kind, sev, max(dt, 0.0) * 0.5, msg,
                                   f"brake at {u.d_s(cmp_.s_brake)} vs "
                                   f"{u.d_s(ref.s_brake)}."))
        d_peak = cmp_.peak_decel_g - ref.peak_decel_g
        if d_peak > 0.12:   # less deceleration (peak_decel is negative)
            out.append(Insight(
                name, "braking", _sev(abs(dt) * 0.5), 0.0,
                f"peak braking only {abs(cmp_.peak_decel_g):.2f} g vs "
                f"{abs(ref.peak_decel_g):.2f} g on the reference",
                "Softer initial brake application stretches the braking zone; "
                "hit peak pressure sooner and bleed off into the corner."))

    # --- entry / apex speed ------------------------------------------------
    if abs(d_min) > T_SPEED_KMH:
        share = 0.5 if abs(d_exit) > T_SPEED_KMH else 0.7
        if d_min < 0:
            out.append(Insight(
                name, "apex", sev, max(dt, 0.0) * share,
                f"{u.spd_s(abs(d_min))} slower at minimum speed "
                f"({u.spd(cmp_.v_min):.1f} vs {u.spd_s(ref.v_min)})",
                "Minimum speed is the single biggest lever in a corner — it "
                "sets both the time in the corner and the speed you start the "
                "next straight from."))
        else:
            out.append(Insight(
                name, "apex", SEV_LOW, 0.0,
                f"{u.spd_s(d_min)} faster at minimum speed",
                "Faster through the apex. Check the exit numbers below — if "
                "exit speed is down, this was too much entry speed."))

    if abs(d_apex) > T_APEX_M:
        where = "later" if d_apex > 0 else "earlier"
        out.append(Insight(
            name, "entry", _sev(abs(dt) * 0.4), 0.0,
            f"minimum speed reached {_fmt_m(d_apex, u)} {where} in the corner",
            f"Min-speed point at {u.d_s(cmp_.s_vmin)} vs "
            f"{u.d_s(ref.s_vmin)}. "
            + ("A later minimum usually means a later turn-in or a tighter "
               "line than the reference."
               if d_apex > 0 else
               "An earlier minimum means you finished slowing too soon and "
               "then carried a low speed to the apex.")))

    # --- exit -------------------------------------------------------------
    if abs(d_coast) > T_COAST_S and d_coast > 0:
        out.append(Insight(
            name, "exit", _sev(d_coast * 0.8), d_coast * 0.5,
            f"{d_coast:.2f}s more coasting between brake release and throttle",
            f"{cmp_.coast_time:.2f}s / {u.d_s(cmp_.coast_dist)} coasting vs "
            f"{ref.coast_time:.2f}s / {u.d_s(ref.coast_dist)}. Dead time with "
            "no brake and no throttle is pure loss — overlap the transition."))

    if abs(d_exit) > T_SPEED_KMH:
        if d_exit < 0:
            out.append(Insight(
                name, "exit", sev, max(dt, 0.0) * 0.5,
                f"{u.spd_s(abs(d_exit))} down {u.d_s(75)} after the exit "
                f"({u.spd(cmp_.v_exit_plus):.1f} vs "
                f"{u.spd_s(ref.v_exit_plus)})",
                "Exit-speed deficits compound all the way down the following "
                "straight, so this costs more than the corner time suggests."))
        else:
            out.append(Insight(
                name, "exit", SEV_LOW, 0.0,
                f"{u.spd_s(d_exit)} up {u.d_s(75)} after the exit", ""))

    # --- line ---------------------------------------------------------------
    # Until now this could only ever say "no clear input difference, probably
    # line". Now it can say where, and by how much.
    if line is not None:
        sev_line = _sev(abs(dt) * 0.5)
        if abs(line.wide_turnin) > T_LINE_M:
            wide = line.wide_turnin > 0
            out.append(Insight(
                name, "line", sev_line, 0.0,
                f"turned in {u.d_abs_s(line.wide_turnin, 1)} "
                f"{'wider' if wide else 'tighter'} than the reference",
                "A wider entry opens the radius and lets you carry more speed, "
                "but costs distance and delays the apex; a tighter one does the "
                "reverse. Read it with the minimum-speed figure above."
                if wide else
                "A tighter entry shortens the corner but closes the radius, "
                "which caps how much speed you can carry through it."))
        if abs(line.wide_apex) > T_LINE_M:
            wide = line.wide_apex > 0
            out.append(Insight(
                name, "line", sev_line, 0.0,
                f"apex {u.d_abs_s(line.wide_apex, 1)} "
                f"{'wider' if wide else 'tighter'} than the reference",
                "Missing the apex wide leaves radius unused; clipping it "
                "tighter than the reference usually means running wide on exit "
                "instead."))
        if abs(line.wide_exit) > T_LINE_M and abs(line.wide_apex) <= T_LINE_M * 2:
            wide = line.wide_exit > 0
            out.append(Insight(
                name, "line", sev_line, 0.0,
                f"exit {u.d_abs_s(line.wide_exit, 1)} "
                f"{'wider' if wide else 'tighter'} than the reference",
                "Using less road on exit than the reference means unwinding "
                "the wheel later, which holds the car in lateral load when it "
                "could be accelerating."
                if not wide else
                "Running wider on exit than the reference — either the entry "
                "carried too much speed, or the throttle came in early."))

    if not out and abs(dt) > T_TIME_S:
        out.append(Insight(
            name, "line", _sev(abs(dt)), max(dt, 0.0),
            f"{abs(dt):.2f}s {'lost' if dt > 0 else 'gained'} with no clear "
            "cause in the inputs or the line",
            "Braking, apex speed, exit and line all match within tolerance. "
            "Traffic, a missed shift, or a surface difference are what is "
            "left."))
    return out


def line_deviation(reference: LapTrack, other: LapTrack) -> np.ndarray:
    """Signed lateral offset of `other` from `reference`, on the reference grid."""
    return geo.line_offset(reference.x, reference.y, other.x, other.y,
                           ds=reference.ds)


def corner_line(corner: Corner, offset: np.ndarray, reference: LapTrack,
                ref_m: CornerMetrics) -> LineDeviation:
    """Sample the offset trace at the points of the corner that matter."""
    def at(s: Optional[float]) -> float:
        if s is None:
            return 0.0
        return float(np.interp(s, reference.s, offset))

    i0 = reference.idx(corner.s_start)
    i1 = max(reference.idx(corner.s_end), i0 + 1)
    span = np.abs(offset[i0:i1 + 1])
    return LineDeviation(
        at_turnin=at(ref_m.s_turnin),
        at_apex=at(ref_m.s_vmin),
        at_exit=at(corner.s_end),
        max_abs=float(span.max()) if span.size else 0.0,
        mean_abs=float(span.mean()) if span.size else 0.0,
        direction=corner.direction)


def compare_laps(reference: LapTrack, other: LapTrack,
                 corners: Sequence[Corner], u: UnitSystem = METRIC
                 ) -> tuple[List[CornerComparison], np.ndarray, np.ndarray]:
    ref_m = {c.index: m for c, m in zip(corners, analyse_lap(reference, corners))}
    cmp_m = {c.index: m for c, m in zip(corners, analyse_lap(other, corners))}
    s_grid, dvec = delta_time(reference, other)
    offset = line_deviation(reference, other)

    comps: List[CornerComparison] = []
    for c in corners:
        d0 = float(np.interp(c.s_win_start, s_grid, dvec))
        d1 = float(np.interp(c.s_win_end, s_grid, dvec))
        dt = d1 - d0
        line = corner_line(c, offset, reference, ref_m[c.index])
        comp = CornerComparison(corner=c, ref=ref_m[c.index], cmp=cmp_m[c.index],
                                dt=dt, line=line)
        comp.insights = compare_corner(c, comp.ref, comp.cmp, dt, u, line)
        comps.append(comp)
    return comps, s_grid, dvec


# --------------------------------------------------------------------------
# Absolute (single-lap) diagnostics
# --------------------------------------------------------------------------


def _elevation_insights(corner: Corner, m: CornerMetrics, relief,
                        u: UnitSystem) -> List[Insight]:
    """Coaching that follows from the gradient rather than from the driving.

    Kept separate because it says something different from everything else
    here: not "you did this", but "the ground does this, so expect it".
    """
    from .elevation import MIN_GRADE, MIN_LOAD_SHIFT

    out: List[Insight] = []
    name = corner.name

    # --- braking on a slope --------------------------------------------------
    if (m.has_braking and abs(relief.grade_braking) > MIN_GRADE
            and abs(relief.braking_penalty_m) > 3.0):
        downhill = relief.grade_braking < 0
        pct = abs(relief.grade_braking) * 100.0
        extra = abs(relief.braking_penalty_m)
        if downhill:
            out.append(Insight(
                name, "braking", SEV_MED, 0.0,
                f"braking zone falls at {pct:.1f}%, adding about "
                f"{u.d_s(extra)} to your stopping distance",
                "Gravity is pulling the car along the track while the brakes "
                "are trying to stop it, so the same pedal buys less. This is "
                "where drivers run deep without knowing why: the corner needs "
                "an earlier brake point than a flat one at the same speed."))
        else:
            out.append(Insight(
                name, "braking", SEV_LOW, 0.0,
                f"braking zone climbs at {pct:.1f}%, saving about "
                f"{u.d_s(extra)} of stopping distance",
                "The slope is helping the brakes here, so this corner takes a "
                "later brake point than its speed suggests. Somewhere to be "
                "brave if you are looking for time."))

    # --- load through the corner ---------------------------------------------
    light = 1.0 - relief.min_load_factor
    heavy = relief.max_load_factor - 1.0
    # Only when the light point is actually in the corner. Elsewhere it belongs
    # to whatever the car was doing there, not to this corner.
    near = (relief.crest_offset_m is not None
            and abs(relief.crest_offset_m) < 90.0)
    if light > MIN_LOAD_SHIFT * 2 and near:
        where = ""
        if relief.crest_offset_m is not None:
            if abs(relief.crest_offset_m) < 25.0:
                where = " right at turn-in"
            elif relief.crest_offset_m < 0:
                where = f" {u.d_abs_s(relief.crest_offset_m)} before turn-in"
            else:
                where = f" {u.d_abs_s(relief.crest_offset_m)} after turn-in"
        out.append(Insight(
            name, "grip", SEV_MED if light > 0.12 else SEV_LOW, 0.0,
            f"crest takes about {light * 100:.0f}% of the load off the "
            f"tires{where}",
            "Cresting costs normal load in proportion to how hard the car is "
            "pushed over the top, and grip goes with it. Expect the car to "
            "feel loose exactly where you are asking most of it, and be "
            "patient with steering and throttle until it settles."))
    elif heavy > MIN_LOAD_SHIFT * 2 and relief.load_at_apex > 1.03:
        out.append(Insight(
            name, "grip", SEV_LOW, 0.0,
            f"compression puts about {heavy * 100:.0f}% more load on the tires",
            "There is more grip here than the radius alone suggests, which is "
            "why corners in a dip often take more speed than they look like "
            "they should. Worth testing if you have been treating this as an "
            "ordinary corner."))

    # --- the exit -------------------------------------------------------------
    if abs(relief.grade_exit) > MIN_GRADE * 1.5:
        pct = abs(relief.grade_exit) * 100.0
        if relief.grade_exit > 0:
            out.append(Insight(
                name, "exit", SEV_LOW, 0.0,
                f"exit climbs at {pct:.1f}%",
                "An uphill exit is forgiving of throttle — the slope is "
                "helping to slow the car, so wheelspin is less likely and "
                "getting to power early is rewarded twice, in this corner and "
                "in the speed you carry up the hill."))
        else:
            out.append(Insight(
                name, "exit", SEV_LOW, 0.0,
                f"exit falls at {pct:.1f}%",
                "A downhill exit gives less grip for acceleration while the "
                "car is gaining speed anyway. Unwinding the steering before "
                "asking for full throttle matters more here than on the flat."))
    return out


def absolute_insights(lap: LapTrack, corners: Sequence[Corner],
                      metrics: Sequence[CornerMetrics],
                      env: GripEnvelope,
                      u: UnitSystem = METRIC) -> List[Insight]:
    """Technique problems visible without any reference lap."""
    out: List[Insight] = []
    from . import elevation as _elev
    elevations = {e.corner_index: e
                  for e in _elev.analyse_lap(lap, corners, metrics)}
    straights = {c.index: straight_after(c, corners, lap.length) for c in corners}
    leverage = exit_leverage(corners, lap.length)
    jerks = [abruptness(lap, c.s_win_start, c.s_win_end) for c in corners]
    driver_jerk = float(np.median(jerks)) if jerks else 0.0

    for c, m in zip(corners, metrics):
        # 1. dead time between brake release and throttle
        if m.has_braking and m.coast_time > 0.30:
            cost = (m.coast_time - 0.10) * 0.45
            out.append(Insight(
                c.name, "exit", _sev(cost), cost,
                f"{m.coast_time:.2f}s coasting ({u.d_s(m.coast_dist)}) between "
                "brake release and throttle",
                "Neither pedal is doing anything here. Either brake later and "
                "trail into the apex, or get back to power sooner."))

        # 2. min speed away from the geometric apex => entry problem.
        #    Scaled by corner length: in a long constant-radius corner the
        #    "geometric apex" is a weak reference and a 30 m offset means little.
        apex_tol = max(20.0, 0.30 * c.length)
        if m.apex_offset < -apex_tol:
            out.append(Insight(
                c.name, "entry", SEV_MED, 0.0,
                f"minimum speed {_fmt_m(m.apex_offset, u)} before the tightest "
                "point — over-slowing on entry",
                "You are at your slowest before the corner is at its tightest, "
                "then accelerating into more curvature. Carry more speed to "
                "the apex or turn in later."))
        elif m.apex_offset > apex_tol:
            out.append(Insight(
                c.name, "entry", SEV_LOW, 0.0,
                f"minimum speed {_fmt_m(m.apex_offset, u)} after the tightest "
                "point — late, tight entry",
                "Slowest point is past the tightest part of the corner, which "
                "usually means turning in late and running out of road on exit."))

        # 3. lateral grip left unused, judged against the driver's own best
        if env.max_lat_g > 0.4 and c.min_radius < 400:
            ratio = m.peak_lat_g / env.max_lat_g
            if ratio < 0.82:
                out.append(Insight(
                    c.name, "grip", SEV_MED if ratio < 0.72 else SEV_LOW, 0.0,
                    f"peak {m.peak_lat_g:.2f} g lateral vs {env.max_lat_g:.2f} g "
                    f"you achieve elsewhere ({ratio * 100:.0f}%)",
                    "This corner is being driven below the grip you have "
                    "already demonstrated — usually a confidence or line "
                    "problem rather than a car limitation."))

        # 4. braking grip left unused
        if m.has_braking and env.max_decel_g > 0.4:
            ratio = abs(m.peak_decel_g) / env.max_decel_g
            if ratio < 0.78:
                out.append(Insight(
                    c.name, "braking", SEV_LOW, 0.0,
                    f"peak braking {abs(m.peak_decel_g):.2f} g vs "
                    f"{env.max_decel_g:.2f} g elsewhere",
                    "Initial brake application is soft here. Peak pressure "
                    "should arrive almost immediately, then bleed off."))

        # 5. long straight-line gap between brake point and turn-in
        if m.has_braking and m.brake_to_turnin > 90.0 and c.min_radius < 300:
            out.append(Insight(
                c.name, "entry", SEV_LOW, 0.03,
                f"{u.d_s(m.brake_to_turnin)} between brake point and turn-in",
                "A long straight-line braking phase followed by a separate "
                "turn-in leaves the tires under-worked in the transition. "
                "Consider braking later and trail-braking to rotate the car."))

        # 6. brake released and re-applied before the apex
        if m.brake_interrupted:
            out.append(Insight(
                c.name, "braking", SEV_MED, 0.0,
                "brake released and re-applied before the apex",
                "An interrupted brake application usually means the first one "
                "was too early or too tentative. One decisive application, "
                "bled off progressively, is faster and settles the car."))

        # 7. the radius and your own demonstrated grip allow more speed
        if env.max_lat_g > 0.4 and c.min_radius < 500 and m.v_min > 5:
            possible = grip_limited_speed(c.min_radius, env.max_lat_g)
            gain = possible - m.v_min
            pct = 100.0 * m.v_min / possible if possible > 0 else 100.0
            # 90%+ of the grip-limited speed is a corner being driven well.
            # Flagging it teaches the driver to ignore this whole category.
            if possible > 0 and gain > 5.0 and pct < 88.0:
                lev = leverage.get(c.index, 0.0)
                cost = min(gain * 0.02 * (0.5 + lev), 0.35)
                out.append(Insight(
                    c.name, "apex", _sev(cost), cost,
                    f"minimum speed {u.spd_s(m.v_min)} against "
                    f"{u.spd_s(possible)} the radius allows at your own "
                    f"{env.max_lat_g:.2f} g ({pct:.0f}% of it)",
                    "Steady-state speed for this radius is v = sqrt(a_lat x r), "
                    "using the lateral g you have already produced elsewhere on "
                    "this lap set. The gap is what the corner still owes you — "
                    "though a wider entry that opens the radius will find some "
                    "of it too."))

        # 8. corners that open onto long straights are worth the most
        lev = leverage.get(c.index, 0.0)
        # only the single highest-leverage exit — naming three defeats the point
        if lev >= 0.999 and c.min_radius < 400 and len(corners) > 2:
            straight = straights.get(c.index, 0.0)
            note = (f"exit leads onto {u.d_s(straight)} of open track — the "
                    "highest-leverage exit on the lap")
            detail = ("A tenth found here is carried the whole way down; the "
                      "same tenth at a corner that leads into the next braking "
                      "zone is handed straight back. If you only fix one exit "
                      "this session, fix this one.")
            if m.coast_time > 0.15 or m.v_exit_plus < m.v_exit * 1.02:
                out.append(Insight(c.name, "exit", SEV_MED, 0.0, note, detail))
            else:
                out.append(Insight(c.name, "exit", SEV_LOW, 0.0, note, detail))

        # 9. abrupt inputs: the tire needs time to build slip angle
        if driver_jerk > 0.5:
            jerk = abruptness(lap, c.s_win_start, c.s_win_end)
            if jerk > driver_jerk * 1.6:
                out.append(Insight(
                    c.name, "smoothness", SEV_LOW, 0.0,
                    f"grip demand changes at {jerk:.1f} g/s here vs "
                    f"{driver_jerk:.1f} g/s typical for you",
                    "A snatched input asks the tire for grip before the slip "
                    "angle has built. Feed the load in over a beat longer and "
                    "the same peak g arrives without the spike."))

        # 10. what the ground is doing under this corner
        relief = elevations.get(c.index)
        if relief is not None and relief.has_relief:
            out.extend(_elevation_insights(c, m, relief, u))

        # 11. no trail braking at all in a slow corner
        if (m.has_braking and m.trail_brake_dist < 3.0
                and c.min_radius < 120 and m.v_min < 90):
            out.append(Insight(
                c.name, "entry", SEV_LOW, 0.0,
                "all braking done in a straight line — no trail braking into "
                "the apex",
                "In a slow corner, carrying a little brake past turn-in helps "
                "the car rotate and lets you release the brake later."))
    return out


# --------------------------------------------------------------------------
# Consistency + theoretical best
# --------------------------------------------------------------------------


def consistency_insights(laps: Sequence[LapTrack], corners: Sequence[Corner],
                         min_laps: int = 3,
                         u: UnitSystem = METRIC) -> List[Insight]:
    if len(laps) < min_laps:
        return []
    out: List[Insight] = []
    per_corner: Dict[int, List[CornerMetrics]] = {c.index: [] for c in corners}
    for lap in laps:
        for c, m in zip(corners, analyse_lap(lap, corners)):
            per_corner[c.index].append(m)

    for c in corners:
        ms = per_corner[c.index]
        brakes = [m.s_brake for m in ms if m.s_brake is not None]
        if len(brakes) >= min_laps:
            spread = float(np.std(brakes))
            if spread > 12.0:
                out.append(Insight(
                    c.name, "consistency", SEV_MED if spread > 20 else SEV_LOW,
                    0.0,
                    f"brake point varies by ±{u.d_s(spread)} across "
                    f"{len(brakes)} laps",
                    "Inconsistent reference point. Pick a fixed visual marker "
                    "for this corner — repeatability here is worth more than "
                    "finding another meter of braking."))
        vmins = [m.v_min for m in ms]
        if len(vmins) >= min_laps:
            spread = float(np.std(vmins))
            if spread > 3.0:
                out.append(Insight(
                    c.name, "consistency", SEV_LOW, 0.0,
                    f"minimum speed varies by ±{u.spd_s(spread)}",
                    "Scatter in apex speed usually follows scatter in the "
                    "brake point or turn-in."))
    return out


@dataclass
class CornerSummary:
    """One corner across every lap of a session, rather than lap against lap."""
    corner: Corner
    laps: int
    best_s: float
    mean_s: float
    worst_s: float
    std_s: float
    best_lap: int
    #: mean minus best: what matching your own best time here would return
    potential_gain_s: float
    #: 100 x (1 - std/mean), clipped. A definition, not a verdict — see the note
    #: in the report. 100% means every lap took the same time through here.
    consistency_pct: float
    v_min_best_kmh: float
    v_min_mean_kmh: float

    @property
    def name(self) -> str:
        return self.corner.name


def corner_summaries(laps: Sequence[LapTrack], corners: Sequence[Corner]
                     ) -> List[CornerSummary]:
    """Best, average and spread through each corner, across the whole session.

    The rest of this module compares one lap against another. That answers
    "where did this lap lose time", but not "which corner do I drive
    inconsistently, and what would matching my own best there be worth" — which
    is the more useful question when deciding what to practice.

    Timed over each corner's analysis window, which includes its braking zone
    and the run to the exit, so the figure covers the whole of what the driver
    does at that corner rather than the geometric turn alone.
    """
    out: List[CornerSummary] = []
    usable = [l for l in laps if l.length > 0]
    if not usable:
        return out

    for corner in corners:
        times, mins, numbers = [], [], []
        for lap in usable:
            a = min(corner.s_win_start, lap.length)
            b = min(corner.s_win_end, lap.length)
            if b - a < 5.0:
                continue
            t = lap.window_time(a, b)
            if t <= 0:
                continue
            times.append(t)
            numbers.append(lap.number)
            i0, i1 = lap.idx(a), max(lap.idx(b), lap.idx(a) + 1)
            mins.append(float(np.min(lap.speed[i0:i1 + 1])) * 3.6)
        if len(times) < 1:
            continue
        arr = np.array(times, dtype=float)
        best_i = int(np.argmin(arr))
        mean = float(arr.mean())
        std = float(arr.std())
        consistency = 100.0 * (1.0 - std / mean) if mean > 0 else 0.0
        best_min = mins[best_i] if mins else 0.0
        out.append(CornerSummary(
            corner=corner,
            laps=len(times),
            best_s=float(arr.min()),
            mean_s=mean,
            worst_s=float(arr.max()),
            std_s=std,
            best_lap=numbers[best_i],
            potential_gain_s=mean - float(arr.min()),
            consistency_pct=float(np.clip(consistency, 0.0, 100.0)),
            v_min_best_kmh=best_min,
            v_min_mean_kmh=float(np.mean(mins)) if mins else 0.0))
    return out


def limit_usage(lap: LapTrack, env: GripEnvelope,
                threshold: float = 0.9) -> Dict[str, float]:
    """How much of the lap is spent near the limit, and where the rest goes.

    Not a judgement on its own — a lap is mostly straights, and a straight at
    0.1 g is exactly right. It is useful as a trend across a session and as a
    way of separating "the car is at its limit and I am losing time" from
    "I am not asking the car for anything".
    """
    g = lap.combined_g()
    ds = lap.ds
    near = g >= threshold * env.max_combined_g
    # Thresholds scale with what this car and driver actually produce. A fixed
    # 0.25 g reads a kart session — which peaks around 0.4 g and is nearly all
    # corner — as 5% cornering, which is nonsense. A quarter of the demonstrated
    # limit means the same thing on a kart as on a slick-shod track car.
    lat_thr = max(0.12, 0.25 * env.max_lat_g)
    dec_thr = max(0.12, 0.25 * env.max_decel_g)
    turning = np.abs(lap.ay_g) > lat_thr
    braking = lap.ax_g < -dec_thr
    dist = max(lap.length, 1e-6)
    return {
        "at_limit_pct": float(np.count_nonzero(near) * ds / dist * 100.0),
        "turning_pct": float(np.count_nonzero(turning) * ds / dist * 100.0),
        "braking_pct": float(np.count_nonzero(braking) * ds / dist * 100.0),
        "cornering_at_limit_pct": float(
            np.count_nonzero(near & turning) * ds
            / max(np.count_nonzero(turning) * ds, 1e-6) * 100.0),
        "peak_combined_g": float(np.max(g)),
    }


def theoretical_best(laps: Sequence[LapTrack], corners: Sequence[Corner],
                     ds: float = 1.0) -> tuple[float, Dict[int, int]]:
    """Best achievable lap by stitching each lap's best segment.

    Segments are split at corner-window boundaries, so a "segment" is a
    braking zone + corner + exit, or the straight between two of them.
    """
    if not laps:
        return 0.0, {}
    edges = [0.0]
    for c in corners:
        edges += [c.s_win_start, c.s_win_end]
    length = min(float(l.length) for l in laps)
    edges = sorted({round(min(max(e, 0.0), length), 2) for e in edges})
    if edges[-1] < length:
        edges.append(length)

    total = 0.0
    owner: Dict[int, int] = {}
    for i, (a, b) in enumerate(zip(edges, edges[1:])):
        if b - a < 1.0:
            continue
        times = [(lap.window_time(a, b), lap.number) for lap in laps]
        tmin, num = min(times)
        total += tmin
        owner[i] = num
    return total, owner


# --------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------


def analyse(session: Session,
            lap_number: Optional[int] = None,
            reference_number: Optional[int] = None,
            units: UnitSystem = METRIC,
            **corner_kw) -> LapAnalysis:
    """Full analysis of one lap, optionally against a reference lap."""
    pool = session.valid_laps or session.laps
    if not pool:
        raise ValueError("No laps detected in this session")

    ref = (session.by_number(reference_number) if reference_number
           else session.best_lap())
    lap = session.by_number(lap_number) if lap_number else ref
    if lap is None:
        raise ValueError(f"Lap {lap_number} not found")

    # Corners always come from the reference lap so windows are consistent.
    # a straight-line run has no corners — GPS jitter at walking pace at the launch would otherwise
    # read as a hairpin
    corners = [] if getattr(session, "mode", "laps") == "runs" else detect_corners(ref, **corner_kw)
    # Attach any names the driver gave these corners at this circuit. Failing
    # to read the store must never stop an analysis.
    try:
        from . import history
        history.apply_names(history.circuit_key(session), corners, ref)
    except Exception:                                    # noqa: BLE001
        pass
    metrics = analyse_lap(lap, corners)
    env = GripEnvelope.from_laps(pool)

    res = LapAnalysis(lap=lap, corners=corners, metrics=metrics, reference=ref)
    absolutes = absolute_insights(lap, corners, metrics, env, units)

    if ref is not None and lap is not ref:
        comps, s_grid, dvec = compare_laps(ref, lap, corners, units)
        res.comparisons = comps
        res.s_delta, res.delta = s_grid, dvec
        # Ordered by *measured* time lost per corner, not by any estimate. The
        # delta trace is ground truth; everything else is interpretation.
        ranked: List[Insight] = []
        for comp in sorted(comps, key=lambda c: -c.dt):
            # A corner where you were level or faster did not "cause" anything.
            # Its input differences still show in the delta table, but putting
            # them in the causes list buries the corners that matter.
            if comp.dt <= T_TIME_S:
                continue
            ranked.extend(comp.insights)
        res.comparative = ranked
        # Drop absolute notes that a comparative finding already covers, so the
        # same problem isn't reported twice with two different numbers.
        covered = {(i.corner, i.kind) for i in ranked}
        absolutes = [i for i in absolutes if (i.corner, i.kind) not in covered]

    _sev_rank = {SEV_HIGH: 0, SEV_MED: 1, SEV_LOW: 2}
    absolutes.sort(key=lambda i: (-i.time_cost, _sev_rank[i.severity]))
    res.absolute = absolutes
    res.insights = res.comparative + absolutes
    return res
