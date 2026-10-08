"""
The overlay layout editor.

Stage 3 of the video feature. Opened from the export dialog, it lets a real
layout be shaped against a real frame: drag each panel where it should sit,
toggle the ones you do not want, and set how solid the backing panels are so
they read over bright footage.

The design splits cleanly along what can be tested. The *state* being edited —
which elements show, where their boxes are, the panel opacity — is an
``OverlayLayout``, which is plain data with a JSON round-trip and is fully
covered by tests. Only the drag canvas itself is visual, and it does nothing
but translate mouse movement into edits of that data; it holds no state of its
own beyond which box is being dragged.

A frame of the actual lap is rendered behind the boxes so positioning is judged
against the telemetry as it will appear, not an abstract rectangle.
"""

from __future__ import annotations

import io
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..overlay import (ELEMENTS, OverlayData, OverlayLayout, OverlayRenderer,
                       PIL_AVAILABLE, landscape, portrait)


def _canvas_label(key: str) -> str:
    if key.startswith("ch:"):
        from ..overlay import _channel_label
        return _channel_label(key[3:])
    return ELEMENT_LABELS.get(key, key)


#: friendly names for the toggle list
ELEMENT_LABELS = {
    "speed": "Speed",
    "clock": "Lap clock",
    "delta": "Delta to best",
    "pedals": "Throttle / brake",
    "latg": "Lateral g",
    "map": "Corner map",
}


