"""
Signal + geometry primitives.

Deliberately numpy-only (no scipy) so this drops onto a slim box without a
build toolchain. The Savitzky-Golay implementation below is equivalent to
scipy.signal.savgol_filter with mode='nearest'/'wrap'.
"""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np

EARTH_R = 6378137.0  # WGS-84 semi-major axis, meters
G = 9.80665


# --------------------------------------------------------------------------
# Smoothing / differentiation
# --------------------------------------------------------------------------


def savgol_coeffs(window: int, poly: int, deriv: int = 0, delta: float = 1.0) -> np.ndarray:
    """Least-squares Savitzky-Golay correlation kernel."""
    if window % 2 == 0:
        window += 1
    poly = min(poly, window - 1)
    half = window // 2
    x = np.arange(-half, half + 1, dtype=float)
    A = np.vander(x, poly + 1, increasing=True)
    coef = np.linalg.pinv(A)[deriv]
    return coef * (math.factorial(deriv) / (delta ** deriv))


def pad_signal(y: np.ndarray, half: int, wrap: bool = False,
               closed: bool = True) -> np.ndarray:
    """Extend a signal by `half` samples at each end.

    `wrap=True, closed=True` is the case that matters for lap data: a lap array
    starts and ends at the *same physical point* on the track, so naive wrap
    padding duplicates that sample. A duplicated position is a zero-length step,
    which a derivative filter reads as infinite curvature — you get a phantom
    hairpin sitting exactly on the start/finish line.
    """
    y = np.asarray(y, dtype=float)
    n = y.size
    if half <= 0 or n == 0:
        return y.copy()
    if not wrap:
        return np.pad(y, half, mode="edge")
    period = n - 1 if (closed and n > 2) else n
    idx = np.arange(-half, n + half) % period
    return y[idx]


def savgol(y: np.ndarray, window: int, poly: int = 2, deriv: int = 0,
           delta: float = 1.0, wrap: bool = False,
           closed: bool = True) -> np.ndarray:
    """Smooth (or differentiate) a 1-D signal.

    `wrap=True` treats the signal as cyclic, which is what you want for a
    closed racing lap — it stops the smoother from flattening the corner that
    straddles the start/finish line.
    """
    y = np.asarray(y, dtype=float)
    n = y.size
    if n == 0:
        return y.copy()
    window = int(window) | 1
    if window >= n:
        window = max(3, (n - 1) | 1)
    if window < 3:
        return y.copy() if deriv == 0 else np.gradient(y, delta)
    half = window // 2
    k = savgol_coeffs(window, poly, deriv, delta)
    pad = pad_signal(y, half, wrap=wrap, closed=closed)
    return np.convolve(pad, k[::-1], mode="valid")


def moving_average(y: np.ndarray, window: int, wrap: bool = False,
                   closed: bool = True) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    window = max(1, int(window) | 1)
    if window >= y.size:
        return np.full_like(y, y.mean())
    half = window // 2
    pad = pad_signal(y, half, wrap=wrap, closed=closed)
    kern = np.ones(window) / window
    return np.convolve(pad, kern, mode="valid")


