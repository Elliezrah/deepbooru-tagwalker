"""
ui/asset_viewer_dialog.py

A bigger look at a reference image.

Character wiki pages illustrate their outfits with `!asset` embeds
rather than posts — a media asset is just a picture, with no post
behind it and so no rating, no tag list and nothing to inspect. That
was taken as a reason to make those thumbnails unclickable, which was
wrong: an asset tile shows an image, and clicking an image to see it
larger is the obvious thing to expect. A tile that renders but ignores
clicks is indistinguishable from a broken one.

So they open here instead. No tag list, because there genuinely is
not one — just the picture, its caption from the wiki, and the same
zoom and pan as everywhere else.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ui.media_view import (MediaView, UNSUPPORTED_NOTICE,
                           is_animated)


class AssetViewerDialog(QDialog):
    def __init__(self, asset_id: int, caption: str = "",
                 parent=None) -> None:
        super().__init__(parent)
        self._asset_id = asset_id
        self.setWindowTitle(f"Reference image \u2014 asset #{asset_id}")
        self.setModal(False)
        self.resize(700, 700)

        layout = QVBoxLayout(self)
        header = QLabel(
            f"<b>asset #{asset_id}</b>"
            + (f" \u00b7 {caption}" if caption else ""))
        header.setWordWrap(True)
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        self.media = MediaView()
        self.media.setToolTip(
            "Mouse wheel zooms, drag pans. Double-click anywhere to "
            "close.")
        layout.addWidget(self.media, 1)

        note = QLabel(
            "A wiki reference image. There is no post behind it, so "
            "no tags or rating to show.")
        note.setWordWrap(True)
        layout.addWidget(note)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        row.addWidget(btn_close)
        layout.addLayout(row)

        from ui.zoom_view import suppress_context_menus
        suppress_context_menus(self)
        self.media.set_notice("loading\u2026")

    # ------------------------------------------------------------------
    def set_bytes(self, data: bytes | None) -> None:
        if not data:
            self.media.set_notice("This image could not be loaded.")
            return
        self.media.set_notice("")
        if is_animated(data) and self.media.show_animation(data):
            return
        from PySide6.QtGui import QPixmap

        pix = QPixmap()
        if pix.loadFromData(data):
            self.media.show_pixmap(pix)
        else:
            self.media.set_notice(UNSUPPORTED_NOTICE)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Same dismissal as the post inspector, so the two windows
        behave alike."""
        self.close()
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.media.stop()
        except RuntimeError:
            pass
        super().closeEvent(event)
