"""
Elevation.

The height channel has been carried through and plotted all along, but never
used to say anything. It supports several things nothing else in the analysis
can, because gradient changes what the car can do rather than what the driver
did:

* **Braking downhill costs distance.** On a slope the component of gravity
  along the track adds to or subtracts from what the brakes are doing, so a
  corner that drops away needs an earlier brake point than a flat one at the
  same speed — and that is a common place to run deep without knowing why.
* **A crest unloads the tires.** Following a convex vertical profile costs
  normal load in proportion to the vertical acceleration, so a crest at
  turn-in asks for grip exactly where the car has least of it.
* **A compression lends it back**, which is why some corners take more speed
  than their radius suggests.

Two cautions run through all of it. GPS altitude is far noisier than horizontal
position — meters rather than centimeters — so everything here is smoothed hard
and nothing is claimed below a threshold that noise could produce. And a
barometric channel, where a logger has one, drifts with weather over a session
even while being locally excellent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from . import geometry as geo
from .corners import Corner, CornerMetrics
from .laps import LapTrack

G = 9.80665

#: Smoothing window for the elevation profile. Wide, because GPS altitude noise
#: is meters: a 1 m wobble over 10 m of track reads as a 10% gradient, which is
#: an alpine pass rather than a race circuit.
SMOOTH_M = 45.0

#: Below this the elevation channel is treated as flat. A circuit with under a
#: couple of meters of relief is within the noise of a GPS altitude fix.
MIN_RELIEF_M = 3.0

#: Gradients below this are not worth mentioning and are within what smoothing
#: leaves behind.
MIN_GRADE = 0.012          # 1.2%

#: Load change below this is not worth a driver's attention.
MIN_LOAD_SHIFT = 0.04      # 4% of static load


@dataclass
class CornerElevation:
    """What the ground under a corner is doing, and what it costs."""
    corner_index: int
    #: rise over run, positive uphill, averaged across each phase
    grade_braking: float
    grade_entry: float
    grade_exit: float
    #: meters gained or lost across the corner's whole window
    change_m: float
    #: least normal load through the corner, 1.0 being static
    min_load_factor: float
    #: most normal load, above 1.0 in a compression
    max_load_factor: float
    #: distance from the corner's turn-in to the lightest point, signed
    crest_offset_m: Optional[float]
    #: extra stopping distance from braking on this slope, meters
    braking_penalty_m: float
    #: normal load at the minimum-speed point, where grip matters most
    load_at_apex: float = 1.0

    @property
    def has_relief(self) -> bool:
        return (abs(self.change_m) > 1.0
                or abs(self.grade_braking) > MIN_GRADE
                or abs(self.grade_exit) > MIN_GRADE)


def usable(lap: LapTrack) -> bool:
    """Whether this lap's elevation channel says anything at all."""
    z = lap.extras.get("height")
    if z is None:
        return False
    z = np.asarray(z, dtype=float)
    finite = z[np.isfinite(z)]
    return finite.size > 10 and float(np.ptp(finite)) >= MIN_RELIEF_M


def profile(lap: LapTrack) -> np.ndarray:
    """Smoothed elevation against distance, in meters."""
    z = np.asarray(lap.extras.get("height"), dtype=float)
    z = np.nan_to_num(z, nan=float(np.nanmedian(z)) if np.any(np.isfinite(z))
                      else 0.0)
    win = max(5, int(round(SMOOTH_M / max(lap.ds, 1e-6))) | 1)
    seam = float(np.hypot(lap.x[0] - lap.x[-1], lap.y[0] - lap.y[-1]))
    return geo.savgol(z, win, 2, wrap=seam < 2.0)


def grade(lap: LapTrack) -> np.ndarray:
    """Gradient as rise over run: +0.05 is a 5% climb."""
    z = profile(lap)
    win = max(5, int(round(SMOOTH_M / max(lap.ds, 1e-6))) | 1)
    seam = float(np.hypot(lap.x[0] - lap.x[-1], lap.y[0] - lap.y[-1]))
    return geo.savgol(z, win, 2, deriv=1, delta=lap.ds, wrap=seam < 2.0)


