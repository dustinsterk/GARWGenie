"""Main window."""

from __future__ import annotations

import os
import sys
import time
import traceback
from typing import List, Optional

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ..corners import Corner
from ..insights import (LapAnalysis, analyse, consistency_insights,
                        limit_usage, theoretical_best)
from ..corners import GripEnvelope
from ..laps import Session, build_session, channel_label
from ..parser import open_log, parse_vbo
from ..export import session_html
from ..report import fmt_time, insight_report
from ..units import IMPERIAL, METRIC, UnitSystem
from ..video import VideoSync, find_video
from .video_panel import VideoPanel
from .view3d import OPENGL, View3D
from .. import basemap as bm
from .. import geometry as geo
from .widgets import (BG, ChannelPlot, CMP_COLOUR, DeltaPlot, DistanceCursor,
                      GAIN_COLOUR, LOSS_COLOUR, ReadoutBar, REF_COLOUR,
                      SpeedPlot, TrackMap)

STYLE = f"""
QWidget {{ background: {BG}; color: #c8ccd4;
           font-family: -apple-system, 'Segoe UI', sans-serif; font-size: 12px; }}
QGroupBox {{ border: 1px solid #262b33; border-radius: 4px; margin-top: 14px;
             padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px;
                    color: #6b7280; font-size: 10px;
                    text-transform: uppercase; letter-spacing: 1px; }}
QTableWidget {{ background: #171a1f; gridline-color: #262b33;
                border: 1px solid #262b33; selection-background-color: #2d3644; }}
QHeaderView::section {{ background: #1c2027; color: #6b7280; border: 0;
                        border-bottom: 1px solid #262b33; padding: 4px; }}
QTextEdit {{ background: #171a1f; border: 1px solid #262b33; }}
QSplitter::handle {{ background: #262b33; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}

QTabWidget::pane {{ border: 1px solid #262b33; border-radius: 3px;
                    background: {BG}; top: -1px; }}
QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
QTabBar::tab {{ background: #1a1e24; color: #8b93a1; border: 1px solid #262b33;
                border-bottom: 0; border-top-left-radius: 3px;
                border-top-right-radius: 3px; padding: 5px 16px;
                margin-right: 2px; font-size: 11px; letter-spacing: 1px; }}
QTabBar::tab:hover {{ background: #232932; color: #e8ecf2; }}
QTabBar::tab:selected {{ background: #232932; color: #ffffff;
                         border-color: #3a4350; }}
QTabBar::tab:disabled {{ color: #4a515c; }}
QCheckBox {{ color: #8b93a1; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid #3a4350;
                        border-radius: 2px; background: #1a1e24; }}
QCheckBox::indicator:checked {{ background: {REF_COLOUR};
                                border-color: {REF_COLOUR}; }}

/* Toolbars read as a settings strip, not a row of buttons: the selectors are
   flat text with a chevron, and only real actions look pressable. */
QToolBar {{ background: #14161a; border: 0; spacing: 2px; padding: 5px 6px; }}
QToolBar#main {{ border-bottom: 1px solid #262b33; }}
QToolBar#playback {{ border-top: 1px solid #262b33; }}
QToolButton#qt_toolbar_ext_button {{
    background: {REF_COLOUR}; border-radius: 3px; }}
QToolBar QToolButton {{ background: transparent; border: 0; color: #e8ecf2;
    font-size: 12px; padding: 4px 8px; border-radius: 3px; }}
QToolBar QToolButton:hover {{ color: {REF_COLOUR}; }}
QToolBar QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolBar QLabel {{ color: #5a616e; font-size: 10px; letter-spacing: 1px;
                   text-transform: uppercase; padding: 0 2px 0 10px; }}
QToolBar QComboBox {{ background: transparent; border: 0; border-radius: 0;
                      color: #e8ecf2; font-size: 12px;
                      padding: 3px 22px 3px 4px; }}
QToolBar QComboBox:hover {{ color: {REF_COLOUR}; }}
QToolBar QComboBox:focus {{ outline: none; }}
QToolBar QComboBox::drop-down {{ border: 0; background: transparent;
                                 width: 20px; }}
QComboBox QAbstractItemView {{ background: #1c2027; border: 1px solid #303743;
                               selection-background-color: #2d3644;
                               outline: none; padding: 2px; }}
QToolButton {{ background: transparent; border: 0; border-radius: 3px;
               padding: 4px 9px; color: #c8ccd4; }}
QToolButton:hover {{ background: #232932; color: #e8ecf2; }}

/* real controls still look like controls */
QPushButton {{ background: #1f242c; border: 1px solid #303743;
               border-radius: 3px; padding: 4px 10px; }}
QPushButton:hover {{ background: #2a313b; }}
QComboBox#focus {{ background: #1a1e24; border: 1px solid #262b33;
                   border-radius: 3px; padding: 5px 24px 5px 8px;
                   color: #c8ccd4; }}
QComboBox {{ padding-right: 22px; }}
QComboBox QAbstractItemView::item {{ min-height: 22px; padding: 2px 6px; }}
QSlider::groove:horizontal {{ height: 3px; background: #262b33;
                              border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {REF_COLOUR}; width: 11px;
                              margin: -5px 0; border-radius: 5px; }}
QSlider::sub-page:horizontal {{ background: #35505c; border-radius: 2px; }}
"""

