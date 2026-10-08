"""
Burning the telemetry overlay onto onboard video.

Stage 2 of the video feature. The overlay renderer (``overlay.py``) produces
one transparent frame at a time; this module drives it across a whole clip and
composites the result over the footage with ffmpeg, producing a finished
landscape or portrait video.

How it works, and why:

* **ffmpeg does the heavy lifting.** It decodes the source video, and we feed
  it a second video stream of overlay frames on its stdin as raw RGBA. ffmpeg
  overlays the two and encodes the result. Rendering frames in Python and
  compositing in ffmpeg keeps the fast path (decode, scale, encode) in C while
  leaving the part that needs the telemetry — the overlay — in Python.

* **Frames are matched to the video's own clock.** Each output frame is at a
  known time into the clip; that time is mapped through the VideoSync to a
  moment of the lap, and the overlay is rendered for that moment. A frame with
  no telemetry behind it (before the lap starts, say) gets a fully transparent
  overlay, so the footage shows through untouched.

* **It runs as a job with progress.** Encoding a session is a minutes-long
  batch, so this is built to run off the UI thread and report progress a line
  at a time — a fraction and a human message — rather than blocking. The CLI
  prints those lines; a GUI could show them in a status field.

Nothing here needs a display, but the *result* can only really be judged by
watching it, so the pixel-level tests cover the frame timing and the overlay
content while the encode itself is exercised end-to-end on a short clip.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .overlay import OverlayData, OverlayLayout, OverlayRenderer, PIL_AVAILABLE
from .video import VideoSync


ProgressFn = Callable[[float, str], None]


def find_ffmpeg() -> Optional[str]:
    """Locate an ffmpeg binary: the system one first, then a bundled fallback.

    A system ffmpeg is preferred because it is the one the user's other tools
    use and is likely built with the codecs their footage needs. imageio-ffmpeg
    ships a static build that covers the common case when nothing is on PATH.
    """
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                                     # noqa: BLE001
        return None


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration_s: float


def probe(path: str, ffmpeg: Optional[str] = None) -> VideoInfo:
    """Read a clip's dimensions, frame rate and duration from ffmpeg output."""
    ff = ffmpeg or find_ffmpeg()
    if ff is None:
        raise RuntimeError("ffmpeg was not found")
    out = subprocess.run([ff, "-i", path], capture_output=True, text=True).stderr

    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    if m:
        h, mn, sec = m.groups()
        dur = int(h) * 3600 + int(mn) * 60 + float(sec)

    width = height = 0
    fps = 30.0
    v = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    if v:
        width, height = int(v.group(1)), int(v.group(2))
    f = re.search(r"([\d.]+)\s*fps", out)
    if f:
        fps = float(f.group(1))
    if not (width and height):
        raise RuntimeError(f"could not read video dimensions from {path}")
    return VideoInfo(width, height, fps, dur)


def grab_frame(path: str, at_s: float = 0.0, ffmpeg: Optional[str] = None):
    """Return a PIL image of the video frame at `at_s` seconds, or None.

    Used by the editor so overlays are positioned against the real footage
    rather than a stand-in. Decodes a single frame to PNG via ffmpeg and reads
    it back, so it needs no video-decoding library of its own.
    """
    if not PIL_AVAILABLE:
        return None
    ff = ffmpeg or find_ffmpeg()
    if ff is None:
        return None
    import subprocess
    import tempfile
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        out = tmp.name
    try:
        r = subprocess.run(
            [ff, "-y", "-ss", f"{max(0.0, at_s):.3f}", "-i", path,
             "-frames:v", "1", out],
            capture_output=True)
        if r.returncode != 0 or not os.path.getsize(out):
            return None
        return Image.open(out).convert("RGBA").copy()
    except Exception:                                     # noqa: BLE001
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


