"""
Synced video playback.

Degrades in stages rather than failing: no QtMultimedia -> an explanatory
panel; no video file found -> a button to pick one; no embedded sync -> a
manual offset with nudge controls.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6 import QtCore, QtWidgets

from .jumpslider import JumpSlider

from ..video import VideoSync

try:                                                    # pragma: no cover
    from PySide6.QtGui import QPainter
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
    MULTIMEDIA = True
except Exception:                                       # pragma: no cover
    MULTIMEDIA = False

#: How far the player may drift from the target before it is re-seeked.
#:
#: Generous on purpose. While playing, the playhead follows the *video* clock
#: rather than the other way round, so the two cannot diverge and this should
#: essentially never fire. It exists for the cases that can still knock them
#: apart — a scrub, a lap change, a rate change — where one correction is
#: right and a steady trickle of them is what you hear as skipping.
DRIFT_TOLERANCE_MS = 1200.0


class VideoPanel(QtWidgets.QWidget):
    """Video view locked to the distance cursor."""

    #: a stationary click on the picture, for play/pause
    clicked = QtCore.Signal()

    CLICK_SLOP_PX = 4

    def __init__(self) -> None:
        super().__init__()
        self._press_pos = None
        self.sync: Optional[VideoSync] = None
        self.path: Optional[str] = None
        self._playing = False
        self._suppress = False
        self._loaded = False
        self._muted = True
        #: True when the transport is taking its position from this player
        self._following = False
        #: whether the footage reaches the moment last asked for
        self._covered: Optional[bool] = True
        #: session time of the first sample, for phrasing coverage messages
        self._session_start: Optional[float] = None
        #: True until the first real frame has been forced after a load
        self._pending_first_frame = False
        #: True while showing a frame the user scrubbed to, which the cursor
        #: should not immediately override
        self._held_scrub = False
        #: True while a play-a-beat repaint step is in flight, so overlapping
        #: decode requests during a fast drag cannot leave the player running
        self._decoding = False
        #: returns the session time the current lap begins at, for aligning a
        #: clip that was filmed on a camera the log knows nothing about
        self._lap_start: Optional[Callable[[], Optional[float]]] = None
        #: returns the session time the data cursor is at
        self._cursor_time: Optional[Callable[[], Optional[float]]] = None
        #: True while the user is dragging the video's own scrubber
        self._scrubbing = False

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.message = QtWidgets.QLabel()
        self.message.setAlignment(QtCore.Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color:#6b7280; padding:24px;")
        lay.addWidget(self.message, 1)

        if MULTIMEDIA:
            # A QGraphicsVideoItem in a plain raster QGraphicsView, rather than
            # a QVideoWidget. QVideoWidget takes a native window on most
            # platforms, and a native window in the same top-level as the 3D
            # view's QOpenGLWidget fights over the compositing path: the video
            # goes black or stutters whenever the GL view is visible. The
            # graphics-scene route renders through the normal paint pipeline
            # and coexists with GL.
            self._scene = QtWidgets.QGraphicsScene(self)
            self.video = QtWidgets.QGraphicsView(self._scene)
            self.video.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.video.setStyleSheet("background:#0e1013; border:0;")
            self.video.setRenderHints(QPainter.SmoothPixmapTransform)
            self.video.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.video.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.video.setMinimumHeight(80)
            self._item = QGraphicsVideoItem()
            self._scene.addItem(self._item)

            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.audio.setVolume(0.0)          # muted until asked; engine noise
            self.player.setVideoOutput(self._item)          # over a data trace
            self.player.errorOccurred.connect(self._on_error)
            self.player.durationChanged.connect(self._on_duration)
            self.player.playbackStateChanged.connect(self._on_state)
            self.player.mediaStatusChanged.connect(self._on_media_status)
            self._item.nativeSizeChanged.connect(lambda *_: self._fit())
            # the graphics view swallows mouse events, so watch its viewport
            self.video.viewport().installEventFilter(self)
            lay.addWidget(self.video, 5)
            self.video.hide()
        else:
            self.video = None
            self.player = None
            self._item = None

        # ---- controls
        # Two rows, because seven controls in one line set the minimum width
        # of the whole application. The window fitting a laptop screen matters
        # more than the buttons sitting on one line.
        controls = QtWidgets.QVBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(2)
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(4, 0, 4, 0)
        bar.setSpacing(3)
        self.open_btn = QtWidgets.QPushButton("Open…")
        self.open_btn.setMaximumWidth(64)
        self.open_btn.setToolTip("Pair a video with this session")
        self.open_btn.clicked.connect(self._pick_file)
        bar.addWidget(self.open_btn)

        self.mute_btn = QtWidgets.QPushButton("🔇")
        self.mute_btn.setToolTip("Audio")
        self.mute_btn.setMaximumWidth(38)
        self.mute_btn.clicked.connect(self._toggle_audio)
        bar.addWidget(self.mute_btn)

        # A video scrubber, active while paused. Without a way to move the
        # video on its own there is no way to say "this frame is that moment":
        # the video follows the cursor, so any attempt to align them is
        # circular.
        self.position = JumpSlider(QtCore.Qt.Horizontal)
        self.position.setRange(0, 1000)
        self.position.setToolTip(
            "Scrub the video on its own, then press Sync to tie the frame you "
            "are looking at\nto where the data cursor is.")
        self.position.sliderPressed.connect(self._scrub_begin)
        self.position.sliderMoved.connect(self._scrub_move)
        self.position.sliderReleased.connect(self._scrub_end)
        # A repeating timer, so the picture keeps up during a continuous drag.
        # A single-shot restarted on every move only ever fired once the drag
        # stopped, which read as the video responding only to large jumps.
        self._scrub_repaint = QtCore.QTimer(self)
        self._scrub_repaint.setInterval(80)
        self._scrub_repaint.timeout.connect(self._decode_current_frame)
        bar.addWidget(self.position, 1)

        self.sync_btn = QtWidgets.QPushButton("Sync")
        # sized for the wider confirmation word it briefly shows, so "Synced"
        # is not clipped to "Synce…"
        self.sync_btn.setMinimumWidth(64)
        self.sync_btn.setMaximumWidth(72)
        self.sync_btn.setToolTip(
            "Line the video up with the data.\n\n"
            "1. Drag the bar above to a moment you recognise in the video "
            "\u2014 or leave it at the\n   start if the clip begins at a "
            "lap.\n"
            "2. Click that same moment on a trace to put the cursor there "
            "\u2014 or select the lap.\n"
            "3. Press Sync.\n\n"
            "One alignment holds for the whole session; the offset buttons "
            "fine-tune it.")
        self.sync_btn.clicked.connect(self.sync_here)
        bar.addWidget(self.sync_btn)

        controls.addLayout(bar)
        bar = QtWidgets.QHBoxLayout()      # third row: sync state and offset
        bar.setContentsMargins(4, 0, 4, 0)
        bar.setSpacing(3)

        self.sync_label = QtWidgets.QLabel("")
        self.sync_label.setStyleSheet("color:#5a616e;")
        # let the filename shrink rather than forcing the window wider
        self.sync_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                      QtWidgets.QSizePolicy.Preferred)
        bar.addWidget(self.sync_label, 1)
        bar.addStretch(1)

        self.nudge_caption = QtWidgets.QLabel("offset")
        self.nudge_caption.setStyleSheet(
            "color:#5a616e; font-size:10px; letter-spacing:1px;")
        self.nudge_caption.setSizePolicy(QtWidgets.QSizePolicy.Fixed,
                                         QtWidgets.QSizePolicy.Preferred)
        bar.addWidget(self.nudge_caption)
        self.nudge_btns = []
        for label, delta in (("−1s", -1.0), ("−0.1", -0.1),
                             ("+0.1", 0.1), ("+1s", 1.0)):
            b = QtWidgets.QPushButton(label)
            b.setMaximumWidth(42)
            b.setMinimumWidth(28)
            b.clicked.connect(lambda _=False, d=delta: self._nudge(d))
            bar.addWidget(b)
            self.nudge_btns.append(b)
        controls.addLayout(bar)
        lay.addLayout(controls)

        self._set_message("No video loaded.")

    def eventFilter(self, obj, event):                    # noqa: N802
        if self.video is not None and obj is self.video.viewport():
            if event.type() == QtCore.QEvent.MouseButtonPress:
                self._press_pos = event.position().toPoint()
            elif event.type() == QtCore.QEvent.MouseButtonRelease:
                start, self._press_pos = self._press_pos, None
                if (start is not None
                        and event.button() == QtCore.Qt.LeftButton
                        and (event.position().toPoint() - start).manhattanLength()
                        <= self.CLICK_SLOP_PX):
                    self.clicked.emit()
        return super().eventFilter(obj, event)

    # ---------------------------------------------------------------- layout
    def _fit(self) -> None:
        """Scale the video item to the view, preserving aspect ratio."""
        if not MULTIMEDIA or self._item is None:
            return
        native = self._item.nativeSize()
        if not native.isValid() or native.width() <= 0:
            return
        avail = self.video.viewport().size()
        if avail.width() <= 0 or avail.height() <= 0:
            return
        scale = min(avail.width() / native.width(),
                    avail.height() / native.height())
        self._item.setSize(native * scale)
        rect = self._item.boundingRect()
        self._scene.setSceneRect(rect)
        self.video.centerOn(self._item)

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self._fit()

    # ------------------------------------------------------------------ setup
    def _set_message(self, text: str, show_video: bool = False) -> None:
        self.message.setText(text)
        self.message.setVisible(not show_video)
        if self.video is not None:
            self.video.setVisible(show_video)

    def load(self, path: Optional[str], sync: Optional[VideoSync],
             session_start: Optional[float] = None) -> None:
        self.sync = sync
        self.path = path
        self._session_start = session_start
        self._covered = True
        if not MULTIMEDIA:
            self._set_message(
                "Video playback needs Qt Multimedia, which isn't available in "
                "this install.\n\npip install PySide6 --upgrade")
            return
        if not path:
            if self.player is not None:
                self.player.stop()
                self.player.setSource(QtCore.QUrl())
            self._loaded = False
            self._set_message(
                "No video found next to this .vbo.\n\n"
                "Use Open video… to pick one. If the file has no embedded "
                "sync, line it up with the offset buttons.")
            return
        self.player.setSource(QtCore.QUrl.fromLocalFile(os.path.abspath(path)))
        self._apply_volume()
        self._loaded = True
        self._set_message("", show_video=True)
        self._describe()
        self._show_nudge(sync is None or not sync.exact)
        # Setting a source leaves the player parked at 0 and undecoded, which
        # renders as a black rectangle until something forces a frame. Seek to
        # where the cursor already is — usually the start of a lap — and prod
        # it into decoding, so the panel shows the track rather than black the
        # moment a video opens.
        # setSource is asynchronous: the media is not decodable yet, so seeking
        # now does nothing and the panel stays black until the first play. Flag
        # that a frame is wanted and let _on_media_status force it once the
        # media is actually loaded. Also try immediately, in case it was cached
        # and is already loaded.
        self._pending_first_frame = True
        self._force_first_frame()

    def _show_nudge(self, visible: bool) -> None:
        # An embedded sync should not need adjusting, but converters do get it
        # a frame or two out, so the controls stay available — just quieter.
        for w in (self.nudge_caption,):
            w.setVisible(True)
        self.nudge_caption.setText("offset" if visible else "trim")

    def set_lap_start_provider(self, fn) -> None:
        self._lap_start = fn

    def set_cursor_time_provider(self, fn) -> None:
        self._cursor_time = fn

    # ---------------------------------------------------------- alignment
    def _scrub_begin(self) -> None:
        self._scrubbing = True

    def _scrub_move(self, value: int) -> None:
        if not MULTIMEDIA or self.player is None:
            return
        duration = self.player.duration()
        if duration <= 0:
            return
        self._set_message("", show_video=True)
        self._covered = True
        self.player.setPosition(int(duration * value / 1000.0))
        # A paused player will not repaint on setPosition alone. Keep a
        # repeating decode running while the drag is live; it is stopped in
        # _scrub_end. start() on an active timer is a no-op, so this does not
        # reset it.
        if not self._scrub_repaint.isActive():
            self._scrub_repaint.start()

    def _scrub_end(self) -> None:
        self._scrubbing = False
        self._scrub_repaint.stop()
        # Hold the frame the scrub landed on rather than snapping straight back
        # to where the data cursor is. The whole point of scrubbing is to look
        # at a moment in the video *without* the cursor, so that you can then
        # line the two up; jumping back the instant the slider is released
        # makes alignment impossible. The next cursor move (a click, a nudge,
        # playback) takes over again.
        self._held_scrub = True
        self._decode_current_frame()

    def sync_here(self) -> None:
        """Tie the frame on screen to where the data cursor is.

        The general alignment tool, and the only one that works for a clip of
        a whole session: pick any moment you recognise in the video, put the
        cursor at the same moment on the trace, and the offset follows. One
        alignment holds for the rest of the session.
        """
        if not MULTIMEDIA or self.player is None or self._cursor_time is None:
            return
        at = self._cursor_time()
        if at is None:
            return
        video_s = self.player.position() / 1000.0
        self.sync = VideoSync.manual(float(at) - video_s)
        if self.player.duration() > 0:
            self.sync.duration_ms = float(self.player.duration())
        self._covered = None
        self._held_scrub = False
        self._describe()
        self._set_coverage(float(at))
        self._flash_synced()

    def align_to_lap(self) -> None:
        """Anchor the video's first frame to the start of the current lap.

        Used as the opening assumption when a clip is picked by hand — a clip
        of one lap usually starts at that lap — after which Sync or the offset
        buttons refine it. No longer its own button: one alignment control
        (Sync) is less to explain than two that overlap.
        """
        if self._lap_start is None or self.sync is None:
            return
        start = self._lap_start()
        if start is None:
            return
        self.sync = VideoSync.manual(float(start))
        self.sync.duration_ms = (float(self.player.duration())
                                 if MULTIMEDIA and self.player is not None
                                 and self.player.duration() > 0 else None)
        self._covered = None
        self._describe()
        self.seek_to_session_time(float(start))

    def _describe(self) -> None:
        """One place that writes the alignment label, so it cannot go stale.

        Stated relative to the session, because `offset_s` is an absolute
        timestamp — for a log holding time of day that is a five-figure number
        which tells nobody anything, and if it is the only feedback then
        adjusting the offset looks like it did nothing.
        """
        name = os.path.basename(self.path) if self.path else "no video"
        if self.sync is None:
            self.sync_label.setText(f"{name} \u2014 no sync")
            return
        self.sync_label.setText(
            f"{name} \u2014 "
            f"{self.sync.describe_relative(self._session_start)}")

    def _pick_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open video", "",
            "Video (*.mp4 *.avi *.mov *.m4v *.mkv *.mts *.m2ts);;All files (*)")
        if not path:
            return
        self._pick_file_with(path)

    def _pick_file_with(self, path: str) -> None:
        # Any sync read from the log described a *different* file, so it cannot
        # apply to one chosen by hand. Anchor the clip's first frame to the
        # start of the *session*, not the lap on screen: for a whole-session
        # recording that lines every lap up approximately, and for a one-lap
        # clip it is refined by scrubbing and Sync anyway. Anchoring to the
        # current lap instead would push every earlier lap to negative video
        # time, where it reads as "the video does not reach this part".
        sync = VideoSync.manual(self._session_start or 0.0)
        vbo = getattr(self, "vbo", None)
        if vbo is not None:
            try:
                gps = VideoSync.from_gopro(path, vbo)   # GoPro clip / .LRV: exact sync from its GPS
            except Exception:                            # noqa: BLE001
                gps = None
            if gps is not None:
                sync = gps
        self.load(path, sync, self._session_start)

    def _on_error(self, *args) -> None:                 # pragma: no cover
        err = self.player.errorString() if self.player else ""
        self._set_message(
            f"Could not play this video.\n\n{err}\n\n"
            "Qt needs a system codec for this format. H.264 .mp4 is the "
            "safest bet; otherwise re-encode with ffmpeg.")

    def _flash_synced(self) -> None:
        base = self.sync_btn.text()
        self.sync_btn.setText("Synced")
        QtCore.QTimer.singleShot(1200, lambda: self.sync_btn.setText(base
                                 if self.sync_btn.text() == "Synced" else
                                 self.sync_btn.text()))

    def _apply_volume(self) -> None:
        """The single source of truth for how loud the video is.

        Volume is driven only by the mute button. An earlier version also
        toggled `setMuted` to silence the brief step used to force a repaint,
        and when those two timers overlapped the player could be left muted
        with the button still saying otherwise — audio "lost" after scrubbing.
        The repaint step now relies on volume already sitting at zero while
        paused, so there is nothing to race.
        """
        if not MULTIMEDIA or self.audio is None:
            return
        playing = (self.player is not None
                   and self.player.playbackState()
                   == QMediaPlayer.PlayingState)
        # silent unless actually playing and unmuted: the repaint step below
        # calls play() for 40 ms, and this keeps that step silent without a
        # second mute flag to get out of sync
        self.audio.setVolume(0.0 if (self._muted or not playing) else 0.7)

    def _refresh_paused_frame(self) -> None:
        """Make a paused player show the frame it was just seeked to.

        Seeking a paused QMediaPlayer does not reliably repaint: with a
        graphics-scene video item the old frame can stay on screen until
        playback resumes, so adjusting the offset looks like it is doing
        nothing. Stepping the player for a moment forces a decode; volume is
        already zero while paused, so the step is silent with no extra mute to
        leave stuck.
        """
        if not MULTIMEDIA or self.player is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            return
        self._suppress = True
        self.player.play()
        QtCore.QTimer.singleShot(40, self._end_refresh_step)

    def _end_refresh_step(self) -> None:
        self._decoding = False
        if MULTIMEDIA and self.player is not None:
            self.player.pause()
        self._suppress = False
        self._apply_volume()

    def _decode_current_frame(self) -> None:
        """Force a repaint at the current position, even mid-scrub.

        Plays for a beat to make the decoder emit a frame, then pauses. Guarded
        by an in-flight flag so overlapping calls during a fast drag cannot
        stack play() on play() and leave the video actually running past the
        scrub point — which desynced the whole panel and made scrubbing seem to
        respond only to large jumps.
        """
        if not MULTIMEDIA or self.player is None:
            return
        if self._decoding:
            return
        self._decoding = True
        self._suppress = True
        self.player.play()
        QtCore.QTimer.singleShot(40, self._end_refresh_step)

    def _show_position(self, ms: float) -> None:
        if not MULTIMEDIA or self.player is None:
            return
        duration = self.player.duration()
        if duration <= 0 or self._scrubbing:
            return
        self.position.blockSignals(True)
        self.position.setValue(int(1000 * max(0.0, min(ms, duration)) / duration))
        self.position.blockSignals(False)

    def _on_state(self, _state=None) -> None:
        """Keep the volume in step with play/pause, in one place."""
        self._apply_volume()

    def _on_media_status(self, status) -> None:
        """Force the opening frame once the media is decodable.

        setSource does not load synchronously, so the seek issued at load time
        runs before there is anything to decode. This fires when the player
        reaches LoadedMedia (or buffered), which is the first moment a frame
        can actually be produced.
        """
        if not MULTIMEDIA or self.player is None:
            return
        loaded = (QMediaPlayer.MediaStatus.LoadedMedia,
                  QMediaPlayer.MediaStatus.BufferedMedia)
        if status in loaded and self._pending_first_frame:
            self._force_first_frame()

    def _force_first_frame(self) -> None:
        """Seek to where the cursor is and decode a frame, if we can yet."""
        if not MULTIMEDIA or self.player is None or self.sync is None:
            return
        status = self.player.mediaStatus()
        ready = (QMediaPlayer.MediaStatus.LoadedMedia,
                 QMediaPlayer.MediaStatus.BufferedMedia)
        if status not in ready:
            return                       # try again from _on_media_status
        self._pending_first_frame = False
        at = self._cursor_time() if self._cursor_time is not None else None
        if at is not None:
            self.seek_to_session_time(float(at))
        else:
            self._refresh_paused_frame()

    def _on_duration(self, ms: int) -> None:
        if self.sync is not None and ms > 0:
            self.sync.duration_ms = float(ms)

    def _toggle_audio(self) -> None:
        if not MULTIMEDIA:
            return
        self._muted = not self._muted
        self._apply_volume()
        self.mute_btn.setText("🔇" if self._muted else "🔊")

    def _nudge(self, delta: float) -> None:
        """Shift the video by `delta` seconds and show the result immediately.

        Two cases, because the frame on screen is not always the one under the
        data cursor:

        * If a frame was scrubbed to and is being held, the offset buttons fine-
          tune *that* frame — the video steps forward or back by delta and
          stays put. Snapping to the cursor instead (what this used to do) threw
          the user back to the old sync point the moment they nudged, undoing
          the scrub they were trying to refine.
        * Otherwise the video is tracking the cursor, so nudging re-seeks to the
          cursor at the new offset, as before.
        """
        if self.sync is None or not MULTIMEDIA or self.player is None:
            return

        if self._held_scrub:
            # move the picture itself by delta, and fold the same shift into the
            # sync so it sticks. "+" advances the footage, matching the label.
            pos = self.player.position() / 1000.0 + delta
            dur = self.player.duration() / 1000.0 if self.player.duration() else None
            if dur:
                pos = max(0.0, min(pos, dur))
            self.sync.nudge(delta)
            self._describe()
            self._suppress = True
            self.player.setPosition(int(pos * 1000))
            self._suppress = False
            self._show_position(pos * 1000)
            self._decode_current_frame()
            return

        self.sync.nudge(delta)
        self._describe()
        at = self._cursor_time() if self._cursor_time else None
        if at is not None:
            self._covered = None
            self.seek_to_session_time(float(at))

    # --------------------------------------------------------------- playback
    @property
    def ready(self) -> bool:
        return bool(MULTIMEDIA and self.path and self.sync is not None)

    def _set_coverage(self, session_t: float) -> None:
        """Say when the footage does not reach this part of the session.

        A camera started after the logger leaves the earlier laps with no
        video at all. Showing a black frame looks like a decoding failure;
        saying so is the difference between a bug and a fact.
        """
        if self.sync is None or self.video is None:
            return
        covered = self.sync.covers(session_t)
        if covered == self._covered:
            return
        self._covered = covered
        if covered:
            self._set_message("", show_video=True)
            return
        span = self.sync.covered_session_span
        if span is not None and self._session_start is not None:
            lo = span[0] - self._session_start
            hi = span[1] - self._session_start
            self._set_message(
                "No video for this part of the session.\n\n"
                f"The recording covers {lo:,.0f}s to {hi:,.0f}s of it — the "
                "camera was started after the logger, so the earlier laps "
                "were never filmed.")
            return
        # a hand-aligned clip: it is short, and probably aligned to a different
        # lap than the one being viewed
        self._set_message(
            "The video does not reach this part of the session.\n\n"
            "If this clip is of a single lap, select that lap and press "
            "\u201cAlign to lap\u201d, then use the offset buttons to fine-"
            "tune.")

    def seek_to_session_time(self, session_t: float) -> None:
        """Point the video at a session time. Used while scrubbing or paused."""
        if not self.ready:
            return
        self._set_coverage(session_t)
        ms = self.sync.video_ms(session_t)
        if ms < 0:
            ms = 0.0
        if self._scrubbing:
            return
        self._held_scrub = False
        self._suppress = True
        self.player.setPosition(int(ms))
        self._suppress = False
        self._show_position(ms)
        self._refresh_paused_frame()

    def follow(self, session_t: float, playing: bool, rate: float) -> None:
        """Keep the video with the playhead during playback.

        Only corrects when it has drifted meaningfully — continuous seeking
        makes the picture stutter and the audio chirp.
        """
        if not self.ready:
            return
        self._set_coverage(session_t)
        target = self.sync.video_ms(session_t)
        if not playing:
            if self._playing:
                self.player.pause()
                self._playing = False
            self._following = False
            self.seek_to_session_time(session_t)
            return

        if not self._playing:
            self.player.setPlaybackRate(float(rate))
            self.player.setPosition(int(max(target, 0)))
            self.player.play()
            self._playing = True
            self._following = True
        elif abs(self.player.playbackRate() - rate) > 1e-6:
            self.player.setPlaybackRate(float(rate))

        # Do not correct while the playhead is following this player: the
        # target is derived from our own position, so any correction is
        # chasing our own tail.
        if self._following:
            return
        self._show_position(self.player.position())
        if abs(self.player.position() - target) > DRIFT_TOLERANCE_MS:
            self.player.setPosition(int(max(target, 0)))

    @property
    def is_playing(self) -> bool:
        if not MULTIMEDIA or self.player is None:
            return False
        return (self._playing
                and self.player.playbackState()
                == self.player.PlaybackState.PlayingState)

    def jump_while_playing(self, session_t: float) -> None:
        """The user moved the playhead (scrub bar, plot, map) during playback: take the video there and
        keep playing. For a moment afterwards the player's position is not trusted — it can still report
        the old spot until the seek lands — so the playhead runs on its own clock from the new place
        instead of being pulled back."""
        if not self.ready:
            return
        self._set_coverage(session_t)
        target = self.sync.video_ms(session_t)
        self._suppress = True
        self.player.setPosition(int(max(target, 0)))
        self._suppress = False
        self._show_position(target)
        self._jump_hold = QtCore.QDeadlineTimer(400)

    def current_session_time(self) -> Optional[float]:
        """Where the video actually is, in session seconds.

        The media player runs on its own clock and is the one thing here that
        cannot be told to hurry up. Reading its position and following it —
        rather than running a parallel clock and correcting the drift — is what
        removes the periodic seek.
        """
        if not self.ready or not self.is_playing:
            return None
        hold = getattr(self, "_jump_hold", None)
        if hold is not None and not hold.hasExpired():
            return None
        pos = self.player.position()
        if pos <= 0:
            return None
        return self.sync.session_t(float(pos))

    def stop(self) -> None:
        """Pause, keeping the file loaded and the position where it is."""
        if MULTIMEDIA and self.player is not None:
            self.player.pause()
        self._playing = False
        self._following = False

    def unload(self) -> None:
        """Release the media entirely.

        Pausing is not enough when the panel is dismissed: the player keeps the
        file open and, depending on the backend, keeps feeding the audio sink —
        so you still hear it from a panel you closed. Clearing the source is
        what actually silences it.
        """
        self._playing = False
        self._following = False
        if not MULTIMEDIA or self.player is None:
            return
        self.player.stop()
        self.audio.setVolume(0.0)
        self.player.setSource(QtCore.QUrl())
        self._loaded = False

    def reload(self) -> None:
        """Restore the media released by :meth:`unload`."""
        if not MULTIMEDIA or self.player is None or self._loaded:
            return
        if self.path:
            self.load(self.path, self.sync)
