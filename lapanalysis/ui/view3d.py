"""
3D chase view.

A ribbon of the racing line in real elevation, with a camera that sits behind
and above the car and swings to face the way it is pointing. The height comes
from the GPS altitude channel where the file has one, so gradient is real
rather than decorative — which matters, because a corner's difficulty often
lives in its camber and crest rather than its radius.

Needs OpenGL (`pip install PyOpenGL`). Without it this panel explains itself
rather than breaking the window.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ..laps import LapTrack

try:                                                    # pragma: no cover
    import pyqtgraph.opengl as gl
    from pyqtgraph.opengl.GLGraphicsItem import GLOptions as _GL_OPTIONS
    from OpenGL import GL                               # noqa: F401
    _BINDINGS = True
except Exception:                                       # pragma: no cover
    _BINDINGS = False


def gl_context_available() -> bool:
    """Whether a GL context can actually be created here.

    Having the bindings installed is not the same as being able to render:
    a headless session, a remote desktop or a broken driver will fail at
    context creation, and Qt aborts the *process* rather than raising. So
    probe with a throwaway context first and fall back to a message.
    """
    if not _BINDINGS:
        return False
    try:
        from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
        ctx = QOpenGLContext()
        if not ctx.create():
            return False
        surf = QOffscreenSurface()
        surf.setFormat(ctx.format())
        surf.create()
        return bool(surf.isValid() and ctx.makeCurrent(surf))
    except Exception:                                    # noqa: BLE001
        return False


OPENGL = _BINDINGS

#: Vertical relief applied to the racing line, as a multiple of true scale.
#:
#: Zero by default, which lays the lap flat on the imagery. Aerial tiles are
#: flat quads with no elevation of their own, and there is no honest way to
#: drape them over terrain we never measured: per-tile heights leave gaps at
#: the seams and let the ground occlude the very line you are trying to look
#: at. So either the line sits on the imagery and agrees with it exactly, or
#: it floats and the two disagree by height over tan(camera pitch). Flat is
#: the setting where the imagery can be trusted as a reference; raise it when
#: you want to see gradient and can accept the parallax.
DEFAULT_Z_SCALE = 0.0

#: Ribbon opacity. Solid by default hides the one thing the imagery is there
#: for — the kerbs and track edges you are judging your line against. Slightly
#: see-through by default so the surface reads through it.
DEFAULT_RIBBON_ALPHA = 0.55
TRACK_HALF_WIDTH = 4.0          # meters either side of the logged line

# Draw order. pyqtgraph sorts scene items by depthValue() and draws ascending,
# and every item defaults to 0 — so without these the order is however the
# items happened to be added. That is fatal for a translucent ribbon: drawn
# before the ground it blends against the empty background instead of the
# imagery, and then the ground fails the depth test underneath it and never
# appears at all. The result is a solid black band exactly where you wanted to
# see the kerbs.
DEPTH_GROUND = -100
DEPTH_RIBBON = 0
DEPTH_LINES = 5
DEPTH_CAR = 10


def _gl_opts(base: str, depth_write: bool) -> dict:
    """GL state for an item, always stating its depth-write setting.

    pyqtgraph applies each item's GL options immediately before drawing it and
    **never restores them**. So one item turning depth writes off turns them off
    for every item drawn after it, and for every following frame, until
    something turns them back on. Left implicit, that corrupts the whole scene:
    the ground tiles stop writing depth, draw over one another in arbitrary
    order, and the view tears into horizontal bands.

    Every item therefore declares `glDepthMask` rather than inheriting whatever
    the last one left behind.
    """
    opts = dict(_GL_OPTIONS[base])
    opts["glDepthMask"] = (GL.GL_TRUE if depth_write else GL.GL_FALSE,)
    return opts


def tile_texture(arr) -> Optional[np.ndarray]:
    """Convert a row-major tile image into what GLImageItem actually wants.

    GLImageItem indexes its array as ``data[x][y]`` — it transposes internally
    and takes the quad width from ``shape[0]`` — which is the opposite of the
    row-major ``(row, col)`` convention used for the 2D map. Handing it a
    row-major array mirrors every tile about its own diagonal: positions stay
    right, so the mosaic looks roughly correct, but no tile lines up with its
    neighbors.

    Returns an (x, y, 4) array where x runs east and y runs north.
    """
    if arr is None:
        return None
    a = np.ascontiguousarray(arr)
    if a.ndim != 3 or a.shape[2] not in (3, 4):
        return None
    if a.shape[2] == 3:
        rgba = np.empty((a.shape[0], a.shape[1], 4), dtype=np.uint8)
        rgba[..., :3] = a
        rgba[..., 3] = 255
        a = rgba
    # rows run north->south and columns west->east; we need x=east, y=north
    return np.ascontiguousarray(a[::-1].transpose(1, 0, 2))


def speed_colours(kmh: np.ndarray) -> np.ndarray:
    """Blue -> cyan -> yellow -> red, matching the 2D map."""
    v = np.asarray(kmh, dtype=float)
    lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
    t = np.clip((v - lo) / max(hi - lo, 1e-6), 0, 1)
    stops = np.array([[0.24, 0.35, 0.78],
                      [0.35, 0.78, 0.82],
                      [0.94, 0.78, 0.27],
                      [0.92, 0.31, 0.27]])
    pos = np.array([0.0, 0.45, 0.75, 1.0])
    out = np.empty((len(t), 4))
    for c in range(3):
        out[:, c] = np.interp(t, pos, stops[:, c])
    out[:, 3] = 1.0
    return out


class View3D(QtWidgets.QWidget):
    """Bird's-eye chase camera following the playhead."""

    #: a stationary click in the scene, for play/pause. Dragging still orbits.
    clicked = QtCore.Signal()

    CLICK_SLOP_PX = 4

    def __init__(self) -> None:
        super().__init__()
        self._press_pos = None
        self.lap: Optional[LapTrack] = None
        self.ref: Optional[LapTrack] = None
        self._z_scale = DEFAULT_Z_SCALE
        self._follow = True
        self._items: list = []
        self._tile_items: list = []      # kept across rebuilds; tiles are dear
        self._tile_specs: list = []      # so they can be re-laid at a new height
        self._car = None
        self._ref_line = None
        self._mesh = None
        self._edge = None
        self._gate_items: list = []
        self._start_finish = None
        self._sector_lines: list = []
        self._sf_label = "S/F"
        self._sector_labels: list = []
        self._corner_items: list = []
        self._corners: list = []
        self._ribbon_data = None         # (verts, faces, colours) for restyling
        self._alpha = DEFAULT_RIBBON_ALPHA

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # decided per instance: the probe needs a QApplication to exist
        self._gl = gl_context_available()

        if self._gl:
            self.view = gl.GLViewWidget()
            self.view.setMinimumHeight(80)
            self.view.setBackgroundColor(QtGui.QColor("#0e1013"))
            self.view.installEventFilter(self)
            lay.addWidget(self.view, 1)
        else:
            self.view = None
            msg = QtWidgets.QLabel(
                ("The 3D view needs OpenGL bindings.\n\n"
                 "    pip install PyOpenGL PyOpenGL_accelerate\n\n"
                 "Everything else works without it.")
                if not _BINDINGS else
                ("No OpenGL context is available here.\n\n"
                 "This happens over remote desktop or with a driver that "
                 "can't provide one.\n\nEverything else works without it."))
            msg.setAlignment(QtCore.Qt.AlignCenter)
            msg.setStyleSheet("color:#6b7280; padding:24px;")
            lay.addWidget(msg, 1)

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(6, 0, 6, 2)
        bar.setSpacing(4)
        self.follow_box = QtWidgets.QCheckBox("Chase cam")
        self.follow_box.setToolTip("Camera follows the car")
        self.follow_box.setChecked(True)
        self.follow_box.toggled.connect(self._set_follow)
        bar.addWidget(self.follow_box)

        bar.addWidget(self._caption("opacity"))
        self.alpha_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.alpha_slider.setRange(10, 100)
        self.alpha_slider.setValue(int(DEFAULT_RIBBON_ALPHA * 100))
        self.alpha_slider.setMinimumWidth(40)
        self.alpha_slider.setMaximumWidth(90)
        self.alpha_slider.setToolTip(
            "How solid the racing line is drawn.\n"
            "Turn it down to read the kerbs and track edges underneath.")
        self.alpha_slider.valueChanged.connect(self._alpha_changed)
        bar.addWidget(self.alpha_slider)

        bar.addWidget(self._caption("relief"))
        self.z_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        # 0 flattens the lap onto the imagery, which is the useful setting for
        # checking your line against the actual track edges
        self.z_slider.setRange(0, 20)
        self.z_slider.setValue(int(DEFAULT_Z_SCALE))
        self.z_slider.setToolTip(
            "0 lays the lap flat on the imagery, where the two agree exactly.\n"
            "Above 0 shows real gradient, but the line will appear offset from\n"
            "the flat imagery by its height divided by tan(camera pitch).")
        self.z_slider.setMinimumWidth(40)
        self.z_slider.setMaximumWidth(90)
        self.z_slider.valueChanged.connect(self._z_changed)
        bar.addWidget(self.z_slider)

        bar.addWidget(self._caption("distance"))
        self.dist_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.dist_slider.setRange(20, 400)
        self.dist_slider.setValue(120)
        self.dist_slider.setMinimumWidth(40)
        self.dist_slider.setMaximumWidth(90)
        self.dist_slider.valueChanged.connect(lambda *_: self._update_camera())
        bar.addWidget(self.dist_slider)

        bar.addWidget(self._caption("pitch"))
        self.pitch_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.pitch_slider.setRange(5, 89)
        self.pitch_slider.setValue(28)
        self.pitch_slider.setMinimumWidth(40)
        self.pitch_slider.setMaximumWidth(90)
        self.pitch_slider.valueChanged.connect(lambda *_: self._update_camera())
        bar.addWidget(self.pitch_slider)
        bar.addStretch(1)
        lay.addLayout(bar)

    def eventFilter(self, obj, event):                    # noqa: N802
        if self.view is not None and obj is self.view:
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

    @property
    def available(self) -> bool:
        return self._gl

    @staticmethod
    def _caption(text: str) -> QtWidgets.QLabel:
        lab = QtWidgets.QLabel(text)
        lab.setStyleSheet("color:#5a616e; font-size:10px; letter-spacing:1px;"
                          "padding-left:8px;")
        # Fixed, not Ignored: an Ignored horizontal policy lets the label
        # shrink to zero width, which silently deletes the text rather than
        # compressing it. The few pixels saved are not worth unlabelled sliders.
        lab.setSizePolicy(QtWidgets.QSizePolicy.Fixed,
                          QtWidgets.QSizePolicy.Preferred)
        return lab

    # -------------------------------------------------------------- ground
    def clear_tiles(self) -> None:
        self._tile_specs.clear()
        self._drop_tile_items()

    def _drop_tile_items(self) -> None:
        if not self._gl:
            return
        for it in self._tile_items:
            try:
                self.view.removeItem(it)
            except Exception:                            # noqa: BLE001
                pass
        self._tile_items.clear()

    def add_tile(self, arr, left: float, bottom: float,
                 width: float, height: float) -> None:
        """Lay one aerial tile on the ground plane beneath the ribbon."""
        if arr is None:
            return
        self._tile_specs.append((arr, left, bottom, width, height))
        self._place_tile(arr, left, bottom, width, height)

    def _place_tile(self, arr, left: float, bottom: float,
                    width: float, height: float) -> None:
        if not self._gl or arr is None:
            return
        tex = tile_texture(arr)
        if tex is None:
            return
        w_px, h_px = tex.shape[0], tex.shape[1]     # texture is x-major
        item = gl.GLImageItem(tex)
        item.scale(width / w_px, height / h_px, 1.0)
        item.translate(left, bottom, self._ground_z())
        # the ground draws first each frame, so this is also what restores
        # depth writing after a translucent ribbon switched it off
        item.setGLOptions(_gl_opts("opaque", True))
        item.setDepthValue(DEPTH_GROUND)
        self.view.addItem(item)
        self._tile_items.append(item)

    def _relay_tiles(self) -> None:
        """Re-place tiles after the ground height changes."""
        specs = list(self._tile_specs)
        self._drop_tile_items()
        for spec in specs:
            self._place_tile(*spec)

    def _ground_z(self) -> float:
        """Height of the imagery plane — one plane, below the whole lap.

        Every tile sits at the same height. Giving tiles individual heights to
        chase the terrain looks like the right idea and is not: adjacent quads
        at different heights leave open seams you can see through, and any tile
        that lands above a dip in the track hides the racing line behind it.
        """
        if self.lap is None:
            return -2.0
        z = self._elevation(self.lap) * self._z_scale
        return float(np.nanmin(z)) - 2.0

    # ------------------------------------------------------------- building
    def _elevation(self, lap: LapTrack) -> np.ndarray:
        h = lap.extras.get("height")
        if h is None or not np.any(np.isfinite(h)):
            return np.zeros_like(lap.s)
        h = np.asarray(h, dtype=float)
        return h - float(np.nanmedian(h))

    def set_corners(self, corners) -> None:
        """Mark the turns (T1, T2…) in 3D, at their apex on the racing line.

        Each corner gets a short post topped by its name, so a driver reading
        the 3D view sees where each turn is as they approach — the same
        turn-by-turn frame the coaching uses. Kept and redrawn on a lap change.
        """
        self._corners = list(corners or [])
        self._redraw_corners()

    def _redraw_corners(self) -> None:
        if not self._gl or self.lap is None:
            return
        for it in getattr(self, "_corner_items", []):
            try:
                self.view.removeItem(it)
            except Exception:                            # noqa: BLE001
                pass
        self._corner_items = []
        lap = self.lap
        for c in self._corners:
            try:
                i = int(np.clip(lap.idx(c.s_geo_apex), 0, len(lap.x) - 1))
            except Exception:                            # noqa: BLE001
                continue
            px, py = float(lap.x[i]), float(lap.y[i])
            z = self._elevation(lap)[i] * self._z_scale
            self._add_corner(px, py, z, c.name)

    def _add_corner(self, px, py, z, label) -> None:
        """A short post at a turn's apex, topped by its name."""
        post = 6.0
        stem = np.array([[px, py, z], [px, py, z + post]])
        item = gl.GLLinePlotItem(pos=stem, color=(0.35, 0.82, 0.90, 0.9),
                                 width=2, antialias=True, mode="line_strip")
        item.setGLOptions(_gl_opts("translucent", True))
        item.setDepthValue(DEPTH_CAR)
        self.view.addItem(item)
        self._corner_items.append(item)
        if label and hasattr(gl, "GLTextItem"):
            try:
                txt = gl.GLTextItem(pos=np.array([px, py, z + post + 2.0]),
                                    text=str(label), color=(90, 209, 230))
                txt.setDepthValue(DEPTH_CAR)
                self.view.addItem(txt)
                self._corner_items.append(txt)
            except Exception:                            # noqa: BLE001
                pass

    def set_gates(self, start_finish, sector_lines, sf_label="S/F",
                  sector_labels=None) -> None:
        """Draw the timing gates in 3D, matching the flat map.

        start_finish is a local-metre (ax, ay, bx, by) quad or None; each
        sector line is the same. sf_label and sector_labels annotate the gates
        ("S/F", "S1"…) so a driver can read where each is. They are kept and
        redrawn whenever the lap changes, so a rebuild does not drop them.
        """
        self._start_finish = start_finish
        self._sector_lines = list(sector_lines or [])
        self._sf_label = sf_label
        self._sector_labels = list(sector_labels or [])
        self._redraw_gates()

    def _redraw_gates(self) -> None:
        if not self._gl or self.lap is None:
            return
        for it in getattr(self, "_gate_items", []):
            try:
                self.view.removeItem(it)
            except Exception:                            # noqa: BLE001
                pass
        self._gate_items = []
        gates = []
        sf = getattr(self, "_start_finish", None)
        if sf is not None:
            gates.append((sf, (1.0, 1.0, 1.0, 0.9),
                          getattr(self, "_sf_label", "S/F")))
        labels = getattr(self, "_sector_labels", [])
        for k, line in enumerate(getattr(self, "_sector_lines", [])):
            lbl = labels[k] if k < len(labels) else f"S{k + 1}"
            gates.append((line, (1.0, 0.77, 0.0, 0.9), lbl))
        for (ax, ay, bx, by), colour, label in gates:
            self._add_gate(ax, ay, bx, by, colour, label)
        import os
        if os.environ.get("LAPANALYSIS_DEBUG_GATES"):
            sf_n = 1 if getattr(self, "_start_finish", None) is not None else 0
            sec_n = len(getattr(self, "_sector_lines", []))
            print(f"[gates] start/finish={sf_n} sectors={sec_n} "
                  f"line-items drawn={len(self._gate_items)}", flush=True)

    def _gate_ground_z(self, x: float, y: float) -> float:
        """Elevation at the nearest lap sample to (x, y), in scaled units."""
        lap = self.lap
        if lap is None:
            return 0.0
        d2 = (lap.x - x) ** 2 + (lap.y - y) ** 2
        i = int(np.argmin(d2))
        return float(self._elevation(lap)[i]) * self._z_scale

    def _add_gate(self, ax, ay, bx, by, colour, label="") -> None:
        """A gate: a line across the track plus two short posts, so it reads at
        chase-camera angles the way the flat line reads from above, with a text
        label floating above the crossbar so a driver knows which gate it is."""
        za = self._gate_ground_z(ax, ay)
        zb = self._gate_ground_z(bx, by)
        post = 8.0
        # One connected strip drawn as a gate: up the left post, across the
        # top, down the right post. A single item per gate rather than three
        # separate ones, which is simpler for the GL backend to batch and
        # avoids any ordering quirk between multiple line items.
        strip = np.array([
            [ax, ay, za],
            [ax, ay, za + post],
            [bx, by, zb + post],
            [bx, by, zb],
        ])
        item = gl.GLLinePlotItem(pos=strip, color=colour, width=3,
                                 antialias=True, mode="line_strip")
        item.setGLOptions(_gl_opts("translucent", True))
        item.setDepthValue(DEPTH_CAR)
        self.view.addItem(item)
        self._gate_items.append(item)

        # a text label centred over the crossbar, lifted a little higher so it
        # sits above the gate rather than on it
        if label and hasattr(gl, "GLTextItem"):
            mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
            mz = max(za, zb) + post + 3.0
            try:
                txt = gl.GLTextItem(pos=np.array([mx, my, mz]), text=str(label),
                                    color=tuple(int(c * 255) for c in colour[:3]))
                txt.setDepthValue(DEPTH_CAR)
                self.view.addItem(txt)
                self._gate_items.append(txt)
            except Exception:                            # noqa: BLE001
                pass

    def set_laps(self, lap: Optional[LapTrack], ref: Optional[LapTrack]) -> None:
        self.lap, self.ref = lap, ref
        if not self._gl or lap is None:
            return
        for it in self._items:
            try:
                self.view.removeItem(it)
            except Exception:                            # noqa: BLE001
                pass
        self._items.clear()
        self._car = self._ref_line = None
        for it in getattr(self, "_gate_items", []):
            try:
                self.view.removeItem(it)
            except Exception:                            # noqa: BLE001
                pass
        self._gate_items = []
        for it in getattr(self, "_corner_items", []):
            try:
                self.view.removeItem(it)
            except Exception:                            # noqa: BLE001
                pass
        self._corner_items = []

        z = self._elevation(lap) * self._z_scale
        pts = np.column_stack([lap.x, lap.y, z])
        cols = speed_colours(lap.speed_kmh)

        # A ribbon rather than a line: at chase-camera distances a 1 px line
        # disappears over crests, and a surface shows camber and gradient.
        ribbon, rib_cols = self._ribbon(pts, cols)
        self._ribbon_data = (ribbon["verts"], ribbon["faces"], rib_cols)
        mesh = gl.GLMeshItem(vertexes=ribbon["verts"], faces=ribbon["faces"],
                             vertexColors=rib_cols.copy(), smooth=False,
                             drawEdges=False, shader=None)
        mesh.setDepthValue(DEPTH_RIBBON)
        self.view.addItem(mesh)
        self._items.append(mesh)
        self._mesh = mesh

        edge = gl.GLLinePlotItem(pos=pts, color=(1, 1, 1, 0.25), width=1,
                                 antialias=True, mode="line_strip")
        edge.setGLOptions(_gl_opts("translucent", True))
        edge.setDepthValue(DEPTH_LINES)
        self.view.addItem(edge)
        self._items.append(edge)
        self._edge = edge
        self._apply_alpha()

        if ref is not None and ref is not lap:
            zr = self._elevation(ref) * self._z_scale
            rp = np.column_stack([ref.x, ref.y, zr + 0.4])
            self._ref_line = gl.GLLinePlotItem(pos=rp, color=(0.35, 0.82, 0.90, 0.8),
                                               width=2, antialias=True,
                                               mode="line_strip")
            self._ref_line.setGLOptions(_gl_opts("translucent", True))
            self._ref_line.setDepthValue(DEPTH_LINES)
            self.view.addItem(self._ref_line)
            self._items.append(self._ref_line)

        self._car = gl.GLScatterPlotItem(pos=np.zeros((1, 3)), size=14,
                                         color=(1, 1, 1, 1), pxMode=True)
        self._car.setGLOptions(_gl_opts("translucent", True))
        self._car.setDepthValue(DEPTH_CAR)
        self.view.addItem(self._car)
        self._items.append(self._car)
        self._redraw_gates()
        self._redraw_corners()
        self._relay_tiles()
        self._update_camera()

    @staticmethod
    def _ribbon(pts: np.ndarray, cols: np.ndarray):
        """Extrude the centreline sideways into a triangle strip."""
        d = np.gradient(pts[:, :2], axis=0)
        n = np.linalg.norm(d, axis=1, keepdims=True)
        d = d / np.maximum(n, 1e-9)
        left = np.column_stack([-d[:, 1], d[:, 0]]) * TRACK_HALF_WIDTH
        a = pts.copy()
        b = pts.copy()
        a[:, :2] += left
        b[:, :2] -= left
        verts = np.empty((len(pts) * 2, 3))
        verts[0::2] = a
        verts[1::2] = b
        m = len(pts)
        faces = []
        for i in range(m - 1):
            k = 2 * i
            faces.append([k, k + 1, k + 2])
            faces.append([k + 1, k + 3, k + 2])
        vcols = np.repeat(cols, 2, axis=0)
        return {"verts": verts, "faces": np.array(faces, dtype=int)}, vcols

    # ------------------------------------------------------------- opacity
    def _alpha_changed(self, value: int) -> None:
        self._alpha = max(0.05, min(1.0, value / 100.0))
        self._apply_alpha()

    def _apply_alpha(self) -> None:
        """Restyle the ribbon in place rather than rebuilding it.

        Dragging the slider would otherwise tear down and re-extrude several
        thousand triangles per frame.
        """
        if not self._gl or self._ribbon_data is None or self._mesh is None:
            return
        verts, faces, cols = self._ribbon_data
        shaded = cols.copy()
        shaded[:, 3] = self._alpha
        # Opaque geometry can use the depth buffer; translucent geometry has to
        # blend, and asking for blending when it is not needed costs fill rate
        # and can order surfaces wrongly.
        if self._alpha >= 0.99:
            self._mesh.setGLOptions(_gl_opts("opaque", True))
        else:
            # Depth *writes* off for a see-through surface: stamping the depth
            # buffer would occlude what is drawn after it, including the far
            # side of the ribbon itself on a hairpin.
            self._mesh.setGLOptions(_gl_opts("translucent", False))
        self._mesh.setMeshData(vertexes=verts, faces=faces,
                               vertexColors=shaded)
        if self._edge is not None:
            self._edge.setData(color=(1, 1, 1, 0.25 * self._alpha + 0.05))
        if self._ref_line is not None:
            self._ref_line.setData(
                color=(0.35, 0.82, 0.90, min(1.0, 0.55 + 0.35 * self._alpha)))

    # -------------------------------------------------------------- camera
    def _set_follow(self, on: bool) -> None:
        self._follow = bool(on)
        self._update_camera()

    def _z_changed(self, value: int) -> None:
        self._z_scale = float(value)
        self.set_laps(self.lap, self.ref)
        self._relay_tiles()

    def heading_at(self, s: float) -> float:
        """Direction of travel in degrees, from the path itself."""
        lap = self.lap
        if lap is None:
            return 0.0
        i = lap.idx(s)
        j = min(i + 3, len(lap.x) - 1)
        k = max(i - 3, 0)
        return float(np.degrees(np.arctan2(lap.y[j] - lap.y[k],
                                           lap.x[j] - lap.x[k])))

    def update_cursor(self, s: float) -> None:
        if not self._gl or self.lap is None or self._car is None:
            return
        lap = self.lap
        z = float(np.interp(s, lap.s, self._elevation(lap))) * self._z_scale
        pos = np.array([[lap.at(s, lap.x), lap.at(s, lap.y), z + 1.0]])
        self._car.setData(pos=pos)
        if self._follow:
            self._update_camera(s)

    def _update_camera(self, s: Optional[float] = None) -> None:
        if not self._gl or self.lap is None:
            return
        lap = self.lap
        if s is None:
            s = 0.0
        z = float(np.interp(s, lap.s, self._elevation(lap))) * self._z_scale
        centre = QtGui.QVector3D(lap.at(s, lap.x), lap.at(s, lap.y), z)
        if self._follow:
            # sit behind the car: pyqtgraph azimuth is the direction the camera
            # looks *from*, so trail the heading by 180 degrees
            az = self.heading_at(s) + 180.0
            self.view.setCameraPosition(pos=centre,
                                        distance=float(self.dist_slider.value()),
                                        elevation=float(self.pitch_slider.value()),
                                        azimuth=az)
        else:
            span = float(max(lap.x.max() - lap.x.min(),
                             lap.y.max() - lap.y.min()))
            mid = QtGui.QVector3D(float(lap.x.mean()), float(lap.y.mean()), 0.0)
            self.view.setCameraPosition(pos=mid, distance=span * 1.6,
                                        elevation=55.0, azimuth=-90.0)
