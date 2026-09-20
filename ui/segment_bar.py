"""
ui/segment_bar.py

One horizontal bar split into labelled proportions, with a legend.

Used where a pie chart would be the obvious choice — vocabulary
coverage, era drift — and deliberately not a pie. At three or four
slices a pie is harder to read than a bar: the eye compares lengths
far better than angles, small slices become unlabellable slivers, and
a bar sits in a narrow strip where a pie needs a square. The one thing
a pie does better, showing that the parts sum to a whole, a single
full-width bar does just as well.

Segments smaller than a couple of pixels are still drawn, so a
category with one member never silently disappears.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from config.theme import Colors, Fonts

BAR_HEIGHT = 26
LEGEND_HEIGHT = 22
GAP = 8
MIN_SEGMENT_PX = 2


class SegmentBar(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._data: list[tuple[str, int, str]] = []
        self._total = 0
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(BAR_HEIGHT + GAP + LEGEND_HEIGHT)

    def set_data(self, data: list[tuple[str, int, str]]) -> None:
        """[(label, value, colour), ...]"""
        self._data = [(str(l), max(0, int(v)), c) for l, v, c in data]
        self._total = sum(v for _l, v, _c in self._data)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        width = self.width()

        if not self._total:
            painter.setPen(QColor(Colors.TEXT_SECONDARY))
            painter.drawText(
                QRect(0, 0, width, BAR_HEIGHT),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                "no data")
            painter.end()
            return

        x = 0
        for index, (_label, value, colour) in enumerate(self._data):
            last = index == len(self._data) - 1
            span = (width - x if last
                    else int(round(width * value / self._total)))
            if value and span < MIN_SEGMENT_PX:
                # A category with a single member must not vanish.
                span = MIN_SEGMENT_PX
            if span <= 0:
                continue
            painter.fillRect(QRect(x, 0, span, BAR_HEIGHT),
                             QColor(colour))
            x += span

        legend_font = QFont()
        legend_font.setPointSize(Fonts.SIZE_SMALL)
        painter.setFont(legend_font)
        metrics = painter.fontMetrics()
        x = 0
        y = BAR_HEIGHT + GAP
        for label, value, colour in self._data:
            swatch = QRect(x, y + 4, 10, 10)
            painter.fillRect(swatch, QColor(colour))
            x += 14
            share = round(100 * value / self._total)
            text = f"{label}  {value:,} ({share}%)"
            painter.setPen(QColor(Colors.TEXT_SECONDARY))
            painter.drawText(
                QRect(x, y, metrics.horizontalAdvance(text) + 4,
                      LEGEND_HEIGHT),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text)
            x += metrics.horizontalAdvance(text) + 18
            if x > width:
                break
        painter.end()
