"""A lightweight modal popup that shows one or more images at a readable size.

Used by the conflict-scan and tag-audit dialogs so the user can glance at the
actual image(s) before deciding whether a flagged issue is fair, without
leaving the dialog.

The image is shown in a zoom/pan view (the same convention as the main image
panel's zoom dialog): the mouse wheel zooms in and out anchored under the
cursor, left-drag pans when zoomed in, and Ctrl+0 refits the image to the
window. Because left-drag is reserved for panning, the popup closes on
DOUBLE-click (or Escape), not a single click.

It accepts either a single path or a list of paths. With multiple images
(e.g. every image that carries a particular tag) it shows a "N / M" counter
and Prev/Next controls, and the Left/Right (or Up/Down) arrow keys page
through them. With a single image those controls are hidden, so existing
single-image callers are unaffected.

The popup is read-only and owns no state — it just displays files. Missing or
unreadable files show a short message instead of failing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence, Union

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Spacing
from ui.zoom_view import ZoomView


# Maximum on-screen size for the previewed image. The popup never exceeds
# this; smaller images are shown at native size (not upscaled).
_MAX_W = 900
_MAX_H = 700

PathLike = Union[str, Path]


class ImagePreviewPopup(QDialog):
    """Modal image viewer with zoom and pan. Wheel zooms (anchored under
    the cursor), left-drag pans, Ctrl+0 refits. Double-click the image or
    press Escape to close. With multiple images, Prev/Next buttons and
    arrow keys navigate."""

    def __init__(
        self,
        images: Union[PathLike, Sequence[PathLike]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        # Normalize to a list of Paths (accepts a single path or a sequence).
        if isinstance(images, (str, Path)):
            self._paths = [Path(images)]
        else:
            self._paths = [Path(p) for p in images]
        if not self._paths:
            self._paths = [Path("")]  # guard; shows a "could not load" note
        self._index = 0
        # The rect of the current pixmap, used to refit on show/Ctrl+0.
        self._fit_rect = None
        self.setModal(True)
        self._build()
        self._show_current()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL
        )
        layout.setSpacing(Spacing.TIGHT)

        self._name = QLabel()
        self._name.setProperty("role", "secondary")
        self._name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._name)

        # The image lives in a zoom/pan graphics view (same convention as
        # the main panel's zoom dialog) instead of a plain label, so the
        # user can scroll to zoom and drag to pan. A single scene and
        # pixmap item are reused across navigation; only the pixmap
        # changes.
        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self._view = ZoomView(self._scene, self)
        self._view.setMinimumSize(320, 320)
        # A separate label overlays a load-failure message; the view is
        # hidden and this shown when a file cannot be read.
        self._error_label = QLabel()
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_label.setMinimumSize(120, 120)
        self._error_label.setVisible(False)
        layout.addWidget(self._view, 1)
        layout.addWidget(self._error_label, 1)

        # Navigation row — only shown when there is more than one image.
        multi = len(self._paths) > 1
        self._nav = QWidget()
        nav_l = QHBoxLayout(self._nav)
        nav_l.setContentsMargins(0, 0, 0, 0)
        self._prev_btn = QPushButton("\u25C0  Prev")   # ◀
        self._next_btn = QPushButton("Next  \u25B6")   # ▶
        self._counter = QLabel()
        self._counter.setProperty("role", "tertiary")
        self._counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._prev_btn.clicked.connect(self._go_prev)
        self._next_btn.clicked.connect(self._go_next)
        nav_l.addWidget(self._prev_btn)
        nav_l.addStretch(1)
        nav_l.addWidget(self._counter)
        nav_l.addStretch(1)
        nav_l.addWidget(self._next_btn)
        layout.addWidget(self._nav)
        self._nav.setVisible(multi)

        hint_text = ("Scroll to zoom · drag to pan · Ctrl+0 to fit · "
                     "double-click or Esc to close")
        if multi:
            hint_text += "   ·   \u2190 \u2192 to navigate"
        hint = QLabel(hint_text)
        hint.setProperty("role", "tertiary")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        # Open at a comfortable large size so there is room to zoom. Sized
        # once here rather than per-image, since the view scales content
        # to fit regardless of the image's own dimensions.
        self.resize(
            min(_MAX_W + 48, 960),
            min(_MAX_H + (140 if multi else 96), 820))

    # ------------------------------------------------------------------
    def _show_current(self) -> None:
        path = self._paths[self._index]
        self.setWindowTitle(path.name or "Image")
        self._name.setText(path.name or "(no file)")
        self._counter.setText(f"{self._index + 1} / {len(self._paths)}")
        self._prev_btn.setEnabled(self._index > 0)
        self._next_btn.setEnabled(self._index < len(self._paths) - 1)
        self._load_image(path)

    def _load_image(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            # Show the error label, hide the zoom view.
            self._item.setPixmap(QPixmap())
            self._fit_rect = None
            self._view.setVisible(False)
            self._error_label.setVisible(True)
            self._error_label.setText(
                f"Could not load image:\n{path.name}")
            self._error_label.setStyleSheet(
                f"color: {Colors.DANGER_RED};")
            return
        # Good image: show the view, drop the error label.
        self._error_label.setVisible(False)
        self._view.setVisible(True)
        self._item.setPixmap(pixmap)
        # Keep the scene rect tight to the current pixmap so fitting and
        # panning bounds are correct as images change size across
        # navigation.
        rect = self._item.boundingRect()
        self._scene.setSceneRect(rect)
        self._fit_rect = rect
        # Fit now if the view already has a real size; otherwise the
        # first fit happens in showEvent (viewport size isn't final until
        # shown).
        if self._view.viewport().width() > 1:
            self._view.reset_zoom(rect)

    # ------------------------------------------------------------------
    def _go_prev(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._show_current()

    def _go_next(self) -> None:
        if self._index < len(self._paths) - 1:
            self._index += 1
            self._show_current()

    # ---- fit on show; double-click / Escape close; arrows navigate --
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # The viewport size is only final once shown, so fit the current
        # image here as well as in _load_image.
        if self._fit_rect is not None:
            self._view.reset_zoom(self._fit_rect)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        # Double-click closes. A single click (and drag) is reserved for
        # panning the zoomed image, so it must NOT close the popup — that
        # was fine for the old static label but would make panning
        # impossible here.
        self.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.accept()
        elif key == Qt.Key.Key_0 and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            # Ctrl+0 refits the current image, matching the main zoom
            # dialog's reset shortcut.
            if self._fit_rect is not None:
                self._view.reset_zoom(self._fit_rect)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self._go_prev()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self._go_next()
        else:
            super().keyPressEvent(event)


def show_image_preview(
    images: Union[PathLike, Sequence[PathLike]],
    parent: Optional[QWidget] = None,
) -> None:
    """Open the image preview popup modally for a single path or a list."""
    popup = ImagePreviewPopup(images, parent)
    popup.exec()
