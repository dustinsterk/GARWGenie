"""
Point-to-point courses from a track list.

A hillclimb, autocross or stage has separate start and finish lines, which a
log alone can't reveal (a circuit's start/finish can be found from where the
car keeps coming back; a one-way course never comes back). GARW Genie keeps
those courses — UserTracks.txt, and Tracks.txt in admin mode — with a start
point, a finish point, a gate width and the start direction. The host app
registers them here; a log that passes within reach of both a course's start
and its finish is timed gate to gate on that course.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

import numpy as np

from . import geometry as geo
from .laps import Session, build_session, gate_at
from .parser import VboFile

_TRACKS: List[object] = []


def register_tracks(tracks: Iterable[object]) -> None:
    """Courses to match against. Each needs .name, .sf (lat, lon), .finish (lat, lon) or None,
    .radius (gate half-width, m) and .start_hdg (compass degrees) or None — laptimer.Track fits."""
    _TRACKS[:] = [t for t in tracks if getattr(t, "finish", None) and getattr(t, "sf", None)]


def registered() -> int:
    return len(_TRACKS)


def match_p2p(vbo: VboFile) -> Optional[Tuple[str, tuple, tuple]]:
    """(course name, start gate, finish gate) for the registered point-to-point course this log drove,
    or None. Gates are (lat1, lon1, lat2, lon2)."""
    if not _TRACKS:
        return None
    la, lo = vbo.channels.get("lat_deg"), vbo.channels.get("lon_deg")
    if la is None or len(la) < 10:
        return None
    x, y, lat0, lon0 = geo.project_local(la, lo)
    step = max(1, len(x) // 20000)              # plenty for a distance test, cheap on long logs
    xs, ys = x[::step], y[::step]
    best = None
    for t in _TRACKS:
        reach = max(float(getattr(t, "radius", 30) or 30), 15.0) + 30.0
        dists = []
        for lat, lon in (t.sf, t.finish):
            px, py, _, _ = geo.project_local(np.array([lat]), np.array([lon]), lat0, lon0)
            dists.append(float(np.min(np.hypot(xs - px[0], ys - py[0]))))
        if max(dists) <= reach and (best is None or sum(dists) < best[0]):
            best = (sum(dists), t)
    if best is None:
        return None
    t = best[1]
    hw = max(float(getattr(t, "radius", 15) or 15), 8.0)
    sg = gate_at(vbo, t.sf[0], t.sf[1], hw, getattr(t, "start_hdg", None))
    fg = gate_at(vbo, t.finish[0], t.finish[1], hw)
    if sg is None or fg is None:
        return None
    return t.name, sg, fg


def session_for(vbo: VboFile, **kw) -> Session:
    """build_session, timed on a matching point-to-point course when there is one."""
    m = None if kw.get("start_finish_latlon") else match_p2p(vbo)
    if m:
        name, sg, fg = m
        s = build_session(vbo, start_finish_latlon=list(sg), finish_latlon=list(fg), course_name=name, **kw)
        if s.laps:
            return s
    return build_session(vbo, **kw)
