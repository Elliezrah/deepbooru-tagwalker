"""Tools → Audit exceptions: manage the Danbooru-audit exception list.

The tag audit (Tools → Audit tags) flags tags that aren't clean Danbooru
matches. Tags marked "Always OK" there land in a persistent exception
list and stop being flagged. This dialog lets the user review that list,
remove entries (so a tag gets checked again), and add tags by hand.

The list itself lives in core.audit_exceptions_io; this is just a thin
editor over it. Every change writes through immediately.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Spacing, scrollbar_with_arrows_qss
from core import audit_exceptions_io as aex
from ui.tag_autocomplete import TagAutocomplete


class AuditExceptionsDialog(QDialog):
    """Review/add/remove tags on the audit exception list."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Audit exceptions")
        self.resize(440, 480)
        self._build()
        self._reload()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(Spacing.NORMAL)

        intro = QLabel(
            "Tags listed here are never flagged by the tag audit. Add the "
            "deliberate ones \u2014 studio names, OC tags, conventions your "
            "dataset uses on purpose. Remove a tag to have the audit check "
            "it again."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "secondary")
        outer.addWidget(intro)

        # Add row: tag box (with autocomplete) + Add button.
        add_row = QHBoxLayout()
        self.add_box = QLineEdit()
        self.add_box.setPlaceholderText("tag to never flag\u2026")
        self.add_box.returnPressed.connect(self._on_add)
        self._add_ac = TagAutocomplete(self.add_box)
        add_row.addWidget(self.add_box, 1)
        self.btn_add = QPushButton("Add")
        self.btn_add.clicked.connect(self._on_add)
        add_row.addWidget(self.btn_add)
        outer.addLayout(add_row)

        # The list of excepted tags.
        self.list = QListWidget()
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.list.verticalScrollBar().setStyleSheet(
            scrollbar_with_arrows_qss()
        )
        self.list.itemSelectionChanged.connect(self._update_buttons)
        outer.addWidget(self.list, 1)

        # Bottom buttons.
        btn_row = QHBoxLayout()
        self.btn_remove = QPushButton("Remove selected")
        self.btn_remove.clicked.connect(self._on_remove)
        btn_row.addWidget(self.btn_remove)
        btn_row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_close.setDefault(True)
        btn_row.addWidget(btn_close)
        outer.addLayout(btn_row)

    # ------------------------------------------------------------------

    def _reload(self) -> None:
        self.list.clear()
        tags = sorted(aex.load_exceptions())
        if tags:
            for tag in tags:
                QListWidgetItem(tag, self.list)
        else:
            placeholder = QListWidgetItem("(no exceptions yet)")
            placeholder.setForeground(QBrush(QColor(Colors.TEXT_TERTIARY)))
            # Non-selectable so it can't be "removed".
            placeholder.setFlags(
                placeholder.flags() & ~Qt.ItemFlag.ItemIsSelectable
            )
            self.list.addItem(placeholder)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.btn_remove.setEnabled(len(self.list.selectedItems()) > 0)

    def _on_add(self) -> None:
        tag = self.add_box.text().strip().lower()
        if not tag:
            return
        aex.add_exception(tag)
        self.add_box.clear()
        self._reload()

    def _on_remove(self) -> None:
        tags = [item.text() for item in self.list.selectedItems()]
        if not tags:
            return
        for t in tags:
            aex.remove_exception(t)
        self._reload()
