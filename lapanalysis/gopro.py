"""
GoPro GPS telemetry (GPMF) — exact video ↔ data-log sync.

A GoPro records its GPS into a metadata track ('gpmd') of every clip — read
straight from the MP4. The low-resolution proxy it writes alongside (.LRV)
carries the same track on the same timeline, and is used as a fallback when
the MP4 has been trimmed or re-encoded and lost its metadata. Each GPS fix therefore has two clocks attached: its position in
the video, and UTC from the satellites. A VBOX or Garmin log is stamped in UTC
too, so the two line up directly — no landmark-matching by eye.

The UTC alignment is then refined by cross-correlating GoPro GPS speed with the
log's speed, which removes the GoPro's GPS-timestamp latency and also syncs
logs whose clock is not UTC (a CSV with relative time).

Pure Python + numpy: an MP4 box walker, a GPMF KLV reader for the GPS5 (HERO5–10,
UTC per ~1 s payload) and GPS9 (HERO11+, UTC per sample) streams, and the sync.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

# --------------------------------------------------------------------------
# MP4: locate the GPMF track and its samples
# --------------------------------------------------------------------------

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta"}


def _boxes(f, start: int, end: int):
    """Yield (type, payload_start, payload_end) for the boxes in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        hlen = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen:
            return
        yield typ, pos + hlen, pos + size
        pos += size


def _find(f, start: int, end: int, path: List[bytes]):
    for typ, a, b in _boxes(f, start, end):
        if typ == path[0]:
            if len(path) == 1:
                return a, b
            return _find(f, a, b, path[1:])
    return None


@dataclass
class _Track:
    timescale: int
    starts: np.ndarray          # sample start, seconds from video start
    durations: np.ndarray       # seconds
    offsets: List[int]
    sizes: List[int]


def _gpmd_track(f, file_size: int) -> Optional[_Track]:
    moov = _find(f, 0, file_size, [b"moov"])
    if not moov:
        return None
    for typ, ta, tb in _boxes(f, *moov):
        if typ != b"trak":
            continue
        mdia = _find(f, ta, tb, [b"mdia"])
        if not mdia:
            continue
        stbl = _find(f, *mdia, [b"minf", b"stbl"])
        stsd = stbl and _find(f, *stbl, [b"stsd"])
        if not stsd:
            continue
        f.seek(stsd[0] + 8)                      # version/flags + entry count
        if f.read(8)[4:8] != b"gpmd":            # first entry: size(4) + format(4)
            continue
        mdhd = _find(f, *mdia, [b"mdhd"])
        f.seek(mdhd[0])
        ver = f.read(1)[0]
        f.seek(mdhd[0] + (20 if ver == 1 else 12))
        timescale = struct.unpack(">I", f.read(4))[0] or 1

        def table(name):
            r = _find(f, *stbl, [name])
            if not r:
                return None
            f.seek(r[0])
            return f.read(r[1] - r[0])

        stts, stsz, stsc = table(b"stts"), table(b"stsz"), table(b"stsc")
        co, wide = table(b"stco"), False
        if co is None:
            co, wide = table(b"co64"), True
        if not (stts and stsz and stsc and co):
            continue
        # sample durations
        n = struct.unpack(">I", stts[4:8])[0]
        durs: List[int] = []
        for i in range(n):
            cnt, d = struct.unpack(">II", stts[8 + 8 * i:16 + 8 * i])
            durs += [d] * cnt
        # sample sizes
        fixed, count = struct.unpack(">II", stsz[4:12])
        sizes = [fixed] * count if fixed else list(struct.unpack(f">{count}I", stsz[12:12 + 4 * count]))
        # chunk offsets
        nco = struct.unpack(">I", co[4:8])[0]
        chunk_off = list(struct.unpack(f">{nco}{'Q' if wide else 'I'}", co[8:8 + (8 if wide else 4) * nco]))
        # sample → chunk
        nsc = struct.unpack(">I", stsc[4:8])[0]
        runs = [struct.unpack(">III", stsc[8 + 12 * i:20 + 12 * i]) for i in range(nsc)]
        offsets: List[int] = []
        s = 0
        for ci in range(nco):
            per = next(spc for first, spc, _ in reversed(runs) if first <= ci + 1)
            o = chunk_off[ci]
            for _ in range(per):
                if s >= count:
                    break
                offsets.append(o)
                o += sizes[s]
                s += 1
        m = min(len(offsets), len(sizes), len(durs))
        d = np.asarray(durs[:m], dtype=float) / timescale
        starts = np.concatenate([[0.0], np.cumsum(d)[:-1]]) if m else np.zeros(0)
        return _Track(timescale, starts, d, offsets[:m], sizes[:m])
    return None


