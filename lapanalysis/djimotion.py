"""
Syncing a DJI Osmo Action clip to a log from the camera's own motion.

DJI action cameras have no GPS, so there is no clock to match against the log. They do record the camera's
orientation, on the video's own clock, and a camera fixed to a car turns exactly when the car turns. The
camera's turning rate, lined up against the log's (from its GPS heading), gives the offset: every corner is a
sync mark, and nothing has to be done while driving.

Where the orientation is (both are protobuf, one message per video frame, read straight from the MP4):

* ``dbgi`` ("CAM dbgi" / "DJI dbgi") — DJI's debug track. A pair of unit quaternions per frame (Action 4/5:
  every frame; Action 6: every other frame). Undocumented, but present whatever the settings, including
  RockSteady on and 16:9 — what most people record.
* ``djmd`` ("DJI meta") — the documented metadata track. Its ``imu_attitude_after_fusion`` holds ~16
  quaternions per frame (1 kHz), but only with RockSteady off, FOV Wide and 4:3 (what Gyroflow needs).

Either one is enough: syncing works at a few tens of Hz. The quaternion's reference frame differs between
models, so nothing here relies on it — rotation rates are taken in the camera's own frame and the car's
turning axis is found from the clip itself (it is where most of the rotation is during a drive).
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .gopro import _find, _sample_track

#: quaternion samples per second to read; more adds reading time without helping the sync
READ_HZ = 20.0
#: the grid both turning-rate signals are compared on
SYNC_HZ = 20.0


@dataclass
class DjiMotion:
    t: np.ndarray            # seconds from the first video frame
    q: np.ndarray            # (n, 4) unit quaternions w, x, y, z
    source: str              # 'djmd' (documented attitude) or 'dbgi' (debug track)
    model: str = ""
    stabilised: Optional[bool] = None


# --------------------------------------------------------------------------
# protobuf, just enough to walk to a field
# --------------------------------------------------------------------------

def _varint(b: bytes, i: int) -> Tuple[int, int]:
    r = sh = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << sh
        sh += 7
        if c < 0x80:
            return r, i


def _fields(b: bytes):
    """(field, wire type, value) for each field of a protobuf message; stops quietly at anything odd."""
    i, n = 0, len(b)
    try:
        while i < n:
            k, i = _varint(b, i)
            f, w = k >> 3, k & 7
            if f == 0:
                return
            if w == 0:
                v, i = _varint(b, i)
            elif w == 1:
                v, i = b[i:i + 8], i + 8
            elif w == 5:
                v, i = b[i:i + 4], i + 4
            elif w == 2:
                ln, i = _varint(b, i)
                v, i = b[i:i + ln], i + ln
            else:
                return
            if i > n:
                return
            yield f, w, v
    except IndexError:
        return


def _first(b: bytes, *path: int) -> Optional[bytes]:
    for f in path:
        b = next((v for ff, w, v in _fields(b) if ff == f and w == 2), None)
        if b is None:
            return None
    return b


def _unit(q) -> Optional[np.ndarray]:
    q = np.asarray(q, dtype=float)
    n = float(np.linalg.norm(q))
    return q / n if 0.98 < n < 1.02 else None


def _quat_dbgi(pkt: bytes) -> Optional[np.ndarray]:
    blk = _first(pkt, 2, 1, 11, 7)
    if blk is None:
        return None
    raw = next((v for f, w, v in _fields(blk) if f == 3 and w == 2 and len(v) == 16), None)
    return _unit(struct.unpack("<4f", raw)) if raw else None


def _quat_djmd(pkt: bytes) -> Optional[np.ndarray]:
    att = _first(pkt, 3, 3, 2)                   # frame meta → imu_frame_meta → imu_attitude_after_fusion
    if att is None:
        return None
    q = next((v for f, w, v in _fields(att) if f == 3 and w == 2), None)
    if q is None:
        return None
    vals = {f: struct.unpack("<f", v)[0] for f, w, v in _fields(q) if w == 5}
    if not all(k in vals for k in (1, 2, 3, 4)):
        return None
    return _unit([vals[1], vals[2], vals[3], vals[4]])


def _is_dji(f, size: int) -> Tuple[bool, str]:
    """(DJI clip?, model) from the encoder tag DJI writes ('DJI OsmoAction4')."""
    f.seek(0)
    head = f.read(min(size, 64 * 1024))
    moov = _find(f, 0, size, [b"moov"])
    tail = b""
    if moov:
        f.seek(moov[0])
        tail = f.read(min(moov[1] - moov[0], 256 * 1024))
    blob = head + tail
    i, found = blob.find(b"DJI "), False
    while i >= 0:
        found = True
        j = i + 4
        while j < len(blob) and 32 < blob[j] < 127:
            j += 1
        name = blob[i + 4:j].decode("ascii", "replace")
        if name not in ("meta", "dbgi"):          # the Action 4's track handlers are also named "DJI …"
            return True, name
        i = blob.find(b"DJI ", j)
    return found, ""


def _read_track(f, size: int, fmt: bytes, parse) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    trk = _sample_track(f, size, fmt)
    if trk is None or not len(trk.sizes):
        return None
    n = len(trk.sizes)
    dur = float(trk.starts[-1] + trk.durations[-1]) if n else 0.0
    step = max(1, int(round(n / max(dur, 1e-3) / READ_HZ)))
    ts, qs = [], []
    i = 0
    while i < n:
        # Action 6 only has the quaternion in every other debug packet: try the next one or two
        for k in range(i, min(i + 3, n)):
            f.seek(trk.offsets[k])
            q = parse(f.read(trk.sizes[k]))
            if q is not None:
                ts.append(float(trk.starts[k]))
                qs.append(q)
                break
        i += step
    if len(ts) < 20:
        return None
    return np.asarray(ts), np.asarray(qs)


def read_dji_motion(path: str) -> Optional[DjiMotion]:
    """The camera's orientation over the clip, or None (not a DJI clip, or no orientation in it)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            dji, model = _is_dji(f, size)
            if not dji:
                return None
            for fmt, parse in ((b"djmd", _quat_djmd), (b"dbgi", _quat_dbgi)):
                got = _read_track(f, size, fmt, parse)
                if got is not None:
                    t, q = got
                    # neighbouring quaternions q and -q are the same orientation: keep them on one side
                    for k in range(1, len(q)):
                        if float(np.dot(q[k], q[k - 1])) < 0:
                            q[k] = -q[k]
                    return DjiMotion(t, q, fmt.decode(), model)
    except (OSError, struct.error, ValueError, IndexError):
        return None
    return None


