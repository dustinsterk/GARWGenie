"""
Display units.

Everything inside the analysis stays metric — speeds in km/h, distances in
meters — so thresholds, comparisons and stored metrics never change meaning.
Conversion happens only at the point text or an axis is produced. Mixing
display units into the analysis is how tools end up with a threshold that is
6 m on one machine and 5 m on another.
"""

from __future__ import annotations

from dataclasses import dataclass

MPH_PER_KMH = 0.621371
FT_PER_M = 3.280840
MI_PER_M = 0.000621371


@dataclass(frozen=True)
class UnitSystem:
    name: str
    speed_label: str          # axis / column label for speed
    dist_label: str           # axis / column label for distance
    _speed_k: float           # from km/h
    _dist_k: float            # from meters

    # ---- numeric conversion (input is always km/h or meters) --------------
    def spd(self, kmh: float) -> float:
        return kmh * self._speed_k

    def d_inv(self, value: float) -> float:
        """Back from display distance to metres.

        Needed whenever a value comes *from* the interface rather than going to
        it — clicking a plot gives a number in whatever the axis is showing.
        """
        return float(value) / self._dist_k

    def d(self, m: float) -> float:
        return m * self._dist_k

    # ---- formatted strings ------------------------------------------------
    def spd_s(self, kmh: float, dp: int = 1) -> str:
        return f"{self.spd(kmh):.{dp}f} {self.speed_label}"

    def d_s(self, m: float, dp: int = 0) -> str:
        return f"{self.d(m):.{dp}f} {self.dist_label}"

    def d_abs_s(self, m: float, dp: int = 0) -> str:
        """Magnitude only — for phrases that already say earlier/later."""
        return f"{abs(self.d(m)):.{dp}f} {self.dist_label}"

    def length_s(self, m: float) -> str:
        """Lap-scale distance: meters or miles, not thousands of feet."""
        if self.name == "imperial":
            return f"{m * MI_PER_M:.2f} mi"
        return f"{m:.0f} m"


METRIC = UnitSystem("metric", "km/h", "m", 1.0, 1.0)
IMPERIAL = UnitSystem("imperial", "mph", "ft", MPH_PER_KMH, FT_PER_M)

SYSTEMS = {"metric": METRIC, "imperial": IMPERIAL}


def get(name: str) -> UnitSystem:
    return SYSTEMS.get(str(name).lower(), METRIC)