SEV_COLOUR = {"high": "#e5484d", "medium": "#f2b134", "low": "#6b7280"}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, path: Optional[str] = None,
                 session: Optional[Session] = None) -> None:
        super().__init__()
        self.setWindowTitle("Lap Analysis — GARW Genie")
        # A window larger than the screen cannot be resized smaller than its
        # contents allow, so size to the available desktop rather than to a
        # fixed number that assumes a big display.
        self.setMinimumSize(720, 480)
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.resize(min(1500, int(avail.width() * 0.92)),
                        min(950, int(avail.height() * 0.92)))
        else:
            self.resize(1200, 800)
        self.session: Optional[Session] = None
        self.analysis: Optional[LapAnalysis] = None
        self.cursor = DistanceCursor()
        self.units: UnitSystem = IMPERIAL
        self._play_s = 0.0
        self._play_rate = 1.0
        #: set while playback is stepping from one lap to the next, so the
        #: refresh that follows does not stop the transport it is continuing
        self._carry_on = False
        self._last_corner: Optional[int] = None
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)                # ~30 fps
        self._timer.timeout.connect(self._tick)
        #: real elapsed time between ticks. A Qt timer is not a clock — under
        #: load it fires late, and advancing the playhead by the *nominal*
        #: interval makes it fall behind the video a little every frame.
        self._clock = QtCore.QElapsedTimer()
        self._build()
        if session is not None:
            self._adopt(session)
        elif path:
            self.load(path)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        self.setStyleSheet(STYLE)

        bar = self.addToolBar("main")
        bar.setObjectName("main")
        bar.setMovable(False)
        # let the toolbar overflow into a chevron menu instead of forcing the
        # whole window wider than a laptop screen
        bar.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                          QtWidgets.QSizePolicy.Preferred)
        open_act = QtGui.QAction("Open log", self)
        open_act.setShortcut("Ctrl+O")
        open_act.triggered.connect(self._open)
        bar.addAction(open_act)
        bar.addSeparator()

        bar.addWidget(QtWidgets.QLabel("  reference "))
        self.ref_box = QtWidgets.QComboBox()
        fit_combo(self.ref_box)
        self.ref_box.currentIndexChanged.connect(self._refresh)
        bar.addWidget(self.ref_box)

        bar.addWidget(QtWidgets.QLabel("  compare "))
        self.cmp_box = QtWidgets.QComboBox()
        fit_combo(self.cmp_box)
        self.cmp_box.currentIndexChanged.connect(self._refresh)
        bar.addWidget(self.cmp_box)

        bar.addWidget(QtWidgets.QLabel("  map "))
        self.colour_box = QtWidgets.QComboBox()
        self.colour_box.addItems(["color by speed", "color by delta"])
        self.colour_box.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToContents)
        self.colour_box.setMinimumWidth(150)
        self.colour_box.currentIndexChanged.connect(self._draw_map)
        self.colour_box.currentIndexChanged.connect(self._fill_map_key)
        fit_combo(self.colour_box)
        bar.addWidget(self.colour_box)

        bar.addWidget(QtWidgets.QLabel("  imagery "))
        self.base_box = QtWidgets.QComboBox()
        self.base_box.addItem("off", None)
        for key, prov in bm.PROVIDERS.items():
            self.base_box.addItem(prov.label, key)
        self.base_box.setMinimumWidth(170)
        default = self.base_box.findData(bm.DEFAULT_PROVIDER)
        self.base_box.setCurrentIndex(default if default >= 0 else 0)
        self.base_box.currentIndexChanged.connect(self._basemap_changed)
        fit_combo(self.base_box)
        bar.addWidget(self.base_box)

        bar.addWidget(QtWidgets.QLabel("  units "))
        self.unit_box = QtWidgets.QComboBox()
        self.unit_box.addItems(["km/h · m", "mph · ft"])
        self.unit_box.setMinimumWidth(120)
        self.unit_box.setCurrentIndex(1)                 # mph/ft by default
        self.unit_box.currentIndexChanged.connect(self._units_changed)
        fit_combo(self.unit_box)
        bar.addWidget(self.unit_box)

        self.video_toggle = QtWidgets.QCheckBox("video")
        self.video_toggle.setChecked(True)
        self.video_toggle.toggled.connect(self._toggle_video_pane)
        bar.addWidget(self.video_toggle)

        export = QtGui.QAction("Export report", self)
        export.triggered.connect(self._export)
        bar.addAction(export)

        export_video = QtGui.QAction("Export video", self)
        export_video.setToolTip(
            "Burn the telemetry overlay onto the loaded video")
        export_video.triggered.connect(self._export_video)
        bar.addAction(export_video)

        save_hist = QtGui.QAction("Save to history", self)
        save_hist.setToolTip("Record this session so progress can be tracked")
        save_hist.triggered.connect(self._save_history)
        bar.addAction(save_hist)

        trends = QtGui.QAction("History", self)
        trends.setToolTip("Progress across recorded sessions, and what is stored")
        trends.triggered.connect(self._show_trends)
        bar.addAction(trends)

        # Timing actions grouped under one labelled menu button, so they stay
        # discoverable rather than vanishing into the toolbar overflow chevron
        # on a narrow window.
        set_sf = QtGui.QAction("Set start/finish", self)
        set_sf.setToolTip("Click two points on the track map to place the "
                          "start/finish line, then the laps re-split")
        set_sf.triggered.connect(lambda: self._begin_gate("sf"))

        add_sector = QtGui.QAction("Add sector", self)
        add_sector.setToolTip("Click two points on the track map to place a "
                              "sector boundary")
        add_sector.triggered.connect(lambda: self._begin_gate("sector"))

        clear_sectors = QtGui.QAction("Clear sectors", self)
        clear_sectors.setToolTip("Remove sector boundaries you have placed")
        clear_sectors.triggered.connect(self._clear_custom_sectors)

        timing_menu = QtWidgets.QMenu(self)
        timing_menu.addAction(set_sf)
        timing_menu.addAction(add_sector)
        timing_menu.addAction(clear_sectors)
        timing_btn = QtWidgets.QToolButton(self)
        timing_btn.setText("Timing \u25be")
        timing_btn.setMenu(timing_menu)
        timing_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        timing_btn.setToolTip("Set the start/finish line or sector boundaries "
                              "by clicking the track map")
        bar.addWidget(timing_btn)

        # ---- playback transport
        play_bar = QtWidgets.QToolBar("playback")
        play_bar.setObjectName("playback")
        play_bar.setMovable(False)
        play_bar.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                               QtWidgets.QSizePolicy.Preferred)
        self.addToolBar(QtCore.Qt.BottomToolBarArea, play_bar)

        self.play_btn = QtWidgets.QPushButton("▶  Play lap")
        self.play_btn.setShortcut("Space")
        self.play_btn.setMinimumWidth(110)
        self.play_btn.clicked.connect(self._toggle_play)
        play_bar.addWidget(self.play_btn)

        stop = QtWidgets.QPushButton("⏮")
        stop.setToolTip("Back to the start/finish line")
        stop.clicked.connect(self._rewind)
        play_bar.addWidget(stop)

        play_bar.addWidget(QtWidgets.QLabel("  speed "))
        self.rate_box = QtWidgets.QComboBox()
        self.rate_box.setMinimumWidth(76)
        for label in ("0.25x", "0.5x", "1x", "2x", "4x"):
            self.rate_box.addItem(label)
        self.rate_box.setCurrentText("1x")
        self.rate_box.currentTextChanged.connect(
            lambda t: setattr(self, "_play_rate", float(t.rstrip("x"))))
        fit_combo(self.rate_box)
        play_bar.addWidget(self.rate_box)

        self.hover_box = QtWidgets.QCheckBox("follow mouse")
        self.hover_box.setToolTip(
            "Move the playhead as the mouse passes over a plot.\n"
            "Convenient for a quick look; turn it off when lining up a video, "
            "or the\nplayhead runs away the moment you reach for another "
            "control.")
        self.hover_box.setChecked(False)
        self.hover_box.toggled.connect(self._hover_changed)
        play_bar.addWidget(self.hover_box)

        play_bar.addWidget(QtWidgets.QLabel("  through "))
        self.scope_box = QtWidgets.QComboBox()
        self.scope_box.addItem("this lap", "lap")
        self.scope_box.addItem("whole session", "session")
        self.scope_box.setToolTip(
            "At the end of a lap, either start it again or carry on into the "
            "next one.\nUse the session setting with video of a whole "
            "session.")
        fit_combo(self.scope_box)
        play_bar.addWidget(self.scope_box)

        self.scrub = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.scrub.setRange(0, 1000)
        self.scrub.sliderMoved.connect(self._scrubbed)
        play_bar.addWidget(self.scrub)

        self.pos_label = QtWidgets.QLabel("--")
        self.pos_label.setMinimumWidth(150)
        self.pos_label.setStyleSheet("font-family:'SF Mono','Menlo','DejaVu Sans Mono','Consolas','Liberation Mono',monospace; color:#e8ecf2;")
        play_bar.addWidget(self.pos_label)

        # ---- left: laps + corner table
        self.lap_table = QtWidgets.QTableWidget(0, 4)
        self.lap_table.setHorizontalHeaderLabels(["Lap", "Time", "Delta", ""])
        self.lap_table.horizontalHeader().setMinimumSectionSize(46)
        self.lap_table.verticalHeader().setVisible(False)
        self.lap_table.setSelectionBehavior(QtWidgets.QTableWidget.SelectRows)
        self.lap_table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        self.lap_table.itemSelectionChanged.connect(self._lap_clicked)
        self.lap_table.horizontalHeader().setStretchLastSection(True)
        lap_group = _group("laps", self.lap_table)

        self.corner_table = QtWidgets.QTableWidget(0, 1)
        self.corner_table.verticalHeader().setVisible(False)
        self.corner_table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        self.corner_table.itemSelectionChanged.connect(self._corner_clicked)
        self.corner_table.itemDoubleClicked.connect(self._rename_corner)
        self.corner_table.setToolTip(
            "Double-click a corner to name it. Names follow the corner between "
            "sessions,\nunlike the numbers, which shift whenever detection "
            "resolves a corner differently.")
        corner_group = _group("corners", self.corner_table)

        left = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        left.addWidget(lap_group)
        left.addWidget(corner_group)
        left.setChildrenCollapsible(False)
        left.setHandleWidth(6)
        left.setSizes([260, 640])

        # ---- centre: map + traces
        self.map = TrackMap(self.cursor)
        self.speed = SpeedPlot(self.cursor)
        self.delta = DeltaPlot(self.cursor)
        self.speed.setXLink(self.delta)
        self.readout = ReadoutBar(self.cursor)
        self.readout.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                   QtWidgets.QSizePolicy.Preferred)
        self.cursor.moved.connect(self._on_cursor_moved)
        self.cursor.repaint.connect(self._on_cursor_repaint)

        # A stack of channel plots rather than one. Channels carry different
        # units — throttle in per cent, heart rate in bpm, g in g — so they
        # cannot honestly share a y axis; each gets its own pane.
        self.channel_rows: List[dict] = []
        self._channel_names: List[str] = []

        self.channel_stack = QtWidgets.QVBoxLayout()
        self.channel_stack.setContentsMargins(0, 0, 0, 0)
        self.channel_stack.setSpacing(4)

        stack_host = QtWidgets.QWidget()
        stack_host.setLayout(self.channel_stack)
        # Scrollable, so adding a sixth channel degrades into scrolling rather
        # than into six panes too short to read.
        self.channel_scroll = QtWidgets.QScrollArea()
        self.channel_scroll.setWidgetResizable(True)
        self.channel_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.channel_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarAlwaysOff)
        self.channel_scroll.setWidget(stack_host)

        chan_wrap = QtWidgets.QWidget()
        cwl = QtWidgets.QVBoxLayout(chan_wrap)
        cwl.setContentsMargins(0, 0, 0, 0)
        cwl.setSpacing(3)
        cwl.addWidget(self.channel_scroll, 1)

        add_row = QtWidgets.QHBoxLayout()
        self.add_channel_btn = QtWidgets.QPushButton("+ channel")
        self.add_channel_btn.setToolTip(
            "Show another channel alongside, each with its own scale")
        self.add_channel_btn.clicked.connect(lambda: self._add_channel_row())
        add_row.addWidget(self.add_channel_btn)
        add_row.addStretch(1)
        cwl.addLayout(add_row)

        self.channel_group = _group("extra channels", chan_wrap)

        self.video_panel = VideoPanel()
        self.video_panel.set_lap_start_provider(
            lambda: (self.analysis.lap.t_start if self.analysis else None))
        self.video_panel.set_cursor_time_provider(self._cursor_session_time)
        self.top_tabs = QtWidgets.QTabWidget()
        self.top_tabs.setDocumentMode(True)
        self.view3d = View3D()
        # the 3D ground plane is textured from the map's tile fetch, so imagery
        # is downloaded once and shown in both views
        self.map.tileReady.connect(self.view3d.add_tile)
        self.map.basemapCleared.connect(self.view3d.clear_tiles)
        self.map.gatePlaced.connect(self._on_gate_placed)

        self.map_key = _key("")
        map_wrap = QtWidgets.QWidget()
        mwl = QtWidgets.QVBoxLayout(map_wrap)
        mwl.setContentsMargins(0, 0, 0, 0)
        mwl.setSpacing(1)
        mwl.addWidget(self.map, 1)
        mwl.addWidget(self.map_key)
        self.top_tabs.addTab(map_wrap, "TRACK")
        self.top_tabs.addTab(self.view3d, "3D")
        # keep indices by lookup, not by counting — inserting a tab above
        # otherwise silently relabels the wrong one
        self._tab_3d = self.top_tabs.indexOf(self.view3d)
        if not self.view3d.available:
            self.top_tabs.setTabText(self._tab_3d, "3D (UNAVAILABLE)")

        self.video_wrap = QtWidgets.QTabWidget()
        self.video_wrap.setDocumentMode(True)
        self.video_wrap.addTab(self.video_panel, "VIDEO")

        # Side by side rather than tabbed: the point of the video is watching it
        # against the trace, which you cannot do if seeing one hides the other.
        self.top_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.top_split.addWidget(self.top_tabs)
        self.top_split.addWidget(self.video_wrap)
        self.top_split.setChildrenCollapsible(False)
        self.top_split.setHandleWidth(6)
        self.top_split.setStretchFactor(0, 5)
        self.top_split.setStretchFactor(1, 3)
        self.top_split.setSizes([760, 420])
        self.top_tabs.currentChanged.connect(self._top_tab_changed)

        # Click any view to start or stop the lap running. Wired here, after
        # every view exists.
        # A click on the map, the 3D view or the video toggles playback; a
        # click on a plot puts the playhead where you clicked, which is what
        # you want from something with a distance axis.
        for view in (self.map, self.view3d, self.video_panel):
            view.clicked.connect(self._toggle_play)
        for plot in (self.speed, self.delta):
            plot.clickedAt.connect(self._seek_to)

        centre = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        centre.addWidget(self.top_split)
        # The markers and the delta fill were only ever explained in the
        # documentation, which is no use while looking at the screen.
        self.speed_key = _key("")
        centre.addWidget(_group("speed", self.speed, self.speed_key))
        # squares, because the plot fills areas rather than marking points
        self.delta_key = _key(
            f"<span style='color:{LOSS_COLOUR}'>\u25a0</span> above the line: "
            f"behind the reference &nbsp;&nbsp;"
            f"<span style='color:{GAIN_COLOUR}'>\u25a0</span> below: ahead "
            f"&nbsp;&nbsp;|&nbsp;&nbsp; read the <b>slope</b>, not the height "
            f"&mdash; rising means time is being lost right there")
        centre.addWidget(_group("time delta vs reference", self.delta,
                                self.delta_key))
        centre.addWidget(self.channel_group)
        self._centre_splitter = centre
        # Not collapsible: a pane dragged to zero height takes its handle with
        # it, and once several handles stack at the same position there is
        # nothing left to grab to bring the panes back. Each keeps a floor.
        centre.setChildrenCollapsible(False)
        centre.setHandleWidth(6)
        # stretch factors, not fixed sizes, so the panes share the window as it
        # grows instead of pinning one of them
        for i, stretch in enumerate((5, 3, 2, 2)):
            centre.setStretchFactor(i, stretch)
        centre.setSizes([400, 230, 180, 180])

        centre_wrap = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(centre_wrap)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.readout)
        cl.addWidget(centre, 1)

        # ---- right: insights
        self.focus_box = QtWidgets.QComboBox()
        self.focus_box.setObjectName("focus")
        self.focus_box.addItems(["Coaching at the cursor",
                                 "Top 3 losses only",
                                 "Everything"])
        self.focus_box.currentIndexChanged.connect(lambda *_: self._fill_insights())
        fit_combo(self.focus_box)

        # How the lap is spent: lap-level context that belongs on screen, not
        # buried in a text report. Not a verdict on its own — a lap is mostly
        # straights, and a straight at 0.1 g is exactly right — but it
        # separates "at the limit and losing time" from "not asking the car
        # for anything".
        self.usage_bar = QtWidgets.QWidget()
        usage_lay = QtWidgets.QHBoxLayout(self.usage_bar)
        usage_lay.setContentsMargins(8, 2, 8, 5)
        usage_lay.setSpacing(14)
        self._usage_cells = {}
        for key, cap in (("turning", "cornering"), ("braking", "braking"),
                         ("limit", "at limit"), ("peak", "peak g")):
            cell = QtWidgets.QVBoxLayout()
            cell.setSpacing(0)
            label = QtWidgets.QLabel(cap)
            label.setStyleSheet("color:#5a616e; font-size:9px;"
                                "letter-spacing:1px;")
            value = QtWidgets.QLabel("--")
            value.setStyleSheet("color:#c8ccd4; font-size:13px;"
                                "font-family:'SF Mono','Menlo','DejaVu Sans Mono','Consolas','Liberation Mono',monospace;")
            # each cell has to be at least as wide as its caption, or the
            # captions run into one another and stop being readable
            width = max(label.sizeHint().width(), 46) + 4
            for widget in (label, value):
                widget.setMinimumWidth(width)
            cell.addWidget(label)
            cell.addWidget(value)
            usage_lay.addLayout(cell)
            self._usage_cells[key] = value
        usage_lay.addStretch(1)

        self.insight_view = QtWidgets.QTextEdit()
        self.insight_view.setReadOnly(True)
        self.insight_view.setMinimumWidth(220)
        self.quality_badge = QtWidgets.QLabel("")
        self.quality_badge.setWordWrap(True)
        self.quality_badge.setStyleSheet("font-size:11px; padding:3px 6px;")
        self.summary = QtWidgets.QLabel("Open a log file to begin.")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:#e8ecf2; font-size:13px; padding:6px;")
        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.summary)
        rl.addWidget(self.quality_badge)
        rl.addWidget(self.usage_bar)
        rl.addWidget(self.focus_box)
        rl.addWidget(_group("findings", self.insight_view), 1)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(centre_wrap)
        split.addWidget(right)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 4)
        split.setStretchFactor(2, 2)
        split.setSizes([300, 760, 400])
        self.setCentralWidget(split)
        self.statusBar().showMessage("ready")

    # ------------------------------------------------------------------ data
    def _open(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open a data log", "",
            "Data logs (*.vbo *.fit *.csv *.tsv);;VBOX (*.vbo);;"
            "Garmin FIT (*.fit);;CSV (*.csv *.tsv);;All files (*)")
        if path:
            self.load(path)

    def load(self, path: str) -> None:
        try:
            self.statusBar().showMessage(f"parsing {path} ...")
            QtWidgets.QApplication.processEvents()
            vbo = open_log(path)
            session = build_session(vbo)
            self._vbo_path = path
        except Exception as exc:                       # noqa: BLE001
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(
                self, "Could not read file",
                f"{exc}\n\nSupported: .vbo, Garmin .fit, and CSV exports.\n"
                "If coordinates look wrong in a .vbo, it may use a "
                "non-standard convention — try the --degrees or "
                "--east-positive flags from the command line.")
            self.statusBar().showMessage("load failed")
            return
        if not session.laps:
            QtWidgets.QMessageBox.warning(
                self, "No laps found",
                "No start/finish crossings were detected. The file may contain "
                "less than one full lap, or the timing line may be wrong.")
            return
        self._adopt(session)

    def _begin_gate(self, kind: str) -> None:
        """Enter map edit mode to place a start/finish or sector gate."""
        if self.session is None:
            return
        self.map.begin_gate_edit(kind)
        what = "start/finish line" if kind == "sf" else "sector boundary"
        self.statusBar().showMessage(
            f"Click two points on the track map to set the {what}.")

    def _on_gate_placed(self, kind: str, ax: float, ay: float,
                        bx: float, by: float) -> None:
        """Turn two clicked track points into a gate and re-analyse.

        The map is in local metres; unproject the two endpoints to lat/lon so
        the gate is stored the same way the file's own lines are, then rebuild
        the session from the original log with the correction applied.
        """
        from ..geometry import unproject_local
        if self.session is None or not self._vbo_path:
            return
        la1, lo1 = unproject_local(ax, ay, self.session.lat0, self.session.lon0)
        la2, lo2 = unproject_local(bx, by, self.session.lat0, self.session.lon0)
        gate = (float(la1), float(lo1), float(la2), float(lo2))

        try:
            vbo = open_log(self._vbo_path)
            if kind == "sf":
                new = build_session(vbo, start_finish_latlon=list(gate))
                new.custom_splits = list(self.session.custom_splits)
                msg = "Start/finish updated; laps re-split."
            else:
                new = build_session(
                    vbo, start_finish_latlon=self._current_sf_latlon())
                new.custom_splits = list(self.session.custom_splits) + [gate]
                msg = f"Sector boundary added ({len(new.custom_splits)} total)."
        except Exception as exc:                       # noqa: BLE001
            traceback.print_exc()
            QtWidgets.QMessageBox.warning(self, "Could not apply", str(exc))
            return

        if not new.laps:
            QtWidgets.QMessageBox.warning(
                self, "No laps",
                "That start/finish line was never crossed — the laps could "
                "not be split. The line may be off the track or facing the "
                "wrong way.")
            return
        self._adopt(new)
        self.statusBar().showMessage(msg)

    def _current_sf_latlon(self):
        """The current start/finish line as a lat/lon quad, so rebuilding for a
        sector change keeps the line the user already set."""
        from ..geometry import unproject_local
        if self.session is None or self.session.start_finish is None:
            return None
        ax, ay, bx, by = self.session.start_finish
        la1, lo1 = unproject_local(ax, ay, self.session.lat0, self.session.lon0)
        la2, lo2 = unproject_local(bx, by, self.session.lat0, self.session.lon0)
        return [float(la1), float(lo1), float(la2), float(lo2)]

    def _clear_custom_sectors(self) -> None:
        if self.session is None or not self.session.custom_splits:
            self.statusBar().showMessage("No placed sectors to clear.")
            return
        self.session.custom_splits = []
        self._refresh()
        self.statusBar().showMessage("Placed sectors cleared.")

    def _sector_gate_lines(self, lap, analysis):
        """Gate lines and labels for each sector boundary.

        Returns (lines, labels) where each line is a local-metre (ax,ay,bx,by)
        segment laid across the track at a boundary, perpendicular to travel,
        and each label is a short name for that gate ("S1", "S2", or the file's
        split name). Works for file-declared, user-placed, and auto sectors.
        """
        import numpy as np
        from ..sectors import build_sectors

        try:
            sec = build_sectors(self.session, lap)
        except Exception:                                # noqa: BLE001
            return [], []
        lines, labels = [], []
        half = 15.0                       # metres either side of the racing line
        names = getattr(sec, "names", None) or []
        for k, s in enumerate(sec.boundaries):
            i = int(np.clip(lap.idx(s), 1, len(lap.x) - 2))
            # travel direction from neighbouring samples, then its perpendicular
            dx = float(lap.x[i + 1] - lap.x[i - 1])
            dy = float(lap.y[i + 1] - lap.y[i - 1])
            n = (dx * dx + dy * dy) ** 0.5 or 1.0
            nx, ny = -dy / n, dx / n
            px, py = float(lap.x[i]), float(lap.y[i])
            lines.append((px + nx * half, py + ny * half,
                          px - nx * half, py - ny * half))
            # a file-provided name if there is one for this boundary, else "Sn".
            # names is aligned with sectors ("" then one per split); the gate at
            # boundary k opens sector k+1, so its name is names[k+1] when present.
            name = ""
            if k + 1 < len(names):
                name = names[k + 1]
            labels.append(name or f"S{k + 1}")
        return lines, labels

    def _adopt(self, session: Session) -> None:
        self.session = session
        self._load_video()
        best = session.best_lap()
        self._populate_laps()
        for box in (self.ref_box, self.cmp_box):
            box.blockSignals(True)
            box.clear()
            for lap in session.laps:
                mark = "" if lap.valid else "  (out)"
                box.addItem(f"Lap {lap.number} — {fmt_time(lap.lap_time)}{mark}",
                            lap.number)
            box.blockSignals(False)
            fit_combo(box)
        self.ref_box.setCurrentIndex(session.laps.index(best))
        second = self._second_best()
        self.cmp_box.blockSignals(True)
        self.cmp_box.setCurrentIndex(session.laps.index(second))
        self.cmp_box.blockSignals(False)
        self._refresh()

    def _second_best(self):
        pool = [l for l in (self.session.valid_laps or self.session.laps)]
        best = self.session.best_lap()
        others = [l for l in pool if l is not best]
        return min(others, key=lambda l: l.lap_time) if others else best

    def _populate_laps(self) -> None:
        from ..sectors import best_sectors, build_sectors, sector_times
        laps = self.session.laps
        best = self.session.best_lap()

        sec = None
        try:
            sec = build_sectors(self.session, best)
            pool = self.session.valid_laps or laps
            _best_times, owner = best_sectors(pool, sec)
        except Exception:                                 # noqa: BLE001
            owner = []
        cols = ["Lap", "Time", "Delta"]
        if sec is not None and sec.count >= 2:
            cols += [sec.label(i) for i in range(sec.count)]
        cols += [""]
        self.lap_table.setColumnCount(len(cols))
        self.lap_table.setHorizontalHeaderLabels(cols)
        self.lap_table.setRowCount(len(laps))
        for r, lap in enumerate(laps):
            d = lap.lap_time - best.lap_time
            cells = [str(lap.number), fmt_time(lap.lap_time),
                     "—" if lap is best else f"{d:+.3f}"]
            purple = set()
            if sec is not None and sec.count >= 2:
                times = sector_times(lap, sec)
                for i, t in enumerate(times):
                    cells.append(fmt_time(t))
                    if i < len(owner) and owner[i] == lap.number:
                        purple.add(len(cells) - 1)
            cells.append("" if lap.valid else "out")
            for c, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if c > 0:
                    item.setTextAlignment(QtCore.Qt.AlignRight
                                          | QtCore.Qt.AlignVCenter)
                if c in purple:
                    # the session's fastest time in that sector
                    item.setForeground(QtGui.QColor("#b085f5"))
                elif lap is best:
                    item.setForeground(QtGui.QColor(REF_COLOUR))
                elif not lap.valid:
                    item.setForeground(QtGui.QColor("#5a616e"))
                self.lap_table.setItem(r, c, item)
        self.lap_table.resizeColumnsToContents()

    def _select_lap_row(self, number: int) -> None:
        """Highlight the compared lap in the list without looping back.

        The signal is blocked while doing it: setting the selection would
        otherwise fire _lap_clicked, which sets cmp_box, which re-runs the
        analysis — a needless round trip, and during playback a source of
        judder.
        """
        row = next((i for i, lap in enumerate(self.session.laps)
                    if lap.number == number), None)
        if row is None:
            return
        blocked = self.lap_table.blockSignals(True)
        try:
            self.lap_table.selectRow(row)
        finally:
            self.lap_table.blockSignals(blocked)

    def _lap_clicked(self) -> None:
        rows = self.lap_table.selectionModel().selectedRows()
        if not rows or self.session is None:
            return
        lap = self.session.laps[rows[0].row()]
        idx = self.session.laps.index(lap)
        if idx != self.cmp_box.currentIndex():
            self.cmp_box.setCurrentIndex(idx)

    # -------------------------------------------------------------- analysis
    def _refresh(self) -> None:
        if self.session is None:
            return
        ref_n = self.ref_box.currentData()
        cmp_n = self.cmp_box.currentData()
        if ref_n is None or cmp_n is None:
            return
        try:
            self.analysis = analyse(self.session, lap_number=cmp_n,
                                    reference_number=ref_n, units=self.units)
        except Exception as exc:                       # noqa: BLE001
            traceback.print_exc()
            self.statusBar().showMessage(f"analysis failed: {exc}")
            return
        a = self.analysis
        ref, lap = a.reference, a.lap

        for w in [self.speed, self.delta, self.readout] + \
                [r["plot"] for r in self.channel_rows]:
            w.set_units(self.units)
        self._populate_channels()
        self.speed.set_laps(ref, lap)
        self.speed.set_corners(a.corners, label=True)
        self.delta.set_corners(a.corners)
        self.delta.set_delta(a.s_delta, a.delta)
        self.readout.set_context(ref, lap, a.s_delta, a.delta)

        markers: List[tuple[float, float, str]] = []
        for m in a.metrics:
            if m.s_brake is not None:
                markers.append((m.s_brake, lap.at(m.s_brake, lap.speed_kmh), "brake"))
            if m.s_turnin is not None:
                markers.append((m.s_turnin, lap.at(m.s_turnin, lap.speed_kmh), "turnin"))
            markers.append((m.s_vmin, m.v_min, "apex"))
            if m.s_throttle is not None:
                markers.append((m.s_throttle,
                                lap.at(m.s_throttle, lap.speed_kmh), "throttle"))
        self.speed.set_markers(markers)

        self.speed.setXRange(0, self.units.d(float(lap.s[-1])), padding=0.01)
        self._draw_map()
        self.view3d.set_laps(lap, ref if ref is not lap else None)
        self._fill_corner_table()
        self._select_lap_row(lap.number)
        self._last_corner = None
        if not getattr(self, "_carry_on", False):
            self._stop_playback()
            self.video_panel.stop()
        self._fill_insights()
        self._play_s = min(self._play_s, float(lap.s[-1]))
        self.cursor.set(self._play_s)

        self._fill_usage()
        self._fill_quality()
        self._fill_keys()
        tb, _ = theoretical_best(self.session.valid_laps or self.session.laps,
                                 a.corners)
        best = self.session.best_lap()
        self.summary.setText(
            f"<b>Lap {lap.number}</b> {fmt_time(lap.lap_time)} &nbsp; vs ref "
            f"<b>lap {ref.number}</b> {fmt_time(ref.lap_time)} "
            f"&nbsp;<span style='color:{CMP_COLOUR}'>"
            f"{lap.lap_time - ref.lap_time:+.3f}s</span><br>"
            f"<span style='color:#6b7280'>{len(a.corners)} corners · "
            f"{self.units.length_s(lap.length)} · theoretical best {fmt_time(tb)} "
            f"({tb - best.lap_time:+.3f} vs best)</span>")
        self.statusBar().showMessage(
            f"{len(self.session.laps)} laps · start/finish from "
            f"{self.session.sf_source} · {self.session.vbo.sample_rate:.0f} Hz")

    def _top_tab_changed(self, *_) -> None:
        if not self.analysis:
            return
        # The hidden view ignores cursor updates for performance, so when it
        # becomes visible its dot can be stale — the cursor does not move while
        # paused, so no signal refreshes it. Push the current position to
        # whichever view was just switched to.
        current = self.top_tabs.currentWidget()
        if current is self.view3d:
            self.view3d.update_cursor(self.cursor.s)
        else:
            self.map.sync_cursor(self.cursor.s)

    def _toggle_video_pane(self, on: bool) -> None:
        on = bool(on)
        if not on:
            # remember the width so re-showing doesn't come back at zero
            sizes = self.top_split.sizes()
            if len(sizes) > 1 and sizes[1] > 0:
                self._video_width = sizes[1]
            self.video_panel.unload()
        self.video_wrap.setVisible(on)
        if on:
            self.video_panel.reload()
            total = sum(self.top_split.sizes()) or 1000
            want = getattr(self, "_video_width", 0) or int(total * 0.38)
            want = max(260, min(want, int(total * 0.65)))
            self.top_split.setSizes([total - want, want])

    def _basemap_changed(self) -> None:
        self._apply_basemap()

    def _apply_basemap(self) -> None:
        """Fetch aerial imagery under the track, if a provider is selected."""
        if self.session is None:
            return
        sess = self.session
        key = self.base_box.currentData()
        if not key:
            self.map.clear_basemap()
            return
        lat0, lon0 = sess.lat0, sess.lon0

        def project(la, lo):
            xx, yy, _, _ = geo.project_local(np.array([la]), np.array([lo]),
                                             lat0, lon0)
            return float(xx[0]), float(yy[0])

        self.map.set_projection(project)
        lat = sess.vbo.channels["lat_deg"]
        lon = sess.vbo.channels["lon_deg"]
        extent = float(max(sess.x.max() - sess.x.min(),
                           sess.y.max() - sess.y.min()))
        self.map.set_basemap(key,
                             (float(lat.min()), float(lon.min()),
                              float(lat.max()), float(lon.max())),
                             lat0, lon0, extent)
        self.statusBar().showMessage("fetching imagery…", 3000)

    def _load_video(self) -> None:
        """Find and attach the video for this session, if there is one."""
        path = getattr(self, "_vbo_path", None) or self.session.vbo.path
        try:
            sync = VideoSync.from_vbo(self.session.vbo)
            found = find_video(path, self.session.vbo) if path else None
        except Exception:                                # noqa: BLE001
            traceback.print_exc()
            return
        first = self.session.vbo.channels.get("t")
        self.video_panel.load(found, sync,
                              float(first[0]) if first is not None and len(first)
                              else None)
        has = bool(found)
        self.video_wrap.setTabText(0, "VIDEO" if has else "VIDEO (NONE)")
        # With no video there is nothing to watch, so give the width back to
        # the map. Ticking `video` reveals the pane and its Open video button.
        self.video_toggle.setChecked(has)
        if has and sync.exact:
            self.statusBar().showMessage(
                f"linked video: {os.path.basename(found)}", 6000)

    def _populate_channels(self) -> None:
        """List every extra channel this file actually carries."""
        a = self.analysis
        if a is None:
            return
        logged = sorted(set(a.lap.extras) | set(a.reference.extras
                                                if a.reference else set()))
        # already have dedicated plots, or are logger housekeeping rather than
        # anything you would plot against a lap
        hide = {"ax_g_gps", "ay_g_gps", "avifileindex", "avitime", "avisynctime",
                "tsample", "sampleperiod"}
        logged = [n for n in logged if n not in hide]
        # Derived channels come first and are always present: a GPS-only file
        # has no accelerometer column, but lateral and longitudinal g are
        # computed from speed and curvature for every lap regardless.
        derived = [n for n in ChannelPlot.DERIVED if n != "delta_rate"]
        names = derived + logged
        self._channel_names = names
        self.channel_group.setVisible(bool(names))
        if not names:
            for row in list(self.channel_rows):
                self._remove_channel_row(row)
            return

        if not self.channel_rows:
            # Open on something worth looking at rather than whatever is first
            # alphabetically, which is usually "ascent".
            first = next((p for p in ("throttle", "brake", "rpm", "heartrate",
                                      "ay_g", "ay_g_calc", "height")
                          if p in names), names[0])
            self._add_channel_row(first)
        else:
            for row in self.channel_rows:
                self._fill_channel_box(row)
        self._sync_channel_rows()

    def _fill_channel_box(self, row: dict) -> None:
        """Refill one picker, keeping its selection if the channel still exists."""
        box = row["box"]
        wanted = row.get("name")
        box.blockSignals(True)
        box.clear()
        box.addItem("none", None)
        for name in self._channel_names:
            box.addItem(ChannelPlot.label_for(name, self.session.vbo.units),
                        name)
        if wanted in self._channel_names:
            box.setCurrentIndex(self._channel_names.index(wanted) + 1)
        box.blockSignals(False)
        fit_combo(box)

    def _add_channel_row(self, name: Optional[str] = None) -> None:
        if self.analysis is None or not self._channel_names:
            return
        plot = ChannelPlot(self.cursor)
        plot.hover_enabled = self.hover_box.isChecked()
        plot.set_units(self.units)
        plot.clicked.connect(self._toggle_play)
        plot.setXLink(self.delta)
        plot.clickedAt.connect(self._seek_to)

        box = QtWidgets.QComboBox()
        remove = QtWidgets.QToolButton()
        remove.setText("\u2715")
        remove.setToolTip("Remove this channel")

        header = QtWidgets.QHBoxLayout()
        caption = QtWidgets.QLabel("channel")
        caption.setStyleSheet("color:#5a616e; font-size:10px;"
                              "letter-spacing:1px;")
        header.addWidget(caption)
        header.addWidget(box)
        header.addStretch(1)
        header.addWidget(remove)

        holder = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addLayout(header)
        lay.addWidget(plot)

        row = {"plot": plot, "box": box, "widget": holder,
               "name": name or self._channel_names[0], "remove": remove}
        self.channel_rows.append(row)
        self.channel_stack.addWidget(holder)

        self._fill_channel_box(row)
        box.currentIndexChanged.connect(
            lambda *_, r=row: self._channel_changed(r))
        remove.clicked.connect(lambda *_, r=row: self._remove_channel_row(r))
        self._channel_changed(row)
        self._sync_channel_rows()
        self._grow_channel_pane()

    def _remove_channel_row(self, row: dict) -> None:
        if row not in self.channel_rows:
            return
        self.channel_rows.remove(row)
        self.channel_stack.removeWidget(row["widget"])
        row["widget"].setParent(None)
        row["widget"].deleteLater()
        self._sync_channel_rows()

    def _grow_channel_pane(self) -> None:
        """Give the channel pane room for the rows it now holds.

        Adding a channel to a pane that does not grow just squeezes every row
        until none of them can be read, which looks like the new channel
        failed to appear.
        """
        splitter = getattr(self, "_centre_splitter", None)
        if splitter is None or not self.channel_rows:
            return
        wanted = len(self.channel_rows) * 92 + 44
        sizes = splitter.sizes()
        if len(sizes) < 2 or sizes[-1] >= wanted:
            return
        need = wanted - sizes[-1]
        # take it from the largest pane above, which is the map
        donor = max(range(len(sizes) - 1), key=lambda i: sizes[i])
        spare = max(0, sizes[donor] - 140)
        take = min(need, spare)
        if take <= 0:
            return
        sizes[donor] -= take
        sizes[-1] += take
        splitter.setSizes(sizes)

    def _sync_channel_rows(self) -> None:
        """The last remaining row keeps its close button disabled.

        Removing every pane would leave an empty titled box and no obvious way
        back; the group hides itself instead when a file has no extra channels
        at all.
        """
        single = len(self.channel_rows) <= 1
        for row in self.channel_rows:
            row["remove"].setEnabled(not single)
        self.add_channel_btn.setEnabled(
            bool(self._channel_names)
            and len(self.channel_rows) < len(self._channel_names))

    def _channel_changed(self, row: Optional[dict] = None) -> None:
        if self.analysis is None:
            return
        rows = [row] if row is not None else list(self.channel_rows)
        for r in rows:
            if r not in self.channel_rows:
                continue
            r["name"] = r["box"].currentData()
            r["plot"].set_corners(self.analysis.corners)
            r["plot"].set_channel(r["name"], self.analysis.reference,
                                  self.analysis.lap, self.session.vbo.units)

    # ------------------------------------------------------------- units
    def _units_changed(self) -> None:
        self.units = IMPERIAL if self.unit_box.currentIndex() == 1 else METRIC
        if self.session is not None:
            self._refresh()

    # ---------------------------------------------------------- playback
    def _toggle_play(self) -> None:
        if self.analysis is None:
            return
        if self._timer.isActive():
            self._timer.stop()
            self.cursor.playing = False
            self.video_panel.stop()
            self.play_btn.setText("▶  Play lap")
        else:
            # Start from wherever the cursor is, so hovering to a corner and
            # clicking plays from there rather than from the last playhead.
            self._play_s = float(np.clip(self.cursor.s, 0.0,
                                         self.analysis.lap.length))
            self.cursor.playing = True
            self._clock.restart()
            self._timer.start()
            self.play_btn.setText("❚❚  Pause")

    def _rewind(self) -> None:
        self._play_s = 0.0
        self.cursor.set(0.0)

    def _stop_playback(self) -> None:
        if self._timer.isActive():
            self._toggle_play()

    def _scrubbed(self, value: int) -> None:
        if self.analysis is None:
            return
        self._play_s = float(value) / 1000.0 * self.analysis.lap.length
        self.cursor.set(self._play_s)

    def _tick(self) -> None:
        """Advance the playhead.

        Position comes from the video's own clock whenever a video is playing,
        because the media player cannot be told to hurry up: running a separate
        clock alongside it means the two diverge and the video has to be
        re-seeked every couple of seconds, which you hear as a skip. Following
        it makes the divergence impossible rather than small.

        With no video, real elapsed time is used — not the timer's nominal
        interval, which under load is optimistic and accumulates the same drift.
        """
        if self.analysis is None:
            self._timer.stop()
            return
        lap = self.analysis.lap
        elapsed = self._clock.restart() / 1000.0

        from_video = self.video_panel.current_session_time()
        if from_video is not None:
            lap_t = from_video - lap.t_start
            if lap_t > float(lap.t[-1]):
                # past the end of this lap: move to the next and let the video
                # keep playing into it. If there is no next lap, or the scope is
                # "this lap", _advance_lap does nothing and we loop the video
                # back to the lap start so it stays in step with the data
                # (which also loops) rather than running on ahead of it.
                if self._advance_lap():
                    return
                if self.scope_box.currentData() != "session":
                    self._play_s = 0.0
                    self.cursor.set(0.0)
                    self.video_panel.seek_to_session_time(float(lap.t_start))
                    return
            elif -1.0 <= lap_t <= float(lap.t[-1]) + 1.0:
                self._play_s = float(np.interp(
                    np.clip(lap_t, 0.0, float(lap.t[-1])), lap.t, lap.s))
                self.cursor.set(self._play_s)
                return
            # the video has run past this lap (or off its own end); carry on
            # under our own clock so a short clip does not freeze the session

        # Cap the step: after a stall or a window drag, a huge elapsed value
        # would teleport the playhead across half the circuit.
        dt = min(max(elapsed, 0.0), 0.25)
        v = max(lap.at(self._play_s, lap.speed), 1.0)      # m/s
        self._play_s += v * dt * self._play_rate
        if self._play_s >= lap.s[-1]:
            if not self._advance_lap():
                self._play_s = 0.0
            return
        self.cursor.set(self._play_s)

    def _advance_lap(self) -> bool:
        """At the end of a lap, move on to the next one if asked to.

        Looping the same lap forever is right when studying one lap and wrong
        when watching video of a whole session, so it is a choice rather than
        an assumption. Returns True if the lap changed.
        """
        if self.scope_box.currentData() != "session" or self.session is None:
            return False
        laps = self.session.laps
        current = self.analysis.lap if self.analysis else None
        if current is None:
            return False
        # match on lap number rather than object identity: a re-analysis
        # rebuilds the lap, and an identity lookup would then silently fail and
        # stall the session at that boundary
        position = next((i for i, lap in enumerate(laps)
                         if lap.number == current.number), None)
        if position is None or position + 1 >= len(laps):
            return False

        # keep playing across the switch; _refresh stops the transport
        was_playing = self._timer.isActive()
        self._carry_on = was_playing
        self._play_s = 0.0
        self.cmp_box.setCurrentIndex(position + 1)
        if was_playing and not self._timer.isActive():
            self._toggle_play()
        self._carry_on = False
        return True

    def _hover_changed(self, on: bool) -> None:
        """Turn mouse-following on or off across every plot at once."""
        for plot in [self.speed, self.delta] + \
                [r["plot"] for r in self.channel_rows]:
            plot.hover_enabled = bool(on)

    def _seek_to(self, x: float) -> None:
        """Put the playhead where the plot was clicked."""
        if self.analysis is None:
            return
        s = float(self.units.d_inv(x)) if hasattr(self.units, "d_inv") else float(x)
        s = max(0.0, min(s, float(self.analysis.lap.s[-1])))
        self._play_s = s
        self.cursor.set(s)

    def _on_cursor_moved(self, s: float) -> None:
        """Runs on every cursor update. Keep it cheap: the video sync is here,
        and it needs the full rate to stay in step."""
        if self.analysis is None:
            return
        lap = self.analysis.lap
        self.video_panel.follow(lap.t_start + lap.at(s, lap.t),
                                self._timer.isActive(), self._play_rate)

    def _on_cursor_repaint(self, s: float) -> None:
        """Rate limited by the cursor. Everything that costs real paint time.

        At 30 updates a second across five plot panes, redrawing was taking
        longer than a video frame, so the decoder never got the main thread and
        playback stuttered. Visually, 25 fps of cursor movement is
        indistinguishable; a stuttering video is not.
        """
        if self.analysis is None:
            return
        lap = self.analysis.lap
        u = self.units
        if not self.scrub.isSliderDown():
            self.scrub.blockSignals(True)
            self.scrub.setValue(int(1000 * s / max(lap.length, 1e-6)))
            self.scrub.blockSignals(False)
        t_here = lap.at(s, lap.t)
        if self.top_tabs.currentWidget() is self.view3d:
            self.view3d.update_cursor(s)
        d = ""
        if self.analysis.delta is not None:
            dv = float(np.interp(s, self.analysis.s_delta, self.analysis.delta))
            d = f"   \u0394 {dv:+.2f}s"
        text = f"{u.d(s):>6.0f} {u.dist_label}   {fmt_time(t_here)}{d}"
        if text != self.pos_label.text():
            self.pos_label.setText(text)
        # Only rebuild the findings panel when the cursor enters a new corner —
        # regenerating HTML on every update would stutter and make it unreadable.
        cur = self._corner_at(s)
        if cur != self._last_corner:
            self._last_corner = cur
            if self.focus_box.currentIndex() == 0:
                self._fill_insights()

    def _cursor_session_time(self) -> Optional[float]:
        """Where the cursor is, in session time rather than lap distance."""
        if self.analysis is None:
            return None
        lap = self.analysis.lap
        return float(lap.t_start + lap.at(self.cursor.s, lap.t))

    def _corner_at(self, s: float) -> Optional[int]:
        if self.analysis is None:
            return None
        for c in self.analysis.corners:
            if c.s_win_start <= s <= c.s_win_end:
                return c.index
        return None

    def _draw_map(self) -> None:
        if self.analysis is None:
            return
        a = self.analysis
        lap = a.lap
        if self.colour_box.currentIndex() == 1 and a.delta is not None:
            # per-meter rate of loss reads far better on a map than the
            # cumulative delta, which just grows towards the end of the lap
            d = np.interp(lap.s, a.s_delta, a.delta)
            rate = np.gradient(d, lap.ds) * 100.0
            self.map.set_lap(lap, rate, cmap="delta")
        else:
            self.map.set_lap(lap, lap.speed_kmh, cmap="speed")
        self.map.set_compare_line(a.reference if a.reference is not lap else None)
        brakes = [m.s_brake for m in a.metrics if m.s_brake is not None]
        self.map.set_corner_labels(lap, a.corners, brakes)
        self.map.set_start_finish(self.session.start_finish, label="S/F")
        # Sector gate lines + labels from the ACTUAL sectors (file, user, or
        # auto), drawn on both maps. The 3D view also gets the labels so each
        # gate is annotated ("S/F", "S1"…) the way a driver would want them.
        sector_lines, sector_labels = self._sector_gate_lines(lap, a)
        self.map.set_sector_lines(sector_lines, sector_labels)
        if hasattr(self, "view3d"):
            self.view3d.set_gates(self.session.start_finish, sector_lines,
                                  sf_label="S/F", sector_labels=sector_labels)
            self.view3d.set_corners(a.corners)
        self._apply_basemap()

    def _fill_quality(self) -> None:
        """A one-line verdict on the log, with the detail on hover.

        Without it the coaching reads with identical authority on 25 Hz data
        with six clean laps and on a 1 Hz export with one usable lap.
        """
        from ..quality import assess, significance_note
        if self.analysis is None:
            return
        q = assess(self.session, self.analysis.corners, self.units)
        # graded on what could be different, not on what the device cannot do
        colour = {"good": "#3dd68c", "fair": "#f2b134",
                  "poor": "#e5484d"}[q.actionable_grade]
        weak = [c for c in q.checks if c.grade != "good" and not c.inherent]
        tail = ("" if not weak else
                " · " + ", ".join(c.name for c in weak[:3]))
        limits = q.device_limits
        note = ("" if not limits else
                f" <span style='color:#5a616e'>· {limits[0].summary.split(' —')[0]}"
                f"</span>")
        self.quality_badge.setText(
            f"<span style='color:{colour}'>&#9679;</span> "
            f"<span style='color:#8b93a1'>data {q.actionable_grade}{tail}"
            f"</span>{note}")
        tip = [f"{q.grade.upper()} — {q.headline}", ""]
        for c in q.checks:
            tip.append(f"[{c.grade}] {c.name}: {c.summary}")
        note = significance_note(q, self.units)
        if note:
            tip += ["", note]
        for c in weak[:4]:
            tip += ["", f"{c.name}: {c.detail}"]
        self.quality_badge.setToolTip("\n".join(tip))

    def _fill_keys(self) -> None:
        """Say what the marks on the plots mean, where they are being looked at.

        The glyphs are chosen to match the plotted symbols: a down triangle for
        the brake point, a diamond for turn-in, a dot for minimum speed, an up
        triangle for throttle.
        """
        a = self.analysis
        if a is None:
            return
        ref, lap = a.reference, a.lap
        parts = []
        if ref is not None and ref is not lap:
            parts.append(
                f"<span style='color:{REF_COLOUR}'>\u2014</span> reference "
                f"lap {ref.number} &nbsp;"
                f"<span style='color:{CMP_COLOUR}'>\u2014</span> lap "
                f"{lap.number}")
        else:
            parts.append(f"<span style='color:{REF_COLOUR}'>\u2014</span> "
                         f"lap {lap.number}")
        parts.append(
            f"<span style='color:{LOSS_COLOUR}'>\u25bc</span> brake &nbsp;"
            f"<span style='color:#9d7bf5'>\u25c6</span> turn-in &nbsp;"
            f"<span style='color:#ffffff'>\u25cf</span> min speed &nbsp;"
            f"<span style='color:{GAIN_COLOUR}'>\u25b2</span> throttle &nbsp;"
            f"<span style='color:#8b93a1'>shaded</span> = corner")
        self.speed_key.setText(" &nbsp;&nbsp;|&nbsp;&nbsp; ".join(parts))
        self._fill_map_key()

    def _fill_map_key(self) -> None:
        by_delta = self.colour_box.currentIndex() == 1
        if by_delta:
            self.map_key.setText(
                f"color: <span style='color:{GAIN_COLOUR}'>gaining</span> "
                f"&rarr; <span style='color:{LOSS_COLOUR}'>losing</span> time, "
                f"per 100 {self.units.dist_label} &nbsp;&nbsp;|&nbsp;&nbsp; "
                f"<span style='color:{LOSS_COLOUR}'>\u25a0</span> brake point "
                f"&nbsp; <span style='color:#5ad1e6'>&middot;&middot;&middot;"
                f"</span> reference line")
        else:
            # the words are tinted with the actual stops of the speed ramp
            self.map_key.setText(
                "color: <span style='color:#3c5ac8'>slow</span> &rarr; "
                "<span style='color:#5ac8d2'>medium</span> &rarr; "
                "<span style='color:#f0c846'>quick</span> &rarr; "
                f"<span style='color:#eb5046'>fast</span> &nbsp;&nbsp;|"
                f"&nbsp;&nbsp; <span style='color:{LOSS_COLOUR}'>\u25a0</span> "
                f"brake point &nbsp; <span style='color:#5ad1e6'>&middot;"
                f"&middot;&middot;</span> reference line &nbsp; "
                f"<span style='color:#ffffff'>\u2502</span> start/finish")

    def _fill_usage(self) -> None:
        a = self.analysis
        if a is None:
            return
        pool = self.session.valid_laps or self.session.laps
        env = GripEnvelope.from_laps(pool)
        use = limit_usage(a.lap, env)
        self._usage_cells["turning"].setText(f"{use['turning_pct']:.0f}%")
        self._usage_cells["braking"].setText(f"{use['braking_pct']:.0f}%")
        self._usage_cells["limit"].setText(f"{use['at_limit_pct']:.0f}%")
        self._usage_cells["peak"].setText(f"{use['peak_combined_g']:.2f}")
        self.usage_bar.setToolTip(
            f"{use['turning_pct']:.0f}% of the lap is cornering and "
            f"{use['braking_pct']:.0f}% is braking, measured against a quarter "
            f"of your own demonstrated limits.\n"
            f"{use['at_limit_pct']:.0f}% of the lap and "
            f"{use['cornering_at_limit_pct']:.0f}% of the cornering is within "
            f"10% of the grip you sustain across this session "
            f"({env.max_combined_g:.2f} g, 97th percentile).\n"
            f"Peak on this lap: {use['peak_combined_g']:.2f} g.")

    def _fill_corner_table(self) -> None:
        a = self.analysis
        from ..insights import corner_summaries
        u = self.units
        d, sp = u.dist_label, u.speed_label
        pool = self.session.valid_laps or self.session.laps
        summaries = {c.corner.index: c for c in
                     corner_summaries(pool, a.corners)} if len(pool) > 1 else {}
        cols = ["Cnr", "dT s"]
        if summaries:
            cols += ["Best", "Avg", "Consist", "To gain"]
        cols += [f"R {d}", f"Brake {d}", f"Brk {sp}", "Dec g",
                 f"Min {sp}", f"Apex {d}", "Lat g", "Coast s", f"Exit {sp}"]
        self.corner_table.setColumnCount(len(cols))
        self.corner_table.setHorizontalHeaderLabels(cols)
        self.corner_table.setRowCount(len(a.corners))
        dts = {c.corner.index: c.dt for c in a.comparisons}
        for r, (c, m) in enumerate(zip(a.corners, a.metrics)):
            dt = dts.get(c.index)
            summary = summaries.get(c.index)
            vals = [
                f"{c.name} {c.direction}",
                "—" if dt is None else f"{dt:+.3f}",
            ]
            if summaries:
                vals += ([f"{summary.best_s:.3f}", f"{summary.mean_s:.3f}",
                          f"{summary.consistency_pct:.0f}%",
                          f"{summary.potential_gain_s:+.3f}"]
                         if summary else ["—", "—", "—", "—"])
            vals += [
                f"{u.d(c.min_radius):.0f}",
                "—" if m.s_brake is None else f"{u.d(m.s_brake):.0f}",
                "—" if m.v_brake is None else f"{u.spd(m.v_brake):.0f}",
                f"{abs(m.peak_decel_g):.2f}",
                f"{u.spd(m.v_min):.1f}",
                f"{u.d(m.apex_offset):+.0f}",
                f"{m.peak_lat_g:.2f}",
                f"{m.coast_time:.2f}",
                f"{u.spd(m.v_exit_plus):.0f}",
            ]
            for cc, text in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(QtCore.Qt.UserRole, c.index)
                if cc > 0:
                    item.setTextAlignment(QtCore.Qt.AlignRight
                                          | QtCore.Qt.AlignVCenter)
                if cc == 1 and dt is not None:
                    item.setForeground(QtGui.QColor(
                        "#e5484d" if dt > 0.04 else
                        "#3dd68c" if dt < -0.04 else "#6b7280"))
                elif summaries and summary and cols[cc] == "Consist":
                    item.setForeground(QtGui.QColor(
                        "#3dd68c" if summary.consistency_pct >= 95 else
                        "#f2b134" if summary.consistency_pct >= 88
                        else "#e5484d"))
                elif summaries and summary and cols[cc] == "To gain":
                    item.setForeground(QtGui.QColor(
                        "#e5484d" if summary.potential_gain_s > 0.15
                        else "#8b93a1"))
                self.corner_table.setItem(r, cc, item)
        self.corner_table.resizeColumnsToContents()

    def _corner_clicked(self) -> None:
        rows = self.corner_table.selectionModel().selectedRows()
        if not rows or self.analysis is None:
            return
        idx = self.corner_table.item(rows[0].row(), 0).data(QtCore.Qt.UserRole)
        for c in self.analysis.corners:
            if c.index == idx:
                self.cursor.set(c.s_geo_apex)
                self.speed.setXRange(self.units.d(c.s_win_start - 40),
                                     self.units.d(c.s_win_end + 40))
                break

    def _rename_corner(self, item) -> None:
        """Name the corner under the cursor, for good.

        Stored against the apex position rather than the corner number, so it
        survives detection resolving the corner differently another day.
        """
        if self.analysis is None or self.session is None:
            return
        index = item.data(QtCore.Qt.UserRole)
        corner = next((c for c in self.analysis.corners if c.index == index),
                      None)
        if corner is None:
            return
        from .. import history

        current = corner.given_name or ""
        name, ok = QtWidgets.QInputDialog.getText(
            self, f"Name {corner.number}",
            f"Name for {corner.number} "
            f"({'left' if corner.direction == 'L' else 'right'}, "
            f"{self.units.d_s(corner.min_radius)} radius):\n"
            "Leave blank to go back to the number.",
            text=current)
        if not ok:
            return
        ref = self.analysis.reference or self.analysis.lap
        i = ref.idx(corner.s_geo_apex)
        try:
            history.set_name(history.circuit_key(self.session),
                             float(ref.lat[i]), float(ref.lon[i]),
                             name, corner.direction)
        except Exception as exc:                          # noqa: BLE001
            traceback.print_exc()
            QtWidgets.QMessageBox.warning(self, "Could not save name", str(exc))
            return
        corner.given_name = name.strip() or None
        self._fill_corner_table()
        self._fill_insights()
        self._draw_map()
        self.statusBar().showMessage(
            f"{corner.number} is now \u201c{corner.name}\u201d"
            if corner.given_name else
            f"{corner.number} is back to its number", 6000)

    def _fill_insights(self) -> None:
        """Three levels of detail, because 40 findings at once is unreadable.

        The default follows the playhead and shows one corner at a time — which
        is how a coach actually talks to you: not a list of everything wrong
        with the lap, but the one thing to fix at the corner you're arriving at.
        """
        a = self.analysis
        if a is None:
            return
        mode = self.focus_box.currentIndex()
        u = self.units
        html = ["<div style='font-family:sans-serif'>"]

        if mode == 0:
            html.append(self._cursor_panel())
        elif mode == 1:
            html.append(self._losses_bars(limit=3))
            top = {c.corner.name for c in a.top_losses(3)}
            items = [i for i in a.comparative if i.corner in top][:6]
            html.append(self._insight_block("WHAT TO FIX FIRST", items))
        else:
            html.append(self._losses_bars(limit=6))
            html.append(self._insight_block("WHAT CAUSED IT", a.comparative[:12]))
            html.append(self._insight_block("TECHNIQUE NOTES", a.absolute[:12]))
            cons = consistency_insights(
                self.session.valid_laps or self.session.laps, a.corners, u=u)
            if cons:
                html.append(self._insight_block("CONSISTENCY", cons[:8]))

        straddle = [c.name for c in a.corners if c.note_straddle]
        if straddle and mode == 2:
            html.append(
                "<div style='margin-top:12px;color:#f2b134'>"
                f"{', '.join(straddle)} runs across the start/finish line, so "
                "its metrics are split between the top and bottom of the lap. "
                "Move the timing line onto a straight for cleaner numbers."
                "</div>")
        html.append("</div>")
        self.insight_view.setHtml("".join(html))

    def _cursor_panel(self) -> str:
        """Everything about the corner the playhead is in — and nothing else."""
        a = self.analysis
        u = self.units
        idx = self._last_corner
        if idx is None:
            nxt = self._next_corner()
            if nxt is None:
                return ("<div style='color:#6b7280;padding:12px'>"
                        "On track between corners.</div>")
            comp = self._comparison(nxt.index)
            dt = f" ({comp.dt:+.2f}s)" if comp else ""
            return (f"<div style='color:#6b7280;padding:12px'>On a straight."
                    f"<div style='margin-top:8px;color:#c8ccd4;font-size:15px'>"
                    f"Next: <b>{nxt.name}</b>{dt}</div></div>")

        corner = next(c for c in a.corners if c.index == idx)
        m = a.metrics_for(idx)
        comp = self._comparison(idx)
        out = []

        dt_txt = ""
        if comp is not None:
            col = ("#e5484d" if comp.dt > 0.04
                   else "#3dd68c" if comp.dt < -0.04 else "#6b7280")
            dt_txt = (f" <span style='color:{col};font-size:16px'>"
                      f"{comp.dt:+.3f}s</span>")
        out.append(f"<div style='font-size:20px;margin-bottom:2px'>"
                   f"<b>{corner.name}</b> "
                   f"<span style='color:#6b7280;font-size:13px'>"
                   f"{'left' if corner.direction == 'L' else 'right'}, "
                   f"{u.d_s(corner.min_radius)} radius</span>{dt_txt}</div>")

        # the four numbers that matter, with reference values alongside
        rows = [
            ("brake", None if m.s_brake is None else u.d_s(m.s_brake),
             None if (comp is None or comp.ref.s_brake is None)
             else u.d_s(comp.ref.s_brake)),
            ("min speed", u.spd_s(m.v_min),
             None if comp is None else u.spd_s(comp.ref.v_min)),
            ("coast", f"{m.coast_time:.2f}s",
             None if comp is None else f"{comp.ref.coast_time:.2f}s"),
            ("exit", u.spd_s(m.v_exit_plus),
             None if comp is None else u.spd_s(comp.ref.v_exit_plus)),
        ]
        out.append("<table style='margin:10px 0;width:100%'>")
        for label, val, ref in rows:
            if val is None:
                continue
            refc = (f"<td style='color:#5a616e;text-align:right'>ref {ref}</td>"
                    if ref else "<td></td>")
            out.append(
                f"<tr><td style='color:#6b7280;width:78px'>{label}</td>"
                f'<td style="color:#e8ecf2;font-family:SF Mono,Menlo,DejaVu Sans Mono,Consolas,Liberation Mono,monospace">{val}</td>'
                f"{refc}</tr>")
        out.append("</table>")

        items = []
        if comp is not None:
            items += [i for i in a.comparative if i.corner == corner.name]
        items += [i for i in a.absolute if i.corner == corner.name]
        if items:
            out.append(self._insight_block("", items[:4]))
        else:
            out.append("<div style='color:#3dd68c;margin-top:8px'>"
                       "Nothing to fix here.</div>")
        return "".join(out)

    def _next_corner(self):
        s = self.cursor.s
        ahead = [c for c in self.analysis.corners if c.s_win_start > s]
        return ahead[0] if ahead else (self.analysis.corners[0]
                                       if self.analysis.corners else None)

    def _comparison(self, index: int):
        for c in self.analysis.comparisons:
            if c.corner.index == index:
                return c
        return None

    def _losses_bars(self, limit: int) -> str:
        a = self.analysis
        top = a.top_losses(limit)
        if not top:
            return ""
        total = sum(c.dt for c in a.comparisons if c.dt > 0)
        out = ["<div style='color:#6b7280;font-size:10px;letter-spacing:1px'>"
               "WHERE THE TIME WENT</div>"]
        for comp in top:
            pct = 100 * comp.dt / total if total else 0
            out.append(
                f"<div style='margin:3px 0'><b>{comp.corner.name}</b> "
                f"<span style='color:#e5484d'>{comp.dt:+.3f}s</span>"
                f"<div style='background:#262b33;height:5px;margin-top:2px'>"
                f"<div style='background:#e5484d;height:5px;"
                f"width:{pct:.0f}%'></div></div></div>")
        return "".join(out)

    def _insight_block(self, title: str, items) -> str:
        if not items:
            return ""
        out = []
        if title:
            out.append(f"<div style='color:#6b7280;font-size:10px;"
                       f"letter-spacing:1px;margin-top:12px'>{title}</div>")
        for ins in items:
            col = SEV_COLOUR[ins.severity]
            cost = (f" <span style='color:#6b7280'>~{ins.time_cost:.2f}s</span>"
                    if ins.time_cost >= 0.01 else "")
            out.append(
                f"<div style='margin:7px 0;padding-left:7px;"
                f"border-left:2px solid {col}'>"
                f"<b style='color:{col}'>{ins.corner or '--'}</b> "
                f"{ins.message}{cost}"
                + (f"<div style='color:#7f8794;margin-top:2px'>{ins.detail}"
                   f"</div>" if ins.detail else "")
                + "</div>")
        return "".join(out)

    def _export_video(self) -> None:
        """Burn the overlay onto the loaded clip, on a worker thread.

        Asks what to export (this lap or the whole session) and the layout,
        then runs the encode off the UI thread with a live progress bar, so a
        minutes-long render does not freeze the window. Ends with a Reveal
        button rather than opening Finder unprompted.
        """
        if self.analysis is None:
            return
        panel = self.video_panel
        if not getattr(panel, "path", None) or panel.sync is None:
            QtWidgets.QMessageBox.information(
                self, "Export video",
                "Load and line up a video first — Open… on the video bar, "
                "then Sync.")
            return
        from ..export_video import find_ffmpeg
        if find_ffmpeg() is None:
            QtWidgets.QMessageBox.warning(
                self, "Export video",
                "ffmpeg was not found.\n\nInstall it (brew install ffmpeg / "
                "apt install ffmpeg) or `pip install imageio-ffmpeg`.")
            return
        ExportVideoDialog(self, panel.path, panel.sync).exec()

    def _save_history(self) -> None:
        if self.analysis is None:
            return
        from .. import history
        key = history.circuit_key(self.session)
        known = history.sessions_for(key)
        default = known[-1].label if known else history.circuit_label(self.session)
        label, ok = QtWidgets.QInputDialog.getText(
            self, "Save to history", "Name this circuit:", text=default)
        if not ok:
            return
        try:
            record = history.build_record(
                self.session, self.analysis.corners,
                self.analysis.reference or self.analysis.lap,
                label=label.strip() or default)
            fresh = history.record_session(record)
        except Exception as exc:                          # noqa: BLE001
            traceback.print_exc()
            QtWidgets.QMessageBox.warning(self, "Could not save", str(exc))
            return
        verb = "Recorded" if fresh else "Updated"
        self.statusBar().showMessage(
            f"{verb} session at {record.label} "
            f"({len(history.sessions_for(key))} recorded here)", 8000)

    def _show_trends(self) -> None:
        """Trends at this circuit, and a place to manage what is stored.

        The two belong together: the question "why does this trend look odd?"
        is usually answered by "because that session should not be in here".
        """
        if self.session is None:
            return
        from .. import history
        from ..report import trend_report

        key = history.circuit_key(self.session)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("History")
        dialog.resize(820, 600)
        lay = QtWidgets.QVBoxLayout(dialog)

        view = QtWidgets.QPlainTextEdit()
        view.setReadOnly(True)
        view.setStyleSheet("font-family:'SF Mono','Menlo','DejaVu Sans Mono','Consolas','Liberation Mono',monospace; font-size:12px;")
        lay.addWidget(view, 3)

        caption = QtWidgets.QLabel("Recorded sessions at this circuit")
        caption.setStyleSheet("color:#6b7280; font-size:10px;"
                              "letter-spacing:1px; padding-top:6px;")
        lay.addWidget(caption)

        table = QtWidgets.QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Date", "Best lap", "Laps", "File"])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QtWidgets.QTableWidget.SelectRows)
        table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(table, 2)

        def refresh() -> None:
            trend = history.trend_for(key)
            if trend is not None:
                view.setPlainText(trend_report(trend, self.units))
            else:
                count = len(history.sessions_for(key))
                view.setPlainText(
                    f"{count} session recorded at this circuit.\n\n"
                    "Trends need at least two. Use Save to history after each\n"
                    "session and they will build up.")
            records = history.sessions_for(key)
            table.setRowCount(len(records))
            for row, rec in enumerate(records):
                when = (time.strftime("%d %b %Y %H:%M",
                                      time.localtime(rec.session_at))
                        if rec.session_at else "—")
                cells = [when, fmt_time(rec.best_lap_s),
                         f"{rec.clean_laps}/{rec.lap_count}",
                         os.path.basename(rec.source)]
                for col, text in enumerate(cells):
                    item = QtWidgets.QTableWidgetItem(text)
                    item.setData(QtCore.Qt.UserRole, rec.source)
                    table.setItem(row, col, item)
            table.resizeColumnsToContents()
            total = len(history.load())
            venues = len(history.circuits())
            summary.setText(f"{total} sessions across {venues} circuits stored "
                            f"in {history.STORE_PATH}")

        summary = QtWidgets.QLabel("")
        summary.setStyleSheet("color:#5a616e; font-size:11px;")
        summary.setWordWrap(True)
        lay.addWidget(summary)

        def confirm(title: str, text: str) -> bool:
            return QtWidgets.QMessageBox.question(
                dialog, title, text,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) == QtWidgets.QMessageBox.Yes

        def remove_selected() -> None:
            rows = table.selectionModel().selectedRows()
            if not rows:
                QtWidgets.QMessageBox.information(
                    dialog, "Remove session",
                    "Select a session in the list first.")
                return
            source = table.item(rows[0].row(), 0).data(QtCore.Qt.UserRole)
            if not confirm("Remove session",
                           f"Remove the recorded session from\n"
                           f"{os.path.basename(source)}?\n\n"
                           "The log file itself is not touched."):
                return
            history.forget_session(source)
            refresh()

        def clear_circuit() -> None:
            count = len(history.sessions_for(key))
            if not confirm("Clear circuit",
                           f"Remove all {count} recorded sessions at this "
                           f"circuit?\n\nThe log files themselves are not "
                           f"touched."):
                return
            history.forget_circuit(key)
            refresh()

        def clear_all() -> None:
            count = len(history.load())
            if not confirm("Clear all history",
                           f"Remove all {count} recorded sessions, at every "
                           f"circuit?\n\nThe log files themselves are not "
                           f"touched. This cannot be undone."):
                return
            history.clear()
            refresh()

        buttons = QtWidgets.QHBoxLayout()
        for label, slot in (("Remove session", remove_selected),
                            ("Clear circuit", clear_circuit),
                            ("Clear all history", clear_all)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(dialog.accept)
        buttons.addWidget(close)
        lay.addLayout(buttons)

        refresh()
        dialog.exec()

    def _export(self) -> None:
        if self.analysis is None:
            return
        lap = self.analysis.lap
        default = f"lap{lap.number}_report.html"
        path, chosen = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save report", default,
            "HTML report (*.html);;Text report (*.txt)")
        if not path:
            return
        as_text = path.lower().endswith(".txt") or "Text" in (chosen or "")
        if not as_text and not path.lower().endswith(".html"):
            path += ".html"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                if as_text:
                    fh.write(insight_report(self.analysis, self.session,
                                            units=self.units))
                else:
                    fh.write(session_html(self.analysis, self.session,
                                          self.units))
        except OSError as exc:                            # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.statusBar().showMessage(f"wrote {path}", 8000)



