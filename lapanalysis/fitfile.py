"""
Garmin FIT activities, read natively.

Adapted from the `fit2vbo.py` converter that previously did this as a separate
step. Two things change by bringing it inside.

**No text round-trip.** The converter wrote a .vbo and the tool read it back,
which meant every value passed through a fixed-precision text format and every
lap- and session-scope field had to be flattened into free-text comments
because VBO has no place for them. Reading the FIT directly keeps the values
and keeps the structure.

**Real sectors.** A FIT lap message carries `sector1..sector8` — actual sector
times from the watch. Written into VBO comments those were unusable: sector
times without the boundaries they were measured between cannot be compared to
anything. Here they can be turned back into positions, by walking each lap's
own trace to the point where the cumulative sector time falls, which gives
genuine split lines instead of arbitrary equal thirds.

Requires `fitparse` (`pip install fitparse`); everything else works without it.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .parser import VboFile

SEMI_TO_DEG = 180.0 / (2 ** 31)
G = 9.80665
#: GPS-derived accelerations get clamped to something a car could produce
G_CLAMP = 3.0

try:                                                     # pragma: no cover
    import fitparse
    FITPARSE = True
except ImportError:                                      # pragma: no cover
    FITPARSE = False


class FitUnavailable(RuntimeError):
    """fitparse is not installed."""


def _require_fitparse() -> None:
    if not FITPARSE:
        raise FitUnavailable(
            "Reading .fit files needs the fitparse package:\n"
            "    pip install fitparse\n"
            "Or convert to .vbo first and open that instead.")


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _semi(v) -> Optional[float]:
    return None if v is None else v * SEMI_TO_DEG


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def read_records(path: str) -> List[dict]:
    """Every FIT `record` message that has both a time and a position."""
    _require_fitparse()
    rows: List[dict] = []
    for msg in fitparse.FitFile(path).get_messages("record"):
        vals = {d.name: d.value for d in msg}
        ts = vals.get("timestamp")
        lat = _semi(vals.get("position_lat"))
        lon = _semi(vals.get("position_long"))
        if ts is None or lat is None or lon is None:
            continue
        rows.append({
            "ts": ts,
            "lat": lat,
            "lon": lon,
            "speed": vals.get("enhanced_speed", vals.get("speed")),
            "alt": vals.get("enhanced_altitude", vals.get("altitude")),
            "hr": vals.get("heart_rate"),
            "temp": vals.get("temperature"),
            "g_lat": vals.get("g_lat"),        # watch-app developer fields
            "g_long": vals.get("g_long"),
            "spo2": vals.get("spo2"),
            "resp": vals.get("respiration"),
        })
    rows.sort(key=lambda r: r["ts"])
    return rows


def read_laps(path: str) -> List[dict]:
    """Lap messages: elapsed time, sector times, pit time, start position."""
    _require_fitparse()
    out: List[dict] = []
    try:
        for msg in fitparse.FitFile(path).get_messages("lap"):
            v = {d.name: d.value for d in msg}
            out.append({
                "time": v.get("total_elapsed_time"),
                "sectors": [v.get("sector%d" % i) for i in range(1, 9)],
                "pit": v.get("pit_time"),
                "flag": v.get("lap_flag"),
                "start_lat": _semi(v.get("start_position_lat")),
                "start_lon": _semi(v.get("start_position_long")),
                "max_lat_g": v.get("lap_max_lat_g"),
                "max_brake_g": v.get("lap_max_brake_g"),
                "start_ts": v.get("start_time"),
            })
    except Exception:                                    # noqa: BLE001
        pass
    return out


def read_session(path: str) -> dict:
    """Session-scope fields, including the watch app's own."""
    _require_fitparse()
    keys = ("track", "pit_count", "pit_best", "pit_total", "best_lap",
            "theoretical_best", "max_g", "max_speed", "avg_speed",
            "max_lat_g", "max_brake_g", "max_accel_g", "max_temp",
            "total_ascent", "total_descent", "sport")
    out: dict = {}
    try:
        for msg in fitparse.FitFile(path).get_messages("session"):
            for d in msg:
                if d.name in keys and d.value is not None:
                    out[d.name] = d.value
    except Exception:                                    # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def _monotonic_seconds(rows: Sequence[dict]) -> np.ndarray:
    """Seconds since midnight UTC, strictly increasing.

    FIT timestamps are whole seconds, so several records can share one. Ties
    are spread evenly across the second rather than left duplicated, which
    would otherwise put a zero time step into every distance and speed
    integral downstream.
    """
    stamps = [r["ts"] for r in rows]
    base = [t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
            for t in stamps]
    out = np.array(base, dtype=float)
    # unwrap midnight
    for i in range(1, out.size):
        while out[i] < out[i - 1] - 43200.0:
            out[i:] += 86400.0
            break
    i = 0
    n = out.size
    while i < n:
        j = i
        while j + 1 < n and out[j + 1] == out[i]:
            j += 1
        if j > i:
            span = (out[j + 1] - out[i]) if j + 1 < n else 1.0
            span = span if span > 0 else 1.0
            for k in range(i, j + 1):
                out[k] = out[i] + span * (k - i) / (j - i + 1)
        i = j + 1
    return out