# --------------------------------------------------------------------------
# turning rates
# --------------------------------------------------------------------------

def _qmul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    w1, x1, y1, z1 = a.T
    w2, x2, y2, z2 = b.T
    return np.stack([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2], axis=1)


def body_rates(m: DjiMotion) -> Tuple[np.ndarray, np.ndarray]:
    """(t, ω) — angular velocity in the camera's own frame, rad/s, at the midpoints between samples."""
    q = m.q
    r = _qmul(q[:-1] * np.array([1, -1, -1, -1]), q[1:])
    r *= np.where(r[:, :1] < 0, -1.0, 1.0)
    vec = r[:, 1:]
    s = np.linalg.norm(vec, axis=1)
    ang = 2.0 * np.arctan2(s, r[:, 0])
    dt = np.diff(m.t)
    ok = dt > 1e-4
    w = np.zeros_like(vec)
    w[ok] = vec[ok] / np.maximum(s[ok], 1e-12)[:, None] * (ang[ok] / dt[ok])[:, None]
    return (m.t[:-1] + m.t[1:])[ok] / 2.0, w[ok]


def _lowpass(a: np.ndarray, hz: float, cutoff_s: float) -> np.ndarray:
    n = max(1, int(round(cutoff_s * hz)))
    if n <= 1:
        return a
    k = np.ones(n) / n
    return np.convolve(a, k, mode="same")


def camera_yaw_rate(m: DjiMotion, hz: float = SYNC_HZ) -> Tuple[np.ndarray, np.ndarray]:
    """(t, yaw rate) on a regular grid: rotation about the camera axis that carries most of the slow
    (cornering-speed) rotation. Its sign is arbitrary — the sync tries both."""
    t, w = body_rates(m)
    grid = np.arange(t[0], t[-1], 1.0 / hz)
    wg = np.stack([_lowpass(np.interp(grid, t, w[:, k]), hz, 0.5) for k in range(3)], axis=1)
    _u, _s, vt = np.linalg.svd(wg - wg.mean(axis=0), full_matrices=False)
    return grid, wg @ vt[0]


