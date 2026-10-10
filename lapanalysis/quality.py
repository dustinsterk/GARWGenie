"""
How much to trust the analysis.

Every number this tool produces is only as good as the log behind it, and the
log varies enormously: a 25 Hz VBOX with six clean laps supports conclusions
that a 1 Hz watch export with one usable lap does not. Without saying so, the
coaching reads with identical authority in both cases, which is the most
misleading thing a tool like this can do.

Nothing here changes the analysis. It reports what the analysis is standing on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from . import geometry as geo
from .corners import Corner, detect_corners
from .laps import LapTrack, Session
from .units import METRIC, UnitSystem

GOOD, FAIR, POOR = "good", "fair", "poor"
_RANK = {GOOD: 0, FAIR: 1, POOR: 2}


@dataclass
class Check:
    name: str
    grade: str
    summary: str
    detail: str = ""
    #: True when this is simply what the device does, rather than something
    #: that went wrong or that the driver could have done differently. An
    #: inherent limit still constrains what can be measured, but calling it a
    #: fault is both wrong and useless: there is nothing to act on.
    inherent: bool = False


@dataclass
class Quality:
    checks: List[Check] = field(default_factory=list)
    #: meters of uncertainty in a reported brake point, from the log rate alone
    brake_point_precision_m: float = 0.0

    @property
    def grade(self) -> str:
        if not self.checks:
            return GOOD
        return max((c.grade for c in self.checks), key=lambda g: _RANK[g])

    @property
    def actionable_grade(self) -> str:
        """The grade ignoring limits inherent to the device.

        What the driver could actually do something about — a different timing
        line, more laps, a cleaner fix. Reporting a watch session as "poor"
        because a watch logs at 1 Hz buries the checks that are worth acting
        on behind one that never will be.
        """
        rest = [c.grade for c in self.checks if not c.inherent]
        return max(rest, key=lambda g: _RANK[g]) if rest else GOOD

    @property
    def device_limits(self) -> List[Check]:
        return [c for c in self.checks if c.inherent and c.grade != GOOD]

    @property
    def headline(self) -> str:
        counts = {GOOD: 0, FAIR: 0, POOR: 0}
        for c in self.checks:
            counts[c.grade] += 1
        limits = [c for c in self.checks if c.inherent and c.grade != GOOD]
        if counts[POOR]:
            return "weak — treat the findings as indicative only"
        if limits and counts[FAIR] == len(limits):
            return ("as good as this device gets — see what the log rate can "
                    "and cannot resolve")
        if counts[FAIR]:
            return "usable — some findings are less certain than others"
        return "good — the findings are well supported"

    def worst(self, limit: int = 4) -> List[Check]:
        return sorted(self.checks, key=lambda c: -_RANK[c.grade])[:limit]


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------


def _from_watch(session: Session) -> bool:
    """Whether this came off a wrist rather than out of a car.

    A watch is capped at 1 Hz by its platform, and files converted from one
    still say so in their header, so a .vbo made by the converter is
    recognized too.
    """
    if getattr(session.vbo, "source_format", "vbo") == "fit":
        return True
    # Deliberately specific: a bare app name would match the creator line this
    # application writes into files it generates — including its test fixtures —
    # and every 1 Hz log would then excuse itself as a watch. "lap watch" matches
    # the watch app's own header.
    text = " ".join(session.vbo.comments[:8]).lower()
    return any(mark in text for mark in
               ("garmin fit", "fit2vbo", "connect iq", "lap watch"))


def _rate_check(session: Session, u: UnitSystem) -> tuple[Check, float]:
    """Log rate, expressed as what it means for a brake point.

    A brake point can never be located more precisely than the distance the car
    travels between two fixes. At 100 km/h that is under 3 m at 10 Hz and 28 m
    at 1 Hz — the difference between "you braked six meters early" being a
    finding and being noise.

    A watch logs at 1 Hz because that is what the platform allows. Grading it a
    fault is both wrong and useless: there is nothing the driver can do about
    it, and marking the session down for it buries the checks that *are*
    actionable. It is reported as an inherent limit instead, saying plainly
    what the rate supports and what it does not.
    """
    hz = session.vbo.sample_rate or 0.0
    laps = session.valid_laps or session.laps
    speed = max((float(np.percentile(l.speed, 90)) for l in laps), default=30.0)
    spacing = speed / hz if hz > 0 else float("inf")

    # a hair under 10, because a nominal 10 Hz logger measures 9.99
    if hz >= 9.5:
        return Check(
            name="log rate",
            grade=GOOD,
            summary=f"{hz:.0f} Hz — about {u.d_s(spacing)} between fixes at speed",
            detail=("Brake points and apex positions are well located; "
                    f"differences above roughly {u.d_s(spacing)} are real."),
        ), spacing

    if _from_watch(session):
        return Check(
            name="log rate",
            grade=FAIR,
            summary=(f"{hz:.0f} Hz, the most a watch records — about "
                     f"{u.d_s(spacing)} between fixes at speed"),
            detail=(
                "This is the platform ceiling, not a fault in the recording. "
                "Lap and sector times, minimum and exit speeds, and "
                "corner-by-corner consistency are all sound at this rate. "
                "What is soft is position within a corner: brake points, "
                "turn-in and apex locations are interpolated between fixes "
                f"about {u.d_s(spacing)} apart, so read differences smaller "
                "than that as noise rather than as findings."),
            inherent=True,
        ), spacing

    grade = FAIR if hz >= 4.5 else POOR
    note = ("brake points are approximate" if hz >= 4.5 else
            "brake points, turn-in and apex positions are interpolated "
            "between distant fixes")
    return Check(
        name="log rate",
        grade=grade,
        summary=f"{hz:.0f} Hz — about {u.d_s(spacing)} between fixes at speed",
        detail=(f"A brake point cannot be located more precisely than the "
                f"distance covered between two fixes, here roughly "
                f"{u.d_s(spacing)}; {note}. Differences smaller than that "
                f"are not measurements."),
    ), spacing


def _lap_count_check(session: Session) -> Check:
    clean = len(session.valid_laps)
    total = len(session.laps)
    if clean >= 5:
        grade, note = GOOD, ("enough laps for consistency and theoretical-best "
                             "figures to mean something")
    elif clean >= 3:
        grade, note = FAIR, ("consistency figures are thin; three laps is a "
                             "small sample for a standard deviation")
    elif clean == 2:
        grade, note = FAIR, ("with two laps the reference is simply the other "
                             "lap, not a best-of; a single mistake on either "
                             "dominates every comparison")
    else:
        grade, note = POOR, ("nothing to compare against — only absolute "
                             "technique notes are available")
    return Check(name="clean laps",
                 grade=grade,
                 summary=f"{clean} clean of {total} detected",
                 detail=note.capitalize() + ".")


def _timing_line_check(session: Session) -> Check:
    src = session.sf_source
    if src == "file":
        return Check("start/finish", GOOD, "declared in the file",
                     "Lap times match whatever the logger reported.")
    if "logger" in src:
        return Check("start/finish", GOOD, "the logger's own start/finish",
                     "Laps are split where the logger itself counted them, so lap times match the dash.")
    if "ignored" in src:
        return Check(
            "start/finish", FAIR, "declared line rejected, placed automatically",
            "The file declared a timing line that does not sit on this data, so "
            "one was chosen from the trajectory. Lap times are internally "
            "consistent but will not match the logger's own.")
    return Check(
        "start/finish", FAIR, "placed automatically",
        "The file declared no timing line, so one was chosen from the "
        "trajectory. Lap times are internally consistent but arbitrary in "
        "where they start; set one with --sf to match official timing.")


def _closure_check(session: Session, u: UnitSystem) -> Check:
    laps = session.valid_laps or session.laps
    if not laps:
        return Check("lap closure", POOR, "no laps", "")
    gaps = [float(np.hypot(l.x[0] - l.x[-1], l.y[0] - l.y[-1])) for l in laps]
    worst = max(gaps)
    if worst < 2.0:
        grade, note = GOOD, "laps close cleanly at the timing line"
    elif worst < 8.0:
        grade, note = FAIR, ("the car crosses the line on a slightly different "
                             "line each lap, which is normal")
    else:
        grade, note = POOR, ("the entry and exit points differ enough that the "
                             "timing line may be badly placed — across a "
                             "corner, or at an angle to the track")
    return Check(
        name="lap closure",
        grade=grade,
        summary=f"worst gap {u.d_s(worst, 1)} between lap entry and exit",
        detail=note.capitalize() + ".")


def _continuity_check(session: Session) -> Check:
    t = session.vbo.channels.get("t")
    speed = session.vbo.channels.get("speed")
    if t is None or speed is None:
        return Check("continuity", FAIR, "not assessable", "")
    dt = np.diff(t)
    gaps = int(np.count_nonzero(dt > 2.0))
    valid = geo.plausible_steps(session.x, session.y, t, speed)
    jumps = int(np.count_nonzero(~valid))
    total = max(len(dt), 1)
    if gaps == 0 and jumps == 0:
        return Check("continuity", GOOD, "no dropouts",
                     "The trace is continuous throughout.")
    grade = POOR if (jumps / total) > 0.005 or gaps > 3 else FAIR
    return Check(
        name="continuity",
        grade=grade,
        summary=f"{gaps} time gaps, {jumps} position jumps",
        detail=("Dropouts are excluded from lap timing, but the surrounding "
                "samples are interpolated, so metrics near them are softer "
                "than elsewhere."))


def _channels_check(session: Session) -> Check:
    have = set(session.vbo.channels)
    logged = [n for n in ("brake", "throttle", "ax_g", "ay_g") if n in have]
    if "brake" in have and "throttle" in have:
        return Check("channels", GOOD, "brake and throttle logged",
                     "Brake release and throttle application are measured "
                     "directly, so coasting and trail-braking figures are "
                     "exact rather than inferred.")
    from_watch = _from_watch(session)
    if logged:
        return Check(
            "channels", FAIR,
            f"{', '.join(logged)} logged; no pedal inputs"
            + (" (a watch cannot reach them)" if from_watch else ""),
            "Without brake and throttle channels, the transition between them "
            "is inferred from how speed is changing. Coasting time is a "
            "reasonable estimate rather than a measurement."
            + (" A watch has no connection to the car, so this is expected "
               "rather than a gap in the recording." if from_watch else ""),
            inherent=from_watch)
    from_watch = _from_watch(session)
    return Check(
        "channels", FAIR, "GPS only" + (" (as a watch is)" if from_watch else ""),
        "Everything is derived from position and speed. Lateral and "
        "longitudinal g are computed rather than measured, and brake release "
        "is inferred from deceleration, which drag also produces."
        + (" A watch has no access to the car, so this is expected."
           if from_watch else ""),
        inherent=from_watch)


def _corner_stability_check(session: Session, corners: Sequence[Corner]) -> Check:
    """Does corner detection agree with itself across the session's laps?"""
    laps = session.valid_laps or session.laps
    if len(laps) < 2:
        return Check("corner detection", FAIR, f"{len(corners)} corners",
                     "Only one lap, so detection could not be cross-checked.")
    counts = [len(detect_corners(l)) for l in laps]
    spread = max(counts) - min(counts)
    if spread == 0:
        grade, note = GOOD, "every lap resolves the same corners"
    elif spread <= 2:
        grade, note = FAIR, ("a marginal bend appears on some laps and not "
                             "others; corner numbering may shift")
    else:
        grade, note = POOR, ("detection disagrees markedly between laps, so "
                             "corner numbering is unreliable and cross-lap "
                             "comparisons may pair different corners")
    return Check(
        name="corner detection",
        grade=grade,
        summary=f"{min(counts)}–{max(counts)} corners across {len(laps)} laps",
        detail=note.capitalize() + ".")


