"""
ui/media_view.py

The picture area of the post inspector: a still, or an animation, on
one zoomable surface.

Design decisions worth stating, because Danbooru posts arrive at every
size from a few hundred pixels to several thousand:

FIT BY DEFAULT, NEVER UPSCALED. Showing a 4000px post at native size
would blow the window apart, and every post would resize it to
something different. Fitting keeps the layout stable no matter what
arrives. Not upscaling means a genuinely small image is shown small
rather than blurrily inflated — which is honest about the source
rather than flattering it.

WHEEL ZOOMS, DRAG PANS. Nothing else. A click-to-toggle gesture was
tried and removed: the wheel already does the job, so clicking bought
nothing and competed with the pan. The gestures are the main image
viewer's, shared verbatim from ui/zoom_view.py rather than
reimplemented, so the two viewers cannot drift.

ANIMATION IS OPT-IN. A GIF renders its first frame and waits for the
Play button. Nothing moves until asked, matching the hover-to-reveal
principle the rest of the tool follows. Playing does not disturb zoom:
frames are pushed into the same item, so an animation can be zoomed
and panned while it runs.

A NOTICE SITS OVER THE PICTURE, not in place of it. When a post cannot
be displayed — a video — the still Danbooru provides is still worth
seeing, so the message is a translucent banner across it rather than a
replacement for it.
"""
from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QTimer
from PySide6.QtGui import QImageReader, QMovie, QPixmap
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.zoom_view import ZoomView

# A viewport smaller than this in either direction cannot be a real
# laid-out size, so a fit computed against it would be meaningless.
MIN_REAL_VIEWPORT = 40

# Danbooru serves a few formats Qt has no decoder for. Naming them is
# more useful than "cannot be displayed", which reads like a fault in
# the program rather than a property of the file.
UNSUPPORTED_NOTICE = (
    "This file is in a format this app cannot display "
    "\u2014 AVIF, JPEG XL, PSD, Flash (SWF) or a ZIP-packed "
    "animation. Download it to view in another program.\n\n"
    "(JPEG, PNG, GIF, WebP, BMP and TIFF all display here; "
    "MP4 and WebM videos show their still frame.)")


def is_animated(data: bytes) -> bool:
    """Whether these bytes hold more than one frame.

    Asked of the data rather than the file extension: a .webp may be
    either, and a mislabelled post should not decide the answer.
    """
    if not data:
        return False
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf)
    try:
        return bool(reader.supportsAnimation()
                    and reader.imageCount() > 1)
    finally:
        buf.close()