def unwrap_deg(a: np.ndarray) -> np.ndarray:
    return np.degrees(np.unwrap(np.radians(np.asarray(a, dtype=float))))


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest signed difference a-b in degrees, in (-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def project_local(lat: np.ndarray, lon: np.ndarray,
                  lat0: float | None = None,
                  lon0: float | None = None) -> Tuple[np.ndarray, np.ndarray, float, float]:
    """Equirectangular projection to meters. Plenty accurate over a circuit."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    if lat0 is None:
        lat0 = float(np.nanmean(lat))
    if lon0 is None:
        lon0 = float(np.nanmean(lon))
    coslat = math.cos(math.radians(lat0))
    x = np.radians(lon - lon0) * EARTH_R * coslat
    y = np.radians(lat - lat0) * EARTH_R
    return x, y, lat0, lon0


def unproject_local(x: np.ndarray, y: np.ndarray, lat0: float, lon0: float):
    coslat = math.cos(math.radians(lat0))
    lat = lat0 + np.degrees(np.asarray(y, dtype=float) / EARTH_R)
    lon = lon0 + np.degrees(np.asarray(x, dtype=float) / (EARTH_R * coslat))
    return lat, lon


# --------------------------------------------------------------------------
# Distance
# --------------------------------------------------------------------------


def distance_from_speed(t: np.ndarray, speed: np.ndarray) -> np.ndarray:
    """Cumulative distance by trapezoidal integration of GPS speed.

    Preferred over integrating position: VBOX ground speed is Doppler-derived
    and far less noisy than differentiated position, so distance stays smooth
    and laps line up properly.
    """
    t = np.asarray(t, dtype=float)
    v = np.clip(np.asarray(speed, dtype=float), 0.0, None)
    dt = np.diff(t)
    dt = np.where((dt > 0) & (dt < 5.0), dt, 0.0)
    ds = 0.5 * (v[:-1] + v[1:]) * dt
    return np.concatenate([[0.0], np.cumsum(ds)])


def path_length(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    ds = np.hypot(np.diff(x), np.diff(y))
    return np.concatenate([[0.0], np.cumsum(ds)])


# --------------------------------------------------------------------------
# Curvature
# --------------------------------------------------------------------------


def curvature_from_path(x: np.ndarray, y: np.ndarray, ds: float,
                        smooth_m: float = 20.0, wrap: bool = False):
    """Heading (rad, unwrapped) and curvature (1/m) from a uniformly spaced path.

    Sign convention: positive curvature = turning left (counter-clockwise in
    the local ENU frame).
    """
    win = max(5, int(round(smooth_m / max(ds, 1e-6))) | 1)
    xs = savgol(x, win, 2, wrap=wrap)
    ys = savgol(y, win, 2, wrap=wrap)
    dx = savgol(xs, win, 3, deriv=1, delta=ds, wrap=wrap)
    dy = savgol(ys, win, 3, deriv=1, delta=ds, wrap=wrap)
    ddx = savgol(xs, win, 3, deriv=2, delta=ds, wrap=wrap)
    ddy = savgol(ys, win, 3, deriv=2, delta=ds, wrap=wrap)

    # Position is periodic around a lap; unwrapped heading is NOT (it gains 2*pi
    # per lap), so differentiating heading with cyclic padding puts a -2*pi step
    # at the seam. Take curvature straight from the position derivatives.
    speed2 = dx * dx + dy * dy
    denom = np.power(np.maximum(speed2, 1e-9), 1.5)
    kappa = (dx * ddy - dy * ddx) / denom
    kappa = savgol(kappa, max(5, win // 2) | 1, 2, wrap=wrap)

    heading = np.unwrap(np.arctan2(dy, dx))
    return heading, kappa


def radius_from_curvature(kappa: np.ndarray, cap: float = 5000.0) -> np.ndarray:
    k = np.abs(np.asarray(kappa, dtype=float))
    with np.errstate(divide="ignore"):
        r = np.where(k > 1.0 / cap, 1.0 / np.maximum(k, 1e-12), cap)
    return np.minimum(r, cap)


# --------------------------------------------------------------------------
# Line crossing (start/finish, splits)
# --------------------------------------------------------------------------


def plausible_steps(x: np.ndarray, y: np.ndarray, t: np.ndarray,
                    speed: np.ndarray, slack: float = 3.0,
                    floor_m: float = 30.0) -> np.ndarray:
    """Mask of sample-to-sample steps that are physically consistent.

    A GPS dropout, a spliced file, or a logger reset leaves a jump in position
    that no car made. The straight line drawn across that jump can cut the
    timing line and invent a lap crossing, so crossings there must be ignored.
    """
    step = np.hypot(np.diff(np.asarray(x, float)), np.diff(np.asarray(y, float)))
    dt = np.diff(np.asarray(t, float))
    v = np.asarray(speed, float)
    expect = 0.5 * (v[:-1] + v[1:]) * np.clip(dt, 0.0, None)
    return step <= np.maximum(slack * expect, floor_m)


def segment_crossings(x: np.ndarray, y: np.ndarray,
                      ax: float, ay: float, bx: float, by: float,
                      require_forward: bool = True,
                      valid_steps: "np.ndarray | None" = None) -> list[tuple[int, float]]:
    """Indices and interpolation fractions where a path crosses segment A-B.

    Returns (i, frac) meaning the crossing happens between sample i and i+1,
    at i + frac. Only counts crossings that fall inside the segment, so a
    finish line drawn across the track won't trigger from the paddock.
    """
    ex, ey = bx - ax, by - ay
    seg_len2 = ex * ex + ey * ey
    if seg_len2 <= 0:
        return []
    # signed distance to the infinite line (left of A->B is positive)
    side = (x - ax) * ey - (y - ay) * ex
    out: list[tuple[int, float]] = []
    for i in range(len(side) - 1):
        if valid_steps is not None and i < len(valid_steps) and not valid_steps[i]:
            continue
        s0, s1 = side[i], side[i + 1]
        if s0 == 0.0 and s1 == 0.0:
            continue
        crosses = (s0 < 0 <= s1)
        if not require_forward:
            crosses = crosses or (s1 < 0 <= s0)
        if not crosses:
            continue
        denom = (s0 - s1)
        frac = s0 / denom if denom != 0 else 0.0
        frac = float(np.clip(frac, 0.0, 1.0))
        px = x[i] + frac * (x[i + 1] - x[i])
        py = y[i] + frac * (y[i + 1] - y[i])
        # projection onto the segment must land within it
        u = ((px - ax) * ex + (py - ay) * ey) / seg_len2
        if -0.02 <= u <= 1.02:
            out.append((i, frac))
    return out


def interp_at(arr: np.ndarray, i: int, frac: float) -> float:
    if i + 1 >= len(arr):
        return float(arr[-1])
    return float(arr[i] + frac * (arr[i + 1] - arr[i]))


# --------------------------------------------------------------------------
# Resampling
# --------------------------------------------------------------------------


def resample_to_grid(s_src: np.ndarray, values: np.ndarray,
                     s_grid: np.ndarray) -> np.ndarray:
    """Monotone-safe linear resample onto a distance grid."""
    s_src = np.asarray(s_src, dtype=float)
    values = np.asarray(values, dtype=float)
    order = np.argsort(s_src, kind="stable")
    s_sorted = s_src[order]
    v_sorted = values[order]
    keep = np.concatenate([[True], np.diff(s_sorted) > 1e-9])
    return np.interp(s_grid, s_sorted[keep], v_sorted[keep])


def elapsed_time_from_speed(s_grid: np.ndarray, speed: np.ndarray,
                            v_floor: float = 1.0) -> np.ndarray:
    """t(s) = integral ds/v.

    Integrating 1/v beats using raw GPS timestamps for delta-time work: it is
    immune to timestamp jitter and to sub-sample error in the start-line
    crossing, and both laps are guaranteed to start at exactly t=0.
    """
    v = np.clip(np.asarray(speed, dtype=float), v_floor, None)
    inv = 1.0 / v
    ds = np.diff(np.asarray(s_grid, dtype=float))
    dt = 0.5 * (inv[:-1] + inv[1:]) * ds
    return np.concatenate([[0.0], np.cumsum(dt)])


# --------------------------------------------------------------------------
# Line comparison
# --------------------------------------------------------------------------


def line_offset(ref_x: np.ndarray, ref_y: np.ndarray,
                cmp_x: np.ndarray, cmp_y: np.ndarray,
                ds: float = 1.0, window_m: float = 80.0) -> np.ndarray:
    """Signed lateral offset of one path from another, in meters.

    For every point on the reference path, the closest approach of the compared
    path, signed so that **positive is left** of the reference's direction of
    travel. Returned on the reference's own sample grid.

    Distance along the lap is not enough to compare lines: two laps at the same
    `s` are not at the same place on the track if one took a wider entry, and
    the difference *is* the thing worth measuring. The search is windowed in
    index space because the laps do stay roughly aligned by distance — a full
    nearest-neighbor search would be quadratic for no gain.
    """
    ref_x = np.asarray(ref_x, dtype=float)
    ref_y = np.asarray(ref_y, dtype=float)
    cmp_x = np.asarray(cmp_x, dtype=float)
    cmp_y = np.asarray(cmp_y, dtype=float)
    n, m = ref_x.size, cmp_x.size
    if n == 0 or m == 0:
        return np.zeros(n)

    half = max(3, int(round(window_m / max(ds, 1e-6))))
    # reference heading, for the sign
    hx = np.gradient(ref_x)
    hy = np.gradient(ref_y)
    norm = np.hypot(hx, hy)
    norm[norm < 1e-9] = 1.0
    hx, hy = hx / norm, hy / norm

    scale = m / n if n else 1.0
    out = np.zeros(n)
    for i in range(n):
        centre = int(round(i * scale))
        lo = max(0, centre - half)
        hi = min(m, centre + half + 1)
        dx = cmp_x[lo:hi] - ref_x[i]
        dy = cmp_y[lo:hi] - ref_y[i]
        d2 = dx * dx + dy * dy
        j = int(np.argmin(d2))
        # cross product of heading with the offset: +ve means to the left
        out[i] = hx[i] * dy[j] - hy[i] * dx[j]
    return out
