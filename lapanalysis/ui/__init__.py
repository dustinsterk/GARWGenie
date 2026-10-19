"""Qt user interface (requires PySide6 and pyqtgraph)."""

# Before any widget code loads: guard against PySide6's leak of references to
# None (PySide6 6.12.0 on Python < 3.12 — see qtfixes.py).
from . import qtfixes as _qtfixes  # noqa: E402

_qtfixes.apply()
