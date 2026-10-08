"""
Racelogic VBOX (.vbo) file parser.

Format notes (the parts that bite you):
  * Sections are bracketed:  [header] [channel units] [comments] [laptiming]
    [column names] [data]
  * Latitude / longitude are stored in *minutes of arc*, and longitude is
    positive to the WEST. So:  deg = minutes / 60 ; lon_deg = -lon_min / 60
  * `time` is UTC formatted HHMMSS.SS (not seconds).
  * `velocity` is km/h.
  * Column count can disagree with the header list; we trust [column names]
    and fall back to [header].
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

# --------------------------------------------------------------------------
# Channel name normalisation
# --------------------------------------------------------------------------

# canonical -> accepted aliases (after squashing to lowercase alphanumerics)
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "sats": ("satellites", "sats", "nsat"),
    "time": ("time", "utctime"),
    "lat": ("latitude", "lat"),
    "lon": ("longitude", "long", "lon", "longitude2"),
    "speed_kmh": ("velocity", "velocitykmh", "speed", "speedkmh", "vel"),
    "heading": ("heading", "head", "course"),
    "height": ("height", "altitude", "alt"),
    "vspeed": ("verticalvelocity", "vertvel", "vertvelkmh", "verticalspeed"),
    "ax_g": ("longacc", "longitudinalacceleration", "longaccg", "longaccel",
             "acclong", "longitudinalaccel"),
    "ay_g": ("latacc", "lateralacceleration", "lataccg", "lataccel",
             "acclat", "lateralaccel"),
    "brake": ("brake", "brakepedal", "brakepressure", "brakeswitch", "bps"),
    "throttle": ("throttle", "throttlepedal", "tps", "pedal", "accelpedal",
                 "throttleposition"),
    "steer": ("steering", "steerangle", "steeringangle", "steeringwheelangle",
              "swa"),
    "rpm": ("rpm", "enginespeed", "engrpm"),
    "gear": ("gear", "gearposition", "selectedgear"),
    "distance": ("distance", "dist"),
    # VBOX HD2 calls this `avitime`; RaceChrono calls it `avisynctime`. Both
    # are the offset into the video in milliseconds for that sample.
    "avitime": ("avitime", "avisynctime", "avisync", "videotime"),
    "lap": ("lapnumber", "lapno", "lap"),
}

_CANON = {alias: canon for canon, aliases in _ALIASES.items() for alias in aliases}


def _squash(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.strip().lower())


def canonical_name(raw: str) -> str:
    """Map a raw VBO channel name to a canonical key, else a safe slug."""
    sq = _squash(raw)
    if sq in _CANON:
        return _CANON[sq]
    # strip trailing unit noise like "velocitykmh" handled above; otherwise slug
    return sq or "unnamed"


# --------------------------------------------------------------------------
# Data container
# --------------------------------------------------------------------------


@dataclass
class VboFile:
    path: str
    #: 'vbo' | 'fit' | 'csv' — what the data was read from, which sets what is
    #: reasonable to expect of it
    source_format: str = "vbo"
    channels: Dict[str, np.ndarray] = field(default_factory=dict)
    units: Dict[str, str] = field(default_factory=dict)
    raw_columns: List[str] = field(default_factory=list)
    header_lines: List[str] = field(default_factory=list)
    comments: List[str] = field(default_factory=list)
    #: video filename stem and extension from the [avi] section, if present
    video_base: Optional[str] = None
    video_ext: Optional[str] = None
    #: (lat1, lon1, lat2, lon2) in decimal degrees, if the file declares one
    start_finish: Optional[Tuple[float, float, float, float]] = None
    splits: List[Tuple[float, float, float, float]] = field(default_factory=list)
    #: names for those splits where the file gives them — RaceChrono writes
    #: the real sector names, which beat "S1, S2, S3" by a distance
    split_names: List[str] = field(default_factory=list)
    sample_rate: float = 0.0

    def has(self, *names: str) -> bool:
        return all(n in self.channels for n in names)

    def get(self, name: str, default: Optional[np.ndarray] = None):
        return self.channels.get(name, default)

    @property
    def n_samples(self) -> int:
        return len(next(iter(self.channels.values()))) if self.channels else 0

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (f"<VboFile {self.path!r} samples={self.n_samples} "
                f"{self.sample_rate:.0f}Hz channels={sorted(self.channels)}>")


# --------------------------------------------------------------------------
# Time / coordinate helpers
# --------------------------------------------------------------------------


def _looks_like_hhmmss(col: np.ndarray, tol: float = 0.01) -> bool:
    """VBOX writes HHMMSS.SS. Detect that vs. plain seconds-since-start.

    Deliberately tolerant: loggers and third-party exporters occasionally emit
    a rounded '...60.00' sample, and a couple of bad rows must not flip the
    whole column into the wrong interpretation (which silently adds 40 s at
    every minute boundary).
    """
    finite = col[np.isfinite(col)]
    if finite.size == 0:
        return False
    if float(np.nanmax(np.abs(finite))) <= 2400.0:
        return False   # too small to be HHMMSS for any real session
    mm = (np.abs(finite) % 10000) // 100
    ss = np.abs(finite) % 100
    bad = np.count_nonzero((mm >= 60) | (ss >= 60))
    # Allow a small absolute count as well as a small fraction: on a short file
    # (or a small slice) a single rounded sample is a large percentage, and
    # rejecting on that basis picks the interpretation that adds 40 s per
    # minute boundary.
    if bad > max(4, tol * finite.size):
        return False
    # Sanity check the decoded step: HHMMSS decoding should give a sane rate.
    hh = np.floor(np.abs(finite) / 10000.0)
    t = hh * 3600.0 + np.minimum(mm, 59) * 60.0 + np.minimum(ss, 59.999)
    d = np.diff(t)
    d = d[(d > 0) & (d < 5.0)]
    return bool(d.size == 0 or 1e-4 < float(np.median(d)) < 2.0)


def decode_time(col: np.ndarray) -> np.ndarray:
    """Return monotonically increasing seconds, handling midnight rollover."""
    col = np.asarray(col, dtype=float)
    if _looks_like_hhmmss(col):
        a = np.abs(col)
        hh = np.floor(a / 10000.0)
        mm = np.floor((a % 10000.0) / 100.0)
        ss = a % 100.0
        # Absorb rounded overflow fields (e.g. a logged '...60.00') instead of
        # letting them punch a 40 s hole in the timeline.
        carry = ss >= 60.0
        mm = mm + np.where(carry, 1.0, 0.0)
        ss = np.where(carry, ss - 60.0, ss)
        carry_m = mm >= 60.0
        hh = hh + np.where(carry_m, 1.0, 0.0)
        mm = np.where(carry_m, mm - 60.0, mm)
        t = hh * 3600.0 + mm * 60.0 + ss
    else:
        t = col.copy()
    # unwrap rollovers (UTC midnight, or a logger that restarts its clock)
    d = np.diff(t)
    rollover = np.zeros_like(t)
    rollover[1:] = np.cumsum(np.where(d < -43200.0, 86400.0, 0.0))
    return t + rollover


def minutes_to_degrees(lat_min: np.ndarray, lon_min: np.ndarray,
                       lon_positive_west: bool = True) -> Tuple[np.ndarray, np.ndarray]:
    """VBO stores arc-minutes; longitude sign convention is West-positive."""
    lat = np.asarray(lat_min, dtype=float) / 60.0
    lon = np.asarray(lon_min, dtype=float) / 60.0
    if lon_positive_west:
        lon = -lon
    return lat, lon


def _parse_line_coords(tokens: List[str], in_minutes: bool,
                       lon_positive_west: bool,
                       near: Optional[Tuple[float, float]] = None
                       ) -> Optional[Tuple[float, float, float, float]]:
    """Parse a timing-line entry into (lat1, lon1, lat2, lon2) degrees.

    The field order is not agreed between writers: VBOX puts latitude first,
    RaceChrono puts longitude first. Rather than guess, both readings are tried
    and the one that lands near the recorded data wins — which is decisive,
    since the wrong order puts a Finnish circuit in the South Atlantic.
    """
    nums = []
    for tok in tokens:
        try:
            nums.append(float(tok))
        except ValueError:
            continue
    if len(nums) < 4:
        return None
    scale = 60.0 if in_minutes else 1.0
    a1, b1, a2, b2 = (v / scale for v in nums[:4])

    def build(lat1, lon1, lat2, lon2):
        if lon_positive_west:
            lon1, lon2 = -lon1, -lon2
        return float(lat1), float(lon1), float(lat2), float(lon2)

    lat_first = build(a1, b1, a2, b2)
    lon_first = build(b1, a1, b2, a2)

    if near is None:
        return lat_first

    def distance(coords):
        lat = (coords[0] + coords[2]) / 2.0
        lon = (coords[1] + coords[3]) / 2.0
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return float("inf")
        return math.hypot(lat - near[0], (lon - near[1]) * 0.5)

    return min((lat_first, lon_first), key=distance)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def parse_vbo(path: str,
              coords_in_minutes: bool = True,
              lon_positive_west: bool = True) -> VboFile:
    """Parse a .vbo file into a :class:`VboFile`.

    Parameters
    ----------
    coords_in_minutes:
        Standard VBOX files store lat/lon in arc-minutes. Set False if your
        exporter already wrote decimal degrees.
    lon_positive_west:
        Standard VBOX convention. Set False if your file uses East-positive.
    """
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    preamble: List[str] = []

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            m = _SECTION_RE.match(line)
            if m:
                current = m.group(1).strip().lower()
                sections.setdefault(current, [])
                continue
            if current is None:
                if line.strip():
                    preamble.append(line.strip())
            else:
                sections[current].append(line)

    if "data" not in sections:
        raise ValueError(f"{path}: no [data] section found — is this a .vbo file?")

    vbo = VboFile(path=path, source_format="vbo")
    vbo.header_lines = [l.strip() for l in sections.get("header", []) if l.strip()]
    vbo.comments = preamble + [l.strip() for l in sections.get("comments", []) if l.strip()]

    # ---- column names -----------------------------------------------------
    col_lines = [l.strip() for l in sections.get("column names", []) if l.strip()]
    if col_lines:
        names = col_lines[0].split()
    else:
        names = vbo.header_lines[:]
    vbo.raw_columns = names

    unit_lines = [l.strip() for l in sections.get("channel units", []) if l.strip()]
    units = unit_lines[0].split() if unit_lines else []

    # ---- data -------------------------------------------------------------
    rows: List[List[float]] = []
    ncol = len(names)
    for line in sections["data"]:
        if not line.strip():
            continue
        toks = line.split()
        if not toks:
            continue
        vals: List[float] = []
        for tok in toks:
            try:
                vals.append(float(tok))
            except ValueError:
                vals.append(np.nan)
        if ncol and len(vals) < ncol * 0.6:
            continue  # badly truncated row
        rows.append(vals)

    if not rows:
        raise ValueError(f"{path}: [data] section is empty")

    width = min(len(r) for r in rows)
    if ncol:
        width = min(width, ncol)
    arr = np.array([r[:width] for r in rows], dtype=float)

    for i in range(width):
        raw = names[i] if i < len(names) else f"col{i}"
        key = canonical_name(raw)
        if key in vbo.channels:            # duplicate name -> suffix it
            key = f"{key}_{i}"
        vbo.channels[key] = arr[:, i]
        if i < len(units):
            vbo.units[key] = units[i]

    # ---- post-process time & coordinates ---------------------------------
    if "time" in vbo.channels:
        vbo.channels["t"] = decode_time(vbo.channels["time"])
    else:
        vbo.channels["t"] = np.arange(arr.shape[0], dtype=float) / 10.0

    if vbo.has("lat", "lon"):
        if coords_in_minutes:
            lat, lon = minutes_to_degrees(vbo.channels["lat"], vbo.channels["lon"],
                                          lon_positive_west)
        else:
            lat = vbo.channels["lat"].copy()
            lon = vbo.channels["lon"].copy()
            if lon_positive_west:
                lon = -lon
        vbo.channels["lat_deg"] = lat
        vbo.channels["lon_deg"] = lon
    else:
        raise ValueError(f"{path}: no latitude/longitude channels found")

    if "speed_kmh" in vbo.channels:
        vbo.channels["speed"] = vbo.channels["speed_kmh"] / 3.6  # m/s
    else:
        raise ValueError(f"{path}: no velocity channel found")

    t = vbo.channels["t"]
    dt = np.diff(t)
    dt = dt[(dt > 1e-4) & (dt < 5.0)]
    vbo.sample_rate = float(1.0 / np.median(dt)) if dt.size else 0.0

    # Where the data actually is, so a timing line can be checked against it.
    here = (float(np.nanmedian(vbo.channels["lat_deg"])),
            float(np.nanmedian(vbo.channels["lon_deg"])))

    # ---- linked video -----------------------------------------------------
    # VBOX HD2 writes the filename stem on one line and the extension on the
    # next; the per-sample `avitime` column then gives the offset into that
    # video in milliseconds, which is what makes exact sync possible.
    avi = [l.strip() for l in sections.get("avi", []) if l.strip()]
    if not avi:
        avi = [l.strip() for l in sections.get("avifileindex", []) if l.strip()]
    if avi:
        vbo.video_base = avi[0]
        if len(avi) > 1:
            vbo.video_ext = avi[1].lstrip(".")

    # ---- lap timing lines -------------------------------------------------
    for line in sections.get("laptiming", []):
        toks = line.split()
        if not toks:
            continue
        tag = toks[0].lower()
        coords = _parse_line_coords(toks[1:], coords_in_minutes,
                                    lon_positive_west, near=here)
        if coords is None:
            continue
        # a trailing comment after the coordinates is the name, where present:
        #   Split  -1465.04 +3660.20 -1465.06 +3660.17 ¬ Varikko
        name = ""
        for marker in ("\u00ac", "#", ";", "//"):
            if marker in line:
                name = line.split(marker, 1)[1].strip()
                break
        if tag.startswith("start") or tag.startswith("finish"):
            vbo.start_finish = coords
        elif tag.startswith("split"):
            vbo.splits.append(coords)
            vbo.split_names.append(name)

    return vbo


# --------------------------------------------------------------------------
# Format dispatch
# --------------------------------------------------------------------------

#: extensions `open_log` understands, for file dialogs and error messages
SUPPORTED_EXTENSIONS = (".vbo", ".fit", ".csv", ".tsv", ".txt")


def open_log(path: str, **kwargs) -> VboFile:
    """Read a data log, whatever format it is in.

    `.vbo` is parsed directly; `.fit` is read natively rather than converted
    to a .vbo first — a text round-trip through a fixed-precision format loses
    resolution, and VBO has nowhere to put the lap- and session-scope fields a
    FIT carries, so they end up flattened into free-text comments.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fit":
        from .fitfile import parse_fit
        return parse_fit(path)
    if ext in (".csv", ".tsv"):
        from .csvlog import parse_csv
        return parse_csv(path)
    try:
        return parse_vbo(path, **kwargs)
    except ValueError:
        # a .txt or .log may hold either VBO text or CSV; try the other one
        # before giving up, and report the CSV failure since it is the more
        # informative of the two
        from .csvlog import parse_csv
        return parse_csv(path)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