def log_yaw_rate(log_t: np.ndarray, heading_deg: Optional[np.ndarray], speed: Optional[np.ndarray],
                 lat_g: Optional[np.ndarray] = None, hz: float = SYNC_HZ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """(grid, yaw rate rad/s) from the log: the GPS heading's rate of change (or lateral g / speed when there
    is no heading). Zero where the car is slow (heading is noise at a standstill) and across gaps in the log."""
    t = np.asarray(log_t, dtype=float)
    if t.size < 50:
        return None
    grid = np.arange(t[0], t[-1], 1.0 / hz)
    v = None if speed is None else np.interp(grid, t, np.nan_to_num(np.asarray(speed, dtype=float)))
    if heading_deg is not None and np.isfinite(heading_deg).sum() > 50:
        h = np.unwrap(np.radians(np.nan_to_num(np.asarray(heading_deg, dtype=float))))
        y = np.gradient(np.interp(grid, t, h), 1.0 / hz)
    elif lat_g is not None and v is not None:
        y = np.interp(grid, t, np.nan_to_num(np.asarray(lat_g, dtype=float))) * 9.81 / np.maximum(v, 3.0)
    else:
        return None
    y = np.clip(y, -3.0, 3.0)                      # a heading wrap or glitch is not a 170°/s corner
    if v is not None:
        y[v < 3.0] = 0.0
    gaps = np.where(np.diff(t) > 1.0)[0]
    for g in gaps:                                 # nothing was measured across a gap in the log
        y[(grid > t[g]) & (grid < t[g + 1])] = 0.0
    return grid, _lowpass(y, hz, 0.5)


# --------------------------------------------------------------------------
# the search
# --------------------------------------------------------------------------

@dataclass
class MotionSyncResult:
    offset_s: float          # session time at which the clip's first frame was recorded
    correlation: float       # 0–1, how well the turning matched at that offset
    runner_up: float         # best match more than a few seconds away — close to `correlation` = unsure
    source: str
    model: str = ""

    @property
    def confident(self) -> bool:
        return self.correlation >= 0.5 and self.correlation - self.runner_up >= 0.08

    def describe(self, path: str = "") -> str:
        src = "motion data" if self.source == "djmd" else "debug-track orientation"
        name = f" from {os.path.basename(path)}" if path else ""
        return (f"DJI {self.model or 'camera'} motion sync{name} ({src}, match {self.correlation:.2f}"
                + ("" if self.confident else f", next best {self.runner_up:.2f} — check it") + ")")


def _ncc(log: np.ndarray, cam: np.ndarray) -> np.ndarray:
    """Normalised correlation of `cam` against every window of `log` (log is longer). Index k = cam[0]
    lined up with log[k]."""
    n, m = len(log), len(cam)
    c = cam - cam.mean()
    cn = float(np.sqrt((c * c).sum())) or 1.0
    size = 1 << int(np.ceil(np.log2(n + m)))
    num = np.fft.irfft(np.fft.rfft(log, size) * np.conj(np.fft.rfft(c, size)), size)[:n - m + 1]
    cs = np.concatenate([[0.0], np.cumsum(log)])
    cs2 = np.concatenate([[0.0], np.cumsum(log * log)])
    s1 = cs[m:] - cs[:-m]
    s2 = cs2[m:] - cs2[:-m]
    var = np.maximum(s2 - s1 * s1 / m, 0.0)
    den = np.sqrt(var) * cn
    out = np.where(den > 1e-9, num / np.maximum(den, 1e-12), 0.0)
    return out


def motion_sync(m: DjiMotion, log_t: np.ndarray, heading_deg: Optional[np.ndarray],
                speed: Optional[np.ndarray], lat_g: Optional[np.ndarray] = None) -> Optional[MotionSyncResult]:
    """Session time of the clip's first frame, found by matching the camera's turning to the log's."""
    lg = log_yaw_rate(log_t, heading_deg, speed, lat_g)
    if lg is None:
        return None
    grid, ly = lg
    ct, cy = camera_yaw_rate(m)
    if len(cy) < SYNC_HZ * 10 or float(np.std(cy)) < 1e-3:
        return None                                  # under 10 s of clip, or a camera that never turned
    # Let the clip hang off either end of the log by up to half its length (camera started first, or kept
    # running after the logger stopped): pad the log with zeros.
    pad = len(cy) // 2
    padded = np.concatenate([np.zeros(pad), ly, np.zeros(pad)])
    if len(padded) <= len(cy):
        return None
    r = _ncc(padded, cy)
    r = np.abs(r)                                    # the camera axis's sign is arbitrary
    k = int(np.argmax(r))
    best = float(r[k])
    away = int(5 * SYNC_HZ)
    others = np.concatenate([r[:max(0, k - away)], r[k + away:]])
    runner = float(others.max()) if others.size else 0.0
    # sub-sample peak (parabola through the three points around it)
    frac = 0.0
    if 0 < k < len(r) - 1:
        a, b, c = r[k - 1], r[k], r[k + 1]
        d = a - 2 * b + c
        if abs(d) > 1e-12:
            frac = float(np.clip(0.5 * (a - c) / d, -0.5, 0.5))
    lag = (k + frac - pad) / SYNC_HZ                 # log grid index at which the clip's grid starts
    offset = float(grid[0] + lag - ct[0])            # session time of video t = 0
    return MotionSyncResult(offset, round(best, 3), round(runner, 3), m.source, m.model)


def sync_dji_video(video_path: str, log_t: np.ndarray, heading_deg: Optional[np.ndarray],
                   speed: Optional[np.ndarray], lat_g: Optional[np.ndarray] = None) -> Optional[MotionSyncResult]:
    m = read_dji_motion(video_path)
    if m is None:
        return None
    return motion_sync(m, log_t, heading_deg, speed, lat_g)