class _ExportWorker(QtCore.QThread):
    """Runs an ExportJob off the UI thread, relaying progress as signals.

    The job's progress callback fires on this thread; emitting Qt signals is
    the safe way to hand those values back to the dialog, which lives on the
    main thread.
    """
    progress = QtCore.Signal(float, str)
    finished_ok = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(self, job):
        super().__init__()
        self._job = job

    def run(self) -> None:                                # noqa: D102
        try:
            out = self._job.run(progress=lambda f, m: self.progress.emit(f, m))
            self.finished_ok.emit(out)
        except Exception as exc:                          # noqa: BLE001
            self.failed.emit(str(exc))

    def cancel(self) -> None:
        self._job.cancel()


class ExportVideoDialog(QtWidgets.QDialog):
    """Ask what to export, then run the burn-in with a progress bar.

    Deliberately small: pick the span (this lap or the whole session) and the
    layout, choose where to save, watch the bar, and reveal the result when it
    is done.
    """

    def __init__(self, parent: "MainWindow", video_path: str, sync):
        super().__init__(parent)
        self.setWindowTitle("Export video")
        self.resize(460, 260)
        self._main = parent
        self._video_path = video_path
        self._sync = sync
        self._worker: Optional[_ExportWorker] = None
        self._out_path: Optional[str] = None

        lay = QtWidgets.QVBoxLayout(self)

        self.span_box = QtWidgets.QComboBox()
        lap = parent.analysis.lap
        self.span_box.addItem(f"This lap (lap {lap.number})", "lap")
        self.span_box.addItem("Whole session", "session")
        self.layout_box = QtWidgets.QComboBox()
        self.layout_box.addItem("Landscape (16:9)", "landscape")
        self.layout_box.addItem("Portrait (9:16)", "portrait")
        # a layout edited in the editor overrides the preset choice; changing
        # the preset dropdown clears it, since a landscape edit is meaningless
        # once you switch to portrait
        self._custom_layout = None
        self.layout_box.currentIndexChanged.connect(
            lambda *_: setattr(self, "_custom_layout", None))

        combo_css = (
            "QComboBox { background:#1a1f27; color:#e8ecf2; border:1px solid "
            "#2a313b; border-radius:4px; padding:4px 24px 4px 8px; }"
            "QComboBox:focus { border-color:#3d6df0; }"
            "QComboBox QAbstractItemView { background:#1a1f27; color:#e8ecf2; "
            "selection-background-color:#3d6df0; selection-color:#ffffff; }")
        for box in (self.span_box, self.layout_box):
            box.setStyleSheet(combo_css)

        form = QtWidgets.QFormLayout()
        form.addRow("Export", self.span_box)
        form.addRow("Layout", self.layout_box)
        lay.addLayout(form)

        edit_row = QtWidgets.QHBoxLayout()
        edit_row.addStretch(1)
        self.edit_btn = QtWidgets.QPushButton("Edit layout…")
        self.edit_btn.setToolTip(
            "Drag panels, toggle elements, and set panel opacity against a "
            "frame of this lap")
        self.edit_btn.clicked.connect(self._edit_layout)
        edit_row.addWidget(self.edit_btn)
        lay.addLayout(edit_row)

        self.status = QtWidgets.QLabel("Ready.")
        self.status.setStyleSheet("color:#8b93a1;")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        lay.addWidget(self.bar)

        buttons = QtWidgets.QHBoxLayout()
        self.reveal_btn = QtWidgets.QPushButton("Reveal")
        self.reveal_btn.setEnabled(False)
        self.reveal_btn.clicked.connect(self._reveal)
        buttons.addWidget(self.reveal_btn)
        buttons.addStretch(1)
        self.start_btn = QtWidgets.QPushButton("Export…")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        buttons.addWidget(self.start_btn)
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        buttons.addWidget(self.close_btn)
        lay.addLayout(buttons)

        # Land focus on the Export button, not the first combo. A focused combo
        # renders its current item as selected text — light on a highlight —
        # which is unreadable the moment the dialog opens.
        self.start_btn.setFocus()

    # -- editing -----------------------------------------------------------

    def _current_layout(self):
        """The layout to export with: an edited one if present, else the
        preset chosen in the dropdown."""
        from ..overlay import LAYOUTS
        if self._custom_layout is not None:
            return self._custom_layout
        return LAYOUTS[self.layout_box.currentData()]()

    def _edit_layout(self) -> None:
        from ..overlay import OverlayData
        from .overlay_editor import OverlayEditorDialog

        main = self._main
        data = OverlayData.from_analysis(main.analysis, main.units)
        lap = main.analysis.lap
        # start from the current layout (edited or preset), on a copy so Cancel
        # leaves the export untouched
        import copy
        start = copy.deepcopy(self._current_layout())

        # pull the real video frame at the preview moment, so overlays are
        # positioned against the actual footage rather than a stand-in
        frame = None
        try:
            from ..export_video import grab_frame
            preview_t = data.lap_time * 0.5
            video_s = self._sync.video_ms(lap.t_start + preview_t) / 1000.0
            frame = grab_frame(self._video_path, max(0.0, video_s))
        except Exception:                                 # noqa: BLE001
            frame = None

        editor = OverlayEditorDialog(self, data, start, data.lap_time * 0.5,
                                     video_frame=frame)
        if editor.exec() == QtWidgets.QDialog.Accepted:
            self._custom_layout = editor.layout
            self.status.setText("Custom layout ready.")

    # -- running -----------------------------------------------------------

    def _start(self) -> None:
        import os
        from ..export_video import ExportJob
        from ..overlay import OverlayData, LAYOUTS

        main = self._main
        default_name = (os.path.splitext(os.path.basename(self._video_path))[0]
                        + "_overlay.mp4")
        out, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save overlaid video",
            os.path.join(os.path.dirname(self._video_path), default_name),
            "Video (*.mp4)")
        if not out:
            return

        data = OverlayData.from_analysis(main.analysis, main.units)
        layout = self._current_layout()
        lap = main.analysis.lap
        if self.span_box.currentData() == "lap":
            # lap.t is lap-relative (starts at 0); the sync works in absolute
            # session time, so offset by t_start. Using lap.t[0] directly asked
            # the sync to place session-time-zero, which is nowhere near the
            # video and read as "the video does not overlap".
            start_t = float(lap.t_start + lap.t[0])
            end_t = float(lap.t_start + lap.t[-1])
        else:
            # whole session: the full span the log covers, already absolute
            t = main.session.vbo.channels["t"]
            start_t, end_t = float(t[0]), float(t[-1])

        job = ExportJob(data=data, sync=self._sync, layout=layout,
                        source_video=self._video_path, out_path=out,
                        start_session_t=start_t, end_session_t=end_t)
        self._out_path = out

        self.start_btn.setEnabled(False)
        self.span_box.setEnabled(False)
        self.layout_box.setEnabled(False)
        self.close_btn.setText("Cancel")
        self.reveal_btn.setEnabled(False)

        self._worker = _ExportWorker(job)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        self.bar.setValue(int(fraction * 1000))
        self.status.setText(message)

    def _on_done(self, out: str) -> None:
        self._retire_worker()
        self.bar.setValue(1000)
        self.status.setText("Done.")
        self.reveal_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self._restore_controls()

    def _on_failed(self, message: str) -> None:
        self._retire_worker()
        self.status.setText(f"Failed: {message}")
        self.start_btn.setEnabled(True)
        self._restore_controls()

    def _retire_worker(self) -> None:
        """Let the finished thread stop before dropping our reference to it.

        Clearing the reference the instant a signal arrives can free the
        QThread while its run() is still unwinding, which Qt aborts on. wait()
        is immediate here because the thread has already emitted its result.
        """
        w = self._worker
        self._worker = None
        if w is not None:
            w.wait(2000)
            w.deleteLater()

    def _restore_controls(self) -> None:
        self.span_box.setEnabled(True)
        self.layout_box.setEnabled(True)
        self.close_btn.setText("Close")

    def _reveal(self) -> None:
        from ..export_video import reveal_in_file_manager
        if self._out_path:
            reveal_in_file_manager(self._out_path)

    def reject(self) -> None:                             # noqa: D102
        # "Cancel" while running stops the encode; "Close" when idle just shuts
        if self._worker is not None and self._worker.isRunning():
            self.status.setText("Cancelling…")
            self._worker.cancel()
            self._worker.wait(5000)
            self._retire_worker()
            self._restore_controls()
            self.start_btn.setEnabled(True)
            return
        super().reject()

    def closeEvent(self, event) -> None:                  # noqa: D102
        # window closed with the X while encoding: stop the thread first
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
            self._retire_worker()
        super().closeEvent(event)


