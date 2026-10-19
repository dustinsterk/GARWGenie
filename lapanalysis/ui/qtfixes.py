"""Work-around for a PySide6 reference-counting bug that kills the app on Python < 3.12.

PySide6 6.12.0 returns Python's ``None`` from *every* void method (setX,
setMatrix, moveTo, …) without taking the reference it hands back, so each such
call costs ``None`` one reference. On Python 3.12+ ``None`` is immortal and
nothing happens. On 3.10/3.11 its count — a few thousand at start-up —
eventually reaches zero and the interpreter aborts with
"Fatal Python error: none_dealloc: deallocating None".

Plotting makes thousands of these calls a second: pyqtgraph calls
``QTransform.setMatrix`` for every curve on every repaint. Zooming the map —
hundreds of repaints in a few seconds — used the references up and the analysis
window vanished. Windows and macOS builds ship Python 3.12, which is why only a
Linux machine running from source on Python 3.10 saw it.

The leak is in every void method, so wrapping a few of them can't fix it.
Instead, when a probe shows ``None`` really is leaking, give it a reserve of
references so large it can never run out — 2**40, about twelve days at a
million leaked calls a second. That is what Python 3.12 does itself (an
"immortal" count). It is only applied when the probe shows the bug, so a fixed
PySide6 or Python 3.12+ is left untouched.

``Signal.emit`` has the same bug with the ``True`` it returns. ``True`` is a
static object, so when its count hits zero the abort is "free(): invalid
pointer" instead — it gets the same reserve, as does ``False``.
"""
from __future__ import annotations

import ctypes
import sys

from PySide6 import QtCore

#: added to None's reference count when the leak is present
RESERVE = 1 << 40

#: True once the reserve has been applied in this process
applied = False
#: references to None lost per void call, as measured (0 = healthy)
leak_per_call = 0


class _Probe(QtCore.QObject):
    sig = QtCore.Signal()


def _lost(obj, fn, n: int = 64) -> int:
    before = sys.getrefcount(obj)
    for _ in range(n):
        fn()
    return round((before - sys.getrefcount(obj)) / n)


def _probe() -> int:
    """References lost per call by the worse of the two known leaks."""
    p = QtCore.QPointF()
    none_leak = _lost(None, lambda: p.setX(1.0))      # a void method: returns None
    o = _Probe()
    true_leak = _lost(True, o.sig.emit)               # emit returns True
    return max(none_leak, true_leak)


def apply() -> bool:
    """Detect the leak and, if present, make None/True/False effectively immortal."""
    global applied, leak_per_call
    if applied or sys.version_info >= (3, 12):
        return applied                 # immortal singletons: the bug is harmless
    try:
        leak_per_call = _probe()
        if leak_per_call <= 0:
            return False
        # CPython 3.10/3.11 (non-debug): ob_refcnt is the object's first field
        counts = [ctypes.c_ssize_t.from_address(id(o)) for o in (None, True, False)]
        for o, cnt in zip((None, True, False), counts):
            if abs(cnt.value - (sys.getrefcount(o) - 1)) > 8:
                return False           # unexpected object layout — leave it alone
        for cnt in counts:
            cnt.value += RESERVE
        applied = True
    except Exception:                  # noqa: BLE001 - never stop the app over this
        return False
    return applied
