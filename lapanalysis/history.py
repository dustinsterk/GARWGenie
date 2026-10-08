"""
Session history: progress across visits to the same circuit.

Analyzing a session in isolation cannot answer the question a coach answers
best — *am I getting better, and at what?* This module keeps a small JSON
record per session so the tool can say "your brake point at the Bowl has moved
forty feet later across four visits, and your scatter there has halved".

Two design problems have to be solved for that to mean anything.

**Which circuit is this?** Sessions are grouped by a fingerprint derived from
the coordinates and the lap length, so visits group themselves with no naming
required. Two circuits a hundred meters apart would collide; two circuits a
kilometer apart will not.

**Which corner is this?** Corner *numbering* is not stable — it comes from
curvature detection on that session's reference lap, so a corner detected as a
double apex one day and a single the next shifts every number after it. Corners
are therefore matched between sessions by the geographic position of their
apex, which does not move.

**Which metrics are comparable?** Anything measured as distance-from-the-line
is worthless across sessions, because the start/finish may be auto-placed
somewhere different. Everything stored here is independent of it: braking
distance *before the apex*, minimum speed, exit speed, lateral g, coast time.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .corners import Corner, CornerMetrics, GripEnvelope, analyse_lap
from .insights import theoretical_best
from .laps import LapTrack, Session

STORE_DIR = os.path.join(
    os.path.expanduser("~/.garw_genie"), "lap_analysis")
STORE_PATH = os.path.join(STORE_DIR, "history.json")

SCHEMA = 1
#: two apexes closer than this across sessions are taken to be the same corner
CORNER_MATCH_M = 45.0


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass
class CornerRecord:
    """One corner, in terms that survive a different start/finish line."""
    lat: float
    lon: float
    name: str                      # 'T4' as detected, or a user-given name
    direction: str
    radius_m: float
    brake_before_apex_m: Optional[float]
    v_min_kmh: float
    v_exit_kmh: float
    peak_lat_g: float
    coast_s: float
    brake_scatter_m: Optional[float] = None


@dataclass
class SessionRecord:
    circuit: str
    label: str                     # human name, editable
    source: str                    # file it came from
    recorded_at: float             # unix time of recording
    session_at: Optional[float]    # unix time of the session itself, if known
    lap_count: int
    clean_laps: int
    best_lap_s: float
    theoretical_best_s: float
    sample_rate_hz: float
    lap_length_m: float
    max_lat_g: float
    max_decel_g: float
    corners: List[CornerRecord] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "SessionRecord":
        corners = [CornerRecord(**c) for c in d.get("corners", [])]
        d = dict(d, corners=corners)
        return cls(**d)


# --------------------------------------------------------------------------
# Circuit fingerprint
# --------------------------------------------------------------------------


def circuit_key(session: Session) -> str:
    """A stable identifier for the venue, from position and lap length.

    Rounded to roughly a kilometer so that a different start/finish line, a
    different session, or GPS wander all land on the same key — while two
    genuinely different circuits do not.
    """
    lat = round(session.lat0, 2)
    lon = round(session.lon0, 2)
    laps = session.valid_laps or session.laps
    length = float(np.median([l.length for l in laps])) if laps else 0.0
    bucket = int(round(length / 250.0)) * 250
    return f"{lat:+07.2f}{lon:+08.2f}_{bucket}"


def circuit_label(session: Session) -> str:
    """A first-guess human label. The user can rename it later."""
    laps = session.valid_laps or session.laps
    length = float(np.median([l.length for l in laps])) if laps else 0.0
    return (f"{abs(session.lat0):.3f}{'N' if session.lat0 >= 0 else 'S'} "
            f"{abs(session.lon0):.3f}{'E' if session.lon0 >= 0 else 'W'} "
            f"({length / 1000.0:.2f} km)")


def session_time(session: Session) -> Optional[float]:
    """Unix time of the session, from the file header if it is parseable."""
    for line in session.vbo.comments[:6]:
        text = line.strip()
        for fmt in ("File created on %d/%m/%Y @ %H:%M:%S",
                    "File created on %d/%m/%Y at %H:%M:%S"):
            try:
                return time.mktime(time.strptime(text, fmt))
            except ValueError:
                continue
    try:
        return os.path.getmtime(session.vbo.path)
    except OSError:
        return None


# --------------------------------------------------------------------------
# Building a record
# --------------------------------------------------------------------------


def brake_scatter(laps: Sequence[LapTrack], corners: Sequence[Corner],
                  index: int) -> Optional[float]:
    """Spread of the braking distance before the apex, across the session.

    Measured with the *whole* corner set, not the corner alone: a corner
    analyzed in isolation gets a 260 m look-back window with nothing to trim
    it, so it happily picks up the previous corner's braking event and reports
    a scatter of several hundred meters.
    """
    points = []
    for lap in laps:
        for c, m in zip(corners, analyse_lap(lap, corners)):
            if c.index != index:
                continue
            if m.has_braking and m.s_brake is not None:
                gap = m.s_vmin - m.s_brake
                if 0.0 < gap < 400.0:
                    points.append(gap)
    return float(np.std(points)) if len(points) >= 3 else None


def _brake_gap(m: CornerMetrics) -> Optional[float]:
    """Braking distance before the apex, or None if there was no real braking.

    Start/finish independent by construction, which is what makes it comparable
    between sessions that may have placed the timing line differently.
    """
    if not m.has_braking or m.s_brake is None:
        return None
    gap = float(m.s_vmin - m.s_brake)
    # a "braking zone" longer than this is the detector having found the
    # previous corner's event, not this one's
    return gap if 0.0 < gap < 400.0 else None


def build_record(session: Session, corners: Sequence[Corner],
                 reference: LapTrack, label: Optional[str] = None
                 ) -> SessionRecord:
    pool = session.valid_laps or session.laps
    env = GripEnvelope.from_laps(pool)
    tb, _ = theoretical_best(pool, corners)
    metrics = analyse_lap(reference, corners)

    records: List[CornerRecord] = []
    for c, m in zip(corners, metrics):
        i = reference.idx(m.s_vmin)
        records.append(CornerRecord(
            lat=float(reference.lat[i]),
            lon=float(reference.lon[i]),
            name=c.name,
            direction=c.direction,
            radius_m=float(c.min_radius),
            brake_before_apex_m=_brake_gap(m),
            v_min_kmh=float(m.v_min),
            v_exit_kmh=float(m.v_exit_plus),
            peak_lat_g=float(m.peak_lat_g),
            coast_s=float(m.coast_time),
            brake_scatter_m=brake_scatter(pool, corners, c.index),
        ))

    return SessionRecord(
        circuit=circuit_key(session),
        label=label or circuit_label(session),
        source=os.path.abspath(session.vbo.path),
        recorded_at=time.time(),
        session_at=session_time(session),
        lap_count=len(session.laps),
        clean_laps=len(session.valid_laps),
        best_lap_s=float(reference.lap_time),
        theoretical_best_s=float(tb),
        sample_rate_hz=float(session.vbo.sample_rate),
        lap_length_m=float(reference.length),
        max_lat_g=float(env.max_lat_g),
        max_decel_g=float(env.max_decel_g),
        corners=records)


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _read_blob(path: str = STORE_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return blob if blob.get("schema") == SCHEMA else {}


def _write_blob(blob: dict, path: str = STORE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob["schema"] = SCHEMA
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=1)
    os.replace(tmp, path)


def load(path: str = STORE_PATH) -> List[SessionRecord]:
    blob = _read_blob(path)
    out = []
    for entry in blob.get("sessions", []):
        try:
            out.append(SessionRecord.from_json(entry))
        except TypeError:
            continue                # a record from a future field set
    return out


def save(records: Sequence[SessionRecord], path: str = STORE_PATH) -> None:
    """Replace the stored sessions, leaving corner names alone.

    The two live in one file but are independent: clearing your session history
    should not silently discard the names you gave the corners.
    """
    blob = _read_blob(path)
    blob["sessions"] = [r.to_json() for r in records]
    _write_blob(blob, path)


# --------------------------------------------------------------------------
# Corner names
# --------------------------------------------------------------------------


def load_names(circuit: str, path: str = STORE_PATH) -> List[dict]:
    """Named corners at a circuit: {'lat', 'lon', 'name', 'direction'}."""
    blob = _read_blob(path)
    return list(blob.get("corner_names", {}).get(circuit, []))


def set_name(circuit: str, lat: float, lon: float, name: str,
             direction: str = "", path: str = STORE_PATH,
             tolerance_m: float = CORNER_MATCH_M) -> None:
    """Name the corner whose apex is at (lat, lon).

    Replaces any existing name within `tolerance_m`, so renaming a corner
    updates it rather than accumulating a second name a few meters away. An
    empty name removes it.
    """
    blob = _read_blob(path)
    names = blob.setdefault("corner_names", {})
    entries = [e for e in names.get(circuit, [])
               if _meters_between(e["lat"], e["lon"], lat, lon) > tolerance_m]
    if name.strip():
        entries.append({"lat": float(lat), "lon": float(lon),
                        "name": name.strip(), "direction": direction})
    if entries:
        names[circuit] = entries
    else:
        names.pop(circuit, None)
    _write_blob(blob, path)


def clear_names(circuit: Optional[str] = None, path: str = STORE_PATH) -> int:
    """Forget corner names, at one circuit or everywhere."""
    blob = _read_blob(path)
    names = blob.get("corner_names", {})
    if circuit is None:
        removed = sum(len(v) for v in names.values())
        blob["corner_names"] = {}
    else:
        removed = len(names.get(circuit, []))
        names.pop(circuit, None)
    _write_blob(blob, path)
    return removed


def apply_names(circuit: str, corners: Sequence, reference,
                path: str = STORE_PATH,
                tolerance_m: float = CORNER_MATCH_M) -> int:
    """Attach stored names to detected corners, by where their apexes are.

    Returns how many were named. Matching on position rather than number is
    the whole point: a corner detected as a double apex one day and a single
    the next would otherwise take the wrong name, or shift every name after it.
    """
    entries = load_names(circuit, path)
    if not entries or reference is None:
        return 0
    used = set()
    named = 0
    for corner in corners:
        i = reference.idx(corner.s_geo_apex)
        lat = float(reference.lat[i])
        lon = float(reference.lon[i])
        best, best_d = None, tolerance_m
        for j, entry in enumerate(entries):
            if j in used:
                continue
            d = _meters_between(entry["lat"], entry["lon"], lat, lon)
            if d < best_d:
                best, best_d = j, d
        if best is not None:
            used.add(best)
            corner.given_name = entries[best]["name"]
            named += 1
    return named


def record_session(record: SessionRecord, path: str = STORE_PATH) -> bool:
    """Add a session, replacing any earlier record of the same file.

    Returns True if this was a new session rather than a re-record, so the
    caller can say which happened.
    """
    existing = load(path)
    before = len(existing)
    existing = [r for r in existing if r.source != record.source]
    existing.append(record)
    existing.sort(key=lambda r: r.session_at or r.recorded_at)
    save(existing, path)
    return len(existing) > before


def sessions_for(circuit: str, path: str = STORE_PATH) -> List[SessionRecord]:
    return [r for r in load(path) if r.circuit == circuit]


def forget_session(source: str, path: str = STORE_PATH) -> int:
    """Remove the record of one logged session, identified by its file."""
    import os as _os
    target = _os.path.abspath(source)
    keep = [r for r in load(path) if _os.path.abspath(r.source) != target]
    removed = len(load(path)) - len(keep)
    if removed:
        save(keep, path)
    return removed


def forget_circuit(circuit: str, path: str = STORE_PATH) -> int:
    """Remove every recorded session at one circuit."""
    existing = load(path)
    keep = [r for r in existing if r.circuit != circuit]
    removed = len(existing) - len(keep)
    if removed:
        save(keep, path)
    return removed


def clear(path: str = STORE_PATH) -> int:
    """Remove the whole history.

    The file is emptied rather than deleted, so the store stays valid and a
    later save does not have to recreate the directory.
    """
    removed = len(load(path))
    save([], path)
    return removed


def circuits(path: str = STORE_PATH) -> List[tuple]:
    """(circuit key, label, session count), most recently visited first."""
    grouped: Dict[str, List[SessionRecord]] = {}
    for r in load(path):
        grouped.setdefault(r.circuit, []).append(r)
    out = []
    for key, records in grouped.items():
        records.sort(key=lambda r: r.session_at or r.recorded_at)
        out.append((key, records[-1].label, len(records)))
    out.sort(key=lambda item: item[2], reverse=True)
    return out


# --------------------------------------------------------------------------
# Matching corners across sessions
# --------------------------------------------------------------------------


def _meters_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dx = math.radians(lon2 - lon1) * 6378137.0 * math.cos(mean_lat)
    dy = math.radians(lat2 - lat1) * 6378137.0
    return math.hypot(dx, dy)


def _comparable(a: CornerRecord, b: CornerRecord) -> bool:
    """Guard against pairing a corner with its neighbor.

    Two apexes can sit close together at a chicane or a double apex, so
    proximity alone is not enough: the corners must turn the same way and be
    of broadly similar tightness.
    """
    if a.direction != b.direction:
        return False
    lo, hi = sorted((max(a.radius_m, 1.0), max(b.radius_m, 1.0)))
    return hi / lo <= 2.5


def match_corners(older: Sequence[CornerRecord], newer: Sequence[CornerRecord],
                  tolerance_m: float = CORNER_MATCH_M
                  ) -> List[Tuple[CornerRecord, CornerRecord]]:
    """Pair corners between two sessions by where their apexes actually are.

    Corner *numbers* are not stable between sessions — they come from curvature
    detection, so one corner resolving as a double apex shifts every number
    after it. Apex position does not move.
    """
    pairs = []
    used = set()
    for a in older:
        best, best_d = None, tolerance_m
        for j, b in enumerate(newer):
            if j in used or not _comparable(a, b):
                continue
            d = _meters_between(a.lat, a.lon, b.lat, b.lon)
            if d < best_d:
                best, best_d = j, d
        if best is not None:
            used.add(best)
            pairs.append((a, newer[best]))
    return pairs


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------


@dataclass
class CornerTrend:
    name: str
    direction: str
    sessions: int
    d_brake_m: Optional[float]      # + = braking later (closer to the apex)
    d_v_min_kmh: float
    d_v_exit_kmh: float
    d_coast_s: float
    scatter_now_m: Optional[float]
    scatter_then_m: Optional[float]


@dataclass
class Trend:
    circuit: str
    label: str
    sessions: int
    first_best_s: float
    last_best_s: float
    best_ever_s: float
    first_at: Optional[float]
    last_at: Optional[float]
    #: which files the two compared sessions came from, and which holds the
    #: best lap — "the latest versus the first" is ambiguous once there are
    #: several, and you cannot check a surprising trend without knowing
    first_source: str = ""
    last_source: str = ""
    best_source: str = ""
    best_at: Optional[float] = None
    corners: List[CornerTrend] = field(default_factory=list)

    @property
    def improvement_s(self) -> float:
        """Seconds gained. Positive means faster now than at the first visit."""
        return self.first_best_s - self.last_best_s

    @property
    def change_s(self) -> float:
        """Signed like every other delta here: negative is faster."""
        return self.last_best_s - self.first_best_s


def trend_for(circuit: str, path: str = STORE_PATH) -> Optional[Trend]:
    records = sessions_for(circuit, path)
    if len(records) < 2:
        return None
    first, last = records[0], records[-1]
    best = min(records, key=lambda r: r.best_lap_s)
    trend = Trend(
        circuit=circuit,
        label=last.label,
        sessions=len(records),
        first_best_s=first.best_lap_s,
        last_best_s=last.best_lap_s,
        best_ever_s=best.best_lap_s,
        first_at=first.session_at,
        last_at=last.session_at,
        first_source=os.path.basename(first.source),
        last_source=os.path.basename(last.source),
        best_source=os.path.basename(best.source),
        best_at=best.session_at)

    for a, b in match_corners(first.corners, last.corners):
        seen = 2
        d_brake = (None if a.brake_before_apex_m is None
                   or b.brake_before_apex_m is None
                   else a.brake_before_apex_m - b.brake_before_apex_m)
        trend.corners.append(CornerTrend(
            name=b.name,
            direction=b.direction,
            sessions=seen,
            d_brake_m=d_brake,
            d_v_min_kmh=b.v_min_kmh - a.v_min_kmh,
            d_v_exit_kmh=b.v_exit_kmh - a.v_exit_kmh,
            d_coast_s=b.coast_s - a.coast_s,
            scatter_now_m=b.brake_scatter_m,
            scatter_then_m=a.brake_scatter_m))
    return trend
