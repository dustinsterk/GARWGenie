"""
Session -> laps -> distance-domain lap traces.

Everything downstream works in the distance domain (meters from the
start/finish line) rather than the time domain. That is the whole trick to
lap comparison: at 1 m resolution two laps are directly comparable point for
point, whereas in the time domain they drift apart immediately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import geometry as geo
from .parser import VboFile

# Channels the lap machinery derives or owns; everything else in the file is
# carried through to LapTrack.extras verbatim so it can be plotted. Loggers and
# converters emit all sorts — heart rate, ascent, oil temp, tire pressures —
# and a whitelist silently drops whatever the author didn't think of.
DERIVED_CHANNELS = frozenset((
    "lat", "lon", "lat_deg", "lon_deg", "time", "t", "speed", "speed_kmh",
))

#: nicer axis labels for channels we recognize; anything else uses its raw name
CHANNEL_LABELS = {
    "ax_g": ("longitudinal g", "g"),
    "ay_g": ("lateral g", "g"),
    "ax_g_gps": ("longitudinal g (from GPS)", "g"),
    "ay_g_gps": ("lateral g (from GPS)", "g"),
    "throttle": ("throttle", "%"),
    "brake": ("brake", "%"),
    "steer": ("steering angle", "deg"),
    "rpm": ("engine speed", "rpm"),
    "gear": ("gear", ""),
    "height": ("elevation", "m"),
    "heading": ("heading", "deg"),
    "heartrate": ("heart rate", "bpm"),
    "ascent": ("ascent", "m"),
    "descent": ("descent", "m"),
    "vspeed": ("vertical speed", "km/h"),
    "sats": ("satellites", ""),
    "curv": ("curvature", "1/m"),
}


def channel_label(name: str, units: Optional[Dict[str, str]] = None) -> str:
    """Human label for a channel, falling back to the file's own unit string."""
    if name in CHANNEL_LABELS:
        label, unit = CHANNEL_LABELS[name]
        return f"{label} ({unit})" if unit else label
    pretty = name.replace("_", " ")
    unit = (units or {}).get(name, "")
    return f"{pretty} ({unit})" if unit else pretty


@dataclass
class LapTrack:
    """One lap, resampled onto a uniform distance grid."""
    number: int
    ds: float
    s: np.ndarray                 # meters from start/finish
    t: np.ndarray                 # elapsed seconds (integral of ds/v)
    x: np.ndarray                 # local ENU meters
    y: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    speed: np.ndarray             # m/s
    curv: np.ndarray              # 1/m, +ve = left
    ax_g: np.ndarray              # longitudinal, +ve = accelerating
    ay_g: np.ndarray              # lateral, signed with curvature
    extras: Dict[str, np.ndarray] = field(default_factory=dict)
    t_start: float = 0.0          # session time of the S/F crossing
    lap_time: float = 0.0         # from line-crossing timestamps (truth)
    length: float = 0.0
    valid: bool = True
    note: str = ""

    @property
    def speed_kmh(self) -> np.ndarray:
        return self.speed * 3.6

    def at(self, s_val: float, arr: np.ndarray) -> float:
        return float(np.interp(s_val, self.s, arr))

    def idx(self, s_val: float) -> int:
        return int(np.clip(np.searchsorted(self.s, s_val), 0, len(self.s) - 1))

    def window_time(self, s0: float, s1: float) -> float:
        return self.at(s1, self.t) - self.at(s0, self.t)

    def combined_g(self) -> np.ndarray:
        return np.hypot(self.ax_g, self.ay_g)


@dataclass
class Session:
    vbo: VboFile
    lat0: float
    lon0: float
    laps: List[LapTrack]
    start_finish: Tuple[float, float, float, float]   # local meters ax,ay,bx,by
    ds: float
    # full-session arrays, handy for the "everything" view
    x: np.ndarray = field(default_factory=lambda: np.array([]))
    y: np.ndarray = field(default_factory=lambda: np.array([]))
    t: np.ndarray = field(default_factory=lambda: np.array([]))
    speed: np.ndarray = field(default_factory=lambda: np.array([]))
    sf_source: str = "auto"
    #: user-placed sector gate lines as (lat1, lon1, lat2, lon2), overriding
    #: any the file declares. Empty means "use the file's, or auto".
    custom_splits: List[Tuple[float, float, float, float]] = field(
        default_factory=list)

    @property
    def valid_laps(self) -> List[LapTrack]:
        return [l for l in self.laps if l.valid]

    def best_lap(self) -> Optional[LapTrack]:
        pool = self.valid_laps or self.laps
        return min(pool, key=lambda l: l.lap_time) if pool else None

    def by_number(self, n: int) -> Optional[LapTrack]:
        for l in self.laps:
            if l.number == n:
                return l
        return None


