"""A slider that jumps to where you click.

A plain QSlider treats a click on its groove as "page step towards here", which for a timeline means a
click goes somewhere other than where you clicked, and its handlers only see sliderMoved during a drag.
This one moves the handle straight under the pointer, signals it like the start of a drag (pressed,
moved, then released on mouse-up) and lets you keep dragging from there.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets


class JumpSlider(QtWidgets.QSlider):
    def _value_at(self, pos: QtCore.QPoint) -> int:
        opt = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(opt)
        style = self.style()
        groove = style.subControlRect(QtWidgets.QStyle.CC_Slider, opt, QtWidgets.QStyle.SC_SliderGroove, self)
        handle = style.subControlRect(QtWidgets.QStyle.CC_Slider, opt, QtWidgets.QStyle.SC_SliderHandle, self)
        if self.orientation() == QtCore.Qt.Horizontal:
            length, start, p = handle.width(), groove.x(), pos.x()
            span = groove.width() - length
        else:
            length, start, p = handle.height(), groove.y(), pos.y()
            span = groove.height() - length
        return QtWidgets.QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), p - start - length // 2,
                                                        max(span, 1), opt.upsideDown)

    def _on_handle(self, pos: QtCore.QPoint) -> bool:
        opt = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(opt)
        handle = self.style().subControlRect(QtWidgets.QStyle.CC_Slider, opt,
                                             QtWidgets.QStyle.SC_SliderHandle, self)
        return handle.contains(pos)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if event.button() != QtCore.Qt.LeftButton or not self.isEnabled():
            return super().mousePressEvent(event)
        pos = event.position().toPoint()
        if self._on_handle(pos):
            return super().mousePressEvent(event)          # an ordinary drag of the handle
        v = self._value_at(pos)
        self.setSliderDown(True)                            # -> sliderPressed
        self.setSliderPosition(v)                           # -> sliderMoved (and valueChanged)
        if self.sliderPosition() == v:
            self.sliderMoved.emit(v)                        # already there: still a deliberate jump
        self._jump_drag = True
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if getattr(self, "_jump_drag", False):
            self.setSliderPosition(self._value_at(event.position().toPoint()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if getattr(self, "_jump_drag", False):
            self._jump_drag = False
            self.setSliderDown(False)                       # -> sliderReleased
            event.accept()
            return
        super().mouseReleaseEvent(event)
