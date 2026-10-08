"""
Telemetry overlay rendering.

Turns a lap into a data-rich heads-up display that can sit on top of onboard
video: current speed, a live delta to the reference lap, throttle and brake
bars, lateral-g, a moving dot on a corner map, and the lap clock. One frame is
rendered at a time for a given moment of the lap, so the same code drives a
live preview and a full render.

The module is deliberately self-contained and has no dependency on the GUI or
on a video being present. It draws to an RGBA image with a transparent
background using Pillow, which makes every pixel testable off-screen — the
thing the rest of the video path is not — and lets the result be composited
over footage later by whatever does the encoding.

Two layouts are provided from the outset:

* **landscape** — a bar across the bottom, sized for 1920x1080 and up, for the
  usual widescreen edit.
* **portrait** — a taller stack for 1080x1920 phone video, where horizontal
  space is scarce and elements sit above one another.

Everything degrades to what the log actually carries. A file with no throttle
channel simply omits the pedal bars rather than drawing empty ones, and a lap
with no reference to compare against omits the delta rather than showing zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:                                       # pragma: no cover
    PIL_AVAILABLE = False

from .laps import LapTrack
from .units import UnitSystem, METRIC


# --------------------------------------------------------------------------
# Colours, shared with the rest of the app so a render matches the screen
# --------------------------------------------------------------------------

INK = (232, 236, 242, 255)
DIM = (139, 147, 161, 255)
FAINT = (90, 97, 110, 255)
PANEL = (16, 19, 26, 150)          # translucent, so footage shows through
LOSS = (229, 72, 77, 255)
GAIN = (61, 214, 140, 255)
THROTTLE = (61, 214, 140, 255)
BRAKE = (229, 72, 77, 255)
SPEED_RAMP = ((60, 90, 200), (90, 200, 210), (240, 200, 70), (235, 80, 70))
TRACE = (90, 97, 110, 220)
DOT = (255, 255, 255, 255)


#: every element the overlay can draw, in a stable order for the editor
ELEMENTS = ("speed", "clock", "delta", "pedals", "latg", "map")


@dataclass
class ChannelStyle:
    """How a custom channel readout is labelled and converted.

    The value shown is ``raw * scale + offset``; a preset like Fahrenheit to
    Celsius is just a scale and offset, so one affine transform covers the
    common conversions without a lookup table. An empty label falls back to the
    automatic one, and `decimals=None` picks precision from the magnitude.
    """
    label: str = ""
    scale: float = 1.0
    offset: float = 0.0
    decimals: Optional[int] = None
    color: Optional[str] = None          # hex "#RRGGBB"; None = default
    #: caption position as a fraction of the box; None = default placement
    label_pos: Optional[Tuple[float, float]] = None

    def apply(self, raw: float) -> float:
        return raw * self.scale + self.offset

    def to_dict(self) -> dict:
        return {"label": self.label, "scale": self.scale,
                "offset": self.offset, "decimals": self.decimals,
                "color": self.color,
                "label_pos": list(self.label_pos) if self.label_pos else None}

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelStyle":
        lp = d.get("label_pos")
        return cls(label=d.get("label", ""),
                   scale=float(d.get("scale", 1.0)),
                   offset=float(d.get("offset", 0.0)),
                   decimals=d.get("decimals"),
                   color=d.get("color"),
                   label_pos=tuple(lp) if lp else None)


@dataclass
class ElementStyle:
    """Per-element overrides for a built-in overlay element.

    `label` replaces the element's caption (empty keeps the default),
    `show_label` hides that caption when false, and `color` recolours the
    element's primary graphic (empty/None keeps the element's own colouring,
    which for speed is the speed ramp and for delta the ahead/behind colours).
    """
    label: str = ""
    show_label: bool = True
    color: Optional[str] = None
    #: caption position as a fraction of the element box (x, y from top-left);
    #: None keeps the element's built-in caption placement
    label_pos: Optional[Tuple[float, float]] = None

    def to_dict(self) -> dict:
        return {"label": self.label, "show_label": self.show_label,
                "color": self.color,
                "label_pos": list(self.label_pos) if self.label_pos else None}

    @classmethod
    def from_dict(cls, d: dict) -> "ElementStyle":
        lp = d.get("label_pos")
        return cls(label=d.get("label", ""),
                   show_label=bool(d.get("show_label", True)),
                   color=d.get("color"),
                   label_pos=tuple(lp) if lp else None)


def _hex_rgba(value: Optional[str], fallback):
    """A hex "#RRGGBB" string as an RGBA tuple, or the fallback if unset/bad."""
    if not value:
        return fallback
    v = value.lstrip("#")
    try:
        r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
        return (r, g, b, 255)
    except (ValueError, IndexError):
        return fallback


#: named value conversions offered in the editor. Each is (scale, offset) so
#: display = raw*scale + offset.
CHANNEL_CONVERSIONS = {
    "None": (1.0, 0.0),
    "°F → °C": (5.0 / 9.0, -32.0 * 5.0 / 9.0),
    "°C → °F": (9.0 / 5.0, 32.0),
    "mph → km/h": (1.609344, 0.0),
    "km/h → mph": (0.621371, 0.0),
    "psi → bar": (0.0689476, 0.0),
    "bar → psi": (14.5038, 0.0),
}


@dataclass
class OverlayLayout:
    """Where each element sits and whether it shows, for one output aspect.

    Positions are fractions of the frame, so the same layout scales to any
    resolution of the same shape. This is what the editor edits: it moves the
    boxes, toggles `show`, and sets `panel_opacity`; the renderer reads them.
    """
    name: str
    width: int
    height: int
    #: (x, y, w, h) as fractions of the frame, per element
    boxes: Dict[str, Tuple[float, float, float, float]]
    show: Tuple[str, ...] = ELEMENTS
    #: 0..1 backing-panel opacity, raised for legibility over busy footage
    panel_opacity: float = 0.72        # was 0.59; washed out on bright track
    #: per custom-channel styling, keyed by the "ch:name" element key
    channel_styles: Dict[str, ChannelStyle] = field(default_factory=dict)
    #: per built-in element styling, keyed by element name (speed, delta, ...)
    element_styles: Dict[str, ElementStyle] = field(default_factory=dict)
    #: how the source video fills the output canvas when the aspects differ:
    #: "fit" letterboxes the whole frame, "fill" crops it to fill the canvas
    video_fit: str = "fit"
    #: for "fill", the crop window centre as a 0..1 fraction of the source
    #: (0.5 = centred). x matters for a portrait canvas from wide footage;
    #: y matters for a landscape canvas from tall footage.
    crop_x: float = 0.5
    crop_y: float = 0.5

    def px(self, key: str) -> Tuple[int, int, int, int]:
        x, y, w, h = self.boxes[key]
        return (int(x * self.width), int(y * self.height),
                int(w * self.width), int(h * self.height))

    def shows(self, key: str) -> bool:
        return key in self.show

    def with_size(self, width: int, height: int) -> "OverlayLayout":
        """A copy at a different pixel size (same fractional layout)."""
        from dataclasses import replace
        return replace(self, width=width, height=height)

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "boxes": {k: list(v) for k, v in self.boxes.items()},
            "show": list(self.show),
            "panel_opacity": self.panel_opacity,
            "channel_styles": {k: v.to_dict()
                               for k, v in self.channel_styles.items()},
            "video_fit": self.video_fit,
            "crop_x": self.crop_x,
            "crop_y": self.crop_y,
            "element_styles": {k: v.to_dict()
                               for k, v in self.element_styles.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OverlayLayout":
        return cls(
            name=d.get("name", "custom"),
            width=int(d["width"]),
            height=int(d["height"]),
            boxes={k: tuple(v) for k, v in d["boxes"].items()},
            show=tuple(d.get("show", ELEMENTS)),
            panel_opacity=float(d.get("panel_opacity", 0.59)),
            channel_styles={k: ChannelStyle.from_dict(v) for k, v
                            in d.get("channel_styles", {}).items()},
            video_fit=d.get("video_fit", "fit"),
            crop_x=float(d.get("crop_x", 0.5)),
            crop_y=float(d.get("crop_y", 0.5)),
            element_styles={k: ElementStyle.from_dict(v) for k, v
                            in d.get("element_styles", {}).items()})


def landscape(width: int = 1920, height: int = 1080) -> OverlayLayout:
    """A HUD strip across the bottom, elements laid out left to right."""
    return OverlayLayout(
        name="landscape", width=width, height=height,
        boxes={
            "speed": (0.02, 0.78, 0.21, 0.19),
            "clock": (0.02, 0.71, 0.21, 0.06),
            "delta": (0.24, 0.80, 0.20, 0.17),
            "pedals": (0.46, 0.72, 0.06, 0.25),
            "latg": (0.55, 0.74, 0.12, 0.22),
            "map": (0.83, 0.66, 0.15, 0.31),
        })


def portrait(width: int = 1080, height: int = 1920) -> OverlayLayout:
    """A taller stack for phone video: elements above one another, not beside.

    Vertical video has almost no horizontal room, so the widescreen strip does
    not transplant — the map goes top-right, the readouts run down the left,
    and the pedal and g meters share a row.
    """
    return OverlayLayout(
        name="portrait", width=width, height=height,
        boxes={
            "map": (0.60, 0.03, 0.37, 0.21),
            "speed": (0.04, 0.03, 0.42, 0.15),
            "clock": (0.04, 0.16, 0.42, 0.05),
            "delta": (0.04, 0.82, 0.44, 0.12),
            "pedals": (0.52, 0.84, 0.10, 0.13),
            "latg": (0.68, 0.83, 0.22, 0.14),
        })


LAYOUTS = {"landscape": landscape, "portrait": portrait}


# --------------------------------------------------------------------------
# What the overlay knows about the lap — computed once, sampled per frame
# --------------------------------------------------------------------------


@dataclass
class OverlayData:
    """Everything an overlay draws, prepared once for a lap.

    Holds references to the whole-lap series so a frame is a cheap lookup at a
    distance rather than a recompute. Channels the log lacks are left as None
    and their elements are skipped.
    """
    lap: LapTrack
    units: UnitSystem
    #: track outline in overlay pixels, precomputed for the map box it targets
    corners: Sequence = ()
    reference: Optional[LapTrack] = None
    s_delta: Optional[np.ndarray] = None
    delta: Optional[np.ndarray] = None
    has_throttle: bool = False
    has_brake: bool = False
    has_latg: bool = False
    lap_time: float = 0.0

    #: channels that already have a dedicated element, so the generic
    #: "extra data" list does not offer a duplicate of them
    _NATIVE = frozenset((
        "throttle", "brake", "speed", "speed_kmh", "lat", "lon", "t", "time",
        "tsample", "lap", "sector", "avitime", "avifileindex",
    ))

    @classmethod
    def from_analysis(cls, analysis, units: UnitSystem = METRIC) -> "OverlayData":
        lap = analysis.lap
        extras = lap.extras
        return cls(
            lap=lap,
            units=units,
            corners=analysis.corners,
            reference=(analysis.reference
                       if analysis.reference is not lap else None),
            s_delta=analysis.s_delta,
            delta=analysis.delta,
            has_throttle="throttle" in extras,
            has_brake="brake" in extras,
            has_latg=_finite(getattr(lap, "ay_g", None)),
            lap_time=float(lap.t[-1] - lap.t[0]) if lap.t.size else 0.0)

    def extra_channels(self) -> list:
        """Log channels that can be added as a generic readout.

        Anything carried through to the lap that has real, varying data and is
        not already shown by a dedicated element — engine speed, temperatures,
        and so on. This is what the editor's "add data" list offers.
        """
        out = []
        for name in sorted(self.lap.extras):
            if name in self._NATIVE or name.endswith("_gps"):
                continue
            if _finite(self.lap.extras[name]):
                out.append(name)
        return out

    def channel_value(self, name: str, s: float):
        """The value of an extra channel at distance s, or None if absent."""
        series = self.lap.extras.get(name)
        if series is None:
            return None
        import numpy as _np
        return float(self.lap.at(s, _np.asarray(series, dtype=float)))

    # -- per-frame samples -------------------------------------------------

    def s_at_time(self, lap_t: float) -> float:
        """Distance travelled at a time into the lap, clamped to the lap."""
        t = self.lap.t - self.lap.t[0]
        return float(np.interp(np.clip(lap_t, 0.0, float(t[-1])), t, self.lap.s))

    def speed_kmh(self, s: float) -> float:
        return float(self.lap.at(s, self.lap.speed_kmh))

    def delta_at(self, s: float) -> Optional[float]:
        if self.s_delta is None or self.delta is None:
            return None
        return float(np.interp(s, self.s_delta, self.delta))

    def latg(self, s: float) -> Optional[float]:
        if not self.has_latg:
            return None
        return float(self.lap.at(s, self.lap.ay_g))

    def pedal(self, s: float, which: str) -> Optional[float]:
        if which not in self.lap.extras:
            return None
        series = np.asarray(self.lap.extras[which], dtype=float)
        v = float(self.lap.at(s, series))
        # normalise to 0..1: channels come as 0..100 or 0..1 depending on logger
        peak = float(np.nanmax(np.abs(series))) if series.size else 1.0
        return float(np.clip(v / peak, 0.0, 1.0)) if peak > 0 else 0.0


def _finite(arr) -> bool:
    if arr is None:
        return False
    a = np.asarray(arr, dtype=float)
    return bool(a.size) and bool(np.any(np.isfinite(a))) and float(np.ptp(a)) > 1e-6


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


# candidate font files per platform. Plain .ttf files are preferred over .ttc
# collections: a collection needs a font index, and asking for one without it
# can make Pillow's renderer divide by zero at small sizes (seen on macOS with
# Helvetica.ttc). The macOS "Supplemental" Arial/Verdana are plain .ttf and
# render cleanly.
_FONT_CANDIDATES = {
    True: [   # bold
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    False: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Verdana.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ],
}

#: (path-or-None, size) -> font, so each face is loaded and proven once
_FONT_CACHE: Dict[Tuple[Optional[str], int, bool], object] = {}


def _renders(font) -> bool:
    """Confirm a font can actually rasterise text at its size.

    A font can load and still throw when asked to render — a .ttc collection
    opened without an index divides by zero in the rasteriser on some builds.
    Better to find that out here, on a throwaway draw, than mid-export.
    """
    try:
        img = Image.new("RGBA", (8, 8))
        ImageDraw.Draw(img).text((0, 0), "0", font=font, fill=(255, 255, 255))
        return True
    except Exception:                                     # noqa: BLE001
        return False


def _font(size: int, bold: bool = False):
    """A usable font at `size`, tried real faces first, default as a fallback.

    Never returns a font that cannot render: every candidate is proven with a
    throwaway draw before it is trusted, and if none work the scalable default
    is used. This is what keeps a machine with unusual fonts from crashing the
    overlay instead of just looking plainer.
    """
    size = max(1, int(size))
    key = (None, size, bold)
    for path in _FONT_CANDIDATES[bool(bold)]:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            continue
        if _renders(font):
            return font
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    try:
        font = ImageFont.load_default(size=size)          # Pillow >= 10
        if not _renders(font):
            raise OSError
    except (OSError, TypeError):
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


#: short, overlay-sized labels for common extra channels
_CHANNEL_LABELS = {
    "enginespeedrpm": "rpm",
    "rpm": "rpm",
    "enginecoolanttempf": "coolant °F",
    "enginecoolanttemp": "coolant",
    "intakeairtempf": "intake °F",
    "vehiclespeedmph": "veh mph",
    "height": "elev",
    "heading": "heading",
    "gear": "gear",
    "steer": "steer",
    "oilpressure": "oil psi",
    "oiltemp": "oil temp",
}


def _channel_label(name: str) -> str:
    if name in _CHANNEL_LABELS:
        return _CHANNEL_LABELS[name]
    pretty = name.replace("_", " ")
    return pretty if len(pretty) <= 12 else pretty[:11] + "…"


def _ramp(fraction: float) -> Tuple[int, int, int, int]:
    """Speed colour from the same blue-cyan-yellow-red ramp as the map."""
    f = float(np.clip(fraction, 0.0, 1.0)) * (len(SPEED_RAMP) - 1)
    i = int(f)
    if i >= len(SPEED_RAMP) - 1:
        r, g, b = SPEED_RAMP[-1]
        return (r, g, b, 255)
    t = f - i
    a, b = SPEED_RAMP[i], SPEED_RAMP[i + 1]
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t), 255)


def _panel(draw, box, radius_frac: float = 0.12,
           opacity: float = 0.72) -> None:
    x, y, w, h = box
    r = int(min(w, h) * radius_frac)
    alpha = int(max(0.0, min(1.0, opacity)) * 255)
    fill = (PANEL[0], PANEL[1], PANEL[2], alpha)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=fill)


class OverlayRenderer:
    """Draws one overlay frame at a time for a lap.

    Track geometry for the map is projected once per layout and cached, since
    it is fixed for the whole lap; everything else is a cheap per-frame draw.
    """

    def __init__(self, data: OverlayData, layout: OverlayLayout):
        if not PIL_AVAILABLE:                             # pragma: no cover
            raise RuntimeError(
                "The overlay renderer needs Pillow: pip install pillow")
        self.data = data
        self.layout = layout
        self._track_px = self._project_track()
        u = min(layout.width, layout.height)
        self._f_big = _font(int(u * 0.075), bold=True)
        self._f_med = _font(int(u * 0.032), bold=True)
        self._f_small = _font(int(u * 0.022))
        self._f_tiny = _font(int(u * 0.017))

    # -- track projection for the mini map --------------------------------

    def _project_track(self) -> Optional[np.ndarray]:
        if "map" not in self.layout.boxes:
            return None
        lap = self.data.lap
        x, y = np.asarray(lap.x, dtype=float), np.asarray(lap.y, dtype=float)
        if x.size < 3:
            return None
        bx, by, bw, bh = self.layout.px("map")
        pad = int(min(bw, bh) * 0.16)
        bx, by, bw, bh = bx + pad, by + pad, bw - 2 * pad, bh - 2 * pad
        # y is flipped: screen y grows downward, track north grows up
        span_x = float(np.ptp(x)) or 1.0
        span_y = float(np.ptp(y)) or 1.0
        scale = min(bw / span_x, bh / span_y)
        ox = bx + (bw - span_x * scale) / 2 - float(np.min(x)) * scale
        oy = by + (bh - span_y * scale) / 2 + float(np.max(y)) * scale
        px = ox + x * scale
        py = oy - y * scale
        return np.column_stack([px, py])

    # -- the full frame ----------------------------------------------------

    def frame(self, lap_t: float) -> "Image.Image":
        """Render the overlay for a moment `lap_t` seconds into the lap."""
        img = Image.new("RGBA", (self.layout.width, self.layout.height),
                        (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        s = self.data.s_at_time(lap_t)
        show = self.layout.show

        if "map" in show and self._track_px is not None:
            self._draw_map(draw, s)
        if "speed" in show:
            self._draw_speed(draw, s)
        if "clock" in show:
            self._draw_clock(draw, lap_t)
        if "delta" in show:
            self._draw_delta(draw, s)
        if "pedals" in show:
            self._draw_pedals(draw, s)
        if "latg" in show:
            self._draw_latg(draw, s)
        for key in show:
            if key.startswith("ch:"):
                self._draw_channel(draw, s, key)
        return img

    # -- elements ----------------------------------------------------------

    def _style(self, key: str) -> "ElementStyle":
        return self.layout.element_styles.get(key) or ElementStyle()

    def _label_xy(self, key: str, box, default_fx: float, default_fy: float):
        """Caption position in pixels: the style's label_pos if set, else the
        element's default placement (fractions of the box)."""
        x, y, w, h = box
        st = self._style(key)
        fx, fy = st.label_pos if st.label_pos else (default_fx, default_fy)
        return (x + w * fx, y + h * fy)

    def _draw_speed(self, draw, s: float) -> None:
        box = self.layout.px("speed")
        _panel(draw, box, opacity=self.layout.panel_opacity)
        x, y, w, h = box
        st = self._style("speed")
        kmh = self.data.speed_kmh(s)
        value = self.data.units.spd(kmh)
        frac = kmh / 260.0
        colour = _hex_rgba(st.color, _ramp(frac))     # override replaces ramp
        draw.text((x + w * 0.06, y + h * 0.06), f"{value:.0f}",
                  font=self._f_big, fill=colour)
        if st.show_label:
            label = st.label or self.data.units.speed_label
            draw.text(self._label_xy("speed", box, 0.06, 0.72),
                      label, font=self._f_small, fill=DIM)

    def _draw_clock(self, draw, lap_t: float) -> None:
        box = self.layout.px("clock")
        _panel(draw, box, opacity=self.layout.panel_opacity)
        x, y, w, h = box
        st = self._style("clock")
        mins = int(lap_t // 60)
        secs = lap_t - mins * 60
        text = f"{mins}:{secs:06.3f}" if mins else f"{secs:.3f}"
        draw.text((x + w * 0.06, y + h * 0.30), text, font=self._f_med,
                  fill=_hex_rgba(st.color, INK))
        if st.show_label:
            draw.text(self._label_xy("clock", box, 0.06, 0.06),
                      st.label or "LAP", font=self._f_small, fill=DIM)

    def _draw_delta(self, draw, s: float) -> None:
        box = self.layout.px("delta")
        _panel(draw, box, opacity=self.layout.panel_opacity)
        x, y, w, h = box
        st = self._style("delta")
        d = self.data.delta_at(s)
        if d is None:
            # No comparison available — the lap is its own reference (viewing
            # the best lap). Show a placeholder instead of a blank hole, so the
            # panel does not look broken.
            draw.text((x + w * 0.06, y + h * 0.08), "\u2014",
                      font=self._f_big, fill=_hex_rgba(st.color, INK))
            if st.show_label:
                label = st.label or "best lap"
                draw.text(self._label_xy("delta", box, 0.06, 0.74),
                          label, font=self._f_small, fill=DIM)
            return
        sign_colour = GAIN if d < 0 else LOSS if d > 0 else INK
        colour = _hex_rgba(st.color, sign_colour)  # override replaces sign col
        draw.text((x + w * 0.06, y + h * 0.08), f"{d:+.2f}",
                  font=self._f_big, fill=colour)
        if st.show_label:
            state = "ahead" if d < 0 else "behind" if d > 0 else "even"
            label = st.label or f"s vs best \u2014 {state}"
            draw.text(self._label_xy("delta", box, 0.06, 0.74),
                      label, font=self._f_small, fill=DIM)

    def _draw_pedals(self, draw, s: float) -> None:
        thr = self.data.pedal(s, "throttle") if self.data.has_throttle else None
        brk = self.data.pedal(s, "brake") if self.data.has_brake else None
        if thr is None and brk is None:
            return
        box = self.layout.px("pedals")
        _panel(draw, box, opacity=self.layout.panel_opacity)
        x, y, w, h = box
        st = self._style("pedals")
        thr_colour = _hex_rgba(st.color, THROTTLE)
        pad = int(w * 0.14)
        inner_h = h - 2 * pad
        cols = [c for c in ((thr, thr_colour, "T"), (brk, BRAKE, "B"))
                if c[0] is not None]
        bw = (w - 2 * pad) / (len(cols) * 2 - 1) if cols else 0
        cx = x + pad
        # the bar trough and fill scale with the panel opacity too, so the
        # whole element fades together. A fixed-alpha trough stayed solid while
        # the panel behind it faded, which read as "pedals ignore the slider".
        alpha = int(max(0.0, min(1.0, self.layout.panel_opacity)) * 255)
        trough = (40, 44, 52, alpha)
        for value, colour, label in cols:
            top = y + pad + inner_h * (1.0 - value)
            fill = (colour[0], colour[1], colour[2], alpha)
            draw.rectangle([cx, y + pad, cx + bw, y + pad + inner_h],
                           fill=trough)
            draw.rectangle([cx, top, cx + bw, y + pad + inner_h], fill=fill)
            if st.show_label:
                draw.text((cx, y + pad + inner_h + 2), label,
                          font=self._f_tiny, fill=DIM)
            cx += bw * 2

    def _draw_latg(self, draw, s: float) -> None:
        g = self.data.latg(s)
        if g is None:
            return
        box = self.layout.px("latg")
        _panel(draw, box, opacity=self.layout.panel_opacity)
        x, y, w, h = box
        cx, cy = x + w / 2, y + h * 0.44
        radius = min(w, h) * 0.34
        # concentric rings at 1g and 2g for reference
        for ring in (0.5, 1.0):
            rr = radius * ring
            draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                         outline=FAINT, width=max(1, int(radius * 0.03)))
        draw.line([cx - radius, cy, cx + radius, cy], fill=FAINT, width=1)
        # the ball: lateral g left/right, up to ~2g at the rim
        st = self._style("latg")
        px = cx + float(np.clip(g / 2.0, -1.0, 1.0)) * radius
        dot = max(3, int(radius * 0.16))
        draw.ellipse([px - dot, cy - dot, px + dot, cy + dot],
                     fill=_hex_rgba(st.color, DOT))
        if st.show_label:
            text = st.label or f"{abs(g):.1f}g"
            if st.label_pos:
                draw.text(self._label_xy("latg", box, 0.5, 0.88), text,
                          font=self._f_tiny, fill=DIM)
            else:
                # default: centred readout under the dial
                draw.text((cx, y + h - int(h * 0.12)), text,
                          font=self._f_tiny, fill=DIM, anchor="mm")

    def _draw_channel(self, draw, s: float, key: str) -> None:
        """A generic value readout for an arbitrary log channel.

        Reads the channel behind `key` (of the form "ch:name"), formats it with
        a sensible precision and a human label, and draws it in the same panel
        style as the built-in readouts. Missing or empty channels draw nothing,
        so a layout carried to a log without that channel degrades quietly.
        """
        name = key[3:]
        if key not in self.layout.boxes:
            return
        value = self.data.channel_value(name, s)
        if value is None:
            return
        style = self.layout.channel_styles.get(key)
        if style is not None:
            value = style.apply(value)
        box = self.layout.px(key)
        _panel(draw, box, opacity=self.layout.panel_opacity)
        x, y, w, h = box
        # precision: the style's if set, else pick from magnitude
        if style is not None and style.decimals is not None:
            text = f"{value:.{max(0, style.decimals)}f}"
        else:
            text = f"{value:.0f}" if abs(value) >= 100 else f"{value:.1f}"
        label = (style.label if style is not None and style.label
                 else _channel_label(name))
        colour = _hex_rgba(style.color if style is not None else None, INK)
        lp = style.label_pos if style is not None else None
        lx = x + w * (lp[0] if lp else 0.06)
        ly = y + h * (lp[1] if lp else 0.72)
        draw.text((x + w * 0.06, y + h * 0.06), text,
                  font=self._f_big, fill=colour)
        draw.text((lx, ly), label, font=self._f_small, fill=DIM)

    def _draw_map(self, draw, s: float) -> None:
        pts = self._track_px
        box = self.layout.px("map")
        _panel(draw, box, radius_frac=0.08, opacity=self.layout.panel_opacity)
        flat = [tuple(p) for p in pts]
        draw.line(flat + [flat[0]], fill=TRACE,
                  width=max(2, int(self.layout.width * 0.0018)))
        st = self._style("map")
        i = int(np.clip(self.data.lap.idx(s), 0, len(pts) - 1))
        cx, cy = pts[i]
        dot = max(4, int(min(box[2], box[3]) * 0.05))
        draw.ellipse([cx - dot, cy - dot, cx + dot, cy + dot],
                     fill=_hex_rgba(st.color, DOT),
                     outline=(0, 0, 0, 255), width=1)
        if st.show_label:
            draw.text(self._label_xy("map", box, 0.06, 0.04),
                      st.label or "MAP", font=self._f_small, fill=DIM)