# --------------------------------------------------------------------------
# Start/finish line determination
# --------------------------------------------------------------------------


def _line_through(px: float, py: float, hx: float, hy: float,
                  half_width: float) -> Tuple[float, float, float, float]:
    """Line of given half-width centred at P, perpendicular to heading (hx,hy).

    Oriented so that traveling along the heading crosses left-to-right, which
    makes `segment_crossings(require_forward=True)` direction-sensitive.
    """
    norm = np.hypot(hx, hy)
    if norm < 1e-9:
        hx, hy, norm = 1.0, 0.0, 1.0
    hx, hy = hx / norm, hy / norm
    # perpendicular
    nx, ny = -hy, hx
    return (px - nx * half_width, py - ny * half_width,
            px + nx * half_width, py + ny * half_width)


def sf_candidates(x: np.ndarray, y: np.ndarray, speed: np.ndarray,
                  n_candidates: int = 24, half_width: float = 30.0
                  ) -> List[Tuple[Tuple[float, float, float, float], str]]:
    """Candidate timing lines: perpendiculars spread through the moving data."""
    moving = np.flatnonzero(speed > 5.0)
    if moving.size < 20:
        return []
    lo, hi = int(moving[0]) + 2, int(moving[-1]) - 3
    if hi <= lo:
        return []
    out = []
    for i in np.linspace(lo, hi, n_candidates):
        i = int(np.clip(round(i), 2, len(x) - 3))
        hx = x[i + 2] - x[i - 2]
        hy = y[i + 2] - y[i - 2]
        out.append((_line_through(x[i], y[i], hx, hy, half_width),
                    f"@sample {i} ({speed[i] * 3.6:.0f} km/h)"))
    return out


def score_sf_line(line, x, y, t, speed, min_lap_time: float = 15.0,
                  valid_steps=None):
    """Count laps a candidate line yields, and how consistent they are."""
    if valid_steps is None:
        valid_steps = geo.plausible_steps(x, y, t, speed)
    cr = geo.segment_crossings(x, y, *line, require_forward=True,
                               valid_steps=valid_steps)
    acc: List[float] = []
    for i, frac in cr:
        tc = geo.interp_at(t, i, frac)
        if acc and (tc - acc[-1]) < min_lap_time:
            continue
        if geo.interp_at(speed, i, frac) < 3.0:
            continue
        acc.append(tc)
    laps = [b - a for a, b in zip(acc, acc[1:])]
    if not laps:
        return 0, float("inf"), 0.0
    med = float(np.median(laps))
    # relative spread of the middle of the distribution: a correct line gives
    # repeatable times, a line placed somewhere crossed at random does not
    rel = float(np.std(laps) / med) if med > 0 else float("inf")
    return len(laps), rel, med


def auto_start_finish(x: np.ndarray, y: np.ndarray, speed: np.ndarray,
                      t: Optional[np.ndarray] = None,
                      half_width: float = 30.0,
                      min_lap_time: float = 15.0
                      ) -> Tuple[Tuple[float, float, float, float], str]:
    """Guess a start/finish line when the file doesn't declare one.

    Tries lines spread through the whole session and keeps the one that yields
    the most laps, breaking ties on consistency. A single guess — even a
    sensible one like "the fastest point early on" — fails whenever the log
    starts in the pit lane, on a transport section, or anywhere else the car
    never returns to, and then the tool reports no laps at all on a file that
    plainly contains several.
    """
    cands = sf_candidates(x, y, speed, half_width=half_width)
    if not cands:
        raise ValueError("Not enough moving data to find a start/finish line")
    if t is None:
        t = np.arange(len(x), dtype=float) / 10.0

    valid = geo.plausible_steps(x, y, t, speed)
    scored = []
    for line, label in cands:
        n, rel, med = score_sf_line(line, x, y, t, speed, min_lap_time, valid)
        scored.append((n, rel, med, line, label))

    best_n = max(s[0] for s in scored)
    if best_n == 0:
        # nothing crossed twice; fall back to the fastest point so the caller
        # still gets a line to draw, and let lap splitting report zero laps
        i = int(np.argmax(speed))
        i = int(np.clip(i, 2, len(x) - 3))
        line = _line_through(x[i], y[i], x[i + 2] - x[i - 2],
                             y[i + 2] - y[i - 2], half_width)
        return line, "auto (no crossings found)"

    # among candidates within one lap of the best count, take the most consistent
    pool = [s for s in scored if s[0] >= best_n - 1]
    pool.sort(key=lambda s: (s[1], -s[0]))
    n, rel, med, line, label = pool[0]
    return line, f"auto {label}, {n} laps, spread {rel * 100:.1f}%"


