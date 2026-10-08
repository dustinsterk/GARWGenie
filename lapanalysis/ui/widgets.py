"""
Plot widgets.

The three views share one distance cursor: hovering the speed or delta trace
moves the dot on the track map and updates the readouts. That linkage is the
point of the whole tool — a number in a table means little until you can see
where on the circuit it happened.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .. import basemap as bm
from ..corners import Corner
from ..laps import LapTrack, channel_label
from ..units import METRIC, UnitSystem

# Palette: dark, low-saturation background so the two lap traces and the
# red/green delta fill carry all the colour information.
BG = "#14161a"
FG = "#c8ccd4"
GRID = "#2a2f38"
REF_COLOUR = "#5ad1e6"      # reference lap — cyan
CMP_COLOUR = "#f2b134"      # compared lap — amber
LOSS_COLOUR = "#e5484d"
GAIN_COLOUR = "#3dd68c"
CORNER_FILL = (255, 255, 255, 14)

pg.setConfigOptions(antialias=True, background=BG, foreground=FG,
                    imageAxisOrder='row-major')


def _pen(colour: str, width: float = 1.8) -> QtGui.QPen:
    return pg.mkPen(colour, width=width)


class DistanceCursor(QtCore.QObject):
    """Shared distance-domain cursor.

    Two signals, because the consumers have very different costs. `moved` fires
    on every update and drives cheap work — keeping the video in step needs the
    full rate. `repaint` is rate limited and drives the plots, whose redraws are
    milliseconds each and which, at 30 a second across five panes, saturate the
    main thread and starve the video decoder.
    """
    moved = QtCore.Signal(float)
    repaint = QtCore.Signal(float)

    def __init__(self, repaint_interval_ms: int = 40) -> None:
        super().__init__()
        self.s = 0.0
        #: True while the transport is driving the cursor. Hover-to-scrub is
        #: suspended then: two things fighting over the playhead makes the
        #: video jump and the traces judder.
        self.playing = False
        self._interval = repaint_interval_ms
        self._last_paint = 0.0
        self._trailing = QtCore.QTimer(self)
        self._trailing.setSingleShot(True)
        self._trailing.timeout.connect(lambda: self.repaint.emit(self.s))

    def set(self, s: float) -> None:
        self.s = float(s)
        self.moved.emit(self.s)
        now = QtCore.QDateTime.currentMSecsSinceEpoch()
        if now - self._last_paint >= self._interval:
            self._last_paint = now
            self._trailing.stop()
            self.repaint.emit(self.s)
        else:
            # always land on the final position once movement stops
            self._trailing.start(self._interval)


def _press_point(ev):
    return ev.position().toPoint() if hasattr(ev, "position") else ev.pos()


class ClickToggleMixin:
    """Emit `clicked` on a press-release that did not move.

    A drag still pans or zooms the plot as normal; only a stationary click
    counts, so nothing fires by accident while you are dragging.

    Plots also emit `clickedAt` with the distance clicked, so a click can put
    the playhead somewhere precise. Following the mouse on hover is convenient
    for a quick look but hopeless when you are trying to line a video up: the
    playhead runs away the moment you reach for another control.
    """
    CLICK_SLOP_PX = 4

    def _init_click(self) -> None:
        self._press_pos = None

    def mousePressEvent(self, ev):                        # noqa: N802
        self._press_pos = _press_point(ev)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):                      # noqa: N802
        super().mouseReleaseEvent(ev)
        start = getattr(self, "_press_pos", None)
        self._press_pos = None
        if start is None or ev.button() != QtCore.Qt.LeftButton:
            return
        moved = (abs(_press_point(ev).x() - start.x()) > self.CLICK_SLOP_PX
                 or abs(_press_point(ev).y() - start.y()) > self.CLICK_SLOP_PX)
        if not moved and hasattr(self, "clickedAt"):
            # `_press_point` already hands back a QPoint, which is what
            # mapToScene wants. Catching everything here once hid a stray
            # `.toPoint()` on it: the signal silently stopped firing and
            # clicking a trace quietly did nothing.
            try:
                pos = self.getPlotItem().vb.mapSceneToView(
                    self.mapToScene(_press_point(ev)))
            except (AttributeError, RuntimeError):
                pos = None
            if pos is not None:
                self.clickedAt.emit(float(pos.x()))
        if (_press_point(ev) - start).manhattanLength() <= self.CLICK_SLOP_PX:
            self.clicked.emit()


class BasePlot(ClickToggleMixin, pg.PlotWidget):
    """Distance-domain plot with corner shading and a cursor line."""

    clicked = QtCore.Signal()
    clickedAt = QtCore.Signal(float)
    #: distance clicked, in whatever units the x axis is showing
    clickedAt = QtCore.Signal(float)

    def __init__(self, cursor: DistanceCursor, ylabel: str = "",
                 height: int = 150) -> None:
        # `height` is a *preferred* size, applied as a size hint rather than a
        # minimum. Real minimums here are what stop a QSplitter from moving at
        # all: the handle cannot travel if every child is already at its floor.
        super().__init__()
        self.cursor = cursor
        self.units: UnitSystem = METRIC
        self._init_click()
        self.setMinimumHeight(60)
        self._preferred_height = height
        self._frozen = (None, None)
        self.showGrid(x=True, y=True, alpha=0.18)
        self.setLabel("left", ylabel)
        self.getAxis("bottom").setLabel("distance from start/finish", "m")
        # pyqtgraph rescales axes to SI prefixes by default, which silently
        # relabels meters as km and seconds as ms while the axis title still
        # says otherwise. Everything here is meters and seconds — keep it so.
        for side in ("left", "bottom"):
            self.getAxis(side).enableAutoSIPrefix(False)
        self.setMouseEnabled(x=True, y=False)
        self._line = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen("#ffffff", width=1,
                                                  style=QtCore.Qt.DashLine))
        self.addItem(self._line, ignoreBounds=True)
        self._corner_items: List[pg.LinearRegionItem] = []
        self._labels: List[pg.TextItem] = []
        cursor.repaint.connect(self._on_cursor)
        self.scene().sigMouseMoved.connect(self._on_mouse)

    #: set by the window from the "follow mouse" control
    hover_enabled = False

    def _on_mouse(self, pos) -> None:
        # While playing, the transport owns the cursor. Letting the mouse move
        # it too means the playhead jumps wherever the pointer happens to rest.
        if self.cursor.playing or not self.hover_enabled:
            return
        if not self.sceneBoundingRect().contains(pos):
            return
        x = self.getPlotItem().vb.mapSceneToView(pos).x()
        # the axis is in display units; the cursor is always meters
        self.cursor.set(x / max(self.units.d(1.0), 1e-9))

    def _on_cursor(self, s: float) -> None:
        if not self.isVisible():
            return
        self._line.setPos(self.units.d(s))

    def set_units(self, u: UnitSystem) -> None:
        self.units = u
        self.getAxis("bottom").setLabel("distance from start/finish",
                                        u.dist_label)

    def fit_then_freeze(self, xs: Sequence[np.ndarray],
                        ys: Sequence[np.ndarray], pad: float = 0.06) -> None:
        """Set the range from the data once, then stop auto-ranging.

        Auto-range is the single largest cost in the UI: pyqtgraph re-evaluates
        the visible range whenever any item changes, so moving one cursor dot
        re-scans every curve in the plot.

        The range is computed here rather than by asking pyqtgraph to fit,
        because a fit performed before the widget has its final geometry lands
        on the wrong numbers and then gets frozen there.
        """
        vb = self.getPlotItem().vb
        vb.disableAutoRange()

        def span(arrays, pad_frac):
            vals = [np.asarray(a, dtype=float) for a in arrays
                    if a is not None and len(a)]
            vals = [v[np.isfinite(v)] for v in vals]
            vals = [v for v in vals if v.size]
            if not vals:
                return None
            # Percentiles, not min/max: a single spike — a GPS glitch, a
            # dropout — otherwise squashes the entire trace into a flat line
            # at the bottom of the plot.
            allv = np.concatenate(vals)
            lo = float(np.percentile(allv, 0.2))
            hi = float(np.percentile(allv, 99.8))
            lo = min(lo, float(np.percentile(allv, 50)) )
            if hi <= lo:
                lo, hi = float(allv.min()), float(allv.max())
            if hi - lo < 1e-9:
                lo, hi = lo - 1.0, hi + 1.0
            margin = (hi - lo) * pad_frac
            return lo - margin, hi + margin

        self._frozen = (span(xs, 0.01), span(ys, pad))
        self._apply_frozen()

    def _apply_frozen(self) -> None:
        xr, yr = getattr(self, "_frozen", (None, None))
        vb = self.getPlotItem().vb
        vb.disableAutoRange()
        if xr:
            self.setXRange(*xr, padding=0)
        if yr:
            self.setYRange(*yr, padding=0)

    def resizeEvent(self, ev) -> None:                    # noqa: N802
        # The range is set before the widget has its final geometry, and Qt
        # lays out after construction. Reassert it once the size is real,
        # otherwise the frozen range keeps whatever it was fitted to.
        super().resizeEvent(ev)
        if getattr(self, "_frozen", None):
            self._apply_frozen()

    def set_corners(self, corners: Sequence[Corner], label: bool = False) -> None:
        for it in self._corner_items + self._labels:
            self.removeItem(it)
        self._corner_items.clear()
        self._labels.clear()
        for c in corners:
            region = pg.LinearRegionItem(
                values=(self.units.d(c.s_start), self.units.d(c.s_end)),
                movable=False,
                brush=pg.mkBrush(*CORNER_FILL), pen=pg.mkPen(None))
            region.setZValue(-100)
            # ignoreBounds: a LinearRegionItem spans the whole view vertically,
            # so counting it when ranging inflates the y axis several fold
            self.addItem(region, ignoreBounds=True)
            self._corner_items.append(region)
            if label:
                txt = pg.TextItem(c.name, color="#7f8794", anchor=(0.5, 1.0))
                txt.setPos(self.units.d(0.5 * (c.s_start + c.s_end)), 0)
                self.addItem(txt, ignoreBounds=True)
                self._labels.append(txt)


class SpeedPlot(BasePlot):
    def __init__(self, cursor: DistanceCursor) -> None:
        super().__init__(cursor, "speed (km/h)", height=200)
        self.units: UnitSystem = METRIC
        self._ref = self.plot([], [], pen=_pen(REF_COLOUR))
        self._cmp = self.plot([], [], pen=_pen(CMP_COLOUR))
        self._ref_dot = pg.ScatterPlotItem(size=7, brush=pg.mkBrush(REF_COLOUR),
                                           pen=pg.mkPen(None))
        self._cmp_dot = pg.ScatterPlotItem(size=7, brush=pg.mkBrush(CMP_COLOUR),
                                           pen=pg.mkPen(None))
        self.addItem(self._ref_dot)
        self.addItem(self._cmp_dot)
        self._ref_lap: Optional[LapTrack] = None
        self._cmp_lap: Optional[LapTrack] = None
        self._markers: List[pg.ScatterPlotItem] = []

    def set_units(self, u: UnitSystem) -> None:
        super().set_units(u)
        self.setLabel("left", f"speed ({u.speed_label})")

    def set_laps(self, ref: Optional[LapTrack], cmp_: Optional[LapTrack]) -> None:
        u = self.units
        self._ref_lap, self._cmp_lap = ref, cmp_
        if ref is not None:
            self._ref.setData(u.d(ref.s), u.spd(ref.speed_kmh))
        else:
            self._ref.setData([], [])
        if cmp_ is not None and cmp_ is not ref:
            self._cmp.setData(u.d(cmp_.s), u.spd(cmp_.speed_kmh))
        else:
            self._cmp.setData([], [])
        laps = [l for l in (ref, cmp_) if l is not None]
        self.fit_then_freeze([u.d(l.s) for l in laps],
                             [u.spd(l.speed_kmh) for l in laps])

    def set_markers(self, points: Sequence[tuple[float, float, str]]) -> None:
        """points = (s, speed_kmh, kind) — brake points and apexes."""
        for m in self._markers:
            self.removeItem(m)
        self._markers.clear()
        styles = {
            "brake": dict(symbol="t", brush=pg.mkBrush(LOSS_COLOUR), size=11),
            "turnin": dict(symbol="d", brush=pg.mkBrush("#9d7bf5"), size=10),
            "apex": dict(symbol="o", brush=pg.mkBrush("#ffffff"), size=8),
            "throttle": dict(symbol="t1", brush=pg.mkBrush(GAIN_COLOUR), size=11),
        }
        for kind, style in styles.items():
            xs = [p[0] for p in points if p[2] == kind]
            ys = [p[1] for p in points if p[2] == kind]
            if not xs:
                continue
            item = pg.ScatterPlotItem(self.units.d(np.array(xs)),
                                      self.units.spd(np.array(ys)),
                                      pen=pg.mkPen(None), **style)
            self.addItem(item)
            self._markers.append(item)

    def _on_cursor(self, s: float) -> None:
        super()._on_cursor(s)
        for lap, dot in ((self._ref_lap, self._ref_dot),
                         (self._cmp_lap, self._cmp_dot)):
            if lap is None:
                dot.setData([], [])
                continue
            dot.setData([self.units.d(s)],
                        [self.units.spd(lap.at(s, lap.speed_kmh))])


class DeltaPlot(BasePlot):
    """Cumulative time delta, filled red where losing and green where gaining."""

    def __init__(self, cursor: DistanceCursor) -> None:
        super().__init__(cursor, "delta (s)", height=160)
        self._zero = pg.InfiniteLine(angle=0, pos=0,
                                     pen=pg.mkPen(GRID, width=1))
        self.addItem(self._zero)
        self._curve = self.plot([], [], pen=_pen("#ffffff", 1.4))
        self._loss = None
        self._gain = None
        self._dot = pg.ScatterPlotItem(size=7, brush=pg.mkBrush("#ffffff"),
                                       pen=pg.mkPen(None))
        self.addItem(self._dot)
        self._s = np.array([])
        self._d = np.array([])

    def set_delta(self, s: Optional[np.ndarray], d: Optional[np.ndarray]) -> None:
        for it in (self._loss, self._gain):
            if it is not None:
                self.removeItem(it)
        self._loss = self._gain = None
        if s is None or d is None or len(s) == 0:
            self._curve.setData([], [])
            self._s = self._d = np.array([])
            return
        self._s, self._d = np.asarray(s), np.asarray(d)
        sx = self.units.d(self._s)
        self._curve.setData(sx, self._d)
        base = pg.PlotDataItem(sx, np.zeros_like(self._d))
        # Two fills: the sign of the *slope* is what matters when reading this,
        # but filling by sign of the value is the convention drivers expect.
        self._loss = pg.FillBetweenItem(
            pg.PlotDataItem(sx, np.maximum(self._d, 0.0)), base,
            brush=pg.mkBrush(229, 72, 77, 90))
        self._gain = pg.FillBetweenItem(
            pg.PlotDataItem(sx, np.minimum(self._d, 0.0)), base,
            brush=pg.mkBrush(61, 214, 140, 90))
        for it in (self._loss, self._gain):
            it.setZValue(-50)
            self.addItem(it)
        # Don't let two near-identical laps blow the y-scale up to +/-5 ms of
        # noise; that reads as drama where there is none.
        span = max(float(np.max(np.abs(self._d))) * 1.15, 0.05)
        self.getPlotItem().vb.disableAutoRange()
        self.setXRange(float(sx[0]), float(sx[-1]), padding=0.01)
        self.setYRange(-span, span)

    def _on_cursor(self, s: float) -> None:
        super()._on_cursor(s)
        if self._s.size:
            self._dot.setData([self.units.d(s)],
                              [float(np.interp(s, self._s, self._d))])


class _TileSignals(QtCore.QObject):
    done = QtCore.Signal(int, int, int, int, object)  # batch, z, x, y, bytes|None

    def __init__(self) -> None:
        super().__init__()
        #: set when the batch is abandoned, so queued jobs return immediately
        self.abort = False


class _TileJob(QtCore.QRunnable):
    """One tile fetch, off the UI thread."""

    def __init__(self, provider, z: int, x: int, y: int,
                 signals: _TileSignals, batch: int) -> None:
        super().__init__()
        self.provider, self.z, self.x, self.y = provider, z, x, y
        self.signals = signals
        self.batch = batch
        self.setAutoDelete(True)

    def run(self) -> None:                          # pragma: no cover - thread
        data = None
        try:
            if not self.signals.abort:
                data = bm.fetch_tile(self.provider, self.z, self.x, self.y)
        except Exception:                            # noqa: BLE001
            data = None
        try:
            self.signals.done.emit(self.batch, self.z, self.x, self.y, data)
        except RuntimeError:
            pass                                     # window closed mid-fetch


def qimage_to_array(data: bytes) -> Optional[np.ndarray]:
    """Decode tile bytes to an (h, w, 3) uint8 array, or None."""
    img = QtGui.QImage()
    if not img.loadFromData(data):
        return None
    img = img.convertToFormat(QtGui.QImage.Format_RGB888)
    w, h = img.width(), img.height()
    if w == 0 or h == 0:
        return None
    stride = img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), np.uint8, count=stride * h)
    # rows are padded to a 4-byte boundary, so trim rather than reshape blindly
    return buf.reshape(h, stride)[:, :w * 3].reshape(h, w, 3).copy()


class TrackMap(ClickToggleMixin, pg.PlotWidget):
    """Plan view of the circuit, colored by speed or by time delta."""

    clicked = QtCore.Signal()
    clickedAt = QtCore.Signal(float)

    #: emitted when the user finishes placing a gate line in edit mode, with
    #: kind ("sf" | "sector") and the two view-space endpoints (ax, ay, bx, by)
    gatePlaced = QtCore.Signal(str, float, float, float, float)

    #: (array, left, bottom, width, height) — re-emitted so the 3D view can
    #: texture its ground plane from the same fetch instead of doubling the
    #: traffic to the tile server
    tileReady = QtCore.Signal(object, float, float, float, float)
    basemapCleared = QtCore.Signal()

    def __init__(self, cursor: DistanceCursor) -> None:
        super().__init__()
        self.cursor = cursor
        self._init_click()
        #: None, or "sf"/"sector" while placing a gate line by clicking
        self._edit_mode = None
        self._edit_pts = []          # view-space points collected so far
        self._edit_preview = None    # the in-progress line item
        self._tiles: List[pg.ImageItem] = []
        self._tile_pool = QtCore.QThreadPool(self)
        self._tile_pool.setMaxThreadCount(6)
        self._tile_signals = _TileSignals()
        self._tile_signals.done.connect(self._tile_ready)
        self._provider = None
        self._project = None
        self._pending = 0
        self._frame = None
        self._fails = 0
        self._hits = 0
        #: incremented per request, so results from a superseded batch — a
        #: different provider, or a retry — are dropped rather than painted
        #: over the current one
        self._batch = 0
        self._attrib = pg.TextItem("", color="#7f8794", anchor=(0, 1))
        f = QtGui.QFont()
        f.setPointSize(7)
        self._attrib.setFont(f)
        self._attrib.setZValue(500)
        self.addItem(self._attrib)
        self._attrib.hide()
        self.setAspectLocked(True)
        self.hideAxis("bottom")
        self.hideAxis("left")
        self.setMinimumHeight(80)
        self._segments: List[pg.PlotDataItem] = []
        self._cmp_line = self.plot([], [], pen=pg.mkPen(CMP_COLOUR, width=1.0,
                                                        style=QtCore.Qt.DotLine))
        self._dot = pg.ScatterPlotItem(size=13, brush=pg.mkBrush("#ffffff"),
                                       pen=pg.mkPen(BG, width=2))
        self.addItem(self._dot)
        self._brakes = pg.ScatterPlotItem(size=9, symbol="s",
                                          brush=pg.mkBrush(LOSS_COLOUR),
                                          pen=pg.mkPen(None))
        self.addItem(self._brakes)
        self._labels: List[pg.TextItem] = []
        self._sf: Optional[pg.PlotDataItem] = None
        self._sector_lines: List[pg.PlotDataItem] = []
        self._sf_label_items: List[pg.TextItem] = []
        self._sector_label_items: List[pg.TextItem] = []
        self._lap: Optional[LapTrack] = None
        cursor.moved.connect(self._on_cursor)

    # ------------------------------------------------------------- basemap
    def set_projection(self, project) -> None:
        """Supply the same lat/lon -> local meters projection the track uses."""
        self._project = project

    def clear_basemap(self) -> None:
        self._batch += 1                 # orphan anything still in flight
        for it in self._tiles:
            self.removeItem(it)
        self._tiles.clear()
        self._attrib.hide()
        self._provider = None
        self.basemapCleared.emit()

    def set_basemap(self, provider_key: Optional[str],
                    bounds: Optional[tuple] = None,
                    lat0: float = 0.0, lon0: float = 0.0,
                    extent_m: float = 1000.0) -> None:
        """Request aerial/topo tiles under the track.

        Tiles arrive asynchronously; each is placed as it lands so the map
        fills in progressively instead of blocking on the whole mosaic.
        """
        self.clear_basemap()
        if not provider_key or bounds is None or self._project is None:
            return
        provider = bm.PROVIDERS.get(provider_key)
        if provider is None:
            return
        self._provider = provider
        lat_min, lon_min, lat_max, lon_max = bounds
        z, tiles = bm.plan(lat0, lon0, lat_min, lon_min, lat_max, lon_max,
                           extent_m, provider)
        self._batch += 1
        batch = self._batch
        self._pending = len(tiles)
        self._fails = self._hits = 0
        self._tile_signals.abort = False
        self._attrib.setText(f"  {provider.attribution}")
        self._attrib.show()
        self._place_attribution()
        for (tz, tx, ty) in tiles:
            self._tile_pool.start(_TileJob(provider, tz, tx, ty,
                                           self._tile_signals, batch))

    def _tile_ready(self, batch: int, z: int, x: int, y: int, data) -> None:
        if batch != self._batch:
            return                       # a superseded request finishing late
        self._pending = max(0, self._pending - 1)
        if not data:
            self._fails += 1
            # Offline, or the provider is unreachable. Without this, a hundred
            # queued requests each sit out their timeout for no possible gain.
            if self._hits == 0 and self._fails >= 8:
                self._tile_signals.abort = True
            return
        if self._project is None or self._provider is None:
            return
        self._hits += 1
        arr = qimage_to_array(bytes(data))
        if arr is None:
            return
        left, bottom, w, h = bm.tile_rect_local(z, x, y, self._project)

        # Hand the 3D view the *raw* tile, row 0 = north. It has its own axis
        # convention and does its own flip; passing the 2D-flipped copy means
        # it flips a flipped array and mirrors every tile north-to-south.
        self.tileReady.emit(arr, left, bottom, w, h)

        # ImageItem.setRect maps image row 0 to the rect's minimum y, which in
        # a y-up ViewBox is its *southern* edge. Tile row 0 is the northern
        # edge, so the display copy is flipped. Verified by rendering a
        # mosaic-wide gradient and checking north really is at the top — a
        # mosaic of identical tiles cannot tell a global inversion from a
        # correct one.
        item = pg.ImageItem(arr[::-1])
        item.setRect(QtCore.QRectF(left, bottom, w, h))
        item.setZValue(-1000)
        item.setOpacity(0.92)
        self.addItem(item)
        self._tiles.append(item)

    def _place_attribution(self) -> None:
        vb = self.getPlotItem().vb
        (x0, _), (y0, _) = vb.viewRange()
        self._attrib.setPos(x0, y0)

    def _clear_segments(self) -> None:
        for it in self._segments:
            self.removeItem(it)
        self._segments.clear()

    def set_lap(self, lap: LapTrack, colour_by: np.ndarray,
                cmap: str = "speed", bins: int = 22) -> None:
        """Draw the racing line, color-banded by `colour_by`.

        Banding into a couple of dozen polylines is far cheaper to render than
        a per-sample scatter, and reads better at a glance.
        """
        self._clear_segments()
        self._lap = lap
        v = np.asarray(colour_by, dtype=float)
        finite = v[np.isfinite(v)]
        if finite.size == 0:
            return
        if cmap == "delta":
            lim = max(abs(float(np.nanmin(finite))), abs(float(np.nanmax(finite))), 1e-6)
            lo, hi = -lim, lim
            stops = [(0.0, (61, 214, 140)), (0.5, (90, 96, 110)), (1.0, (229, 72, 77))]
        else:
            lo, hi = float(np.nanmin(finite)), float(np.nanmax(finite))
            stops = [(0.0, (60, 90, 200)), (0.45, (90, 200, 210)),
                     (0.75, (240, 200, 70)), (1.0, (235, 80, 70))]
        edges = np.linspace(lo, hi, bins + 1)
        idx = np.clip(np.digitize(v, edges) - 1, 0, bins - 1)

        for b in range(bins):
            mask = idx == b
            if not mask.any():
                continue
            # break the band into contiguous runs so we don't draw across gaps
            xs, ys = [], []
            run = np.flatnonzero(mask)
            splits = np.split(run, np.flatnonzero(np.diff(run) > 1) + 1)
            for seg in splits:
                if seg.size < 2:
                    continue
                lo_i = max(seg[0] - 1, 0)
                xs.extend(lap.x[lo_i:seg[-1] + 1].tolist() + [np.nan])
                ys.extend(lap.y[lo_i:seg[-1] + 1].tolist() + [np.nan])
            if not xs:
                continue
            colour = _lerp_colour(stops, b / max(bins - 1, 1))
            item = self.plot(xs, ys, pen=pg.mkPen(colour, width=4.0),
                             connect="finite")
            self._segments.append(item)
        self.frame_track()
        self._place_attribution()

    def frame_track(self, padding: float = 0.06) -> None:
        """Frame the racing line, and nothing else.

        Deliberately explicit rather than `autoRange()`. Auto-ranging fits
        every item in the scene, so once the aerial tiles arrive — which they
        do asynchronously, after this runs — the view would sit back to show
        the whole mosaic, leaving the track as a small shape off to one side.
        The tiles exist to sit *under* the track, not to be framed.
        """
        lap = self._lap
        if lap is None or lap.x.size == 0:
            return
        x0, x1 = float(lap.x.min()), float(lap.x.max())
        y0, y1 = float(lap.y.min()), float(lap.y.max())
        mx = max((x1 - x0) * padding, 12.0)
        my = max((y1 - y0) * padding, 12.0)
        vb = self.getPlotItem().vb
        vb.disableAutoRange()
        self._frame = (x0 - mx, x1 + mx, y0 - my, y1 + my)
        vb.setRange(xRange=(x0 - mx, x1 + mx), yRange=(y0 - my, y1 + my),
                    padding=0)

    def resizeEvent(self, ev) -> None:                    # noqa: N802
        # The aspect is locked, so a resize changes which axis is the
        # constraint; reassert the frame or the track drifts off centre.
        super().resizeEvent(ev)
        if getattr(self, "_frame", None) and self._lap is not None:
            self.frame_track()

    def set_compare_line(self, lap: Optional[LapTrack]) -> None:
        if lap is None:
            self._cmp_line.setData([], [])
        else:
            self._cmp_line.setData(lap.x, lap.y)

    def set_corner_labels(self, lap: LapTrack, corners: Sequence[Corner],
                          brake_points: Sequence[float]) -> None:
        for t in self._labels:
            self.removeItem(t)
        self._labels.clear()
        for c in corners:
            i = lap.idx(c.s_geo_apex)
            txt = pg.TextItem(c.name, color="#e8ecf2", anchor=(0.5, 0.5))
            f = QtGui.QFont()
            f.setPointSize(9)
            f.setBold(True)
            txt.setFont(f)
            txt.setPos(float(lap.x[i]), float(lap.y[i]))
            self.addItem(txt)
            self._labels.append(txt)
        pts = [(float(lap.at(s, lap.x)), float(lap.at(s, lap.y)))
               for s in brake_points]
        self._brakes.setData([p[0] for p in pts], [p[1] for p in pts])

    def begin_gate_edit(self, kind: str) -> None:
        """Start placing a gate line by clicking two points on the track.

        kind is "sf" for the start/finish line or "sector" for a sector
        boundary. The next two clicks set the line; `gatePlaced` then fires.
        """
        self._edit_mode = kind
        self._edit_pts = []
        self._clear_edit_preview()

    def cancel_gate_edit(self) -> None:
        self._edit_mode = None
        self._edit_pts = []
        self._clear_edit_preview()

    @property
    def editing_gate(self) -> bool:
        return self._edit_mode is not None

    def _clear_edit_preview(self) -> None:
        if self._edit_preview is not None:
            self.removeItem(self._edit_preview)
            self._edit_preview = None

    def _view_point(self, ev):
        """The event position in view (local metre) coordinates, or None."""
        try:
            return self.getPlotItem().vb.mapSceneToView(
                self.mapToScene(_press_point(ev)))
        except (AttributeError, RuntimeError):
            return None

    def mousePressEvent(self, ev):                        # noqa: N802
        # in edit mode, a left click places a gate point instead of the usual
        # seek/toggle behaviour
        if (self._edit_mode is not None
                and ev.button() == QtCore.Qt.LeftButton):
            pt = self._view_point(ev)
            if pt is not None:
                self._edit_pts.append((float(pt.x()), float(pt.y())))
                if len(self._edit_pts) == 1:
                    ev.accept()
                    return
                if len(self._edit_pts) >= 2:
                    (ax, ay), (bx, by) = self._edit_pts[0], self._edit_pts[1]
                    kind = self._edit_mode
                    self.cancel_gate_edit()
                    self.gatePlaced.emit(kind, ax, ay, bx, by)
                    ev.accept()
                    return
        super().mousePressEvent(ev)

    def set_start_finish(self, line: Sequence[float], label: str = "") -> None:
        if self._sf is not None:
            self.removeItem(self._sf)
            self._sf = None
        for t in getattr(self, "_sf_label_items", []):
            self.removeItem(t)
        self._sf_label_items = []
        ax, ay, bx, by = line
        self._sf = self.plot([ax, bx], [ay, by],
                             pen=pg.mkPen("#ffffff", width=2))
        if label:
            self._sf_label_items.append(
                self._gate_label(ax, ay, bx, by, label, "#ffffff"))

    def _gate_label(self, ax, ay, bx, by, text, colour):
        """A small text tag centred on a gate line, added to the scene."""
        txt = pg.TextItem(text, color=colour, anchor=(0.5, 0.5))
        f = QtGui.QFont()
        f.setPointSize(8)
        f.setBold(True)
        txt.setFont(f)
        txt.setPos((ax + bx) / 2.0, (ay + by) / 2.0)
        self.addItem(txt)
        return txt

    def set_sector_lines(self, lines, labels=None) -> None:
        """Draw the sector gate lines (local-metre quads), each with a label
        ("S1", "S2"…) matching the 3D view."""
        for item in self._sector_lines:
            self.removeItem(item)
        self._sector_lines = []
        for t in getattr(self, "_sector_label_items", []):
            self.removeItem(t)
        self._sector_label_items = []
        labels = labels or []
        for k, (ax, ay, bx, by) in enumerate(lines):
            item = self.plot([ax, bx], [ay, by],
                             pen=pg.mkPen("#ffc400", width=2,
                                          style=QtCore.Qt.DashLine))
            self._sector_lines.append(item)
            lbl = labels[k] if k < len(labels) else ""
            if lbl:
                self._sector_label_items.append(
                    self._gate_label(ax, ay, bx, by, lbl, "#ffc400"))

    def _on_cursor(self, s: float) -> None:
        if self._lap is None or not self.isVisible():
            return
        self._dot.setData([self._lap.at(s, self._lap.x)],
                          [self._lap.at(s, self._lap.y)])

    def sync_cursor(self, s: float) -> None:
        """Force the dot to position s, bypassing the visibility guard.

        Called when this view is switched to, so its dot reflects the current
        (possibly paused) position immediately rather than waiting for the next
        cursor signal, which never comes while paused.
        """
        if self._lap is None:
            return
        self._dot.setData([self._lap.at(s, self._lap.x)],
                          [self._lap.at(s, self._lap.y)])


def _lerp_colour(stops, t: float) -> QtGui.QColor:
    t = float(np.clip(t, 0.0, 1.0))
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p0 <= t <= p1:
            f = (t - p0) / max(p1 - p0, 1e-9)
            rgb = [int(round(a + (b - a) * f)) for a, b in zip(c0, c1)]
            return QtGui.QColor(*rgb)
    return QtGui.QColor(*stops[-1][1])


class ReadoutBar(QtWidgets.QWidget):
    """Live numeric readout that follows the cursor."""

    FIELDS = ("distance", "reference", "this lap", "delta s", "lat g",
              "long g", "radius")

    def __init__(self, cursor: DistanceCursor) -> None:
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        self._labels = {}
        self._caps = {}
        for name in self.FIELDS:
            box = QtWidgets.QVBoxLayout()
            cap = QtWidgets.QLabel(name)
            cap.setStyleSheet("color:#6b7280; font-size:10px;")
            val = QtWidgets.QLabel("--")
            val.setStyleSheet("color:#e8ecf2; font-size:14px; "
                              "font-family:'SF Mono','Menlo','DejaVu Sans Mono','Consolas','Liberation Mono',monospace;")
            box.addWidget(cap)
            box.addWidget(val)
            lay.addLayout(box)
            self._labels[name] = val
            self._caps[name] = cap
        lay.addStretch(1)
        self._ref = self._cmp = None
        self._s = self._d = None
        self.units: UnitSystem = METRIC
        cursor.repaint.connect(self.update_at)

    def set_units(self, u: UnitSystem) -> None:
        self.units = u
        for name, cap in (("reference", f"ref {u.speed_label}"),
                          ("this lap", f"lap {u.speed_label}"),
                          ("radius", f"radius {u.dist_label}"),
                          ("distance", f"distance {u.dist_label}")):
            self._caps[name].setText(cap)

    def set_context(self, ref, cmp_, s_delta, delta) -> None:
        self._ref, self._cmp = ref, cmp_
        self._s, self._d = s_delta, delta

    def update_at(self, s: float) -> None:
        u = self.units
        show = dict.fromkeys(self.FIELDS, "--")
        show["distance"] = f"{u.d(s):.0f}"
        if self._ref is not None:
            show["reference"] = f"{u.spd(self._ref.at(s, self._ref.speed_kmh)):.1f}"
        lap = self._cmp if self._cmp is not None else self._ref
        if lap is not None:
            show["this lap"] = f"{u.spd(lap.at(s, lap.speed_kmh)):.1f}"
            show["lat g"] = f"{lap.at(s, lap.ay_g):+.2f}"
            show["long g"] = f"{lap.at(s, lap.ax_g):+.2f}"
            k = abs(lap.at(s, lap.curv))
            show["radius"] = f"{u.d(1.0 / k):.0f}" if k > 1e-4 else "—"
        if self._s is not None and self._d is not None and len(self._s):
            show["delta s"] = f"{float(np.interp(s, self._s, self._d)):+.3f}"
        for k2, v in show.items():
            self._labels[k2].setText(v)


class ChannelPlot(BasePlot):
    """Any extra channel in the file, plotted against distance.

    Both laps are drawn where the channel exists on both, so a channel like
    heart rate or brake pressure can be compared the same way speed is.
    """

    def __init__(self, cursor: DistanceCursor) -> None:
        super().__init__(cursor, "", height=150)
        self._ref = self.plot([], [], pen=_pen(REF_COLOUR, 1.5))
        self._cmp = self.plot([], [], pen=_pen(CMP_COLOUR, 1.5))
        self._dot = pg.ScatterPlotItem(size=7, brush=pg.mkBrush("#ffffff"),
                                       pen=pg.mkPen(None))
        self.addItem(self._dot)
        self._ref_lap: Optional[LapTrack] = None
        self._cmp_lap: Optional[LapTrack] = None
        self._channel: Optional[str] = None

    #: Channels computed from GPS for every lap, whether or not the logger
    #: recorded them. A GPS-only file has no accelerometer column, but lateral
    #: and longitudinal g are derived from speed and path curvature regardless
    #: — leaving them out of the picker hid data the tool already had.
    #: name -> (label, unit, kind) where kind drives unit conversion
    DERIVED = {
        "speed": ("speed", "", "speed"),
        "ay_g_calc": ("lateral g (from GPS)", "g", "raw"),
        "ax_g_calc": ("longitudinal g (from GPS)", "g", "raw"),
        "combined_g": ("combined g (friction circle)", "g", "raw"),
        "radius": ("corner radius", "", "dist"),
        "delta_rate": ("time lost per 100 m", "s", "raw"),
    }

    @staticmethod
    def series(lap: Optional[LapTrack], name: str) -> Optional[np.ndarray]:
        if lap is None or name is None:
            return None
        if name == "speed":
            return lap.speed_kmh
        if name == "ay_g_calc":
            return lap.ay_g
        if name == "ax_g_calc":
            return lap.ax_g
        if name == "combined_g":
            return lap.combined_g()
        if name == "curv":
            return lap.curv
        if name == "radius":
            with np.errstate(divide="ignore"):
                return np.minimum(1.0 / np.maximum(np.abs(lap.curv), 1e-4), 2000.0)
        return lap.extras.get(name)

    @classmethod
    def label_for(cls, name: str, file_units=None) -> str:
        if name in cls.DERIVED:
            label, unit, _ = cls.DERIVED[name]
            return f"{label} ({unit})" if unit else label
        return channel_label(name, file_units)

    def _convert(self, name: str, y: np.ndarray) -> np.ndarray:
        """Apply display units to channels whose units we actually know."""
        u = self.units
        kind = self.DERIVED.get(name, (None, None, None))[2]
        if kind is None:
            # meters is the only safe assumption for these logger channels
            kind = "dist" if name in ("height", "ascent", "descent") else "raw"
        if kind == "speed":
            return u.spd(y)
        if kind == "dist":
            return u.d(y)
        return y

    def set_channel(self, name: Optional[str], ref: Optional[LapTrack],
                    cmp_: Optional[LapTrack],
                    file_units: Optional[dict] = None) -> None:
        self._channel, self._ref_lap, self._cmp_lap = name, ref, cmp_
        if name is None:
            self._ref.setData([], [])
            self._cmp.setData([], [])
            self.setLabel("left", "")
            return
        label = self.label_for(name, file_units)
        kind = self.DERIVED.get(name, (None, None, None))[2]
        if kind == "speed":
            label = f"speed ({self.units.speed_label})"
        elif kind == "dist" or name in ("height", "ascent", "descent"):
            base = label.split(" (")[0]
            label = f"{base} ({self.units.dist_label})"
        self.setLabel("left", label)
        u = self.units
        for lap, curve in ((ref, self._ref), (cmp_, self._cmp)):
            y = self.series(lap, name)
            if y is None or lap is None or (lap is ref and cmp_ is ref
                                            and curve is self._cmp):
                curve.setData([], [])
                continue
            curve.setData(u.d(lap.s), self._convert(name, y))
        if cmp_ is None or cmp_ is ref:
            self._cmp.setData([], [])
        laps = [l for l in (ref, cmp_) if l is not None]
        series = [self.series(l, name) for l in laps]
        self.fit_then_freeze(
            [u.d(l.s) for l in laps],
            [self._convert(name, y) for y in series if y is not None])

    def _on_cursor(self, s: float) -> None:
        super()._on_cursor(s)
        lap = self._cmp_lap if self._cmp_lap is not None else self._ref_lap
        y = self.series(lap, self._channel) if self._channel else None
        if y is None or lap is None:
            self._dot.setData([], [])
            return
        self._dot.setData([self.units.d(s)],
                          [float(np.interp(s, lap.s, self._convert(self._channel, y)))])

    def value_at(self, s: float) -> Optional[float]:
        lap = self._cmp_lap if self._cmp_lap is not None else self._ref_lap
        y = self.series(lap, self._channel) if self._channel else None
        if y is None or lap is None:
            return None
        return lap.at(s, y)