#: order VBOX expects the standard channels in; extras follow
_WRITE_ORDER = ("sats", "time", "lat", "lon", "speed_kmh", "heading",
                "height", "vspeed")
_WRITE_HEADER = {
    "sats": ("satellites", "sats"),
    "time": ("time", "time"),
    "lat": ("latitude", "lat"),
    "lon": ("longitude", "long"),
    "speed_kmh": ("velocity kmh", "velocity"),
    "heading": ("heading", "heading"),
    "height": ("height", "height"),
    "vspeed": ("vertical velocity kmh", "vert-vel"),
    "ax_g": ("LongAcc", "LongAcc"),
    "ay_g": ("LatAcc", "LatAcc"),
    "brake": ("Brake", "Brake"),
    "throttle": ("Throttle", "Throttle"),
    "heartrate": ("heart_rate", "heart_rate"),
}


def write_vbo(vbo: VboFile, path: str) -> str:
    """Write a VboFile back out as a .vbo.

    Mainly so a Garmin activity read natively can still be handed to VBOX
    Circuit Tools, which only speaks .vbo. Lap- and session-scope fields go
    into `[comments]` because VBO has nowhere else for them, and split lines
    into `[laptiming]`, where they are actually usable.
    """
    n = vbo.n_samples
    if not n:
        raise ValueError("nothing to write")

    def col(name):
        v = vbo.channels.get(name)
        return None if v is None else np.asarray(v, dtype=float)

    lat = col("lat_deg")
    lon = col("lon_deg")
    if lat is None or lon is None:
        raise ValueError("cannot write a .vbo without latitude and longitude")

    out = dict(vbo.channels)
    out["lat"] = lat * 60.0
    out["lon"] = -lon * 60.0                 # VBO longitude is West-positive
    if "time" not in out and "t" in out:
        t = np.asarray(out["t"], dtype=float) % 86400.0
        out["time"] = (np.floor(t / 3600) * 10000
                       + np.floor((t % 3600) / 60) * 100 + (t % 60))
    if "sats" not in out:
        out["sats"] = np.full(n, 10.0)
    if "vspeed" not in out:
        out["vspeed"] = np.zeros(n)

    names = [k for k in _WRITE_ORDER if k in out]
    names += [k for k in ("ax_g", "ay_g", "brake", "throttle", "heartrate")
              if k in out and k not in names]

    header, columns = [], []
    for key in names:
        head, colname = _WRITE_HEADER.get(key, (key, key))
        header.append(head)
        columns.append(colname)

    lines = ["File created by GARW Genie Lap Analysis", ""]
    lines.append("[header]")
    lines += header
    lines.append("")
    lines.append("[comments]")
    lines += [c for c in vbo.comments if c.strip()] or ["(none)"]
    lines.append("")
    if vbo.start_finish or vbo.splits:
        lines.append("[laptiming]")
        if vbo.start_finish:
            la1, lo1, la2, lo2 = vbo.start_finish
            lines.append(f"Start {la1 * 60:+.5f} {-lo1 * 60:+.5f} "
                         f"{la2 * 60:+.5f} {-lo2 * 60:+.5f}")
        for i, (la1, lo1, la2, lo2) in enumerate(vbo.splits, start=1):
            lines.append(f"Split{i} {la1 * 60:+.5f} {-lo1 * 60:+.5f} "
                         f"{la2 * 60:+.5f} {-lo2 * 60:+.5f}")
        lines.append("")
    lines.append("[column names]")
    lines.append(" ".join(columns))
    lines.append("")
    lines.append("[data]")

    arrays = [np.asarray(out[k], dtype=float) for k in names]
    for i in range(n):
        row = []
        for key, arr in zip(names, arrays):
            v = float(arr[i]) if i < arr.size else 0.0
            if key == "sats":
                row.append(f"{int(v):03d}")
            elif key == "time":
                row.append(f"{v:010.2f}")
            elif key in ("lat", "lon"):
                row.append(f"{v:+011.5f}")
            else:
                row.append(f"{v:.3f}")
        lines.append(" ".join(row))

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