def recentre_gate(gate: Tuple[float, float, float, float],
                  x: np.ndarray, y: np.ndarray,
                  half_width: float = 30.0,
                  max_shift_m: float = 60.0
                  ) -> Tuple[float, float, float, float]:
    """Slide a declared timing gate onto the line the car actually drove.

    A declared gate gives a good heading and an approximate position, but it
    is drawn by hand or by another tool and often sits a few metres to one
    side. The car then crosses the *extension* of that line rather than the
    segment, and the crossing is rejected: on one RaceChrono export, three of
    four laps were missed this way and the session came back as a single
    294-second "lap".

    The declared orientation is kept — that is the part the file knows and the
    data does not. Only the centre moves, and only far enough to reach the
    track.
    """
    ax, ay, bx, by = gate
    ex, ey = bx - ax, by - ay
    length = math.hypot(ex, ey)
    if length < 1e-6 or x.size == 0:
        return gate
    ux, uy = ex / length, ey / length

    cx, cy = (ax + bx) / 2.0, (ay + by) / 2.0
    d2 = (x - cx) ** 2 + (y - cy) ** 2
    i = int(np.argmin(d2))
    shift = math.hypot(x[i] - cx, y[i] - cy)
    if shift > max_shift_m:
        # too far to be the same place; leave it alone and let validation
        # decide whether to trust it at all
        return gate
    return (x[i] - ux * half_width, y[i] - uy * half_width,
            x[i] + ux * half_width, y[i] + uy * half_width)


def start_finish_from_vbo(vbo: VboFile, lat0: float, lon0: float,
                          min_half_width: float = 25.0):
    if vbo.start_finish is None:
        return None
    la1, lo1, la2, lo2 = vbo.start_finish
    xs, ys, _, _ = geo.project_local(np.array([la1, la2]), np.array([lo1, lo2]),
                                     lat0, lon0)
    ax, ay, bx, by = float(xs[0]), float(ys[0]), float(xs[1]), float(ys[1])
    # widen a hair — declared lines are sometimes narrower than the racing line
    cx, cy = (ax + bx) / 2, (ay + by) / 2
    dx, dy = bx - ax, by - ay
    half = np.hypot(dx, dy) / 2
    if half < min_half_width and half > 1e-6:
        scale = min_half_width / half
        ax, ay = cx - dx / 2 * scale, cy - dy / 2 * scale
        bx, by = cx + dx / 2 * scale, cy + dy / 2 * scale
    return ax, ay, bx, by


# --------------------------------------------------------------------------
# Building laps
# --------------------------------------------------------------------------


