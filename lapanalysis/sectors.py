"""
Sectors.

Three sources, in order of preference:

1. **Split lines declared in the file.** VBOX writes `Split1`, `Split2` … in
   `[laptiming]` as pairs of coordinates, exactly like the start/finish line.
   Crossings are found geometrically and timed the same way, so sector times
   are computed from the trace rather than taken on trust.

2. **Sector times written into the comments.** Some converters — including the
   Garmin watch-app export — record per-lap sector times in the header text
   without any split coordinates. Those can be shown, but nothing further can
   be derived from them: there is no way to know *where* the boundaries were,
   so per-sector deltas against another lap are not available.

3. **Automatic sectors.** Equal distance around the lap. Arbitrary, but a
   consistent way to see which third of a circuit is costing time when the
   logger offers nothing better.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import geometry as geo
from .laps import LapTrack, Session, recentre_gate

#: 'Lap 2  0:55.00  S 21.0/11.0/22.9  G(...)' — the Garmin watch-app export format
_COMMENT_SECTORS = re.compile(
    r"^\s*Lap\s+(\d+)\b.*?\bS\s+([\d.]+(?:\s*/\s*[\d.]+)+)", re.I)


@dataclass
class SectorSet:
    """Sector boundaries as distances around the lap, plus where they came from."""
    #: boundary distances in meters, excluding 0 and the lap length
    boundaries: List[float] = field(default_factory=list)
    source: str = "none"                # 'file' | 'auto' | 'none'
    names: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.boundaries) + 1 if self.boundaries else 0

    def edges(self, lap_length: float) -> List[Tuple[float, float]]:
        cuts = [0.0] + sorted(self.boundaries) + [lap_length]
        return list(zip(cuts, cuts[1:]))

    def label(self, index: int) -> str:
        """The sector's name if the file gave one, else its number.

        Index 0 is the sector *before* the first split, which no split names,
        so it keeps its number.
        """
        if index < len(self.names) and self.names[index]:
            return self.names[index]
        return f"S{index + 1}"


def _positions_for_gates(session: Session, lap: LapTrack, gates) -> List[float]:
    """Distances around `lap` at which it crosses each of the given gate lines
    (each a lat1, lon1, lat2, lon2 quad)."""
    out: List[float] = []
    valid = geo.plausible_steps(lap.x, lap.y, lap.t, lap.speed)
    for la1, lo1, la2, lo2 in gates:
        xs, ys, _, _ = geo.project_local(np.array([la1, la2]),
                                         np.array([lo1, lo2]),
                                         session.lat0, session.lon0)
        gate = recentre_gate((float(xs[0]), float(ys[0]),
                              float(xs[1]), float(ys[1])), lap.x, lap.y)
        hits = geo.segment_crossings(lap.x, lap.y, *gate,
                                     require_forward=False,
                                     valid_steps=valid)
        if not hits:
            continue
        i, frac = hits[0]
        out.append(geo.interp_at(lap.s, i, frac))
    return sorted(s for s in out if 1.0 < s < lap.length - 1.0)


def split_positions(session: Session, lap: LapTrack) -> List[float]:
    """Distances around `lap` at which it crosses each file-declared split."""
    return _positions_for_gates(session, lap, session.vbo.splits)


def auto_boundaries(lap_length: float, count: int = 3) -> List[float]:
    count = max(2, int(count))
    return [lap_length * i / count for i in range(1, count)]


def build_sectors(session: Session, lap: LapTrack,
                  auto_count: int = 3) -> SectorSet:
    """Sector boundaries for a session.

    Priority: sector gate lines the user has placed on the map, then any the
    file declares, then equal auto-splits. User-placed gates win because they
    are a deliberate correction of what the file got wrong.
    """
    custom = getattr(session, "custom_splits", None)
    if custom:
        placed = _positions_for_gates(session, lap, custom)
        if placed:
            return SectorSet(boundaries=placed, source="user")
    if session.vbo.splits:
        declared = split_positions(session, lap)
        if declared:
            # A sector running to a named split takes that name: "Varikko"
            # tells a driver where they are, "S2" does not.
            labels: List[str] = []
            names = [n for n in session.vbo.split_names if n]
            if len(names) == len(session.vbo.splits):
                labels = [""] + names
            return SectorSet(boundaries=declared, source="file", names=labels)
    return SectorSet(boundaries=auto_boundaries(lap.length, auto_count),
                     source="auto")


def sector_times(lap: LapTrack, sectors: SectorSet) -> List[float]:
    """Elapsed time within each sector, summing to the measured lap time.

    The lap's internal time trace is the integral of ds/v, which is ideal for
    *differences* — the scale error cancels between two laps — but drifts a few
    tenths of a percent from the lap time measured at the timing line. Sector
    times are absolute figures a driver will add up, so they are scaled to the
    measured lap time. Sectors that do not sum to the lap are simply wrong,
    however defensible the arithmetic behind them.
    """
    if not sectors.boundaries:
        return [lap.lap_time]
    raw = [lap.window_time(min(a, lap.length), min(b, lap.length))
           for a, b in sectors.edges(lap.length)]
    total = sum(raw)
    if total <= 0 or lap.lap_time <= 0:
        return raw
    scale = lap.lap_time / total
    return [t * scale for t in raw]


def sector_table(laps: Sequence[LapTrack], sectors: SectorSet
                 ) -> Dict[int, List[float]]:
    return {lap.number: sector_times(lap, sectors) for lap in laps}


def best_sectors(laps: Sequence[LapTrack], sectors: SectorSet
                 ) -> Tuple[List[float], List[int]]:
    """Fastest time in each sector across the laps, and which lap set it."""
    table = sector_table(laps, sectors)
    if not table:
        return [], []
    n = len(next(iter(table.values())))
    best, owner = [], []
    for i in range(n):
        pairs = [(times[i], num) for num, times in table.items()
                 if i < len(times)]
        t, num = min(pairs)
        best.append(t)
        owner.append(num)
    return best, owner


# --------------------------------------------------------------------------
# Sector times recorded in the file's comments
# --------------------------------------------------------------------------


def comment_sectors(session: Session) -> Dict[int, List[float]]:
    """Per-lap sector times parsed from the header text, if any are there.

    Purely informational. Without split coordinates there is no way to know
    where the boundaries fell, so these cannot be compared against sectors this
    tool computes, and no per-sector delta can be derived from them.
    """
    out: Dict[int, List[float]] = {}
    for line in session.vbo.comments:
        m = _COMMENT_SECTORS.match(line)
        if not m:
            continue
        try:
            lap = int(m.group(1))
            times = [float(v) for v in re.split(r"\s*/\s*", m.group(2))]
        except ValueError:
            continue
        if times:
            out[lap] = times
    return out