def fit_combo(box: QtWidgets.QComboBox, extra: int = 26) -> None:
    """Widen a combo so its longest entry is readable, closed and open.

    A fixed minimum width elides the text instead of fitting it, which on a
    lap picker means every entry reads "Lap 1 - 1:3..." and the thing you chose
    it for is the part that got cut.
    """
    fm = box.fontMetrics()
    widest = 0
    for i in range(box.count()):
        widest = max(widest, fm.horizontalAdvance(box.itemText(i)))
    if widest == 0:
        return
    box.setMinimumWidth(widest + extra)
    view = box.view()
    if view is not None:
        view.setMinimumWidth(widest + extra)
    box.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)


def _key(text: str) -> QtWidgets.QLabel:
    """A small legend line under a plot."""
    label = QtWidgets.QLabel(text)
    label.setTextFormat(QtCore.Qt.RichText)
    label.setStyleSheet("color:#6b7280; font-size:10px; padding:0 4px 1px 4px;")
    label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                        QtWidgets.QSizePolicy.Fixed)
    return label


def _group(title: str, widget: QtWidgets.QWidget,
           legend: Optional[QtWidgets.QWidget] = None) -> QtWidgets.QGroupBox:
    box = QtWidgets.QGroupBox(title)
    lay = QtWidgets.QVBoxLayout(box)
    lay.setContentsMargins(4, 4, 4, 4)
    lay.addWidget(widget)
    if legend is not None:
        lay.addWidget(legend)
    # Small enough that the splitter has plenty of travel, large enough that a
    # pane always keeps a grabbable handle and a sliver of content.
    box.setMinimumHeight(56)
    box.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                      QtWidgets.QSizePolicy.MinimumExpanding)
    return box


def gui_main(argv: Optional[List[str]] = None) -> int:
    """Console-script entry point: `garw_genie.py --lap-analysis [session.vbo]`."""
    args = list(sys.argv[1:] if argv is None else argv)
    return launch(args[0] if args else None)


def launch(path: Optional[str] = None, session: Optional[Session] = None) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow(path, session=session)
    win.show()
    # macOS in particular will open a Python GUI behind whatever has focus
    win.raise_()
    win.activateWindow()
    return app.exec()