def build_session(vbo: VboFile,
                  ds: float = 1.0,
                  start_finish_latlon: Optional[Sequence[float]] = None,
                  min_lap_time: float = 15.0,
                  curv_smooth_m: float = 22.0,
                  sf_half_width: float = 30.0) -> Session:
    """Split a VBO session into laps and resample each to the distance domain."""
    lat = vbo.channels["lat_deg"]
    lon = vbo.channels["lon_deg"]
    t = vbo.channels["t"]
    speed = vbo.channels["speed"]

    x, y, lat0, lon0 = geo.project_local(lat, lon)

    # ---- start/finish -----------------------------------------------------
    sf_source = "auto"
    sf = None
    if start_finish_latlon is not None and len(start_finish_latlon) >= 4:
        la1, lo1, la2, lo2 = start_finish_latlon[:4]
        xs, ys, _, _ = geo.project_local(np.array([la1, la2]), np.array([lo1, lo2]),
                                         lat0, lon0)
        sf = (float(xs[0]), float(ys[0]), float(xs[1]), float(ys[1]))
        sf_source = "user"
    if sf is None:
        sf = start_finish_from_vbo(vbo, lat0, lon0, sf_half_width)
        if sf is not None:
            sf = recentre_gate(sf, x, y, sf_half_width)
            sf_source = "file"
            # A declared line is not automatically a correct one. Loggers ship
            # with a track database and will happily leave a line from a
            # different circuit — or another continent — in the header. Check
            # it actually sits on this data before trusting it.
            cx, cy = 0.5 * (sf[0] + sf[2]), 0.5 * (sf[1] + sf[3])
            reach = 3000.0 + max(x.max() - x.min(), y.max() - y.min())
            too_far = (abs(cx) > reach or abs(cy) > reach)
            valid_probe = geo.plausible_steps(x, y, t, speed)
            n_cross, _, _ = score_sf_line(sf, x, y, t, speed, min_lap_time,
                                          valid_probe)
            if too_far or n_cross < 1:
                km = math.hypot(cx, cy) / 1000.0
                sf = None
                sf_source = (f"auto (declared line ignored: {km:,.0f} km "
                             f"from this data)" if too_far else
                             "auto (declared line never crossed)")
    if sf is None:
        sf, auto_label = auto_start_finish(x, y, speed, t,
                                           half_width=sf_half_width,
                                           min_lap_time=min_lap_time)
        sf_source = (f"{sf_source}; {auto_label}"
                     if sf_source.startswith("auto (") else auto_label)

    # ---- crossings --------------------------------------------------------
    valid_steps = geo.plausible_steps(x, y, t, speed)
    crossings = geo.segment_crossings(x, y, *sf, require_forward=True,
                                      valid_steps=valid_steps)
    # de-duplicate: enforce a minimum time gap between accepted crossings
    accepted: List[Tuple[int, float, float]] = []
    for i, frac in crossings:
        tc = geo.interp_at(t, i, frac)
        if accepted and (tc - accepted[-1][2]) < min_lap_time:
            continue
        if geo.interp_at(speed, i, frac) < 3.0:
            continue
        accepted.append((i, frac, tc))

    # If we only caught the line once (or not at all) with a forward-only test,
    # retry accepting both directions — the auto line may be back-to-front.
    if len(accepted) < 2:
        crossings = geo.segment_crossings(x, y, *sf, require_forward=False,
                                          valid_steps=valid_steps)
        accepted = []
        for i, frac in crossings:
            tc = geo.interp_at(t, i, frac)
            if accepted and (tc - accepted[-1][2]) < min_lap_time:
                continue
            if geo.interp_at(speed, i, frac) < 3.0:
                continue
            accepted.append((i, frac, tc))

    laps: List[LapTrack] = []
    for n in range(len(accepted) - 1):
        i0, f0, t0 = accepted[n]
        i1, f1, t1 = accepted[n + 1]
        lap = _extract_lap(vbo, n + 1, x, y, lat, lon, t, speed,
                           i0, f0, t0, i1, f1, t1, ds, curv_smooth_m, lat0, lon0)
        if lap is not None:
            laps.append(lap)

    _flag_outliers(laps)

    sess = Session(vbo=vbo, lat0=lat0, lon0=lon0, laps=laps, start_finish=sf,
                   ds=ds, x=x, y=y, t=t, speed=speed, sf_source=sf_source)
    return sess