class MediaView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._view = ZoomView(self._scene, self)
        self._view.setFrameShape(ZoomView.Shape.NoFrame)
        self._item: QGraphicsPixmapItem | None = None
        self._fitted = True
        self._movie: QMovie | None = None
        self._buffer: QBuffer | None = None
        self._pending_fit = False
        # Owned by this widget, so it dies with it. A bare
        # QTimer.singleShot would fire into a destroyed object.
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self.fit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._view, 1)

        self.btn_play = QPushButton("\u25b6 Play")
        self.btn_play.setVisible(False)
        self.btn_play.setToolTip(
            "Play this animation. Nothing moves until you ask \u2014 "
            "and playing does not disturb the zoom.")
        self.btn_play.clicked.connect(self.toggle_play)
        layout.addWidget(self.btn_play)

        # Sits over the picture rather than replacing it.
        self.notice = QLabel(self._view)
        self.notice.setWordWrap(True)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notice.setStyleSheet(
            "background: rgba(0,0,0,170); color: #f0f0f0;"
            " padding: 6px; border-radius: 4px;")
        self.notice.setVisible(False)

    # ------------------------------------------------------------------
    def clear(self) -> None:
        self.stop()
        self._scene.clear()
        self._item = None
        self._fitted = True
        self.btn_play.setVisible(False)
        self.set_notice("")

    def set_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
        self._place_notice()

    def _place_notice(self) -> None:
        if not self.notice.isVisible():
            return
        margin = 8
        width = max(60, self._view.width() - margin * 2)
        self.notice.setFixedWidth(width)
        self.notice.adjustSize()
        self.notice.move(
            margin,
            max(margin, self._view.height() - self.notice.height()
                - margin))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_notice()
        if self._fitted or self._pending_fit:
            self.fit()

    def showEvent(self, event) -> None:  # noqa: N802
        """The moment a real viewport exists. An image loaded from the
        cache during construction is still waiting to be sized."""
        super().showEvent(event)
        if self._fitted or self._pending_fit:
            self.fit()
            # One more pass once the layout has settled: on the first
            # show the viewport is still a few pixels short of final,
            # and nothing else would re-run the fit.
            self._fit_timer.start(0)

    # ------------------------------------------------------------------
    def show_pixmap(self, pix: QPixmap) -> None:
        self.stop()
        self._scene.clear()
        self._item = self._scene.addPixmap(pix)
        self._scene.setSceneRect(self._item.boundingRect())
        self.btn_play.setVisible(False)
        self.fit()

    def show_animation(self, data: bytes) -> bool:
        """Load an animation, paused on its first frame."""
        self.stop()
        buf = QBuffer()
        buf.setData(QByteArray(data))
        if not buf.open(QIODevice.OpenModeFlag.ReadOnly):
            return False
        movie = QMovie()
        movie.setDevice(buf)
        movie.setCacheMode(QMovie.CacheMode.CacheAll)
        if not movie.isValid():
            buf.close()
            return False
        # The buffer must outlive the movie or frames stop arriving.
        self._buffer = buf
        self._movie = movie
        self._scene.clear()
        self._item = self._scene.addPixmap(QPixmap())
        movie.frameChanged.connect(self._on_frame)
        movie.jumpToFrame(0)          # show frame one, paused
        self._on_frame(0)
        self.btn_play.setVisible(True)
        self.btn_play.setText("\u25b6 Play")
        self.fit()
        return True

    def _on_frame(self, _index: int = 0) -> None:
        if self._movie is None or self._item is None:
            return
        pix = self._movie.currentPixmap()
        if pix.isNull():
            return
        first = self._scene.sceneRect().isEmpty()
        self._item.setPixmap(pix)
        if first:
            self._scene.setSceneRect(self._item.boundingRect())

    # ------------------------------------------------------------------
    def toggle_play(self) -> None:
        if self._movie is None:
            return
        running = self._movie.state() == QMovie.MovieState.Running
        if running:
            self._movie.setPaused(True)
            self.btn_play.setText("\u25b6 Play")
        else:
            self._movie.start()
            self.btn_play.setText("\u23f8 Pause")

    def is_playing(self) -> bool:
        return (self._movie is not None
                and self._movie.state() == QMovie.MovieState.Running)

    def stop(self) -> None:
        if self._movie is not None:
            try:
                self._movie.stop()
                self._movie.frameChanged.disconnect(self._on_frame)
            except (RuntimeError, TypeError):
                pass
        self._movie = None
        if self._buffer is not None:
            try:
                self._buffer.close()
            except RuntimeError:
                pass
        self._buffer = None
        self.btn_play.setVisible(False)

    # ------------------------------------------------------------------
    def fit(self) -> None:
        """Scale down to the box, but never up.

        FIELD BUG: pictures often opened absurdly small. A cached
        image calls back SYNCHRONOUSLY while the popup is still being
        constructed, so the viewport was still its unlaid-out default
        (about 98x28) and a 1200px image was fitted to that — a scale
        of 0.03. The first view of a post came from the network, after
        layout, and looked right; every later view came from the cache
        and did not, so it got worse the more you browsed.

        Two changes. The scale is computed here rather than handed to
        fitInView, so it is arithmetic anyone can check instead of
        Qt's idea of the viewport including its margins. And a
        viewport too small to be real defers the fit rather than
        obeying it, so the size is decided once the widget actually
        has one.
        """
        self._fitted = True
        if self._item is None:
            return
        rect = self._item.boundingRect()
        if rect.isEmpty() or rect.width() <= 0 or rect.height() <= 0:
            return
        viewport = self._view.viewport().size()
        if viewport.width() < MIN_REAL_VIEWPORT or \
                viewport.height() < MIN_REAL_VIEWPORT:
            # Not laid out yet. Fitting now would bake in a nonsense
            # scale, so wait for a real size.
            self._pending_fit = True
            return
        self._pending_fit = False
        scale = min(1.0,
                    viewport.width() / rect.width(),
                    viewport.height() / rect.height())
        self._view.resetTransform()
        if scale < 1.0:
            self._view.scale(scale, scale)
        self._view._scale = scale
        self._view.centerOn(self._item)

    def is_fitted(self) -> bool:
        return self._fitted