def load_factor(lap: LapTrack) -> np.ndarray:
    """Normal load as a fraction of static, from the vertical profile.

    Following a crest of vertical radius R at speed v needs a downward
    acceleration of v^2/R, and that comes out of what the tires are pressed
    into the road with. Over a compression the sign reverses and the car gains
    load, which is why some corners take more speed than their radius alone
    would allow.
    """
    win = max(5, int(round(SMOOTH_M / max(lap.ds, 1e-6))) | 1)
    seam = float(np.hypot(lap.x[0] - lap.x[-1], lap.y[0] - lap.y[-1]))
    curvature = geo.savgol(profile(lap), win, 2, deriv=2, delta=lap.ds,
                           wrap=seam < 2.0)
    # a convex crest has negative second derivative, which should reduce load
    factor = 1.0 + (lap.speed ** 2) * curvature / G
    return np.clip(factor, 0.3, 1.7)


def braking_penalty(grade_value: float, speed_ms: float,
                    decel_g: float) -> float:
    """Extra stopping distance from braking on a slope, in meters.

    Stopping distance is v^2 / 2a, and on a gradient the achievable
    deceleration becomes a - g*sin(theta): downhill the car is being helped
    along by gravity exactly when it is trying to stop.
    """
    a = abs(decel_g) * G
    if a <= 0.05 or speed_ms <= 1.0:
        return 0.0
    slope = float(np.clip(grade_value, -0.3, 0.3))
    effective = a + G * slope          # uphill (positive) helps
    if effective <= 0.05:
        return float("inf")
    return (speed_ms ** 2) / (2 * effective) - (speed_ms ** 2) / (2 * a)


def analyse_corner(lap: LapTrack, corner: Corner,
                   metrics: CornerMetrics) -> Optional[CornerElevation]:
    """Elevation through one corner, and what it costs."""
    if not usable(lap):
        return None
    z = profile(lap)
    g = grade(lap)
    load = load_factor(lap)

    def mean_between(a: Optional[float], b: Optional[float],
                     arr: np.ndarray) -> float:
        if a is None or b is None or b <= a:
            return 0.0
        i0, i1 = lap.idx(a), max(lap.idx(b), lap.idx(a) + 1)
        return float(np.mean(arr[i0:i1 + 1]))

    brake_grade = mean_between(metrics.s_brake, metrics.s_vmin, g)
    entry_grade = mean_between(metrics.s_turnin, metrics.s_vmin, g)
    exit_grade = mean_between(metrics.s_vmin, corner.s_end + 50.0, g)

    # Load is examined over the corner itself, not its whole analysis window.
    # The window reaches a couple of hundred meters back up the approach, and
    # load scales with v^2 — so a slight brow on a fast straight produces a far
    # larger figure than anything at the corner, and gets attributed to it.
    lo = lap.idx(max(corner.s_start - 40.0, 0.0))
    hi = max(lap.idx(min(corner.s_end + 30.0, lap.s[-1])), lo + 1)
    span = load[lo:hi + 1]
    lightest = lo + int(np.argmin(span))
    i0, i1 = lap.idx(corner.s_win_start), lap.idx(corner.s_win_end)
    i1 = max(i1, i0 + 1)

    penalty = 0.0
    if metrics.has_braking and metrics.s_brake is not None:
        penalty = braking_penalty(brake_grade,
                                  lap.at(metrics.s_brake, lap.speed),
                                  metrics.peak_decel_g)

    return CornerElevation(
        corner_index=corner.index,
        grade_braking=brake_grade,
        grade_entry=entry_grade,
        grade_exit=exit_grade,
        change_m=float(z[i1] - z[i0]),
        min_load_factor=float(span.min()),
        max_load_factor=float(span.max()),
        crest_offset_m=(float(lap.s[lightest] - metrics.s_turnin)
                        if metrics.s_turnin is not None else None),
        load_at_apex=float(load[lap.idx(metrics.s_vmin)]),
        braking_penalty_m=float(penalty) if np.isfinite(penalty) else 0.0)


def analyse_lap(lap: LapTrack, corners: Sequence[Corner],
                metrics: Sequence[CornerMetrics]) -> List[CornerElevation]:
    out = []
    for corner, m in zip(corners, metrics):
        result = analyse_corner(lap, corner, m)
        if result is not None:
            out.append(result)
    return out


def total_relief(lap: LapTrack) -> float:
    return float(np.ptp(profile(lap))) if usable(lap) else 0.0