def _extract_lap(vbo: VboFile, number: int,
                 x, y, lat, lon, t, speed,
                 i0: int, f0: float, t0: float,
                 i1: int, f1: float, t1: float,
                 ds: float, curv_smooth_m: float,
                 lat0: float, lon0: float) -> Optional[LapTrack]:
    lo = i0
    hi = min(i1 + 2, len(t))
    if hi - lo < 20:
        return None

    sl = slice(lo, hi)
    t_seg = t[sl].copy()
    v_seg = np.clip(speed[sl].copy(), 0.0, None)
    x_seg, y_seg = x[sl].copy(), y[sl].copy()

    # Splice exact crossing points onto both ends so lap length and timing are
    # sub-sample accurate rather than quantised to the log rate.
    def _pt(arr_full, i, frac):
        return geo.interp_at(arr_full, i, frac)

    head = dict(t=t0, v=_pt(speed, i0, f0), x=_pt(x, i0, f0), y=_pt(y, i0, f0))
    tail = dict(t=t1, v=_pt(speed, i1, f1), x=_pt(x, i1, f1), y=_pt(y, i1, f1))

    keep = (t_seg > t0) & (t_seg < t1)
    t_seg = np.concatenate([[head["t"]], t_seg[keep], [tail["t"]]])
    v_seg = np.concatenate([[head["v"]], v_seg[keep], [tail["v"]]])
    x_seg = np.concatenate([[head["x"]], x_seg[keep], [tail["x"]]])
    y_seg = np.concatenate([[head["y"]], y_seg[keep], [tail["y"]]])
    idx_keep = np.flatnonzero(keep) + lo

    s_seg = geo.distance_from_speed(t_seg, v_seg)
    length = float(s_seg[-1])
    if length < 200.0:
        return None

    n_grid = max(32, int(round(length / ds)) + 1)
    s_grid = np.linspace(0.0, length, n_grid)
    actual_ds = float(s_grid[1] - s_grid[0])

    R = geo.resample_to_grid
    xg = R(s_seg, x_seg, s_grid)
    yg = R(s_seg, y_seg, s_grid)
    vg = np.clip(R(s_seg, v_seg, s_grid), 0.05, None)

    latg, long_ = geo.unproject_local(xg, yg, lat0, lon0)

    # A lap is only cyclic if it actually closes. The car crosses the timing
    # line on its own line each time, so the exit and entry points can be a few
    # meters apart laterally; wrapping across that gap invents a kink at s=0
    # and reports an impossible lateral g on the first few meters of the lap.
    seam_gap = float(np.hypot(xg[0] - xg[-1], yg[0] - yg[-1]))
    cyclic = seam_gap < 2.0

    heading, kappa = geo.curvature_from_path(xg, yg, actual_ds,
                                             smooth_m=curv_smooth_m, wrap=cyclic)

    # longitudinal accel from dv/ds:  a = v * dv/ds
    win = max(5, int(round(15.0 / actual_ds)) | 1)
    dvds = geo.savgol(vg, win, 2, deriv=1, delta=actual_ds, wrap=cyclic)
    ax_g = (vg * dvds) / geo.G
    ay_g = (vg ** 2 * kappa) / geo.G

    t_grid = geo.elapsed_time_from_speed(s_grid, vg)

    extras: Dict[str, np.ndarray] = {}
    for name, col in vbo.channels.items():
        if name in DERIVED_CHANNELS or col is None:
            continue
        if not np.issubdtype(np.asarray(col).dtype, np.number):
            continue
        seg = np.concatenate([[col[i0]], col[idx_keep], [col[min(i1, len(col) - 1)]]])
        if name == "heading":
            seg = geo.unwrap_deg(seg)
        extras[name] = R(s_seg, seg, s_grid)

    # prefer logged accelerometer data when the file has it — it captures brake
    # modulation that GPS speed differentiation smooths away
    if "ax_g" in extras:
        extras["ax_g_gps"] = ax_g
        ax_g = geo.savgol(extras["ax_g"], win, 2, wrap=cyclic)
    if "ay_g" in extras:
        extras["ay_g_gps"] = ay_g
        logged = geo.savgol(extras["ay_g"], win, 2, wrap=cyclic)
        # match sign convention to curvature (+ve = left)
        if np.sum(np.sign(logged) * np.sign(ay_g)) < 0:
            logged = -logged
        ay_g = logged

    lap = LapTrack(number=number, ds=actual_ds, s=s_grid, t=t_grid,
                   x=xg, y=yg, lat=latg, lon=long_, speed=vg, curv=kappa,
                   ax_g=ax_g, ay_g=ay_g, extras=extras,
                   t_start=t0, lap_time=float(t1 - t0), length=length)
    return lap


def _flag_outliers(laps: List[LapTrack]) -> None:
    if not laps:
        return
    times = np.array([l.lap_time for l in laps])
    best = float(np.min(times))
    med_len = float(np.median([l.length for l in laps]))
    for lap in laps:
        reasons = []
        if lap.lap_time > best * 1.35:
            reasons.append("out/in lap or traffic")
        if abs(lap.length - med_len) > max(60.0, 0.06 * med_len):
            reasons.append("path length off")
        # A slow hairpin on a kart track is not an incident. Only a *sustained*
        # crawl — several seconds of it — means a pit stop, spin or off.
        crawl = lap.speed * 3.6 < 10.0
        if crawl.any():
            secs = float(np.sum(np.diff(lap.t)[crawl[:-1]]))
            if secs > 4.0:
                reasons.append(f"{secs:.0f}s below 10 km/h")
        if reasons:
            lap.valid = False
            lap.note = "; ".join(reasons)


# --------------------------------------------------------------------------
# Cross-lap alignment
# --------------------------------------------------------------------------


def common_grid(laps: Sequence[LapTrack], ds: float = 1.0) -> np.ndarray:
    """Distance grid shared by a set of laps (up to the shortest lap length)."""
    if not laps:
        return np.array([0.0])
    length = min(float(l.length) for l in laps)
    n = max(32, int(round(length / ds)) + 1)
    return np.linspace(0.0, length, n)


def delta_time(reference: LapTrack, other: LapTrack,
               s_grid: Optional[np.ndarray] = None):
    """Cumulative time delta vs distance. Positive => `other` is losing time.

    The derivative of this trace is where the time is actually going: a rising
    section is a section being lost, flat means parity.
    """
    if s_grid is None:
        s_grid = common_grid([reference, other])
    t_ref = np.interp(s_grid, reference.s, reference.t)
    t_oth = np.interp(s_grid, other.s, other.t)
    return s_grid, t_oth - t_ref