# --------------------------------------------------------------------------
# GPMF: GPS samples out of each payload
# --------------------------------------------------------------------------

_TYPES = {"b": "b", "B": "B", "s": "h", "S": "H", "l": "i", "L": "I", "f": "f", "d": "d", "j": "q", "J": "Q"}


def _klv(buf: bytes, start: int = 0, end: Optional[int] = None):
    """Yield (key, type, struct_size, repeat, data) for each KLV in buf[start:end]."""
    end = len(buf) if end is None else end
    pos = start
    while pos + 8 <= end:
        key = buf[pos:pos + 4]
        typ, ssize, rep = buf[pos + 4], buf[pos + 5], struct.unpack(">H", buf[pos + 6:pos + 8])[0]
        n = ssize * rep
        data = buf[pos + 8:pos + 8 + n]
        yield key, typ, ssize, rep, data
        pos += 8 + ((n + 3) & ~3)


def _values(typ: str, ssize: int, rep: int, data: bytes) -> List[Tuple]:
    """Decode a uniform-type KLV payload into `rep` tuples."""
    fmt = _TYPES.get(typ)
    if not fmt:
        return []
    k = ssize // struct.calcsize(fmt)
    out = []
    for i in range(rep):
        out.append(struct.unpack(f">{k}{fmt}", data[i * ssize:(i + 1) * ssize]))
    return out