class LayoutCanvas(QtWidgets.QWidget):
    """A preview frame with draggable element boxes drawn over it.

    The canvas owns no layout state: it reads the boxes from the shared
    ``OverlayLayout`` and writes them straight back as the user drags, then
    asks the dialog to refresh. Keeping the layout the single source of truth
    is what lets the same object drive the canvas, the toggles, the opacity
    slider, and the final render without them drifting apart.
    """

    changed = QtCore.Signal()

    def __init__(self, data: OverlayData, layout: OverlayLayout,
                 preview_lap_t: float, video_frame=None):
        super().__init__()
        self._data = data
        self._layout = layout
        self._preview_t = preview_lap_t
        self._video_frame = video_frame        # a PIL RGBA image, or None
        self._drag_key: Optional[str] = None
        self._drag_dx = 0.0
        self._drag_dy = 0.0
        self._resizing = False           # dragging the corner handle, not moving
        self._drag_label = False         # dragging a caption marker
        self._dragging_crop = False
        self._bg: Optional[QtGui.QPixmap] = None
        self.setMinimumSize(320, 200)
        self.setMouseTracking(True)
        self._render_background()

    def _set_label_pos(self, key: str, pos) -> None:
        from ..overlay import ChannelStyle, ElementStyle
        if key.startswith("ch:"):
            st = self._layout.channel_styles.get(key) or ChannelStyle()
            st.label_pos = pos
            self._layout.channel_styles[key] = st
        else:
            st = self._layout.element_styles.get(key) or ElementStyle()
            st.label_pos = pos
            self._layout.element_styles[key] = st

    def set_layout(self, layout: OverlayLayout) -> None:
        self._layout = layout
        self._render_background()
        self.update()

    # -- geometry ----------------------------------------------------------

    def _frame_rect(self) -> QtCore.QRectF:
        """The preview frame, letterboxed to the layout's aspect."""
        aspect = self._layout.width / self._layout.height
        w, h = self.width(), self.height()
        if w / h > aspect:
            fh = h
            fw = h * aspect
        else:
            fw = w
            fh = w / aspect
        x = (w - fw) / 2
        y = (h - fh) / 2
        return QtCore.QRectF(x, y, fw, fh)

    def _box_rect(self, key: str) -> QtCore.QRectF:
        fr = self._frame_rect()
        x, y, bw, bh = self._layout.boxes[key]
        return QtCore.QRectF(fr.x() + x * fr.width(),
                             fr.y() + y * fr.height(),
                             bw * fr.width(), bh * fr.height())

    # -- rendering ---------------------------------------------------------

    def _render_background(self) -> None:
        """Composite the real footage (fit/cropped) under the overlay preview.

        Everything is rendered into one preview-sized RGBA image so what the
        canvas shows is what the export produces: the video placed exactly as
        the fit mode and crop offset dictate, the HUD on top.
        """
        if not PIL_AVAILABLE:
            self._bg = None
            return
        from PIL import Image
        pw = 640
        ph = int(round(pw / (self._layout.width / self._layout.height)))
        preview = self._layout.with_size(pw, ph)

        base = Image.new("RGBA", (pw, ph), (28, 34, 42, 255))
        if self._video_frame is not None:
            base.alpha_composite(self._place_video(self._video_frame, pw, ph))
        overlay = OverlayRenderer(self._data, preview).frame(self._preview_t)
        base.alpha_composite(overlay.convert("RGBA"))

        buf = io.BytesIO()
        base.save(buf, format="PNG")
        self._bg = QtGui.QPixmap.fromImage(
            QtGui.QImage.fromData(buf.getvalue(), "PNG"))

    def _place_video(self, frame, pw: int, ph: int):
        """Scale/crop the source frame into a pw x ph image, matching export."""
        from PIL import Image
        src = frame.convert("RGBA")
        sw, sh = src.size
        canvas = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        if self._layout.video_fit == "fill":
            scale = max(pw / sw, ph / sh)
            rw, rh = int(round(sw * scale)), int(round(sh * scale))
            resized = src.resize((rw, rh))
            cx = max(0.0, min(1.0, self._layout.crop_x))
            cy = max(0.0, min(1.0, self._layout.crop_y))
            ox = int((rw - pw) * cx)
            oy = int((rh - ph) * cy)
            canvas.paste(resized.crop((ox, oy, ox + pw, oy + ph)), (0, 0))
        else:
            scale = min(pw / sw, ph / sh)
            rw, rh = int(round(sw * scale)), int(round(sh * scale))
            resized = src.resize((rw, rh))
            canvas.paste(resized, ((pw - rw) // 2, (ph - rh) // 2))
        return canvas

    def paintEvent(self, _event) -> None:                 # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        fr = self._frame_rect()

        # a stand-in for footage: a muted gradient, so panels read as they will
        grad = QtGui.QLinearGradient(fr.topLeft(), fr.bottomRight())
        grad.setColorAt(0.0, QtGui.QColor(64, 78, 92))
        grad.setColorAt(1.0, QtGui.QColor(40, 52, 44))
        painter.fillRect(fr, grad)

        if self._bg is not None:
            painter.drawPixmap(fr.toRect(), self._bg)

        # draggable handles around each shown element, built-in or custom
        drawable = list(ELEMENTS) + [k for k in self._layout.show
                                     if k.startswith("ch:")]
        for key in drawable:
            if not self._layout.shows(key):
                continue
            rect = self._box_rect(key)
            active = key == self._drag_key
            pen = QtGui.QPen(QtGui.QColor(61, 109, 240) if active
                             else QtGui.QColor(150, 160, 175))
            pen.setWidth(2 if active else 1)
            pen.setStyle(QtCore.Qt.SolidLine if active else QtCore.Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRect(rect)
            # identify the box only when it is the active one, and put the tag
            # just outside the top-left corner so it never sits on top of the
            # rendered caption (which was the "two labels" confusion)
            if active:
                painter.setPen(QtGui.QColor(120, 170, 255))
                painter.drawText(
                    QtCore.QRectF(rect.x(), rect.y() - 16, rect.width(), 14),
                    QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                    _canvas_label(key))
            # a small grab handle at the bottom-right corner for resizing
            hr = self._handle_rect(rect)
            painter.fillRect(hr, QtGui.QColor(61, 109, 240) if active
                             else QtGui.QColor(150, 160, 175))
            # a draggable marker at the caption's real position, for elements
            # that have a caption and are showing it
            lp = self._label_marker(key)
            if lp is not None:
                mr = QtCore.QRectF(lp[0] - 5, lp[1] - 5, 10, 10)
                painter.setBrush(QtGui.QColor(255, 196, 0))
                painter.setPen(QtGui.QColor(60, 50, 0))
                painter.drawEllipse(mr)
                painter.setBrush(QtCore.Qt.NoBrush)

    # -- dragging ----------------------------------------------------------

    _HANDLE = 14                        # px size of the corner resize handle

    def _handle_rect(self, rect: QtCore.QRectF) -> QtCore.QRectF:
        h = self._HANDLE
        return QtCore.QRectF(rect.right() - h, rect.bottom() - h, h, h)

    #: elements that draw a caption, with its default (fx, fy) in the box
    _CAPTION_DEFAULTS = {
        "speed": (0.06, 0.72),
        "delta": (0.06, 0.74),
        "clock": (0.06, 0.06),
        "map": (0.06, 0.04),
        "latg": (0.5, 0.88),
    }

    def _caption_pos(self, key: str):
        """The caption position (fx, fy) for an element, or None if it has no
        movable caption or the caption is hidden."""
        if key.startswith("ch:"):
            st = self._layout.channel_styles.get(key)
            default = (0.06, 0.72)
        elif key in self._CAPTION_DEFAULTS:
            st = self._layout.element_styles.get(key)
            if st is not None and not st.show_label:
                return None
            default = self._CAPTION_DEFAULTS[key]
        else:
            return None
        if st is not None and getattr(st, "label_pos", None):
            return st.label_pos
        return default

    def _label_marker(self, key: str):
        """Canvas-pixel position of a caption marker, or None."""
        pos = self._caption_pos(key)
        if pos is None:
            return None
        rect = self._box_rect(key)
        return (rect.x() + pos[0] * rect.width(),
                rect.y() + pos[1] * rect.height())

    def mousePressEvent(self, event) -> None:             # noqa: N802
        pos = event.position()
        # topmost shown box under the cursor, custom channels first (they are
        # drawn on top of the built-ins)
        order = ([k for k in self._layout.show if k.startswith("ch:")]
                 + list(ELEMENTS))
        for key in order:
            if not self._layout.shows(key):
                continue
            rect = self._box_rect(key)
            marker = self._label_marker(key)
            if marker is not None:
                mr = QtCore.QRectF(marker[0] - 8, marker[1] - 8, 16, 16)
                if mr.contains(pos):
                    # press on the caption marker -> move the label
                    self._drag_key = key
                    self._drag_label = True
                    self._resizing = False
                    self.update()
                    return
            if self._handle_rect(rect).contains(pos):
                # press on the corner handle -> resize this box
                self._drag_key = key
                self._resizing = True
                self._drag_label = False
                self.update()
                return
            if rect.contains(pos):
                # press elsewhere in the box -> move it
                self._drag_key = key
                self._resizing = False
                self._drag_label = False
                self._drag_dx = pos.x() - rect.x()
                self._drag_dy = pos.y() - rect.y()
                self.update()
                return

    #: smallest a box may be dragged, as a fraction of the frame
    _MIN_W = 0.05
    _MIN_H = 0.04

    def mouseMoveEvent(self, event) -> None:              # noqa: N802
        if self._drag_key is None:
            return
        fr = self._frame_rect()
        pos = event.position()
        x, y, bw, bh = self._layout.boxes[self._drag_key]

        if self._drag_label:
            # place the caption within the box as fractions, clamped inside it
            rect = self._box_rect(self._drag_key)
            fx = (pos.x() - rect.x()) / rect.width()
            fy = (pos.y() - rect.y()) / rect.height()
            fx = max(0.0, min(fx, 0.98))
            fy = max(0.0, min(fy, 0.98))
            self._set_label_pos(self._drag_key, (fx, fy))
            self._render_background()
            self.update()
            self.changed.emit()
            return

        if self._resizing:
            # the top-left stays put; the dragged corner sets width/height,
            # clamped to a minimum and to the frame edge
            nw = (pos.x() - fr.x()) / fr.width() - x
            nh = (pos.y() - fr.y()) / fr.height() - y
            nw = max(self._MIN_W, min(nw, 1.0 - x))
            nh = max(self._MIN_H, min(nh, 1.0 - y))
            self._layout.boxes[self._drag_key] = (x, y, nw, nh)
        else:
            # move: new top-left, clamped so the box stays on the frame
            nx = (pos.x() - self._drag_dx - fr.x()) / fr.width()
            ny = (pos.y() - self._drag_dy - fr.y()) / fr.height()
            nx = max(0.0, min(nx, 1.0 - bw))
            ny = max(0.0, min(ny, 1.0 - bh))
            self._layout.boxes[self._drag_key] = (nx, ny, bw, bh)

        self._render_background()
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, _event) -> None:          # noqa: N802
        self._drag_key = None
        self._resizing = False
        self._drag_label = False
        self.update()


#: dark combo styling shared with the export dialog, so the current item is
#: readable rather than shown as light selected-text on a light highlight
_COMBO_CSS = (
    "QComboBox { background:#1a1f27; color:#e8ecf2; border:1px solid #2a313b; "
    "border-radius:4px; padding:4px 24px 4px 8px; }"
    "QComboBox:focus { border-color:#3d6df0; }"
    "QComboBox QAbstractItemView { background:#1a1f27; color:#e8ecf2; "
    "selection-background-color:#3d6df0; selection-color:#ffffff; }")


def _pick_channel(parent, available) -> Optional[str]:
    """Ask which extra channel to add, with a readable dark combo.

    QInputDialog.getItem gives an unstyled combo whose current item renders as
    light selected-text on a light highlight — unreadable on the dark theme.
    A small purpose-built dialog styles it and keeps focus off the combo.
    """
    from ..overlay import _channel_label

    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle("Add data channel")
    lay = QtWidgets.QVBoxLayout(dlg)
    lay.addWidget(QtWidgets.QLabel("Channel:"))

    combo = QtWidgets.QComboBox()
    combo.setStyleSheet(_COMBO_CSS)
    for name in available:
        combo.addItem(f"{_channel_label(name)}  ({name})", name)
    lay.addWidget(combo)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    lay.addWidget(buttons)
    # focus the OK button, not the combo, so nothing opens pre-selected
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setFocus()

    if dlg.exec() != QtWidgets.QDialog.Accepted:
        return None
    return combo.currentData()


class _ColorField(QtWidgets.QWidget):
    """A colour swatch that opens a picker, with a Default (clear) button.

    Holds a hex string or None (None = the element\'s own default colouring).
    """

    def __init__(self, value):
        super().__init__()
        self._value = value
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self._swatch = QtWidgets.QPushButton()
        self._swatch.setFixedSize(48, 22)
        self._swatch.clicked.connect(self._pick)
        clear = QtWidgets.QPushButton("Default")
        clear.setMaximumWidth(72)
        clear.clicked.connect(self._clear)
        row.addWidget(self._swatch)
        row.addWidget(clear)
        row.addStretch(1)
        self._refresh()

    def value(self):
        return self._value

    def _refresh(self) -> None:
        if self._value:
            self._swatch.setStyleSheet(
                f"background:{self._value}; border:1px solid #444;")
            self._swatch.setText("")
        else:
            self._swatch.setStyleSheet(
                "background:#1a1f27; border:1px solid #444; color:#8b93a1;")
            self._swatch.setText("auto")

    def _pick(self) -> None:
        initial = QtGui.QColor(self._value) if self._value else QtGui.QColor("#e8ecf2")
        chosen = QtWidgets.QColorDialog.getColor(initial, self, "Element colour")
        if chosen.isValid():
            self._value = chosen.name()          # "#rrggbb"
            self._refresh()

    def _clear(self) -> None:
        self._value = None
        self._refresh()


def _edit_element_style(parent, title, key, current):
    """Style a built-in element: caption text, visibility and colour.

    Returns a new ElementStyle or None if cancelled. Some elements (clock, map)
    have no caption to speak of, so the label field is offered but simply has
    no effect on them; the colour applies to every element\'s primary graphic.
    """
    from ..overlay import ElementStyle

    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle(f"Element: {title}")
    form = QtWidgets.QFormLayout(dlg)

    label_edit = QtWidgets.QLineEdit(current.label)
    label_edit.setPlaceholderText("(default)")
    form.addRow("Label", label_edit)

    show_label = QtWidgets.QCheckBox("Show label")
    show_label.setChecked(current.show_label)
    form.addRow("", show_label)

    colour = _ColorField(current.color)
    form.addRow("Colour", colour)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setFocus()

    if dlg.exec() != QtWidgets.QDialog.Accepted:
        return None
    return ElementStyle(label=label_edit.text().strip(),
                        show_label=show_label.isChecked(),
                        color=colour.value())


def _edit_channel_style(parent, channel_name, auto_label, current):
    """Edit a custom channel's label, value conversion and precision.

    Returns a new ChannelStyle, or None if cancelled. The conversion list is
    the affine presets from the overlay module plus a Custom option that
    exposes raw scale and offset for anything not covered.
    """
    from ..overlay import ChannelStyle, CHANNEL_CONVERSIONS

    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle(f"Readout: {channel_name}")
    form = QtWidgets.QFormLayout(dlg)

    label_edit = QtWidgets.QLineEdit(current.label or auto_label)
    form.addRow("Label", label_edit)

    conv = QtWidgets.QComboBox()
    conv.setStyleSheet(_COMBO_CSS)
    names = list(CHANNEL_CONVERSIONS.keys())
    for n in names:
        conv.addItem(n)
    conv.addItem("Custom…")
    # preselect the preset that matches the current scale/offset, else Custom
    match = "Custom…"
    for n, (sc, off) in CHANNEL_CONVERSIONS.items():
        if abs(sc - current.scale) < 1e-6 and abs(off - current.offset) < 1e-6:
            match = n
            break
    conv.setCurrentText(match)
    form.addRow("Convert", conv)

    scale_edit = QtWidgets.QDoubleSpinBox()
    scale_edit.setRange(-1e6, 1e6)
    scale_edit.setDecimals(5)
    scale_edit.setValue(current.scale)
    offset_edit = QtWidgets.QDoubleSpinBox()
    offset_edit.setRange(-1e6, 1e6)
    offset_edit.setDecimals(3)
    offset_edit.setValue(current.offset)
    form.addRow("Scale", scale_edit)
    form.addRow("Offset", offset_edit)

    dec = QtWidgets.QComboBox()
    dec.setStyleSheet(_COMBO_CSS)
    dec.addItem("Auto", None)
    for n in (0, 1, 2):
        dec.addItem(str(n), n)
    dec.setCurrentIndex(0 if current.decimals is None else current.decimals + 1)
    form.addRow("Decimals", dec)

    colour = _ColorField(current.color)
    form.addRow("Colour", colour)

    def sync_custom() -> None:
        # scale/offset are only editable under Custom; a preset drives them
        is_custom = conv.currentText() == "Custom…"
        scale_edit.setEnabled(is_custom)
        offset_edit.setEnabled(is_custom)
        if not is_custom:
            sc, off = CHANNEL_CONVERSIONS[conv.currentText()]
            scale_edit.setValue(sc)
            offset_edit.setValue(off)

    conv.currentIndexChanged.connect(lambda *_: sync_custom())
    sync_custom()

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setFocus()

    if dlg.exec() != QtWidgets.QDialog.Accepted:
        return None
    label = label_edit.text().strip()
    return ChannelStyle(
        label=("" if label == auto_label else label),
        scale=scale_edit.value(),
        offset=offset_edit.value(),
        decimals=dec.currentData(),
        color=colour.value())


class OverlayEditorDialog(QtWidgets.QDialog):
    """Shape an OverlayLayout against a real frame, then hand it back.

    The result is read from ``self.layout`` after ``exec()`` returns accepted.
    The layout is edited in place, so a caller that wants to keep the original
    should pass a copy.
    """

    def __init__(self, parent, data: OverlayData, layout: OverlayLayout,
                 preview_lap_t: float, video_frame=None):
        super().__init__(parent)
        self.setWindowTitle("Overlay layout")
        self.resize(760, 560)
        self.data = data
        self.layout_obj = layout

        root = QtWidgets.QHBoxLayout(self)

        self.canvas = LayoutCanvas(data, layout, preview_lap_t, video_frame)
        root.addWidget(self.canvas, 1)

        side = QtWidgets.QVBoxLayout()

        # video fit: only meaningful when the canvas and source aspects differ,
        # but harmless otherwise, so always shown
        side.addWidget(self._heading("Video"))
        self.fit_box = QtWidgets.QComboBox()
        self.fit_box.setStyleSheet(_COMBO_CSS)
        self.fit_box.addItem("Fit (whole frame, letterboxed)", "fit")
        self.fit_box.addItem("Fill (crop to fill)", "fill")
        self.fit_box.setCurrentIndex(0 if layout.video_fit == "fit" else 1)
        self.fit_box.currentIndexChanged.connect(self._set_fit)
        side.addWidget(self.fit_box)

        self.crop_label = QtWidgets.QLabel("Crop position")
        self.crop_label.setStyleSheet("color:#8b93a1;")
        side.addWidget(self.crop_label)
        self.crop_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.crop_slider.setRange(0, 100)
        # a portrait canvas crops horizontally; a landscape one vertically
        portrait_canvas = layout.width < layout.height
        self._crop_axis = "x" if portrait_canvas else "y"
        self.crop_slider.setValue(int((layout.crop_x if portrait_canvas
                                       else layout.crop_y) * 100))
        self.crop_slider.valueChanged.connect(self._set_crop)
        side.addWidget(self.crop_slider)
        self._sync_crop_enabled()

        side.addSpacing(12)
        side.addWidget(self._heading("Show"))
        self._checks = {}
        for key in ELEMENTS:
            row = QtWidgets.QHBoxLayout()
            cb = QtWidgets.QCheckBox(ELEMENT_LABELS.get(key, key))
            cb.setChecked(layout.shows(key))
            cb.toggled.connect(lambda on, k=key: self._toggle(k, on))
            edit = QtWidgets.QPushButton("✎")
            edit.setMaximumWidth(24)
            edit.setToolTip("Label, colour and visibility for this element")
            edit.clicked.connect(lambda _=False, k=key: self._edit_element(k))
            row.addWidget(cb, 1)
            row.addWidget(edit)
            side.addLayout(row)
            self._checks[key] = cb

        # custom data channels, added on demand
        self._extra_box = QtWidgets.QVBoxLayout()
        self._extra_box.setSpacing(2)
        side.addLayout(self._extra_box)
        self.add_btn = QtWidgets.QPushButton("+ Data channel")
        self.add_btn.setToolTip(
            "Add an extra channel from the log — RPM, temperatures, and so "
            "on — as its own readout")
        self.add_btn.clicked.connect(self._add_channel)
        side.addWidget(self.add_btn)
        # rebuild rows for any channels already in the layout (e.g. reopened)
        self._extra_rows = {}
        self._extra_labels = {}
        for key in [k for k in layout.show if k.startswith("ch:")]:
            self._add_channel_row(key)

        side.addSpacing(12)
        side.addWidget(self._heading("Panel opacity"))
        self.opacity = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.opacity.setRange(20, 100)
        self.opacity.setValue(int(layout.panel_opacity * 100))
        self.opacity.valueChanged.connect(self._set_opacity)
        side.addWidget(self.opacity)
        self.opacity_label = QtWidgets.QLabel(
            f"{int(layout.panel_opacity * 100)}%")
        self.opacity_label.setStyleSheet("color:#8b93a1;")
        side.addWidget(self.opacity_label)

        side.addSpacing(12)
        reset = QtWidgets.QPushButton("Reset to default")
        reset.clicked.connect(self._reset)
        side.addWidget(reset)

        side.addStretch(1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        side.addWidget(buttons)

        wrap = QtWidgets.QWidget()
        wrap.setLayout(side)
        wrap.setFixedWidth(220)
        root.addWidget(wrap)

    def _add_channel(self) -> None:
        """Offer the log's spare channels and add the chosen one as a readout."""
        available = [c for c in self.data.extra_channels()
                     if f"ch:{c}" not in self.layout_obj.boxes]
        if not available:
            QtWidgets.QMessageBox.information(
                self, "Add data channel",
                "No more extra channels in this log to add.")
            return
        name = _pick_channel(self, available)
        if name is None:
            return
        key = f"ch:{name}"
        # drop it in the middle so it is easy to grab, then drag into place
        self.layout_obj.boxes[key] = (0.42, 0.45, 0.16, 0.13)
        self.layout_obj.show = tuple(self.layout_obj.show) + (key,)
        self._add_channel_row(key)
        self.canvas.set_layout(self.layout_obj)

    def _add_channel_row(self, key: str) -> None:
        row = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel(self._row_label(key))
        label.setStyleSheet("color:#e8ecf2;")
        edit = QtWidgets.QPushButton("✎")
        edit.setMaximumWidth(24)
        edit.setToolTip("Label and units for this readout")
        edit.clicked.connect(lambda _=False, k=key: self._edit_channel(k))
        remove = QtWidgets.QPushButton("✕")
        remove.setMaximumWidth(24)
        remove.setToolTip("Remove this readout")
        remove.clicked.connect(lambda _=False, k=key: self._remove_channel(k))
        row.addWidget(label, 1)
        row.addWidget(edit)
        row.addWidget(remove)
        self._extra_box.addLayout(row)
        self._extra_rows[key] = row
        self._extra_labels[key] = label

    def _row_label(self, key: str) -> str:
        from ..overlay import _channel_label
        style = self.layout_obj.channel_styles.get(key)
        if style is not None and style.label:
            return style.label
        return _channel_label(key[3:])

    def _edit_element(self, key: str) -> None:
        """Style a built-in element: caption text, visibility and colour."""
        from ..overlay import ElementStyle
        current = self.layout_obj.element_styles.get(key) or ElementStyle()
        style = _edit_element_style(self, ELEMENT_LABELS.get(key, key),
                                    key, current)
        if style is None:
            return
        self.layout_obj.element_styles[key] = style
        self.canvas.set_layout(self.layout_obj)

    def _edit_channel(self, key: str) -> None:
        from ..overlay import ChannelStyle, _channel_label
        current = self.layout_obj.channel_styles.get(key) or ChannelStyle()
        auto = _channel_label(key[3:])
        style = _edit_channel_style(self, key[3:], auto, current)
        if style is None:
            return
        self.layout_obj.channel_styles[key] = style
        self._extra_labels[key].setText(self._row_label(key))
        self.canvas.set_layout(self.layout_obj)

    def _remove_channel(self, key: str) -> None:
        self.layout_obj.boxes.pop(key, None)
        self.layout_obj.channel_styles.pop(key, None)
        self._extra_labels.pop(key, None)
        self.layout_obj.show = tuple(k for k in self.layout_obj.show
                                     if k != key)
        row = self._extra_rows.pop(key, None)
        if row is not None:
            while row.count():
                item = row.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self._extra_box.removeItem(row)
        self.canvas.set_layout(self.layout_obj)

    def _heading(self, text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setStyleSheet("color:#e8ecf2; font-weight:600;")
        return label

    def _toggle(self, key: str, on: bool) -> None:
        show = set(self.layout_obj.show)
        if on:
            show.add(key)
        else:
            show.discard(key)
        # rebuild with the built-ins in their fixed order, then keep any custom
        # channel readouts. Rebuilding from ELEMENTS alone dropped every ch:
        # channel whenever a default was toggled.
        ordered = [k for k in ELEMENTS if k in show]
        ordered += [k for k in self.layout_obj.show
                    if k.startswith("ch:") and k in show]
        self.layout_obj.show = tuple(ordered)
        self.canvas.set_layout(self.layout_obj)

    def _set_fit(self, _index: int) -> None:
        self.layout_obj.video_fit = self.fit_box.currentData()
        self._sync_crop_enabled()
        self.canvas.set_layout(self.layout_obj)

    def _set_crop(self, value: int) -> None:
        frac = value / 100.0
        if self._crop_axis == "x":
            self.layout_obj.crop_x = frac
        else:
            self.layout_obj.crop_y = frac
        self.canvas.set_layout(self.layout_obj)

    def _sync_crop_enabled(self) -> None:
        # the crop position only applies in fill mode
        on = self.layout_obj.video_fit == "fill"
        self.crop_slider.setEnabled(on)
        self.crop_label.setEnabled(on)

    def _set_opacity(self, value: int) -> None:
        self.layout_obj.panel_opacity = value / 100.0
        self.opacity_label.setText(f"{value}%")
        self.canvas.set_layout(self.layout_obj)

    def _reset(self) -> None:
        fresh = (portrait() if self.layout_obj.name == "portrait"
                 else landscape())
        # tear down any custom-channel rows
        for key in list(self._extra_rows):
            self._remove_channel(key)
        self.layout_obj.boxes = dict(fresh.boxes)
        self.layout_obj.show = fresh.show
        self.layout_obj.panel_opacity = fresh.panel_opacity
        self.layout_obj.video_fit = fresh.video_fit
        self.layout_obj.crop_x = fresh.crop_x
        self.layout_obj.crop_y = fresh.crop_y
        self.layout_obj.element_styles = dict(fresh.element_styles)
        self.fit_box.blockSignals(True)
        self.fit_box.setCurrentIndex(0 if fresh.video_fit == "fit" else 1)
        self.fit_box.blockSignals(False)
        self.crop_slider.blockSignals(True)
        self.crop_slider.setValue(int((fresh.crop_x if self._crop_axis == "x"
                                       else fresh.crop_y) * 100))
        self.crop_slider.blockSignals(False)
        self._sync_crop_enabled()
        for key, cb in self._checks.items():
            cb.blockSignals(True)
            cb.setChecked(self.layout_obj.shows(key))
            cb.blockSignals(False)
        self.opacity.blockSignals(True)
        self.opacity.setValue(int(fresh.panel_opacity * 100))
        self.opacity.blockSignals(False)
        self.opacity_label.setText(f"{int(fresh.panel_opacity * 100)}%")
        self.canvas.set_layout(self.layout_obj)

    @property
    def layout(self) -> OverlayLayout:
        return self.layout_obj
