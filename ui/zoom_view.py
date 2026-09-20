"""
ui/zoom_view.py

The one zoom-and-pan convention this program has.

Extracted from the main image panel so the Tag Referencer's post
inspector behaves identically rather than growing a second set of
gestures that drift apart. Wheel zooms toward the cursor, left-drag
pans, and the scale is clamped so the image cannot be lost to a speck
or absurd magnification.

Nothing is added over the original. Click-to-zoom was tried here and
removed: the wheel already zooms, so a click gesture bought nothing
and only competed with drag-to-pan.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget


def suppress_context_menus(root: QWidget) -> None:
    """Turn off Qt's default right-click menu on a window and every
    widget inside it.

    FIELD REPORT: right-clicking a post offered Copy / Copy Link
    Location / Select All. "Copy" needed a selection first, and "Copy
    Link Location" copied an internal `tag:` address rather than
    anything usable. It was Qt's default menu for interactive text,
    not a feature anyone designed — and it appeared wherever a label
    happened to be selectable or carry a link.

    A first fix named two labels and missed the rest, which is exactly
    what naming widgets individually gets you. This walks the whole
    tree instead, and is called again after any rebuild that creates
    new labels.
    """
    root.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    for child in root.findChildren(QWidget):
        child.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)


class _MenuSuppressor(QObject):
    """Kills right-click menus on anything added to a widget later.

    Sweeping the tree once is not enough where content arrives
    asynchronously: the Tag Referencer builds its document, then fills
    galleries when the posts come back, and every widget created after
    the sweep brings Qt's default menu with it. Rather than chase each
    creation site — which is how the first two attempts at this missed
    things — this watches for children being added and suppresses them
    as they arrive.
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.ChildAdded:
            child = event.child()
            if child is not None and child.isWidgetType():
                try:
                    child.setContextMenuPolicy(
                        Qt.ContextMenuPolicy.NoContextMenu)
                    child.installEventFilter(self)
                    suppress_context_menus(child)
                except RuntimeError:
                    pass
        return False


def suppress_context_menus_forever(root: QWidget):
    """Suppress now, and keep suppressing as the tree grows.

    Returns the filter, which the caller must keep a reference to —
    an event filter that is garbage collected stops working, and
    silently.
    """
    suppress_context_menus(root)
    watcher = _MenuSuppressor(root)
    root.installEventFilter(watcher)
    for child in root.findChildren(QWidget):
        child.installEventFilter(watcher)
    return watcher

class ZoomView(QGraphicsView):
    """QGraphicsView with mouse-wheel zoom and drag-to-pan.

    Wheel up zooms in, wheel down zooms out, anchored under the cursor
    so the point you point at stays put. Left-drag pans when zoomed in.
    Zoom is clamped to a sane range so the user can't lose the image by
    zooming to a speck or to absurd magnification.
    """

    MIN_SCALE = 0.05
    MAX_SCALE = 40.0

    def __init__(self, scene: QGraphicsScene, parent: Optional[QWidget] = None) -> None:
        super().__init__(scene, parent)
        # Zoom toward the cursor, not the view center.
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        # Left-drag pans.
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setRenderHints(self.renderHints())
        # Smooth scaling for the pixmap.
        from PySide6.QtGui import QPainter
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Track cumulative scale so we can clamp.
        self._scale: float = 1.0

    def wheelEvent(self, event) -> None:
        # One notch is typically 120 units. Zoom factor per notch.
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.25 if delta > 0 else 0.8
        new_scale = self._scale * factor
        # Clamp.
        if new_scale < self.MIN_SCALE:
            factor = self.MIN_SCALE / self._scale
            new_scale = self.MIN_SCALE
        elif new_scale > self.MAX_SCALE:
            factor = self.MAX_SCALE / self._scale
            new_scale = self.MAX_SCALE
        self._scale = new_scale
        self.scale(factor, factor)

    def reset_zoom(self, fit_rect=None) -> None:
        """Reset zoom to fit the scene in the view."""
        self.resetTransform()
        self._scale = 1.0
        if fit_rect is not None:
            self.fitInView(fit_rect, Qt.AspectRatioMode.KeepAspectRatio)
            # fitInView changes the transform; recompute our scale proxy
            # from the resulting matrix so subsequent wheel zoom is
            # relative to the fitted size.
            self._scale = self.transform().m11()
