"""
Corner detection and phase segmentation.

A corner is found from the curvature trace of the reference lap; every other
lap is then measured against *that* corner's distance window, so the numbers
are directly comparable across laps.

Phases, in the order the driver experiences them:

    brake point -> turn-in -> (trail brake) -> min speed / apex -> throttle-on -> exit
       |              |                            |                   |
    v_brake      s_turnin                       v_min             coast gap
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from . import geometry as geo
from .laps import LapTrack


@dataclass
class Corner:
    index: int                  # 1-based, in distance order
    direction: str              # 'L' or 'R'
    s_start: float              # curvature onset
    s_geo_apex: float           # peak curvature
    s_end: float                # curvature release
    peak_curv: float
    min_radius: float
    # analysis window (includes braking zone and exit run) — used for timing
    s_win_start: float
    s_win_end: float
    #: True if this corner runs across the start/finish line, in which case its
    #: metrics are split between the top and bottom of the lap and should be
    #: treated with suspicion. Move the timing line onto a straight if so.
    note_straddle: bool = False
    #: a name the driver gave this corner, which follows it between sessions
    given_name: Optional[str] = None

    @property
    def name(self) -> str:
        """What to call this corner: its given name, else its number.

        Numbers are not stable between sessions — they come from curvature
        detection, so one corner resolving as a double apex renumbers every
        corner after it. A name attaches to the apex position instead, and
        "the Bowl" still means the same place next month.
        """
        return self.given_name or f"T{self.index}"

    @property
    def number(self) -> str:
        return f"T{self.index}"

    @property
    def length(self) -> float:
        return self.s_end - self.s_start


@dataclass
class CornerMetrics:
    corner: Corner
    lap_number: int
    # braking
    s_brake: Optional[float]
    v_brake: Optional[float]        # km/h
    peak_decel_g: float
    brake_dist: float               # meters of braking before min speed
    has_braking: bool               # a real braking event, not sensor drift
    brake_interrupted: bool         # released and re-applied before the apex
    # turn-in
    s_turnin: Optional[float]
    v_turnin: Optional[float]       # km/h
    brake_to_turnin: float          # meters between brake point and turn-in
    # apex
    s_vmin: float
    v_min: float                    # km/h
    apex_offset: float              # s_vmin - s_geo_apex; -ve = early min speed
    peak_lat_g: float
    peak_combined_g: float
    # exit
    s_throttle: Optional[float]
    coast_dist: float               # meters neither braking nor accelerating
    coast_time: float               # seconds of that
    v_exit: float                   # km/h at curvature release
    v_exit_plus: float              # km/h 75 m further on
    trail_brake_dist: float         # meters of braking while above 0.4 g lateral
    # timing
    t_window: float                 # seconds across the corner analysis window

    def as_row(self) -> dict:
        f = lambda v: None if v is None else round(float(v), 2)
        return {
            "corner": self.corner.name,
            "dir": self.corner.direction,
            "brake_s": f(self.s_brake),
            "brake_kmh": f(self.v_brake),
            "peak_decel_g": f(self.peak_decel_g),
            "turnin_s": f(self.s_turnin),
            "turnin_kmh": f(self.v_turnin),
            "min_kmh": f(self.v_min),
            "apex_offset_m": f(self.apex_offset),
            "peak_lat_g": f(self.peak_lat_g),
            "coast_s": f(self.coast_time),
            "exit_kmh": f(self.v_exit),
            "exit75_kmh": f(self.v_exit_plus),
            "window_t": f(self.t_window),
        }


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def detect_corners(lap: LapTrack,
                   min_radius: float = 200.0,
                   peak_radius: float = 130.0,
                   min_length_m: float = 12.0,
                   merge_gap_m: float = 30.0,
                   min_sustained_m: float = 20.0,
                   brake_lookback_m: float = 260.0,
                   exit_run_m: float = 75.0) -> List[Corner]:
    """Find corners as sustained-curvature regions on a lap.

    `min_radius` sets what counts as "turning at all"; `peak_radius` is the
    tighter bar a region must reach somewhere; `min_sustained_m` is how much
    arc it must actually hold there.

    That last one matters more than it sounds. Judging a corner on whether its
    curvature *peak* crosses a threshold makes detection hostage to a single
    sample, so GPS noise flickers marginal kinks in and out: on one synthetic
    circuit, re-driving the identical layout produced anywhere from 7 to 11
    corners. Requiring the curvature to be *held* narrowed that to 5 to 6, and
    on real data it discards only kinks of around 120 m radius held for under
    10 m — which are not corners anyone would name.
    """
    k = lap.curv
    s = lap.s
    thr = 1.0 / min_radius
    peak_thr = 1.0 / peak_radius

    above = np.abs(k) > thr
    regions: List[tuple[int, int]] = []
    i = 0
    n = len(k)
    while i < n:
        if not above[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and above[j + 1] and np.sign(k[j + 1]) == np.sign(k[i]):
            j += 1
        regions.append((i, j))
        i = j + 1

    # Merge neighbors of the same hand separated by a short kink — but only if
    # the curvature in the gap stays high enough that it really is one corner.
    # Without that guard, a noisy trace chain-merges half a lap into one
    # "corner": A joins B, the result joins C, and so on.
    merged: List[tuple[int, int]] = []
    for r in regions:
        if merged:
            p = merged[-1]
            gap = s[r[0]] - s[p[1]]
            same_hand = np.sign(k[r[0]]) == np.sign(k[p[0]])
            if gap < merge_gap_m and same_hand:
                gap_k = np.abs(k[p[1]:r[0] + 1])
                floor = min(float(np.abs(k[p[0]:p[1] + 1]).max()),
                            float(np.abs(k[r[0]:r[1] + 1]).max())) * 0.35
                if gap_k.size == 0 or float(gap_k.min()) >= floor:
                    merged[-1] = (p[0], r[1])
                    continue
        merged.append(r)

    # Split double-apex regions: two curvature peaks with a real dip between
    # them are two corners for coaching purposes, even if the wheel never
    # comes fully straight.
    split: List[tuple[int, int]] = []
    for a, b in merged:
        split.extend(_split_double_apex(np.abs(k[a:b + 1]), s[a:b + 1], a))
    merged = split

    corners: List[Corner] = []
    idx = 0
    for a, b in merged:
        seg = np.abs(k[a:b + 1])
        if seg.size == 0:
            continue
        if float(seg.max()) < peak_thr:
            continue
        if (s[b] - s[a]) < min_length_m:
            continue
        held = float(np.count_nonzero(seg >= peak_thr)) * lap.ds
        if held < min_sustained_m:
            continue
        idx += 1
        p = a + int(np.argmax(seg))
        peak = float(k[p])
        corners.append(Corner(
            index=idx,
            direction="L" if peak > 0 else "R",
            s_start=float(s[a]),
            s_geo_apex=float(s[p]),
            s_end=float(s[b]),
            peak_curv=peak,
            min_radius=float(1.0 / max(abs(peak), 1e-9)),
            s_win_start=float(max(s[0], s[a] - brake_lookback_m)),
            s_win_end=float(min(s[-1], s[b] + exit_run_m)),
        ))

    if corners:
        straddle = (corners[0].s_start <= s[0] + 2.0
                    and corners[-1].s_end >= s[-1] - 2.0)
        if straddle:
            corners[0].note_straddle = True
            corners[-1].note_straddle = True

    # Keep analysis windows from overlapping: a meter of track must belong to
    # exactly one corner, or the same time loss gets billed to two corners and
    # the per-corner deltas stop summing to anything meaningful.
    for prev, cur in zip(corners, corners[1:]):
        if prev.s_win_end <= cur.s_win_start:
            continue
        mid = 0.5 * (prev.s_end + cur.s_start)
        mid = float(min(max(mid, prev.s_end), cur.s_start))
        prev.s_win_end = float(min(prev.s_win_end, mid))
        cur.s_win_start = float(max(cur.s_win_start, mid))
    return corners


def _split_double_apex(absk: np.ndarray, s_seg: np.ndarray, offset: int,
                       min_sub_length_m: float = 35.0,
                       dip_ratio: float = 0.45,
                       min_peak_gap_m: float = 55.0) -> List[tuple[int, int]]:
    """Split a same-hand curvature region into two if it is a true double apex.

    Deliberately conservative, and limited to a single split. Curvature traces
    from GPS always ripple, and a permissive rule shatters one long corner into
    half a dozen "corners" that no driver would recognize — which makes every
    downstream comparison useless.
    """
    n = absk.size
    if n < 12 or (s_seg[-1] - s_seg[0]) < 2 * min_sub_length_m:
        return [(offset, offset + n - 1)]

    best = None
    for i in range(2, n - 2):
        if absk[i] > absk[i - 1] or absk[i] > absk[i + 1]:
            continue
        left = absk[:i]
        right = absk[i:]
        lp, rp = float(left.max()), float(right.max())
        lower = min(lp, rp)
        if lower <= 0 or absk[i] / lower > dip_ratio:
            continue
        # the two peaks have to be genuinely far apart, not two ripples
        i_lp = int(np.argmax(left))
        i_rp = i + int(np.argmax(right))
        if (s_seg[i_rp] - s_seg[i_lp]) < min_peak_gap_m:
            continue
        if (s_seg[i] - s_seg[0]) < min_sub_length_m:
            continue
        if (s_seg[-1] - s_seg[i]) < min_sub_length_m:
            continue
        score = absk[i] / lower
        if best is None or score < best[1]:
            best = (i, score)

    if best is None:
        return [(offset, offset + n - 1)]
    i = best[0]
    return [(offset, offset + i), (offset + i, offset + n - 1)]


# --------------------------------------------------------------------------
# Per-corner metrics
# --------------------------------------------------------------------------

DECEL_THR = -0.18      # g, below this we call it braking
ACCEL_THR = 0.10       # g, above this we call it on-power
TRAIL_LAT_THR = 0.40   # g lateral, above this we're meaningfully turning


def _contiguous_runs(mask: np.ndarray) -> List[tuple[int, int]]:
    runs = []
    i = 0
    n = len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs


def corner_metrics(lap: LapTrack, corner: Corner,
                   turnin_frac: float = 0.22) -> CornerMetrics:
    s = lap.s
    i_win0 = lap.idx(corner.s_win_start)
    i_win1 = lap.idx(corner.s_win_end)
    i_c0 = lap.idx(corner.s_start)
    i_c1 = lap.idx(corner.s_end)
    i_geo = lap.idx(corner.s_geo_apex)

    ax = lap.ax_g
    ay = lap.ay_g
    v = lap.speed

    # ---- minimum speed (the practical apex) -------------------------------
    lo = max(i_win0, i_c0 - int(round(25.0 / lap.ds)))
    hi = min(i_win1, i_c1 + int(round(25.0 / lap.ds)))
    hi = max(hi, lo + 1)
    i_vmin = lo + int(np.argmin(v[lo:hi + 1]))
    v_min = float(v[i_vmin] * 3.6)

    # ---- braking event ----------------------------------------------------
    # Prefer a logged brake channel: it marks pedal-off exactly, whereas
    # differentiated GPS speed keeps drifting negative from drag long after the
    # driver came off the brake, which inflates every coasting number.
    brake_ch = lap.extras.get("brake")
    search_lo = i_win0
    if brake_ch is not None and float(np.nanmax(brake_ch)) > 1.0:
        lvl = 8.0 if float(np.nanmax(brake_ch)) > 5.0 else 0.08
        brake_mask = brake_ch[search_lo:i_vmin + 1] > lvl
    else:
        brake_mask = ax[search_lo:i_vmin + 1] < DECEL_THR
    runs = _contiguous_runs(brake_mask)
    s_brake = v_brake = None
    peak_decel = 0.0
    i_brake = None
    i_release = None
    interrupted = False
    if runs:
        # The braking event for this corner is the last real one before v_min...
        real = [(a, b) for a, b in runs
                if float(np.min(ax[search_lo + a: search_lo + b + 1])) < -0.30]
        pick = real[-1] if real else runs[-1]
        i_brake = search_lo + pick[0]
        i_release = search_lo + pick[1]
        # ...but a driver who brakes early, coasts, then re-applies has *one*
        # braking event that began at the first application. Picking only the
        # final run reports that as braking late, which is the opposite of what
        # happened. Absorb earlier runs separated by a short gap.
        gap_samples = max(1, int(round(45.0 / lap.ds)))
        interrupted = False
        for a, b in reversed(runs):
            end = search_lo + b
            if end >= i_brake:
                continue
            if (i_brake - end) <= gap_samples:
                i_brake = search_lo + a
                interrupted = True
            else:
                break
        s_brake = float(s[i_brake])
        v_brake = float(v[i_brake] * 3.6)
        peak_decel = float(np.min(ax[i_brake:i_release + 1]))
    has_braking = peak_decel < -0.35

    # ---- turn-in ----------------------------------------------------------
    k_thr = max(turnin_frac * abs(corner.peak_curv), 1.0 / 500.0)
    sign = np.sign(corner.peak_curv)
    scan_from = i_brake if i_brake is not None else max(i_win0, i_c0 - int(80 / lap.ds))
    seg = lap.curv[scan_from:i_geo + 1] * sign
    hits = np.flatnonzero(seg >= k_thr)
    if hits.size:
        i_turnin = scan_from + int(hits[0])
    else:
        i_turnin = i_c0
    s_turnin = float(s[i_turnin])
    v_turnin = float(v[i_turnin] * 3.6)

    # ---- throttle-on ------------------------------------------------------
    thr_ch = lap.extras.get("throttle")
    i_thr = None
    scan_hi = min(len(s) - 1, i_c1 + int(round(90.0 / lap.ds)))
    if thr_ch is not None:
        span = thr_ch[i_vmin:scan_hi + 1]
        if span.size:
            lvl = 15.0 if float(np.nanmax(thr_ch)) > 5.0 else 0.15
            hits = np.flatnonzero(span > lvl)
            if hits.size:
                i_thr = i_vmin + int(hits[0])
    if i_thr is None:
        span = ax[i_vmin:scan_hi + 1]
        hits = np.flatnonzero(span > ACCEL_THR)
        if hits.size:
            i_thr = i_vmin + int(hits[0])
    s_throttle = float(s[i_thr]) if i_thr is not None else None

    # ---- coast gap (brake released, not yet on power) ---------------------
    # Only meaningful where the driver actually braked; a fast kink taken flat
    # would otherwise report a huge phantom "coast" between two noise events.
    coast_dist = coast_time = 0.0
    if has_braking and i_release is not None and i_thr is not None and i_thr > i_release:
        coast_dist = float(s[i_thr] - s[i_release])
        coast_time = float(lap.t[i_thr] - lap.t[i_release])

    # ---- grip usage -------------------------------------------------------
    peak_lat = float(np.max(np.abs(ay[i_c0:i_c1 + 1]))) if i_c1 > i_c0 else 0.0
    comb = np.hypot(ax[i_win0:i_win1 + 1], ay[i_win0:i_win1 + 1])
    peak_comb = float(np.max(comb)) if comb.size else 0.0

    trail = 0.0
    if i_brake is not None and i_release is not None:
        m = (ax[i_brake:i_release + 1] < DECEL_THR) & \
            (np.abs(ay[i_brake:i_release + 1]) > TRAIL_LAT_THR)
        trail = float(np.count_nonzero(m) * lap.ds)

    # ---- exit -------------------------------------------------------------
    v_exit = float(v[i_c1] * 3.6)
    v_exit_plus = float(lap.at(min(corner.s_end + 75.0, s[-1]), v) * 3.6)

    return CornerMetrics(
        corner=corner,
        lap_number=lap.number,
        s_brake=s_brake,
        v_brake=v_brake,
        peak_decel_g=peak_decel,
        brake_dist=float(s[i_vmin] - s[i_brake]) if i_brake is not None else 0.0,
        has_braking=has_braking,
        brake_interrupted=interrupted,
        s_turnin=s_turnin,
        v_turnin=v_turnin,
        brake_to_turnin=float(s_turnin - s_brake) if s_brake is not None else 0.0,
        s_vmin=float(s[i_vmin]),
        v_min=v_min,
        apex_offset=float(s[i_vmin] - corner.s_geo_apex),
        peak_lat_g=peak_lat,
        peak_combined_g=peak_comb,
        s_throttle=s_throttle,
        coast_dist=coast_dist,
        coast_time=coast_time,
        v_exit=v_exit,
        v_exit_plus=v_exit_plus,
        trail_brake_dist=trail,
        t_window=lap.window_time(corner.s_win_start, corner.s_win_end),
    )


def analyse_lap(lap: LapTrack, corners: Sequence[Corner]) -> List[CornerMetrics]:
    return [corner_metrics(lap, c) for c in corners]


# --------------------------------------------------------------------------
# Driver-level grip reference
# --------------------------------------------------------------------------


@dataclass
class GripEnvelope:
    """What the car+driver has actually demonstrated, from the whole session.

    Used as the yardstick for "you left grip on the table here" — comparing a
    corner against the driver's own best rather than an assumed mu value.
    """
    max_lat_g: float
    max_decel_g: float
    max_accel_g: float
    max_combined_g: float

    @classmethod
    def from_laps(cls, laps: Sequence[LapTrack], pct: float = 97.0) -> "GripEnvelope":
        lat, dec, acc, comb = [], [], [], []
        for l in laps:
            lat.append(np.abs(l.ay_g))
            dec.append(-np.minimum(l.ax_g, 0.0))
            acc.append(np.maximum(l.ax_g, 0.0))
            comb.append(l.combined_g())
        if not lat:
            return cls(1.0, 1.0, 0.5, 1.2)
        f = lambda arrs: float(np.percentile(np.concatenate(arrs), pct))
        return cls(max_lat_g=f(lat), max_decel_g=f(dec),
                   max_accel_g=f(acc), max_combined_g=f(comb))