def derive_channels(rows: Sequence[dict]) -> Dict[str, np.ndarray]:
    """Turn FIT records into the channel arrays a VboFile carries."""
    n = len(rows)
    t = _monotonic_seconds(rows)
    lat = np.array([r["lat"] for r in rows], dtype=float)
    lon = np.array([r["lon"] for r in rows], dtype=float)
    speed = np.array([r["speed"] if r["speed"] is not None else np.nan
                      for r in rows], dtype=float)
    if np.all(np.isnan(speed)):
        speed = np.zeros(n)
    else:
        speed = _fill_gaps(speed)

    heading = np.zeros(n)
    for i in range(n):
        j = min(i + 1, n - 1)
        if j != i:
            heading[i] = bearing_deg(lat[i], lon[i], lat[j], lon[j])
        elif i:
            heading[i] = heading[i - 1]

    dtv = np.diff(t, prepend=t[0] if n else 0.0)
    dtv[dtv <= 0] = np.nan

    have_long = any(r["g_long"] is not None for r in rows)
    if have_long:
        ax = np.array([float(r["g_long"]) if r["g_long"] is not None else 0.0
                       for r in rows])
    else:
        dv = np.diff(speed, prepend=speed[0] if n else 0.0)
        ax = np.clip(np.nan_to_num(dv / dtv / G), -G_CLAMP, G_CLAMP)

    have_lat = any(r["g_lat"] is not None for r in rows)
    if have_lat:
        ay = np.array([float(r["g_lat"]) if r["g_lat"] is not None else 0.0
                       for r in rows])
    else:
        dh = np.diff(heading, prepend=heading[0] if n else 0.0)
        dh = (dh + 180.0) % 360.0 - 180.0
        yaw = np.radians(dh) / dtv
        ay = np.clip(np.nan_to_num(speed * yaw / G), -G_CLAMP, G_CLAMP)

    channels: Dict[str, np.ndarray] = {
        "t": t,
        "lat_deg": lat,
        "lon_deg": lon,
        "speed": np.nan_to_num(speed),
        "speed_kmh": np.nan_to_num(speed) * 3.6,
        "heading": heading,
        "ax_g": ax,
        "ay_g": ay,
    }
    for key, field in (("height", "alt"), ("heartrate", "hr"),
                       ("temperature", "temp"), ("spo2", "spo2"),
                       ("respiration", "resp")):
        vals = [r.get(field) for r in rows]
        if any(v is not None for v in vals):
            channels[key] = _fill_gaps(
                np.array([np.nan if v is None else float(v) for v in vals]))
    return channels


def _fill_gaps(arr: np.ndarray) -> np.ndarray:
    """Linear interpolation across missing samples, holding the ends."""
    a = np.asarray(arr, dtype=float)
    good = np.isfinite(a)
    if not good.any():
        return np.zeros_like(a)
    idx = np.arange(a.size)
    return np.interp(idx, idx[good], a[good])


def start_finish_from_laps(laps: Sequence[dict], lat: np.ndarray,
                           lon: np.ndarray, half_width_m: float = 20.0
                           ) -> Optional[Tuple[float, float, float, float]]:
    """A timing line from where the lap markers cluster.

    FIT lap messages record where each lap began, and on a circuit those all
    land on the same crossing. The first is dropped when there are several: it
    is where the recording started, which is usually the pit lane rather than
    the line.
    """
    pts = [(l["start_lat"], l["start_lon"]) for l in laps
           if l.get("start_lat") is not None and l.get("start_lon") is not None]
    if len(pts) >= 3:
        pts = pts[1:]
    if not pts or lat.size < 2:
        return None
    sf_lat = sum(p[0] for p in pts) / len(pts)
    sf_lon = sum(p[1] for p in pts) / len(pts)

    coslat = math.cos(math.radians(sf_lat))
    d2 = (lat - sf_lat) ** 2 + ((lon - sf_lon) * coslat) ** 2
    i = int(np.argmin(d2))
    j = min(i + 2, lat.size - 1)
    hdg = bearing_deg(float(lat[i]), float(lon[i]),
                      float(lat[j]), float(lon[j]))

    perp = math.radians(hdg + 90.0)
    dlat = (half_width_m * math.cos(perp)) / 111320.0
    dlon = (half_width_m * math.sin(perp)) / (111320.0 * coslat)
    return (sf_lat + dlat, sf_lon + dlon, sf_lat - dlat, sf_lon - dlon)