def _parse_gpsu(data: bytes) -> Optional[float]:
    """'yymmddhhmmss.sss' -> POSIX seconds (UTC)."""
    try:
        s = data[:16].decode("ascii")
        dt = datetime.strptime(s[:12], "%y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return dt.timestamp() + float("0" + s[12:16])
    except (ValueError, UnicodeDecodeError):
        return None


_GPS_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()


def _payload_gps(buf: bytes):
    """[(lat, lon, speed2d, utc_posix_or_None, fix)] for one payload, plus the payload's GPSU (or None)."""
    samples: List[Tuple[float, float, float, Optional[float], int]] = []
    payload_utc: Optional[float] = None

    def walk(start: int, end: int):
        nonlocal payload_utc
        for key, typ, ssize, rep, data in _klv(buf, start, end):
            if typ == 0:                                   # nested: DEVC / STRM
                if key == b"STRM":
                    strm(data)
                else:
                    walk_bytes(data)

    def walk_bytes(data: bytes):
        sub = data
        for key, typ, ssize, rep, d in _klv(sub):
            if typ == 0 and key == b"STRM":
                strm(d)
            elif typ == 0:
                walk_bytes(d)

    def strm(data: bytes):
        nonlocal payload_utc
        scal: List[float] = [1.0]
        tdef = ""
        fix = 3
        utc = None
        for key, typ, ssize, rep, d in _klv(data):
            t = chr(typ) if typ else ""
            if key == b"SCAL":
                v = _values(t, ssize, rep, d)
                scal = [float(x[0]) for x in v] if v else [1.0]
            elif key == b"TYPE":
                tdef = d.decode("ascii", "replace").rstrip("\x00")
            elif key == b"GPSF":
                v = _values(t, ssize, rep, d)
                fix = int(v[0][0]) if v else fix
            elif key == b"GPSU":
                utc = _parse_gpsu(d)
                payload_utc = utc
            elif key == b"GPS5":
                for row in _values(t, ssize, rep, d):
                    sc = scal if len(scal) >= 5 else [scal[0]] * 5
                    lat, lon = row[0] / sc[0], row[1] / sc[1]
                    spd = row[3] / sc[3]
                    samples.append((lat, lon, spd, None, fix))
            elif key == b"GPS9" and tdef:
                fmts = [_TYPES.get(c, "i") for c in tdef]
                for i in range(rep):
                    row = struct.unpack(">" + "".join(fmts), d[i * ssize:(i + 1) * ssize])
                    sc = scal if len(scal) >= len(row) else [scal[0]] * len(row)
                    v = [row[j] / sc[j] for j in range(len(row))]
                    lat, lon, spd = v[0], v[1], v[3]
                    days, secs = row[5] / sc[5], row[6] / sc[6]
                    f9 = int(row[8] / sc[8]) if len(row) > 8 else fix
                    samples.append((lat, lon, spd, _GPS_EPOCH + days * 86400.0 + secs, f9))

    walk_bytes(buf)
    return samples, payload_utc


@dataclass
class GoProGPS:
    source: str                  # file the telemetry came from
    video_t: np.ndarray          # seconds from the start of the clip
    utc: np.ndarray              # POSIX seconds, NaN where unknown
    lat: np.ndarray
    lon: np.ndarray
    speed: np.ndarray            # m/s (2D ground speed)
    per_sample_utc: bool         # GPS9 (exact per fix) vs GPS5 (per payload)

    @property
    def utc_seconds_of_day(self) -> np.ndarray:
        """UTC as seconds since that day's midnight, the clock a .vbo / .fit is stamped with."""
        day0 = np.floor(np.nanmin(self.utc) / 86400.0) * 86400.0
        return self.utc - day0


def read_gopro_gps(path: str) -> Optional[GoProGPS]:
    """GPS fixes from a GoPro .MP4 or .LRV, or None if the file has no GPS telemetry."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            trk = _gpmd_track(f, size)
            if trk is None or not trk.offsets:
                return None
            vt, utc, lat, lon, spd = [], [], [], [], []
            per_sample = False
            for start, dur, off, sz in zip(trk.starts, trk.durations, trk.offsets, trk.sizes):
                f.seek(off)
                samples, payload_utc = _payload_gps(f.read(sz))
                good = [s for s in samples if s[4] >= 2 and (s[0] or s[1])]   # 2D/3D fix only
                n = len(samples)
                for i, s in enumerate(samples):
                    if s not in good:
                        continue
                    frac = i / n if n else 0.0
                    vt.append(start + frac * dur)
                    if s[3] is not None:
                        utc.append(s[3])
                        per_sample = True
                    elif payload_utc is not None:
                        utc.append(payload_utc + frac * dur)
                    else:
                        utc.append(np.nan)
                    lat.append(s[0])
                    lon.append(s[1])
                    spd.append(s[2])
    except (OSError, struct.error, IndexError, StopIteration, ValueError):
        return None
    if len(vt) < 10:
        return None
    return GoProGPS(path, np.asarray(vt), np.asarray(utc, dtype=float), np.asarray(lat), np.asarray(lon),
                    np.asarray(spd), per_sample)


def find_telemetry(video_path: str) -> List[str]:
    """Files that may carry this clip's GPS, best first: the clip itself (a GoPro MP4 has the 'gpmd' track),
    then — for a clip whose telemetry was stripped by trimming or re-encoding — an .LRV with the same name,
    or GoPro's own proxy name (GX010123.MP4 -> GL010123.LRV)."""
    folder, name = os.path.split(os.path.abspath(video_path))
    stem = os.path.splitext(name)[0]
    stems = [stem]
    if len(stem) >= 2 and stem[:2].upper() in ("GX", "GH", "GP"):
        stems.append("GL" + stem[2:])
    out: List[str] = [os.path.abspath(video_path)]
    try:
        listing = {f.lower(): f for f in os.listdir(folder)}
    except OSError:
        listing = {}
    for s in stems:
        hit = listing.get((s + ".lrv").lower())
        if hit:
            out.append(os.path.join(folder, hit))
    return out


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

@dataclass
class GpsSyncResult:
    offset_s: float              # session time at which the clip's first frame was recorded
    method: str                  # 'utc', 'utc+speed', 'speed'
    correlation: Optional[float]
    source: str

    def describe(self) -> str:
        how = {"utc": "GPS clock", "utc+speed": "GPS clock + speed match", "speed": "speed match"}[self.method]
        q = f", match {self.correlation:.2f}" if self.correlation is not None else ""
        return f"GoPro GPS sync from {os.path.basename(self.source)} ({how}{q})"


def _xcorr_best(log_t: np.ndarray, log_v: np.ndarray, g_t: np.ndarray, g_v: np.ndarray,
                lags: np.ndarray, hz: float = 10.0) -> Tuple[float, float]:
    """Lag (s) to add to g_t that best matches g_v onto log_v, and its correlation."""
    best = (0.0, -1.0)
    for lag in lags:
        lo = max(log_t[0], g_t[0] + lag)
        hi = min(log_t[-1], g_t[-1] + lag)
        if hi - lo < 20.0:
            continue
        grid = np.arange(lo, hi, 1.0 / hz)
        a = np.interp(grid, log_t, log_v)
        b = np.interp(grid - lag, g_t, g_v)
        if a.std() < 0.5 or b.std() < 0.5:
            continue
        r = float(np.corrcoef(a, b)[0, 1])
        if r > best[1]:
            best = (float(lag), r)
    return best


def gps_sync(gps: GoProGPS, log_t: np.ndarray, log_speed: np.ndarray) -> Optional[GpsSyncResult]:
    """Session time of the clip's first frame. log_t: session seconds (UTC seconds-of-day for .vbo/.fit),
    log_speed: m/s."""
    log_t = np.asarray(log_t, dtype=float)
    log_v = np.asarray(log_speed, dtype=float)
    ok = np.isfinite(log_t) & np.isfinite(log_v)
    log_t, log_v = log_t[ok], log_v[ok]
    if log_t.size < 20:
        return None
    order = np.argsort(log_t)
    log_t, log_v = log_t[order], log_v[order]
    g_t, g_v = gps.video_t, gps.speed

    # 1. UTC: the clip's frame 0 in the log's clock, if the clocks overlap at all
    offset0 = None
    have_utc = np.isfinite(gps.utc)
    if np.count_nonzero(have_utc) >= 10:
        sod = gps.utc_seconds_of_day
        cand = float(np.median(sod[have_utc] - g_t[have_utc]))
        span = (cand + g_t[0], cand + g_t[-1])
        if span[1] > log_t[0] and span[0] < log_t[-1]:
            offset0 = cand

    # 2. speed correlation: refine the UTC estimate (GPS timestamp latency), or search everything without it
    if offset0 is not None:
        coarse = _xcorr_best(log_t, log_v, g_t, g_v, offset0 + np.arange(-3.0, 3.0001, 0.1))
        if coarse[1] >= 0.9:
            fine = _xcorr_best(log_t, log_v, g_t, g_v, coarse[0] + np.arange(-0.1, 0.1001, 0.01), hz=50.0)
            return GpsSyncResult(fine[0], "utc+speed", round(fine[1], 3), gps.source)
        return GpsSyncResult(offset0, "utc", None, gps.source)
    lo = log_t[0] - g_t[-1] + 20.0
    hi = log_t[-1] - g_t[0] - 20.0
    if hi <= lo:
        return None
    coarse = _xcorr_best(log_t, log_v, g_t, g_v, np.arange(lo, hi, 0.5), hz=5.0)
    if coarse[1] < 0.9:
        return None
    mid = _xcorr_best(log_t, log_v, g_t, g_v, coarse[0] + np.arange(-0.6, 0.6001, 0.05))
    fine = _xcorr_best(log_t, log_v, g_t, g_v, mid[0] + np.arange(-0.05, 0.05001, 0.01), hz=50.0)
    return GpsSyncResult(fine[0], "speed", round(fine[1], 3), gps.source)


def sync_video(video_path: str, log_t: np.ndarray, log_speed: np.ndarray) -> Optional[GpsSyncResult]:
    """Try each telemetry source for this clip (the MP4 itself first, then an .LRV) and return the first sync found."""
    for p in find_telemetry(video_path):
        gps = read_gopro_gps(p)
        if gps is None:
            continue
        r = gps_sync(gps, log_t, log_speed)
        if r is not None:
            return r
    return None
