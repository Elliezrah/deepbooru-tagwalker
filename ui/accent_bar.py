"""
ui/accent_bar.py

Draws a coloured bar down the left edge of the current row.

FIELD REPORT: the click highlight and the row the program is actually
working on drift apart, and bold text alone was not enough to tell
them apart at a glance.

A left edge bar is the right shape for this because it COMPOSES. The
queue already paints rows green, red and amber for yes, no and
skipped, and Qt paints its own selection on top of whatever it likes;
a bar occupies a strip nothing else uses, so it survives every
combination instead of competing for the background.

The colour comes from config.theme.accent_bar_colour(), which
guarantees at least 3.5:1 contrast against every row state in every
shipped theme — including two that fail with their theme's plain
accent.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QStyledItemDelegate

from config.theme import ACCENT_BAR_WIDTH, Colors

# Row data slot marking "this is the row the program is working on".
# A separate role from Qt's selection on purpose: the two mean
# different things and are allowed to disagree.
CURRENT_ROLE = Qt.ItemDataRole.UserRole + 77


class AccentBarDelegate(QStyledItemDelegate):
    """Paints the row normally, then a bar over its left edge.

    Painting after the base delegate rather than before means the bar
    sits above the selection highlight instead of under it — which is
    the whole point, since it has to remain visible exactly when Qt
    has decided to colour the row itself.
    """

    def __init__(self, parent=None, width: int = ACCENT_BAR_WIDTH):
        super().__init__(parent)
        self._width = int(width)

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        if not index.data(CURRENT_ROLE):
            return
        rect = option.rect
        bar = QRect(rect.left(), rect.top(), self._width, rect.height())
        painter.save()
        try:
            painter.fillRect(bar, QColor(Colors.ACCENT_BAR))
        finally:
            painter.restore()
