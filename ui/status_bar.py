"""
ui/status_bar.py

Provides _ShimmerProgressBar: a QWidget progress bar with a subtle
moving-highlight shimmer, used by main_window in the window status bar.

(An earlier StatusBar wrapper class lived here too but was unused dead
code — main_window builds its own QStatusBar and embeds this progress
bar directly — and its internal completion math counted SKIPPED as
"done", which is inconsistent with the rest of the app. It was removed
to avoid that trap; only the progress-bar widget remains.)

Shimmer effect
--------------
A subtle moving highlight sweeps across the filled portion of the bar
every ~3 seconds. It makes the bar feel alive without being
distracting, confirms the app is responsive, and adds a small reward
signal as progress increases. Implemented as a QWidget that draws
everything in paintEvent, driven by a QTimer at 30 FPS.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

from config.theme import Colors


# Shimmer parameters
SHIMMER_PERIOD_MS    = 3000     # one full sweep every 3 seconds
SHIMMER_WIDTH_FRAC   = 0.25     # width of the highlight as fraction of bar
SHIMMER_FPS          = 30


# ---------------------------------------------------------------------------
# Embellished progress bar
# ---------------------------------------------------------------------------


class _ShimmerProgressBar(QWidget):
    """Thin horizontal progress bar with a moving shimmer highlight.

    Not a QProgressBar — we wanted full control over the visual and
    QProgressBar doesn't support animated highlights via stylesheet
    in any portable way. ~80 lines of paintEvent does it cleanly.
    """

    BAR_HEIGHT = 8
    BAR_RADIUS = 4

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.BAR_HEIGHT + 2)
        self.setMinimumWidth(120)
        self._value: float = 0.0    # 0..1
        self._shimmer_phase: float = 0.0  # 0..1, sweeps

        self._timer = QTimer(self)
        self._timer.setInterval(1000 // SHIMMER_FPS)
        self._timer.timeout.connect(self._advance_shimmer)
        self._timer.start()

    def setValue(self, value: float) -> None:
        """Set fill ratio. Clamped to [0, 1]."""
        self._value = max(0.0, min(1.0, float(value)))
        self.update()

    def _advance_shimmer(self) -> None:
        # Advance by frame_ms / period.
        delta = (1000 / SHIMMER_FPS) / SHIMMER_PERIOD_MS
        self._shimmer_phase = (self._shimmer_phase + delta) % 1.0
        # Only redraw if there's something to shimmer over (avoid
        # CPU on empty bar). Cheap check.
        if self._value > 0:
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        # Vertically center.
        y = (self.height() - self.BAR_HEIGHT) / 2.0
        track = QRectF(0, y, w, self.BAR_HEIGHT)

        # Track (unfilled background).
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(Colors.BG_DEEPEST))
        painter.drawRoundedRect(track, self.BAR_RADIUS, self.BAR_RADIUS)

        if self._value <= 0:
            return

        fill_w = w * self._value
        fill_rect = QRectF(0, y, fill_w, self.BAR_HEIGHT)

        # Fill (solid green).
        painter.setBrush(QColor(Colors.SUCCESS_GREEN))
        painter.drawRoundedRect(fill_rect, self.BAR_RADIUS, self.BAR_RADIUS)

        # Shimmer: a soft gradient highlight sweeping across the filled
        # portion. The highlight is whiter at center, transparent at
        # edges. Clipped to the filled rect so it doesn't paint over
        # the empty track.
        painter.save()
        painter.setClipRect(fill_rect)
        shimmer_x = fill_w * self._shimmer_phase * 1.3 - fill_w * 0.15
        # Shimmer's x ranges slightly outside [0, fill_w] so it sweeps
        # in from the left edge and out the right edge.
        shimmer_w = fill_w * SHIMMER_WIDTH_FRAC
        grad = QLinearGradient(
            shimmer_x - shimmer_w / 2, 0,
            shimmer_x + shimmer_w / 2, 0,
        )
        highlight = QColor(Colors.SUCCESS_GREEN)
        # Lighten the highlight relative to the fill — works for both
        # dark and light themes since the highlight is whiter than the
        # green either way.
        highlight = highlight.lighter(140)
        highlight.setAlphaF(0.0)
        grad.setColorAt(0.0, highlight)
        highlight2 = QColor(highlight)
        highlight2.setAlphaF(0.55)
        grad.setColorAt(0.5, highlight2)
        grad.setColorAt(1.0, highlight)  # alpha 0 again
        painter.setBrush(grad)
        painter.drawRoundedRect(fill_rect, self.BAR_RADIUS, self.BAR_RADIUS)
        painter.restore()