@dataclass
class ExportJob:
    """A burn-in export, runnable on a background thread with progress.

    The caller supplies a progress callback that receives a fraction in 0..1
    and a short message. It is invoked from the worker thread, so a GUI must
    marshal back to its own thread before touching widgets; the CLI just
    prints.
    """
    data: OverlayData
    sync: VideoSync
    layout: OverlayLayout
    source_video: str
    out_path: str
    #: which slice of the session to render, in session seconds. Defaults to the
    #: span the video covers, so a one-lap clip exports just that lap.
    start_session_t: Optional[float] = None
    end_session_t: Optional[float] = None
    crf: int = 20                       # visually lossless-ish, reasonable size
    preset: str = "medium"
    ffmpeg: Optional[str] = None

    _proc: Optional[subprocess.Popen] = None
    _cancel: bool = False

    # -- planning ----------------------------------------------------------

    def _resolve_span(self, info: VideoInfo) -> tuple[float, float, float, float]:
        """Work out which part of the clip to render, in both clocks.

        Returns (video_start_s, video_end_s, session_start_t, session_end_t).
        The overlay is only meaningful where the video covers the session, so
        the default span is the intersection of the clip and the lap.
        """
        lap = self.data.lap
        lap_lo = float(lap.t[0])
        lap_hi = float(lap.t[-1])
        s_lo = self.start_session_t if self.start_session_t is not None else lap_lo
        s_hi = self.end_session_t if self.end_session_t is not None else lap_hi

        # clamp to what the video actually contains
        v_lo = self.sync.video_ms(s_lo) / 1000.0
        v_hi = self.sync.video_ms(s_hi) / 1000.0
        v_lo = max(0.0, v_lo)
        v_hi = min(info.duration_s, v_hi) if info.duration_s > 0 else v_hi
        if v_hi <= v_lo:
            raise RuntimeError(
                "the video does not overlap the selected part of the session; "
                "line the video up first")
        return v_lo, v_hi, s_lo, s_hi

    # -- running -----------------------------------------------------------

    def run(self, progress: Optional[ProgressFn] = None) -> str:
        """Render and encode. Blocks; call on a worker thread for a live UI.

        Returns the output path on success, raises on failure or cancellation.
        """
        if not PIL_AVAILABLE:
            raise RuntimeError("the overlay needs Pillow: pip install pillow")
        ff = self.ffmpeg or find_ffmpeg()
        if ff is None:
            raise RuntimeError(
                "ffmpeg was not found. Install it (brew install ffmpeg, or "
                "apt install ffmpeg) or `pip install imageio-ffmpeg` for a "
                "bundled copy.")

        def say(frac: float, msg: str) -> None:
            if progress is not None:
                progress(max(0.0, min(1.0, frac)), msg)

        say(0.0, "Reading the video\u2026")
        info = probe(self.source_video, ff)
        # The output canvas takes the *layout's* aspect, not the source's, so a
        # portrait layout produces a vertical video even from landscape footage.
        # The source is scaled to fit the canvas width and centred; the
        # portrait layout's top and bottom bands are designed for exactly the
        # letterbox this leaves. The overlay is rendered at the canvas size so
        # it lands pixel-for-pixel.
        out_w, out_h = self._output_size(info)
        layout = self.layout.with_size(out_w, out_h)
        renderer = OverlayRenderer(self.data, layout)

        v_lo, v_hi, _s_lo, _s_hi = self._resolve_span(info)
        fps = info.fps or 30.0
        total = max(1, int(round((v_hi - v_lo) * fps)))

        os.makedirs(os.path.dirname(os.path.abspath(self.out_path)) or ".",
                    exist_ok=True)
        cmd = self._build_command(ff, out_w, out_h, v_lo, v_hi, fps)

        say(0.02, f"Rendering {total} frames\u2026")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)

        # a reader thread drains ffmpeg's stderr so its pipe never fills and
        # blocks the encode; the last lines are kept for a useful error
        tail: list[str] = []
        stderr = self._proc.stderr

        def drain() -> None:
            for raw in iter(stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    tail.append(line)
                    del tail[:-40]
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        try:
            for i in range(total):
                if self._cancel:
                    raise RuntimeError("export cancelled")
                video_t = v_lo + i / fps
                session_t = self._session_time_at(video_t)
                # lap.t is lap-relative (starts at 0); the renderer wants time
                # since the lap began, so subtract the lap's absolute start.
                # Subtracting lap.t[0] (which is 0) left lap_t as raw session
                # time — tens of thousands of seconds — which clamped every
                # frame to the lap's end and froze the whole overlay.
                lap_t = session_t - float(self.data.lap.t_start)
                frame = renderer.frame(lap_t)
                try:
                    self._proc.stdin.write(frame.tobytes())
                except BrokenPipeError:
                    break                          # ffmpeg died; report below
                if i % max(1, total // 100) == 0:
                    say(0.02 + 0.96 * (i / total),
                        f"Rendering frame {i + 1} of {total}")
            if self._proc.stdin:
                self._proc.stdin.close()
        finally:
            code = self._proc.wait()
            reader.join(timeout=1.0)

        if self._cancel:
            self._cleanup_partial()
            raise RuntimeError("export cancelled")
        if code != 0:
            self._cleanup_partial()
            raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail[-12:]))
        say(1.0, "Done")
        return self.out_path

    def cancel(self) -> None:
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    # -- helpers -----------------------------------------------------------

    def _session_time_at(self, video_t: float) -> float:
        """Inverse of the sync: which session moment a video time shows.

        The sync maps session -> video; here we need video -> session for the
        frame at hand. For a manual (constant-offset) sync that is just adding
        the offset back; for an embedded sync it is a lookup, done by inverting
        the monotonic mapping.
        """
        if self.sync.kind == "offset":
            return video_t + self.sync.offset_s
        # embedded: video_ms is monotonic in session time, so interpolate back
        t = self.sync._t
        ms = self.sync._ms
        return float(np.interp(video_t * 1000.0, ms, t))

    def _output_size(self, info: VideoInfo) -> tuple[int, int]:
        """Output pixel size, at the layout's aspect.

        When the layout aspect matches the source (landscape layout, landscape
        clip) the source dimensions are kept. When they differ (portrait layout
        from a landscape clip) the output takes the layout's aspect at a height
        matching the source, so nothing is upscaled, and both dimensions are
        made even for H.264.
        """
        layout_aspect = self.layout.width / self.layout.height
        source_aspect = info.width / info.height
        if abs(layout_aspect - source_aspect) < 0.01:
            w, h = info.width, info.height
        elif layout_aspect < 1.0:
            # portrait canvas: keep the source height, width from the aspect
            h = info.height
            w = int(round(h * layout_aspect))
        else:
            # landscape canvas from a taller source: keep the source width
            w = info.width
            h = int(round(w / layout_aspect))
        return (w - w % 2, h - h % 2)

    def _build_command(self, ff: str, out_w: int, out_h: int,
                        v_lo: float, v_hi: float, fps: float) -> list:
        """Assemble the ffmpeg command that composites piped frames over video.

        The source is scaled to fit an out_w x out_h canvas without distortion
        and padded (letterboxed) to fill it, so a landscape clip in a portrait
        canvas keeps its shape with bands above and below rather than being
        stretched. The overlay arrives on stdin as raw RGBA at the canvas size;
        ffmpeg composites and encodes H.264 + AAC.
        """
        # Two ways to place the source in the canvas:
        #   fit  -> scale the whole frame down and letterbox (pad) the rest
        #   fill -> scale up to cover, then crop to the canvas at the chosen
        #           offset, so part of the frame fills the screen edge-to-edge
        if self.layout.video_fit == "fill":
            cx = max(0.0, min(1.0, self.layout.crop_x))
            cy = max(0.0, min(1.0, self.layout.crop_y))
            base = (
                f"[0:v]setpts=PTS-STARTPTS,"
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                # crop x/y: (scaled - out) * offset, clamped by ffmpeg to valid
                f"crop={out_w}:{out_h}:(iw-{out_w})*{cx:.4f}:(ih-{out_h})*{cy:.4f}"
                f"[base];")
        else:
            base = (
                f"[0:v]setpts=PTS-STARTPTS,"
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black[base];")
        return [
            ff, "-y",
            "-ss", f"{v_lo:.3f}", "-to", f"{v_hi:.3f}", "-i", self.source_video,
            "-f", "rawvideo", "-pixel_format", "rgba",
            "-video_size", f"{out_w}x{out_h}",
            "-framerate", f"{fps:g}", "-i", "pipe:0",
            "-filter_complex",
            base
            + "[1:v]setpts=PTS-STARTPTS[ov];"
            "[base][ov]overlay=format=auto:shortest=1[out]",
            "-map", "[out]", "-map", "0:a?",          # keep audio if present
            "-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
            self.out_path,
        ]

    def _cleanup_partial(self) -> None:
        try:
            if os.path.exists(self.out_path):
                os.remove(self.out_path)
        except OSError:
            pass


def reveal_in_file_manager(path: str) -> bool:
    """Open the OS file manager with the exported file selected.

    macOS reveals it in Finder; Windows selects it in Explorer; Linux opens the
    containing folder, since selecting a file is not portable there. Returns
    whether a reveal was attempted.
    """
    path = os.path.abspath(path)
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", path], check=False)
        elif sys.platform.startswith("win"):
            subprocess.run(["explorer", "/select,", path], check=False)
        else:
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            subprocess.run(["xdg-open", folder], check=False)
        return True
    except Exception:                                     # noqa: BLE001
        return False
