"""
Linking a .vbo to its video.

Two sync mechanisms, in order of preference:

1. **Embedded** — VBOX HD2 and similar log an `avitime` column giving the
   offset into the video in milliseconds for every sample, alongside an
   `avifileindex` naming which file of a split recording it lands in. That is
   exact, per sample, and needs no guessing: the logger wrote the video, so it
   knows where each fix falls in it.

2. **Manual offset** — for a GoPro or phone recording with no embedded sync,
   one number: the session time at which the video starts. Everything else
   follows from it. The offset is adjustable because getting it right is a
   human judgement (line up a visual landmark with the map cursor), not
   something the data can tell us.

The mapping is deliberately separate from any Qt code so it can be tested
without a media backend, a display, or a video file.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .parser import VboFile

VIDEO_EXTS = (".mp4", ".avi", ".mov", ".m4v", ".mkv", ".mts", ".m2ts")


# --------------------------------------------------------------------------
# Finding the file
# --------------------------------------------------------------------------


def candidate_names(base: str, ext: Optional[str], index: int = 1) -> List[str]:
    """Filenames a logger might have used for `base`, most likely first."""
    exts = [ext] if ext else [e.lstrip(".") for e in VIDEO_EXTS]
    out: List[str] = []
    for e in exts:
        out.append(f"{base}{index:04d}.{e}")
        out.append(f"{base}.{e}")
        out.append(f"{base}{index}.{e}")
    return out


def declares_video(vbo: VboFile) -> bool:
    """Whether this .vbo claims a video at all.

    Either an `[avi]` header section naming the file, or an `avitime` column
    that only exists because the logger was recording one.
    """
    if vbo.video_base:
        return True
    ms = vbo.channels.get("avitime")
    try:
        import numpy as _np
        return ms is not None and bool(_np.any(_np.isfinite(ms)))
    except Exception:                                    # noqa: BLE001
        return ms is not None


def find_video(vbo_path: str, vbo: VboFile, index: int = 1) -> Optional[str]:
    """Locate the video for a .vbo, searching beside it.

    Only ever returns a file this .vbo actually points at. A session with no
    video reference gets None even when the folder is full of video — pairing
    an unrelated clip to a lap is worse than showing no video at all, because
    it looks right and silently lies about where you were on track.
    """
    folder = os.path.dirname(os.path.abspath(vbo_path))
    base = vbo.video_base

    if base:
        for name in candidate_names(base, vbo.video_ext, index):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path
        # stem as a prefix, e.g. "STEM_" -> "STEM_0001.mp4"
        for hit in sorted(glob.glob(os.path.join(folder, base + "*"))):
            if hit.lower().endswith(VIDEO_EXTS):
                return hit

    # A video named exactly like the .vbo is a deliberate pairing, not a guess,
    # so it counts even when the header says nothing.
    stem = os.path.splitext(os.path.basename(vbo_path))[0]
    for e in VIDEO_EXTS:
        path = os.path.join(folder, stem + e)
        if os.path.isfile(path):
            return path

    if not declares_video(vbo):
        return None

    # The file says a video exists but naming did not match. One video in the
    # folder is then a reasonable resolution; several is a coin toss.
    try:
        vids = [f for f in sorted(os.listdir(folder))
                if f.lower().endswith(VIDEO_EXTS)]
    except OSError:
        return None
    if len(vids) == 1:
        return os.path.join(folder, vids[0])
    return None


# --------------------------------------------------------------------------
# Time mapping
# --------------------------------------------------------------------------


@dataclass
class VideoSync:
    """Maps session time (seconds) to a position in the video (milliseconds)."""
    kind: str                       # 'embedded' | 'offset'
    #: session time at which the video begins, for kind='offset'
    offset_s: float = 0.0
    #: sorted session times and matching video ms, for kind='embedded'
    _t: Optional[np.ndarray] = None
    _ms: Optional[np.ndarray] = None
    _index: Optional[np.ndarray] = None
    duration_ms: Optional[float] = None
    #: set when the offset came from GoPro GPS telemetry (gopro.py): what synced it, and the synced offset
    gps_note: str = ""
    gps_offset: Optional[float] = None

    # ---- construction -----------------------------------------------------
    @classmethod
    def from_vbo(cls, vbo: VboFile) -> "VideoSync":
        t = vbo.channels.get("t")
        ms = vbo.channels.get("avitime")
        if t is None or ms is None or not np.any(np.isfinite(ms)):
            return cls(kind="offset", offset_s=float(t[0]) if t is not None else 0.0)

        ms = np.asarray(ms, dtype=float)
        idx = vbo.channels.get("avifileindex")
        index = None if idx is None else np.asarray(idx, dtype=float)

        # Samples recorded before the camera started carry a sentinel: a
        # negative sync time, and a file index of zero. They are not video
        # positions and must not be interpolated between — a recording that
        # began four minutes into a session would otherwise be stretched
        # across the whole of it, and every seek would land on black.
        valid = np.isfinite(ms) & (ms >= 0)
        if index is not None:
            valid &= index >= 1
        if np.count_nonzero(valid) < 2:
            return cls(kind="offset", offset_s=float(t[0]))

        t = np.asarray(t, dtype=float)[valid]
        ms = ms[valid]
        index = index[valid] if index is not None else None
        order = np.argsort(t, kind="stable")
        return cls(kind="embedded", _t=t[order], _ms=ms[order],
                   _index=(index[order] if index is not None else None))

    @classmethod
    def manual(cls, video_start_session_time: float) -> "VideoSync":
        return cls(kind="offset", offset_s=float(video_start_session_time))

    @classmethod
    def from_gopro(cls, video_path: str, vbo: VboFile) -> Optional["VideoSync"]:
        """Exact sync from GoPro GPS telemetry — the clip's own metadata track, or (if that was stripped)
        a same-name / GoPro GL….LRV proxy — matched to the log by UTC and speed. None if there is no
        usable GPS or it doesn't overlap the log."""
        t, v = vbo.channels.get("t"), vbo.channels.get("speed")
        if t is None or v is None:
            return None
        from .gopro import sync_video
        r = sync_video(video_path, t, v)
        if r is None:
            return None
        return cls(kind="offset", offset_s=r.offset_s, gps_note=r.describe(), gps_offset=r.offset_s)

    # ---- queries ----------------------------------------------------------
    @property
    def exact(self) -> bool:
        return self.kind == "embedded" or bool(self.gps_note)

    def file_index(self, session_t: float) -> int:
        if self.kind != "embedded" or self._index is None:
            return 1
        i = int(np.clip(np.searchsorted(self._t, session_t), 0,
                        len(self._index) - 1))
        return int(self._index[i])

    @property
    def covered_session_span(self) -> Optional[Tuple[float, float]]:
        """Session times the video actually spans, for an embedded sync."""
        if self.kind != "embedded" or self._t is None or self._t.size == 0:
            return None
        return float(self._t[0]), float(self._t[-1])

    def video_ms(self, session_t: float) -> float:
        """Position in the video, in milliseconds.

        Extrapolated linearly outside the recorded window rather than clamped,
        so a caller can tell how far outside it is; `covers()` is the check for
        whether there is anything to see.
        """
        if self.kind == "embedded":
            t, ms = self._t, self._ms
            if session_t < t[0]:
                return float(ms[0] + (session_t - t[0]) * 1000.0)
            if session_t > t[-1]:
                return float(ms[-1] + (session_t - t[-1]) * 1000.0)
            return float(np.interp(session_t, t, ms))
        return (float(session_t) - self.offset_s) * 1000.0

    def session_t(self, video_ms: float) -> float:
        """Inverse mapping, for scrubbing from the video side."""
        if self.kind == "embedded":
            # _ms is monotonic within a file for any sane recording
            return float(np.interp(video_ms, self._ms, self._t))
        return self.offset_s + float(video_ms) / 1000.0

    def covers(self, session_t: float, tol_ms: float = 500.0) -> bool:
        """Whether there is footage for this moment of the session.

        A camera is often started well after the logger — RaceChrono will
        happily record four minutes of driving before the video begins — so
        this is a real question rather than a formality.
        """
        span = self.covered_session_span
        if span is not None:
            lo, hi = span
            tol = tol_ms / 1000.0
            if session_t < lo - tol or session_t > hi + tol:
                return False
        ms = self.video_ms(session_t)
        if ms < -tol_ms:
            return False
        if self.duration_ms is not None and ms > self.duration_ms + tol_ms:
            return False
        return True

    def nudge(self, delta_s: float) -> None:
        """Adjust the alignment by `delta_s`, as the buttons see it.

        Positive advances the footage: press +1s and the frame shown for a
        given moment jumps one second later into the clip, which is what a "+"
        on a video is expected to do. Internally that means the video is
        treated as having started one second *earlier* relative to the data,
        so the offset moves the other way — the sign flip lives here, once, so
        the button and the frame agree.
        """
        if self.kind == "offset":
            self.offset_s -= float(delta_s)
        else:
            self._ms = self._ms + delta_s * 1000.0

    def describe_relative(self, session_start: Optional[float]) -> str:
        """The alignment in terms a person can act on.

        Phrased as how far the footage is shifted, matching the buttons: "+"
        advances the video, and the readout counts up with it. Stating the
        absolute `offset_s` instead would show a five-figure time-of-day
        number that never appears to change.
        """
        if self.kind == "embedded" or self.gps_note:
            return self.describe()
        if session_start is None:
            return self.describe()
        # how much later into the clip a given session moment now falls
        shift = session_start - self.offset_s
        if abs(shift) < 0.05:
            return "video aligned with the data"
        if shift > 0:
            return f"video shifted {shift:+.1f}s later"
        return f"video shifted {shift:.1f}s earlier"

    def describe(self) -> str:
        if self.gps_note:
            moved = (self.gps_offset or 0.0) - self.offset_s
            return self.gps_note + (f", nudged {moved:+.2f}s" if abs(moved) >= 0.005 else "")
        if self.kind == "embedded":
            span = (self._ms[-1] - self._ms[0]) / 1000.0
            return f"embedded sync ({span:.0f}s of video)"
        return f"manual sync, video starts at session t={self.offset_s:.1f}s"
