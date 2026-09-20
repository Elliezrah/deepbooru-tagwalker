"""
ui/paged_help_dialog.py

A small fixed-size help window that pages through short sections.

A message box grows to fit its text, so a long explanation pushes the
window past the edge of the screen with no way to scroll it back — the
longer the help, the less of it can be read. Paging keeps the window a
constant, sensible size no matter how much there is to say, and lets
each page be written as one idea rather than a wall.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class PagedHelpDialog(QDialog):
    def __init__(self, title: str, pages, parent=None) -> None:
        """pages: sequence of (heading, body) pairs."""
        super().__init__(parent)
        self._pages = list(pages) or [("", "(no help available)")]
        self._index = 0
        self.setWindowTitle(title)
        self.setModal(False)
        # Fixed rather than sized to content: the whole point is that
        # the window cannot outgrow the screen.
        self.resize(560, 420)
        self.setMinimumSize(420, 320)

        layout = QVBoxLayout(self)

        self.heading = QLabel("")
        self.heading.setWordWrap(True)
        font = self.heading.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1.5)
        self.heading.setFont(font)
        layout.addWidget(self.heading)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        holder_layout.addWidget(self.body)
        holder_layout.addStretch(1)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        nav = QHBoxLayout()
        self.btn_prev = QPushButton("\u2039")
        self.btn_prev.setFixedWidth(36)
        self.btn_prev.setToolTip("Previous page")
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        nav.addWidget(self.btn_prev)
        self.lbl_page = QLabel("")
        nav.addWidget(self.lbl_page)
        self.btn_next = QPushButton("\u203a")
        self.btn_next.setFixedWidth(36)
        self.btn_next.setToolTip("Next page")
        self.btn_next.clicked.connect(lambda: self.step(1))
        nav.addWidget(self.btn_next)
        nav.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        nav.addWidget(btn_close)
        layout.addLayout(nav)

        self._render()

    def step(self, delta: int) -> None:
        self._index = max(0, min(len(self._pages) - 1,
                                 self._index + delta))
        self._render()

    def _render(self) -> None:
        heading, body = self._pages[self._index]
        self.heading.setText(heading)
        self.body.setText(body)
        self.lbl_page.setText(
            f"{self._index + 1} / {len(self._pages)}")
        self.btn_prev.setEnabled(self._index > 0)
        self.btn_next.setEnabled(self._index < len(self._pages) - 1)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Left:
            self.step(-1)
        elif event.key() == Qt.Key.Key_Right:
            self.step(1)
        else:
            super().keyPressEvent(event)
