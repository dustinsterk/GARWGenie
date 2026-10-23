"""
CSV data logs.

There is no CSV standard for this, so the reader is built to survive the
variations rather than to recognize particular products. In practice the files
from RaceChrono, Harry's LapTimer, TrackAddict, AiM and various dataloggers
differ along five axes, and each is handled explicitly:

* **Preamble.** Metadata lines before the header — sometimes commented with
  `#`, sometimes not. The header is found by looking for the row that both
  names recognizable channels and is followed by numbers.
* **Delimiter.** Comma, semicolon or tab.
* **Units row.** Some exports put units on a second row under the names.
* **Column naming.** Handled by an alias table, with units taken from a
  suffix — `Speed (mph)`, `Speed_kph`, `speed[m/s]`.
* **Time.** Elapsed seconds, `HHMMSS.ss`, a Unix epoch, or an ISO timestamp.

Where a unit genuinely cannot be established it is inferred from magnitude and
the guess is recorded in the file's comments, so it appears in the report
rather than silently shaping every number downstream.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .parser import VboFile, canonical_name

DELIMITERS = (",", ";", "\t", "|")

#: column name -> canonical channel, on top of the .vbo alias table
CSV_ALIASES = {
    "locationlatitude": "lat_deg", "gpslatitude": "lat_deg",
    "latitudedeg": "lat_deg", "lat": "lat_deg", "latitude": "lat_deg",
    "locationlongitude": "lon_deg", "gpslongitude": "lon_deg",
    "longitudedeg": "lon_deg", "lon": "lon_deg", "lng": "lon_deg",
    "long": "lon_deg", "longitude": "lon_deg",
    "locationspeed": "speed", "gpsspeed": "speed", "speed": "speed",
    "velocity": "speed", "groundspeed": "speed",
    "locationaltitude": "height", "gpsaltitude": "height",
    "altitude": "height", "elevation": "height", "alt": "height",
    "locationbearing": "heading", "bearing": "heading", "course": "heading",
    "gpsheading": "heading", "heading": "heading",
    "utctime": "time", "gpstime": "time", "timestamp": "time",
    "time": "time", "elapsedtime": "time", "sessiontime": "time",
    "accelerationx": "ax_g", "accelx": "ax_g", "longitudinalg": "ax_g",
    "gforcex": "ax_g", "longacc": "ax_g", "axg": "ax_g",
    "accelerationy": "ay_g", "accely": "ay_g", "lateralg": "ay_g",
    "gforcey": "ay_g", "latacc": "ay_g", "ayg": "ay_g",
    "heartrate": "heartrate", "hr": "heartrate",
    "satellites": "sats", "numsatellites": "sats", "sats": "sats",
    "brake": "brake", "brakepos": "brake", "brakepressure": "brake",
    "throttle": "throttle", "throttlepos": "throttle", "tps": "throttle",
    "rpm": "rpm", "enginerpm": "rpm",
    "steering": "steer", "steeringangle": "steer",
    "lapnumber": "lap", "lap": "lap", "laptime": "laptime", "currentlaptime": "laptime",
    "distance": "distance", "lapdistance": "distance",
    "itow": "itow", "gpsitow": "itow", "timeofweek": "itow",
    "sector": "sector", "currentsector": "sector", "sectornumber": "sector",
    # text, not a number: the GARW LapTimer writes PRE on rows before the first timed start
    "laptag": "laptag",
}

_UNIT_IN_NAME = re.compile(r"[\(\[\{]\s*([^)\]\}]+?)\s*[\)\]\}]\s*$")
_TRAILING_UNIT = re.compile(r"[_\s]+(mph|kph|kmh|km/h|m/s|ms|g|deg|ft|m|s)$",
                            re.I)

SPEED_FACTORS = {           # -> m/s
    "m/s": 1.0, "ms": 1.0, "mps": 1.0, "meterspersecond": 1.0,
    "km/h": 1 / 3.6, "kmh": 1 / 3.6, "kph": 1 / 3.6, "kmph": 1 / 3.6,
    "mph": 0.44704, "milesperhour": 0.44704,
    "kn": 0.514444, "knots": 0.514444, "kt": 0.514444,
    "ft/s": 0.3048, "fps": 0.3048,
}
# Both spellings, because the unit string comes from whoever wrote
# the file: a British export says "metres".
DISTANCE_FACTORS = {"m": 1.0, "metres": 1.0, "meters": 1.0,
                    "ft": 0.3048, "feet": 0.3048,
                    "km": 1000.0, "mi": 1609.344, "miles": 1609.344}


class CsvFormatError(ValueError):
    """The file could not be read as a data log."""


# --------------------------------------------------------------------------
# Header discovery
# --------------------------------------------------------------------------


def _clean(name: str) -> str:
    return re.sub(r"[^a-z0-9/]", "", name.strip().lower())


def split_unit(name: str) -> Tuple[str, Optional[str]]:
    """`Speed (mph)` -> ('Speed', 'mph'); `speed_kph` -> ('speed', 'kph')."""
    text = name.strip().strip('"').strip("'")
    m = _UNIT_IN_NAME.search(text)
    if m:
        return text[:m.start()].strip(), m.group(1).strip().lower()
    m = _TRAILING_UNIT.search(text)
    if m:
        return text[:m.start()].strip(), m.group(1).strip().lower()
    return text, None


def _map_name(name: str) -> Optional[str]:
    base, _unit = split_unit(name)
    key = _clean(base)
    if key in CSV_ALIASES:
        return CSV_ALIASES[key]
    canon = canonical_name(base)
    known = {"lat", "lon", "speed_kmh", "time", "heading", "height", "sats",
             "ax_g", "ay_g", "brake", "throttle", "rpm", "steer", "gear",
             "heartrate", "distance", "lap", "laptime", "itow", "sector"}
    return canon if canon in known else None


def _numeric_fraction(fields: Sequence[str]) -> float:
    if not fields:
        return 0.0
    good = 0
    for f in fields:
        try:
            float(f.strip().strip('"'))
            good += 1
        except ValueError:
            pass
    return good / len(fields)


def sniff_delimiter(sample: str) -> str:
    best, score = ",", -1.0
    for d in DELIMITERS:
        counts = [line.count(d) for line in sample.splitlines()[:60]
                  if line.strip()]
        if not counts:
            continue
        common = max(set(counts), key=counts.count)
        if common < 2:
            continue
        consistency = counts.count(common) / len(counts)
        value = common * consistency
        if value > score:
            best, score = d, value
    return best


def find_header(rows: Sequence[Sequence[str]]) -> Tuple[int, Optional[int]]:
    """Index of the header row, and of a units row beneath it if present.

    Chosen by how many columns map to something recognized — a preamble line
    that happens to have commas in it will not name channels, and a data row
    is numeric.
    """
    best_idx, best_hits = None, 0
    for i, row in enumerate(rows[:60]):
        if len(row) < 3 or _numeric_fraction(row) > 0.5:
            continue
        hits = sum(1 for c in row if _map_name(c))
        if hits > best_hits:
            best_idx, best_hits = i, hits
    if best_idx is None or best_hits < 2:
        raise CsvFormatError(
            "No header row naming recognizable channels was found. A log needs "
            "at least latitude and longitude columns; speed and time are used "
            "if present.")

    units_idx = None
    nxt = best_idx + 1
    if nxt < len(rows) and rows[nxt]:
        frac = _numeric_fraction(rows[nxt])
        if frac < 0.34 and any(r.strip() for r in rows[nxt]):
            units_idx = nxt
    return best_idx, units_idx


# --------------------------------------------------------------------------
# Time and unit interpretation
# --------------------------------------------------------------------------


def _parse_iso(values: Sequence[str]) -> Optional[np.ndarray]:
    import datetime as dt
    out = []
    for v in values:
        text = v.strip().strip('"').replace("Z", "+00:00")
        try:
            stamp = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        out.append(stamp.hour * 3600 + stamp.minute * 60 + stamp.second
                   + stamp.microsecond / 1e6)
    return np.array(out, dtype=float)


def interpret_time(raw: Sequence[str], unit: Optional[str]
                   ) -> Tuple[np.ndarray, str]:
    """Seconds, monotonic, plus a note on how the column was read."""
    iso = _parse_iso(raw[:5]) if raw else None
    if iso is not None:
        full = _parse_iso(raw)
        if full is not None:
            return _unwrap(full), "ISO timestamps"

    values = np.array([_to_float(v) for v in raw], dtype=float)
    values = _fill(values)
    if values.size == 0:
        raise CsvFormatError("the time column is empty")

    if unit and unit.lower() in ("ms", "millisecond", "milliseconds"):
        return _unwrap(values / 1000.0), "milliseconds"

    span = float(np.nanmax(values) - np.nanmin(values))
    biggest = float(np.nanmax(np.abs(values)))

    if biggest > 1e11:
        return _unwrap(values / 1000.0 % 86400.0), "Unix epoch in milliseconds"
    if biggest > 1e8:
        return _unwrap(values % 86400.0), "Unix epoch seconds"
    # HHMMSS.ss: minutes and seconds fields must stay under 60
    if biggest > 2400:
        mm = (np.abs(values) % 10000) // 100
        ss = np.abs(values) % 100
        if np.count_nonzero((mm >= 60) | (ss >= 60)) <= max(4, values.size * 0.01):
            hh = np.floor(np.abs(values) / 10000.0)
            return (_unwrap(hh * 3600 + np.minimum(mm, 59) * 60
                            + np.minimum(ss, 59.999)), "HHMMSS.ss")
    if span > 0 and span < 4.0 and values.size > 20:
        # a whole session inside four seconds is minutes mislabelled
        return _unwrap(values * 60.0), "minutes"
    return _unwrap(values), "elapsed seconds"


def _unwrap(t: np.ndarray) -> np.ndarray:
    t = np.asarray(t, dtype=float)
    if t.size < 2:
        return t
    d = np.diff(t)
    roll = np.zeros_like(t)
    roll[1:] = np.cumsum(np.where(d < -43200.0, 86400.0, 0.0))
    return t + roll


def _to_float(v) -> float:
    try:
        return float(str(v).strip().strip('"').replace(",", "."))
    except (TypeError, ValueError):
        return np.nan


def _fill(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=float)
    good = np.isfinite(a)
    if not good.any():
        return np.zeros_like(a)
    idx = np.arange(a.size)
    return np.interp(idx, idx[good], a[good])


def speed_to_ms(values: np.ndarray, unit: Optional[str]
                ) -> Tuple[np.ndarray, str]:
    """Convert a speed column to m/s, stating how the unit was decided."""
    if unit:
        key = _clean(unit)
        if key in SPEED_FACTORS:
            return values * SPEED_FACTORS[key], f"speed read as {unit}"
    peak = float(np.nanpercentile(np.abs(values), 99)) if values.size else 0.0
    # A track session peaks somewhere around 40 m/s, 145 km/h or 90 mph, so
    # the magnitude separates them well enough to guess — but say so.
    if peak <= 75:
        return values, "speed unit not stated; assumed m/s from its magnitude"
    if peak <= 260:
        return (values / 3.6,
                "speed unit not stated; assumed km/h from its magnitude")
    return (values * 0.44704,
            "speed unit not stated; assumed mph from its magnitude")


def coords_to_degrees(values: np.ndarray, limit: float
                      ) -> Tuple[np.ndarray, Optional[str]]:
    """Degrees, or arc-minutes converted, decided by range."""
    peak = float(np.nanmax(np.abs(values))) if values.size else 0.0
    if peak <= limit:
        return values, None
    if peak <= limit * 60.0:
        return values / 60.0, "coordinates read as arc-minutes"
    raise CsvFormatError(
        f"coordinate values reach {peak:.1f}, which is neither degrees nor "
        "arc-minutes")


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def parse_csv(path: str, delimiter: Optional[str] = None) -> VboFile:
    """Read a CSV data log into the same structure as a .vbo."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read()
    if not text.strip():
        raise CsvFormatError("the file is empty")

    # A line starting with '#' is a comment wherever it appears — the GARW LapTimer writes a version line
    # near the top, '# link …' lines about once a minute and '# dropped malformed row #N' markers. They go
    # before any CSV parsing: a quote inside a comment must not be able to swallow the rows after it, and
    # their commas mustn't sway the delimiter guess.
    lines = text.splitlines()
    n_comments = sum(1 for ln in lines if ln.lstrip().startswith("#"))
    if n_comments:
        text = "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))
    delim = delimiter or sniff_delimiter(text)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim)]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 3:
        raise CsvFormatError("too few rows to be a data log")

    head_idx, units_idx = find_header(rows)
    header = rows[head_idx]
    unit_row = rows[units_idx] if units_idx is not None else None
    data_start = (units_idx if units_idx is not None else head_idx) + 1

    # A logger that loses power or flushes the wrong buffer mid-write leaves rows of binary junk: control
    # bytes, undecodable bytes, a field count that doesn't match the header. Their numbers are garbage
    # (positions thousands of km away), so drop them rather than let one row wreck a lap.
    def corrupt(r: List[str]) -> bool:
        if abs(len(r) - len(header)) > 1:
            return True
        # \x1e (record separator) ends every row of a GARW LapTimer log; tab and CR are ordinary too
        return any(ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\r\x1e") for c in r for ch in c)
    data = rows[data_start:]
    clean = [r for r in data if not corrupt(r)]
    dropped = len(data) - len(clean)
    rows = rows[:data_start] + clean

    columns: Dict[str, List[str]] = {}
    units: Dict[str, Optional[str]] = {}
    for i, name in enumerate(header):
        canon = _map_name(name)
        if canon is None or canon in columns:
            continue
        _base, inline_unit = split_unit(name)
        unit = inline_unit
        if unit is None and unit_row is not None and i < len(unit_row):
            candidate = unit_row[i].strip().strip('"')
            if candidate and not candidate.replace(".", "").isdigit():
                unit = candidate.lower()
        columns[canon] = [r[i] if i < len(r) else "" for r in rows[data_start:]]
        units[canon] = unit

    if "lat_deg" not in columns or "lon_deg" not in columns:
        raise CsvFormatError(
            "no latitude and longitude columns were recognized; found "
            + (", ".join(sorted(columns)) or "nothing usable"))

    notes: List[str] = [f"Read from CSV (delimiter {delim!r}, "
                        f"header on line {head_idx + 1})"]
    if dropped:
        notes.append(f"{dropped} corrupted row{'s' if dropped != 1 else ''} skipped (binary junk in the file)")
    if n_comments:
        notes.append(f"{n_comments} comment line{'s' if n_comments != 1 else ''} (#) skipped")
    vbo = VboFile(path=path, source_format="csv")
    vbo.raw_columns = [h.strip() for h in header]

    lat, note = coords_to_degrees(_fill(np.array(
        [_to_float(v) for v in columns["lat_deg"]])), 90.0)
    if note:
        notes.append(note)
    lon, _ = coords_to_degrees(_fill(np.array(
        [_to_float(v) for v in columns["lon_deg"]])), 180.0)
    keep = np.isfinite(lat) & np.isfinite(lon) & ((lat != 0) | (lon != 0))
    if not keep.any():
        raise CsvFormatError("every row is missing a position")

    vbo.channels["lat_deg"] = lat[keep]
    vbo.channels["lon_deg"] = lon[keep]
    n = int(np.count_nonzero(keep))

    if "time" in columns:
        t, how = interpret_time(columns["time"], units.get("time"))
        vbo.channels["t"] = t[keep] if t.size == keep.size else t[:n]
        notes.append(f"time read as {how}")
    else:
        vbo.channels["t"] = np.arange(n, dtype=float)
        notes.append("no time column; assumed 1 Hz")

    if "speed" in columns:
        raw = _fill(np.array([_to_float(v) for v in columns["speed"]]))[keep]
        speed, how = speed_to_ms(raw, units.get("speed"))
        notes.append(how)
    else:
        speed = _speed_from_positions(vbo.channels["t"],
                                      vbo.channels["lat_deg"],
                                      vbo.channels["lon_deg"])
        notes.append("no speed column; derived from position and time")
    vbo.channels["speed"] = np.clip(np.nan_to_num(speed), 0.0, None)
    vbo.channels["speed_kmh"] = vbo.channels["speed"] * 3.6

    for canon in ("heading", "height", "ax_g", "ay_g", "brake", "throttle",
                  "rpm", "steer", "gear", "heartrate", "sats", "distance", "lap", "laptime", "itow",
                  "sector"):
        if canon not in columns:
            continue
        values = _fill(np.array([_to_float(v) for v in columns[canon]]))[keep]
        if canon == "height" and units.get(canon):
            factor = DISTANCE_FACTORS.get(_clean(units[canon] or ""))
            if factor:
                values = values * factor
        vbo.channels[canon] = values

    if "laptag" in columns:
        # 1 where the dash had not started timing yet (PRE): those rows are the drive to the line
        tags = np.array([v.strip().strip("\x1e").upper() for v in columns["laptag"]], dtype=object)
        pre = (tags == "PRE").astype(float)[keep]
        if pre.any():
            vbo.channels["pre"] = pre
            notes.append(f"{int(pre.sum())} pre-start row(s) (Lap Tag PRE) not timed")
        res = (tags == "RES").astype(float)[keep]
        if res.any():
            vbo.channels["res"] = res               # rows of a lap the dash carried on across a restart

    dtv = np.diff(vbo.channels["t"])
    dtv = dtv[(dtv > 1e-6) & (dtv < 30.0)]
    vbo.sample_rate = float(1.0 / np.median(dtv)) if dtv.size else 1.0

    summary = find_summary(path)
    if summary:
        try:
            vbo.lap_summary = read_summary(summary)
            vbo.summary_path = summary
            notes.append(f"lap summary read from {os.path.basename(summary)} "
                         f"({len(vbo.lap_summary)} lap row{'s' if len(vbo.lap_summary) != 1 else ''})")
        except (OSError, ValueError) as exc:
            notes.append(f"lap summary {os.path.basename(summary)} couldn't be read ({exc})")

    if not vbo.splits and "sector" in vbo.channels:
        vbo.splits = split_lines_from_sector_column(vbo)
        if vbo.splits:
            vbo.split_source = "sector"
            notes.append(f"{len(vbo.splits)} split line(s) from the log's Sector column")

    vbo.comments = notes + [f"Columns: {', '.join(vbo.raw_columns)}"]
    return vbo


def _speed_from_positions(t: np.ndarray, lat: np.ndarray,
                          lon: np.ndarray) -> np.ndarray:
    """Ground speed differentiated from position, when nothing else is given.

    Noticeably noisier than a logged Doppler speed, which is why it is a last
    resort rather than a shortcut.
    """
    if t.size < 2:
        return np.zeros_like(t)
    coslat = math.cos(math.radians(float(np.nanmean(lat))))
    x = np.radians(lon) * 6378137.0 * coslat
    y = np.radians(lat) * 6378137.0
    # Central differences, with one-sided ends. A backward difference would
    # leave the first sample at zero, which is a standing start the car never
    # made and the largest single error in the trace.
    dx = np.gradient(x)
    dy = np.gradient(y)
    dtv = np.gradient(t)
    dtv[dtv <= 0] = np.nan
    return _fill(np.hypot(dx, dy) / dtv)


def split_lines_from_sector_column(vbo: VboFile, half_width_m: float = 20.0,
                                   max_spread_m: float = 30.0
                                   ) -> List[Tuple[float, float, float, float]]:
    """Split gates from where the logger's Sector counter steps up (the GARW LapTimer writes it).

    The counter going from k to k+1 is the car crossing the dash's k-th split. Each boundary's position is
    the median over every lap that crossed it — a boundary whose crossings scatter is left out rather than
    guessed — and the gate is laid square to the direction of travel there. Pre-start rows and the step
    back to sector 1 at the start/finish don't count."""
    from . import geometry as geo
    sec = np.asarray(vbo.channels.get("sector"), dtype=float)
    lat, lon = vbo.channels["lat_deg"], vbo.channels["lon_deg"]
    n = min(len(sec), len(lat))
    if n < 3:
        return []
    pre = vbo.channels.get("pre")
    speed = vbo.channels.get("speed")
    x, y, lat0, lon0 = geo.project_local(lat[:n], lon[:n])
    t = np.asarray(vbo.channels["t"], dtype=float)
    thr = geo.gap_threshold(t)
    found: Dict[int, List[int]] = {}
    for i in range(1, n):
        if t[i] - t[i - 1] > thr:
            continue                        # across a recording gap: the change happened somewhere unseen
        a, b = sec[i - 1], sec[i]
        if not (np.isfinite(a) and np.isfinite(b)) or a < 1 or b != a + 1:
            continue
        if pre is not None and (pre[i] > 0.5 or pre[i - 1] > 0.5):
            continue
        if speed is not None and speed[i] < 1.0:
            continue
        found.setdefault(int(a), []).append(i)
    gates = []
    for k in sorted(found):
        idx = found[k]
        px = np.array([(x[i - 1] + x[i]) / 2.0 for i in idx])
        py = np.array([(y[i - 1] + y[i]) / 2.0 for i in idx])
        cx, cy = float(np.median(px)), float(np.median(py))
        near = np.hypot(px - cx, py - cy) <= max_spread_m
        if near.sum() * 2 <= len(idx):
            continue                        # no consistent place: better no split than a wrong one
        use = [i for i, ok in zip(idx, near) if ok]
        cx, cy = float(np.mean(px[near])), float(np.mean(py[near]))
        hx = float(np.mean([x[min(i + 2, n - 1)] - x[max(i - 3, 0)] for i in use]))
        hy = float(np.mean([y[min(i + 2, n - 1)] - y[max(i - 3, 0)] for i in use]))
        h = math.hypot(hx, hy)
        if h < 1e-6:
            continue
        ux, uy = -hy / h, hx / h            # across the road
        la1, lo1 = geo.unproject_local(cx - ux * half_width_m, cy - uy * half_width_m, lat0, lon0)
        la2, lo2 = geo.unproject_local(cx + ux * half_width_m, cy + uy * half_width_m, lat0, lon0)
        gates.append((float(la1), float(lo1), float(la2), float(lo2)))
    return gates


# --------------------------------------------------------------------------
# GARW LapTimer session summary (<session>_summary.csv): one row per lap
# --------------------------------------------------------------------------


def find_summary(path: str) -> Optional[str]:
    """The `…_summary.csv` written beside a GARW LapTimer session log, or None.

    Tried as `<log stem>_summary.csv`, then without a `laptimer_` prefix the download may have added (in
    either direction), then any `*_summary.csv` in the folder whose name contains the log's stem."""
    folder, name = os.path.split(os.path.abspath(path))
    stem, _ext = os.path.splitext(name)
    if stem.lower().endswith("_summary"):
        return None                                   # this IS a summary
    cands = [stem]
    if stem.lower().startswith("laptimer_"):
        cands.append(stem[len("laptimer_"):])
    else:
        cands.append("laptimer_" + stem)
    for c in cands:
        for suffix in ("_summary.csv", "_summary.CSV"):
            p = os.path.join(folder, c + suffix)
            if os.path.isfile(p):
                return p
    core = cands[-1] if stem.lower().startswith("laptimer_") else stem
    try:
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith("_summary.csv") and core.lower() in f.lower():
                return os.path.join(folder, f)
    except OSError:
        pass
    return None


_SECTOR_COL = re.compile(r"^(?:s|sec|sector|split)(\d+)(?:time|ms|s)?$")


def parse_duration(text: str, unit_ms: bool = False) -> Optional[float]:
    """'1:23.456', '1:02:03.4', '83.456' (seconds) or, with unit_ms, '83456' -> seconds; blank -> None."""
    v = (text or "").strip().strip('"')
    if not v or v in ("-", "--", "nan", "NaN"):
        return None
    try:
        if ":" in v:
            parts = [float(p) for p in v.split(":")]
            sec = 0.0
            for p in parts:
                sec = sec * 60.0 + p
            return sec
        f = float(v)
    except ValueError:
        return None
    return f / 1000.0 if unit_ms else f


def read_summary(path: str) -> Dict[int, dict]:
    """{dash lap number: {"time", "tag", "pit", "valid", "best", "sectors"}} from a lap summary.

    Column names are matched loosely (lap / lap #, time / lap time / lap time (ms), tag, pit, valid, best,
    S1… / Sector 1…). Times may be m:ss.sss, seconds or milliseconds. An empty sector field (a sector
    missed while the unit was off) reads as None."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if len(lines) < 2:
        return {}
    delim = sniff_delimiter("\n".join(lines[:20]))
    rows = list(csv.reader(lines, delimiter=delim))
    header = [h.strip() for h in rows[0]]

    def key(h: str) -> str:
        return re.sub(r"[^a-z0-9]", "", h.lower())
    keys = [key(h) for h in header]
    raw = [h.lower() for h in header]

    def col(*names) -> Optional[int]:
        for n in names:
            if n in keys:
                return keys.index(n)
        return None
    c_lap = col("lap", "lapnumber", "lapno", "lapnum", "number", "n")
    # milliseconds first when the file has them (the GARW summary writes time_ms beside a formatted time)
    c_time = col("timems", "laptimems", "laptime", "time", "laptimes", "laptimesec", "laptimeseconds")
    if c_lap is None or c_time is None:
        raise ValueError("no lap / time columns")
    time_ms = "ms" in raw[c_time].replace("(", " ").replace(")", " ").split() or keys[c_time].endswith("ms")
    c_tag, c_pit, c_valid, c_best = col("tag", "laptag"), col("pit"), col("valid"), col("best")
    secs = []
    for i, k in enumerate(keys):
        m = _SECTOR_COL.match(k)
        if m and i not in (c_lap, c_time):
            secs.append((int(m.group(1)), i, "ms" in raw[i]))
    secs.sort()

    def flag(r, c) -> Optional[bool]:
        if c is None or c >= len(r) or not r[c].strip():
            return None
        return r[c].strip().lower() in ("1", "true", "yes", "y")

    out: Dict[int, dict] = {}
    for r in rows[1:]:
        if c_lap >= len(r):
            continue
        try:
            n = int(float(r[c_lap]))
        except ValueError:
            continue
        out[n] = {
            "time": parse_duration(r[c_time] if c_time < len(r) else "", time_ms),
            "tag": (r[c_tag].strip().upper() if c_tag is not None and c_tag < len(r) else ""),
            "pit": flag(r, c_pit), "valid": flag(r, c_valid), "best": flag(r, c_best),
            "sectors": [parse_duration(r[i], ms) if i < len(r) else None for _n, i, ms in secs],
        }
    return out