def split_lines_from_sectors(laps: Sequence[dict], t: np.ndarray,
                             lat: np.ndarray, lon: np.ndarray,
                             half_width_m: float = 20.0
                             ) -> List[Tuple[float, float, float, float]]:
    """Turn recorded sector *times* back into split *lines*.

    A sector time on its own cannot be compared to anything — it is a duration
    with no stated boundaries. But the lap it belongs to has a start time, so
    walking that lap's trace to each cumulative sector time gives the position
    the watch was timing to, and a line across the track there is a split gate
    like any other.

    Positions are averaged across laps, which both improves them and confirms
    them: if the sector boundaries were consistent, the per-lap positions
    cluster.
    """
    if t.size < 2:
        return []
    timed = [l for l in laps
             if l.get("start_ts") is not None
             and any(s is not None for s in l.get("sectors", []))]
    if not timed:
        return []

    base = t[0]

    def seconds_of(stamp) -> Optional[float]:
        try:
            return (stamp.hour * 3600 + stamp.minute * 60 + stamp.second
                    + stamp.microsecond / 1e6)
        except AttributeError:
            return None

    per_boundary: Dict[int, List[Tuple[float, float]]] = {}
    for lap in timed:
        start = seconds_of(lap["start_ts"])
        if start is None:
            continue
        # tolerate a lap that starts before the first record we kept
        if start < t[0] - 1.0 or start > t[-1]:
            continue
        sectors = [s for s in lap["sectors"] if s is not None]
        cumulative = 0.0
        for k, sec in enumerate(sectors[:-1]):      # the last ends the lap
            cumulative += float(sec)
            when = start + cumulative
            if when < t[0] or when > t[-1]:
                continue
            per_boundary.setdefault(k, []).append(
                (float(np.interp(when, t, lat)),
                 float(np.interp(when, t, lon))))

    lines: List[Tuple[float, float, float, float]] = []
    for k in sorted(per_boundary):
        pts = per_boundary[k]
        if not pts:
            continue
        # Median, not mean, and only if the laps agree. The same boundary
        # should land in the same place every lap; if it does not, either the
        # sector timing was inconsistent or these are not laps of one circuit,
        # and averaging two distant points would invent a boundary that was
        # never crossed.
        mlat = float(np.median([p[0] for p in pts]))
        mlon = float(np.median([p[1] for p in pts]))
        if len(pts) > 1:
            cl = math.cos(math.radians(mlat))
            spread = max(math.hypot((p[0] - mlat) * 111320.0,
                                    (p[1] - mlon) * 111320.0 * cl)
                         for p in pts)
            if spread > 60.0:
                continue
        coslat = math.cos(math.radians(mlat))
        d2 = (lat - mlat) ** 2 + ((lon - mlon) * coslat) ** 2
        i = int(np.argmin(d2))
        j = min(i + 2, lat.size - 1)
        hdg = bearing_deg(float(lat[i]), float(lon[i]),
                          float(lat[j]), float(lon[j]))
        perp = math.radians(hdg + 90.0)
        dlat = (half_width_m * math.cos(perp)) / 111320.0
        dlon = (half_width_m * math.sin(perp)) / (111320.0 * coslat)
        lines.append((mlat + dlat, mlon + dlon, mlat - dlat, mlon - dlon))
    return lines


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_vbo(path: str, rows: Sequence[dict], laps: Sequence[dict],
              session: dict) -> VboFile:
    """Assemble a VboFile from already-read FIT structures.

    Separate from the reading so it can be exercised without a .fit file.
    """
    if not rows:
        raise ValueError(f"{path}: no GPS records with position and time")

    vbo = VboFile(path=path, source_format="fit")
    vbo.channels = derive_channels(rows)
    t = vbo.channels["t"]
    lat = vbo.channels["lat_deg"]
    lon = vbo.channels["lon_deg"]

    dtv = np.diff(t)
    dtv = dtv[(dtv > 1e-6) & (dtv < 30.0)]
    vbo.sample_rate = float(1.0 / np.median(dtv)) if dtv.size else 1.0

    vbo.start_finish = start_finish_from_laps(laps, lat, lon)
    vbo.splits = split_lines_from_sectors(laps, t, lat, lon)

    vbo.raw_columns = sorted(vbo.channels)
    vbo.comments = _comments(rows, laps, session)
    return vbo


def _comments(rows: Sequence[dict], laps: Sequence[dict],
              session: dict) -> List[str]:
    out = [f"Read from Garmin FIT by {__package__ or 'lapanalysis'}"]
    if session.get("track"):
        out.append(f"Track: {session['track']}")
    have_g = any(r.get("g_lat") is not None for r in rows)
    out.append("Accelerations: " + ("from device g_lat/g_long fields"
                                    if have_g else "derived from GPS"))
    if session.get("best_lap"):
        out.append(f"Device best lap: {session['best_lap']}")
    if session.get("theoretical_best"):
        out.append(f"Device theoretical best: {session['theoretical_best']}")
    for i, lap in enumerate(laps, start=1):
        secs = [s for s in lap.get("sectors", []) if s is not None]
        bits = [f"Lap {i}"]
        if lap.get("time") is not None:
            bits.append(f"{float(lap['time']):.2f}s")
        if secs:
            bits.append("S " + "/".join(f"{float(s):.1f}" for s in secs))
        if lap.get("pit") is not None:
            bits.append(f"pit {float(lap['pit']):.1f}s")
        out.append("  ".join(bits))
    return out


def parse_fit(path: str) -> VboFile:
    """Read a Garmin .fit activity into the same structure as a .vbo."""
    _require_fitparse()
    rows = read_records(path)
    return build_vbo(path, rows, read_laps(path), read_session(path))
