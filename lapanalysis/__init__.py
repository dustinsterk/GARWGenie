# -*- coding: utf-8 -*-
"""
Lap Analysis (GARW Genie) — VBOX (.vbo) GPS lap-time analysis and driver coaching.

Typical use::

    from lapanalysis import parse_vbo, build_session, analyse

    vbo = parse_vbo("session.vbo")
    session = build_session(vbo)
    result = analyse(session, lap_number=3)          # vs the best lap
    for insight in result.insights[:10]:
        print(insight)
"""

# This file is deliberately kept parseable by Python 2 as far as the check
# below, so that running it on an old interpreter produces an explanation
# rather than a SyntaxError about a stray character in a docstring. macOS
# still ships /usr/bin/python as 2.7, and a virtualenv built with it looks
# active while being useless.
import sys as _sys

if _sys.version_info < (3, 9):
    raise SystemExit(
        "Lap Analysis needs Python 3.9 or later; this is Python %d.%d at %s.\n"
        "\n"
        "    python3 -m lapanalysis ...\n"
        "\n"
        "If you are in a virtualenv and still seeing this, it was built with\n"
        "Python 2. Rebuild it:\n"
        "\n"
        "    deactivate && rm -rf .venv\n"
        "    python3 -m venv .venv && source .venv/bin/activate\n"
        "    pip install -r requirements.txt\n"
        % (_sys.version_info[0], _sys.version_info[1], _sys.executable))

from .csvlog import parse_csv
from .parser import (SUPPORTED_EXTENSIONS, VboFile, open_log, parse_vbo,
                     write_vbo)
from .laps import LapTrack, Session, build_session, delta_time
from .corners import Corner, CornerMetrics, GripEnvelope, detect_corners, analyse_lap
from .insights import Insight, LapAnalysis, analyse, compare_laps, theoretical_best
from .export import session_html
from .report import insight_report, session_summary, fmt_time
from .units import IMPERIAL, METRIC, UnitSystem

__app_name__ = "Lap Analysis"
__version__ = "0.1.0"

__all__ = [
    "__app_name__", "__version__",
    "VboFile", "parse_vbo", "parse_csv", "open_log", "write_vbo",
    "SUPPORTED_EXTENSIONS",
    "LapTrack", "Session", "build_session", "delta_time",
    "Corner", "CornerMetrics", "GripEnvelope", "detect_corners", "analyse_lap",
    "Insight", "LapAnalysis", "analyse", "compare_laps", "theoretical_best",
    "insight_report", "session_summary", "fmt_time", "session_html",
    "METRIC", "IMPERIAL", "UnitSystem",
]
