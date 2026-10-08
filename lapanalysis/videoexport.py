"""
Burning the telemetry overlay onto onboard video.

This is the one part of the pipeline that cannot be verified without a real
video and a real encoder, so it is kept deliberately small and its moving parts
are isolated: finding a usable ffmpeg, rendering overlay frames, feeding them to
ffmpeg, and reporting progress. The overlay drawing itself lives in
``overlay.py`` and is tested exhaustively off-screen; here we only arrange the
plumbing around it.

How it works:

* ffmpeg reads two inputs — the source video, and a stream of raw RGBA overlay
  frames piped in on stdin — and composites the second over the first.
* One overlay frame is rendered per output frame, at the video's own frame
  rate, at the moment of the lap that frame corresponds to. The lap-to-video
  mapping is the same :class:`VideoSync` the interactive player uses, so the
  burned-in overlay lines up exactly the way the preview did.
* ffmpeg is asked for machine-readable progress on a separate pipe, which is
  parsed into a fraction and handed to a callback. The whole thing runs on a
  worker thread so a caller — the CLI — stays free to print status.

Nothing here touches the GUI. A render of a full session is a minutes-long
batch job and belongs on the command line, not tied to an interactive window
that would freeze for the duration.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .overlay import (OverlayData, OverlayLayout, OverlayRenderer, PIL_AVAILABLE,
                      landscape, portrait)
from .video import VideoSync


def find_ffmpeg() -> Optional[str]:
    """Locate an ffmpeg binary: a system one first, then imageio's bundle.

    Preferring the system binary keeps a user in control of their codecs;
    falling back to the bundled one means the feature still works on a machine
    that has none installed.
    """
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                                     # noqa: BLE001
        return None


def probe_video(path: str, ffmpeg: str) -> dict:
    """Frame rate, dimensions and duration of a video, via ffmpeg's own output.

    ffprobe is not always shipped alongside ffmpeg, so this reads what it needs
    from ``ffmpeg -i`` stderr rather than assuming a second binary.
    """
    result = subprocess.run([ffmpeg, "-i", path],
                            capture_output=True, text=True)
    text = result.stderr
    info: dict = {"fps": 30.0, "width": 1920, "height": 1080,
                  "duration_s": None}

    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if m:
        info["fps"] = float(m.group(1))
    m = re.search(r"(\d{2,5})x(\d{2,5})", text)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        h, mm, ss = m.groups()
        info["duration_s"] = int(h) * 3600 + int(mm) * 60 + float(ss)
    return info


@dataclass
class ExportSpec:
    """Everything a burn-in needs, resolved before any work starts."""
    video_in: str
    video_out: str
    data: OverlayData
    sync: VideoSync
    layout: OverlayLayout
    fps: float
    width: int
    height: int
    #: session-time window to render; None means the covered span of the video
    start_s: Optional[float] = None
    end_s: Optional[float] = None
    crf: int = 20                    # visually lossless-ish, sane file size
    ffmpeg: str = ""

    @property
    def total_frames(self) -> int:
        span = (self.end_s or 0.0) - (self.start_s or 0.0)
        return max(1, int(round(span * self.fps)))


def build_spec(video_in: str, video_out: str, analysis, sync: VideoSync,
               *, orientation: str = "landscape", units=None,
               lap_only: bool = True) -> ExportSpec:
    """Resolve an :class:`ExportSpec` from an analysis and a video.

    By default the render is trimmed to the lap being analysed rather than the
    whole clip: an onboard file is usually one flying lap plus an in- and out-
    lap nobody wants burned. Pass ``lap_only=False`` for the full clip.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Video overlay export needs Pillow: "
                           "pip install pillow")
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError(
            "No ffmpeg found. Install it from https://ffmpeg.org or "
            "`pip install imageio-ffmpeg` for a bundled copy.")

    info = probe_video(video_in, ffmpeg)
    from .units import METRIC
    data = OverlayData.from_analysis(analysis, units or METRIC)
    layout_fn = portrait if orientation == "portrait" else landscape
    layout = layout_fn(info["width"], info["height"])

    lap = analysis.lap
    if lap_only:
        start = float(lap.t_start)
        end = float(lap.t_start + data.lap_time)
    else:
        span = sync.covered_session_span
        if span is not None:
            start, end = span
        else:
            start = float(lap.t_start)
            end = start + (info["duration_s"] or data.lap_time)

    return ExportSpec(
        video_in=video_in, video_out=video_out, data=data, sync=sync,
        layout=layout, fps=info["fps"], width=info["width"],
        height=info["height"], start_s=start, end_s=end, ffmpeg=ffmpeg)


