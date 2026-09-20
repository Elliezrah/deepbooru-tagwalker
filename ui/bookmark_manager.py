"""
ui/bookmark_manager.py

The full bookmark list, for when the dropdown is not enough.

Two problems this solves. A menu grows with its contents: sixty saved
tags already reach the bottom of a 1080p screen, and past that Qt
falls back to scroll arrows, which is a miserable way to find
anything. And the star only ever toggles the item currently on screen,
so removing a bookmark meant navigating to it first — which is
impossible if it no longer exists.

So the dropdown keeps the handful most recently saved, and everything
else lives here: a real scrolling list, a filter box, and a Remove
that works on anything.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

_TITLES = {
    "post": "Saved posts",
    "tag": "Saved tags",
    "query": "Saved searches",
}


class BookmarkManager(QDialog):
    def __init__(self, kind: str, store, activate, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._store = store
        self._activate = activate
        self.setWindowTitle(_TITLES.get(kind, "Bookmarks"))
        self.setModal(False)
        self.resize(520, 420)

        layout = QVBoxLayout(self)
        self.count_label = QLabel("")
        layout.addWidget(self.count_label)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter\u2026")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._refill)
        layout.addWidget(self.filter)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(
            lambda _item: self._open())
        layout.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_open = QPushButton("Open")
        self.btn_open.clicked.connect(self._open)
        row.addWidget(self.btn_open)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setToolTip(
            "Delete the selected bookmark. The only place this is "
            "possible for an entry you are not currently looking at.")
        self.btn_remove.clicked.connect(self._remove)
        row.addWidget(self.btn_remove)
        row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        row.addWidget(btn_close)
        layout.addLayout(row)

        self._refill()

    # ------------------------------------------------------------------
    def _refill(self) -> None:
        needle = (self.filter.text() or "").strip().lower()
        entries = self._store.all(self._kind)
        shown = [b for b in entries
                 if not needle
                 or needle in b.display().lower()
                 or needle in b.value.lower()]
        self.list.clear()
        for entry in shown:
            item = QListWidgetItem(entry.display())
            item.setData(Qt.ItemDataRole.UserRole, entry)
            if entry.added:
                item.setToolTip(f"saved {entry.added}")
            self.list.addItem(item)
        if entries:
            self.count_label.setText(
                f"{len(shown)} of {len(entries)} shown"
                if needle else f"{len(entries)} saved")
        else:
            self.count_label.setText("Nothing saved yet.")
        if shown:
            self.list.setCurrentRow(0)

    def _selected(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _open(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        try:
            self._activate(entry)
        except Exception:
            pass
        self.close()

    def _remove(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Remove bookmark")
        confirm.setText(f"Remove \u201c{entry.display()}\u201d?")
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes
                                   | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        self._store.remove(entry.kind, entry.value)
        self._refill()