def _satellite_check(session: Session) -> Optional[Check]:
    sats = session.vbo.channels.get("sats")
    if sats is None or not np.any(np.isfinite(sats)):
        return None
    low = float(np.percentile(sats, 5))
    median = float(np.median(sats))
    if low >= 8:
        return Check("gps fix", GOOD, f"{median:.0f} satellites typical",
                     "Position quality is good throughout.")
    grade = POOR if low < 5 else FAIR
    return Check(
        name="gps fix",
        grade=grade,
        summary=f"{median:.0f} satellites typical, {low:.0f} at worst",
        detail=("A weak fix widens position error, which shows up as curvature "
                "noise and therefore as unstable corner detection and lateral "
                "g."))


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------


def assess(session: Session, corners: Sequence[Corner] = (),
           units: UnitSystem = METRIC) -> Quality:
    """Everything worth knowing about how far to trust this session."""
    u = units
    q = Quality()
    rate_check, spacing = _rate_check(session, u)
    q.brake_point_precision_m = spacing
    q.checks.append(rate_check)
    mode = getattr(session, "mode", "laps")
    if mode == "laps":
        # circuit-only checks: straight-line runs have no timing line to close, and the logger
        # pausing between runs is not a dropout
        q.checks.append(_lap_count_check(session))
        q.checks.append(_timing_line_check(session))
        q.checks.append(_closure_check(session, u))
        q.checks.append(_continuity_check(session))
    elif mode == "p2p":
        # start and finish are different places, so there is no lap closure to check
        q.checks.append(_lap_count_check(session))
        q.checks.append(Check("start/finish", GOOD, "start and finish gates (point-to-point)",
                              "Each run is timed from the start gate to the finish gate."))
        q.checks.append(_continuity_check(session))
    junk = next((c for c in getattr(session.vbo, "comments", []) if "corrupted row" in c), None)
    if junk:
        q.checks.append(Check("file integrity", FAIR, junk.split(" (")[0],
                              "The log contains rows of binary junk — usually the logger losing power or "
                              "writing the wrong buffer mid-row. They were skipped; the samples either side "
                              "are joined by interpolation."))
    q.checks.append(_channels_check(session))
    if corners:
        q.checks.append(_corner_stability_check(session, corners))
    sat = _satellite_check(session)
    if sat is not None:
        q.checks.append(sat)
    return q


def significance_note(q: Quality, units: UnitSystem = METRIC) -> str:
    """One line on what size of difference is worth reading at all."""
    m = q.brake_point_precision_m
    if not np.isfinite(m) or m <= 0:
        return ""
    return (f"Differences smaller than about {units.d_s(m)} in a brake point "
            f"are below what this log rate can resolve.")