class ExportJob:
    """Runs one burn-in on a background thread, reporting progress.

    The caller starts the job and polls :attr:`status` / :attr:`progress`, or
    passes an ``on_progress`` callback. Cancellation is cooperative: the frame
    loop checks a flag between frames, so a cancel takes effect within one
    frame rather than leaving a runaway encoder.
    """

    def __init__(self, spec: ExportSpec,
                 on_progress: Optional[Callable[[float, str], None]] = None):
        self.spec = spec
        self._on_progress = on_progress
        self.progress = 0.0
        self.status = "idle"
        self.error: Optional[str] = None
        self.done = False
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None

    # -- control -----------------------------------------------------------

    def start(self) -> "ExportJob":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _report(self, fraction: float, message: str) -> None:
        self.progress = float(np.clip(fraction, 0.0, 1.0))
        self.status = message
        if self._on_progress is not None:
            self._on_progress(self.progress, message)

    # -- the work ----------------------------------------------------------

    def _ffmpeg_command(self) -> list:
        s = self.spec
        # input 0: the source video, trimmed to the render window
        # input 1: raw RGBA overlay frames on a pipe, same size and rate
        start = s.start_s or 0.0
        video_start = max(0.0, s.sync.video_ms(start) / 1000.0)
        duration = ((s.end_s or 0.0) - start)
        return [
            s.ffmpeg, "-y",
            "-ss", f"{video_start:.3f}", "-i", s.video_in,
            "-f", "rawvideo", "-pix_fmt", "rgba",
            "-s", f"{s.width}x{s.height}", "-r", f"{s.fps}",
            "-i", "pipe:0",
            "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto[v]",
            "-map", "[v]", "-map", "0:a?",
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", str(s.crf),
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-progress", "pipe:2", "-nostats", "-loglevel", "error",
            s.video_out,
        ]

    def _run(self) -> None:
        s = self.spec
        try:
            self._report(0.0, "starting ffmpeg")
            renderer = OverlayRenderer(s.data, s.layout)
            self._proc = subprocess.Popen(
                self._ffmpeg_command(),
                stdin=subprocess.PIPE, stderr=subprocess.PIPE)

            total = s.total_frames
            start = s.start_s or 0.0
            lap_start = float(s.data.lap.t_start)
            for i in range(total):
                if self._cancel.is_set():
                    self._report(self.progress, "cancelled")
                    self._proc.stdin.close()
                    self._proc.terminate()
                    self.done = True
                    return
                session_t = start + i / s.fps
                frame = renderer.frame(session_t - lap_start)
                try:
                    self._proc.stdin.write(frame.tobytes())
                except BrokenPipeError:
                    break                     # ffmpeg exited; read its error
                if i % 15 == 0:
                    # rendering is the measurable half; encoding trails it and
                    # finishes in the flush, so cap the render phase at 95%
                    self._report(0.95 * (i / total),
                                 f"rendering frame {i:,} of {total:,}")

            if self._proc.stdin:
                self._proc.stdin.close()
            self._report(0.97, "finishing encode")
            _out, err = self._proc.communicate()
            if self._proc.returncode not in (0, None) and not self._cancel.is_set():
                self.error = (err.decode("utf-8", "replace").strip()
                              or f"ffmpeg exited {self._proc.returncode}")
                self._report(self.progress, "failed")
                self.done = True
                return
            self._report(1.0, "done")
        except Exception as exc:                          # noqa: BLE001
            self.error = str(exc)
            self._report(self.progress, "failed")
        finally:
            self.done = True


def reveal_in_file_manager(path: str) -> bool:
    """Open the platform file manager with `path` selected.

    Best-effort and silent on failure: a finished export is still a finished
    export if the reveal did not work.
    """
    path = os.path.abspath(path)
    try:
        import sys
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", path], check=False)
        elif sys.platform.startswith("win"):
            subprocess.run(["explorer", "/select,", path], check=False)
        else:
            # Linux: no universal "select" verb, so open the containing folder
            folder = os.path.dirname(path)
            subprocess.run(["xdg-open", folder], check=False)
        return True
    except Exception:                                     # noqa: BLE001
        return False
