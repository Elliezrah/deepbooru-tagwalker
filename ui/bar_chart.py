"""
ui/bar_chart.py

A lightweight horizontal bar chart drawn with QPainter.

Used by the stats window's Tag-Frequency and Co-occurrence tabs. We
draw it by hand rather than pulling in QtCharts to stay consistent
with the rest of the app's hand-rendered visuals (swirl, shimmer bar),
keep full control over theming, and avoid the ~30 MB QtCharts payload
in the eventual packaged executable.

Each row is: [label] [====bar====] [value]. Bars are scaled to the
maximum value in the data set. The widget is scrollable when there are
more rows than fit (the parent wraps it in a QScrollArea), so it sizes
its height to fit all rows.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from config.theme import Colors, Fonts


ROW_HEIGHT = 24          # px per bar row
LABEL_WIDTH = 160        # px reserved for the tag name on the left
VALUE_WIDTH = 64         # px reserved for the numeric value on the right
BAR_INSET = 8            # gap between label and bar, bar and value
BAR_VPAD = 4             # vertical padding within a row
TOP_PAD = 6
BOTTOM_PAD = 6


class BarChart(QWidget):
    """Horizontal bar chart. set_data([(label, value), ...])."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        bar_color: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self._data: list[tuple[str, int]] = []
        self._max_value: int = 0
        # Bar color override; defaults to the theme accent at draw time.
        self._bar_color_override = bar_color
        self.setMinimumWidth(LABEL_WIDTH + 120 + VALUE_WIDTH)

    def set_data(self, data: list[tuple[str, int]]) -> None:
        """Replace the chart data. data is a list of (label, value).

        The order given is the order drawn (caller sorts as desired).
        Height is recomputed so the parent scroll area can lay it out.
        """
        self._data = list(data)
        self._max_value = max((v for _, v in self._data), default=0)
        # Resize height to fit all rows so a wrapping QScrollArea scrolls.
        h = TOP_PAD + BOTTOM_PAD + ROW_HEIGHT * len(self._data)
        self.setMinimumHeight(max(h, ROW_HEIGHT))
        self.resize(self.width(), max(h, ROW_HEIGHT))
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        h = TOP_PAD + BOTTOM_PAD + ROW_HEIGHT * len(self._data)
        return QSize(self.minimumWidth(), max(h, ROW_HEIGHT))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if not self._data:
            painter.setPen(QColor(Colors.TEXT_TERTIARY))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "No data",
            )
            return

        bar_color = QColor(
            self._bar_color_override
            if self._bar_color_override
            else Colors.ACCENT_BLUE
        )
        track_color = QColor(Colors.BG_DEEPEST)
        label_color = QColor(Colors.TEXT_PRIMARY)
        value_color = QColor(Colors.TEXT_SECONDARY)

        label_font = QFont(Fonts.FAMILY.split(",")[0].strip(' "'))
        label_font.setPointSize(Fonts.SIZE_SMALL)
        painter.setFont(label_font)

        w = self.width()
        bar_x = LABEL_WIDTH + BAR_INSET
        bar_max_w = max(20, w - bar_x - VALUE_WIDTH - BAR_INSET)

        y = TOP_PAD
        for label, value in self._data:
            row_rect = QRectF(0, y, w, ROW_HEIGHT)

            # Label (left), elided if too long.
            painter.setPen(label_color)
            metrics = painter.fontMetrics()
            elided = metrics.elidedText(
                label, Qt.TextElideMode.ElideRight, LABEL_WIDTH,
            )
            painter.drawText(
                QRectF(0, y, LABEL_WIDTH, ROW_HEIGHT),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                elided,
            )

            # Track (full-width faint background bar).
            bar_top = y + BAR_VPAD
            bar_h = ROW_HEIGHT - 2 * BAR_VPAD
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(track_color)
            painter.drawRoundedRect(
                QRectF(bar_x, bar_top, bar_max_w, bar_h), 3, 3,
            )

            # Filled portion.
            frac = (value / self._max_value) if self._max_value else 0.0
            fill_w = bar_max_w * frac
            if fill_w > 0:
                painter.setBrush(bar_color)
                painter.drawRoundedRect(
                    QRectF(bar_x, bar_top, max(2.0, fill_w), bar_h), 3, 3,
                )

            # Value (right).
            painter.setPen(value_color)
            painter.drawText(
                QRectF(w - VALUE_WIDTH, y, VALUE_WIDTH, ROW_HEIGHT),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                f"{value:,}",
            )

            y += ROW_HEIGHT
