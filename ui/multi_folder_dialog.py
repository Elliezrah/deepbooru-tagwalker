"""
ui/multi_folder_dialog.py

File → Open Multiple Folders — the LOAD LIST manager ("playlist"
model, field revision two).

Users keep multiple NAMED lists of dataset folders in user config.
The dialog is both creator and picker: the top half manages lists
(New / Rename / Delete), the bottom half edits the selected list's
folders (Add / Remove). Edits AUTO-SAVE to config immediately — like
a music playlist, there is no save button to forget. "Load Selected
List" applies the selection; the last-used list is preselected on
open, so the routine restart flow stays two clicks, and switching
between multi-folder datasets is select → Load.

Missing-folder policy (unchanged, now per-list): a listed folder that
no longer exists is marked "(missing)", never silently dropped; Load
offers to proceed with the available folders, and the list KEEPS the
missing entry unless the user removes it — an unplugged drive is not
forgotten.

Migration: a legacy single remembered set (multi_load_folders from
the previous revision) becomes a list named "Remembered folders" on
first open, then the legacy key is cleared.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class MultiFolderDialog(QDialog):
    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._roots: list[Path] = []
        self.setWindowTitle("Open Multiple Folders")
        self.setMinimumSize(560, 460)

        self._lists: dict[str, list[str]] = dict(
            getattr(settings, "multi_load_lists", {}) or {})
        # Migrate the legacy single remembered set (previous field
        # revision) into a named list, once.
        legacy = list(getattr(settings, "multi_load_folders", []) or [])
        if legacy and not self._lists:
            self._lists["Remembered folders"] = legacy
            settings.multi_load_folders = []
            self._persist()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Saved load lists \u2014 select one and Load, or create "
            "and edit lists (changes save automatically):"))

        self.name_list = QListWidget()
        self.name_list.setMaximumHeight(140)
        self.name_list.currentRowChanged.connect(self._on_name_selected)
        layout.addWidget(self.name_list)

        nrow = QHBoxLayout()
        self.btn_new = QPushButton("New List")
        self.btn_new.clicked.connect(lambda: self._new_list())
        self.btn_rename = QPushButton("Rename\u2026")
        self.btn_rename.clicked.connect(self._rename_list)
        self.btn_delete = QPushButton("Delete List")
        self.btn_delete.clicked.connect(self._delete_list)
        nrow.addWidget(self.btn_new)
        nrow.addWidget(self.btn_rename)
        nrow.addWidget(self.btn_delete)
        nrow.addStretch(1)
        layout.addLayout(nrow)

        layout.addWidget(QLabel("Folders in the selected list:"))
        self.folder_list = QListWidget()
        layout.addWidget(self.folder_list, 1)

        frow = QHBoxLayout()
        self.btn_add = QPushButton("Add Folder\u2026")
        self.btn_add.clicked.connect(self._add_via_picker)
        self.btn_remove = QPushButton("Remove Selected")
        self.btn_remove.clicked.connect(self._remove_selected)
        frow.addWidget(self.btn_add)
        frow.addWidget(self.btn_remove)
        frow.addStretch(1)
        layout.addLayout(frow)

        row2 = QHBoxLayout()
        self.btn_load = QPushButton("Load Selected List")
        self.btn_load.clicked.connect(self.accept)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)
        row2.addStretch(1)
        row2.addWidget(self.btn_load)
        row2.addWidget(self.btn_close)
        layout.addLayout(row2)

        self._refresh_names(
            select=getattr(settings, "last_multi_list", "") or None)

    # ------------------------------------------------------------------
    # List-of-lists management
    # ------------------------------------------------------------------
    def _persist(self) -> None:
        self._settings.multi_load_lists = self._lists

    def _current_name(self) -> str | None:
        item = self.name_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _refresh_names(self, select: str | None = None) -> None:
        keep = select or self._current_name()
        self.name_list.blockSignals(True)
        self.name_list.clear()
        row_to_select = 0
        for i, (name, paths) in enumerate(self._lists.items()):
            missing = sum(1 for p in paths if not Path(p).is_dir())
            label = f"{name}  ({len(paths)} folders"
            label += f", {missing} missing)" if missing else ")"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.name_list.addItem(item)
            if name == keep:
                row_to_select = i
        self.name_list.blockSignals(False)
        if self.name_list.count():
            self.name_list.setCurrentRow(row_to_select)
        else:
            self.folder_list.clear()
        self._update_enabled()

    def _update_enabled(self) -> None:
        has = self._current_name() is not None
        for b in (self.btn_rename, self.btn_delete,
                  self.btn_add, self.btn_remove, self.btn_load):
            b.setEnabled(has)

    def _on_name_selected(self, _row: int) -> None:
        self._rebuild_folder_list()
        self._update_enabled()

    def _unique_name(self, base: str) -> str:
        if base not in self._lists:
            return base
        i = 2
        while f"{base} {i}" in self._lists:
            i += 1
        return f"{base} {i}"

    def _new_list(self, name: str | None = None) -> str:
        name = self._unique_name(name or "New list")
        self._lists[name] = []
        self._persist()
        self._refresh_names(select=name)
        return name

    def _rename_list(self) -> None:
        old = self._current_name()
        if old is None:
            return
        new, ok = QInputDialog.getText(
            self, "Rename List", "List name:", text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        if new in self._lists:
            QMessageBox.information(
                self, "Name Taken",
                f'A list named "{new}" already exists.')
            return
        self._lists = {(new if k == old else k): v
                       for k, v in self._lists.items()}
        self._persist()
        self._refresh_names(select=new)

    def _delete_list(self) -> None:
        name = self._current_name()
        if name is None:
            return
        if QMessageBox.question(
            self, "Delete List",
            f'Delete the load list "{name}"?\n'
            "(Folders on disk are not touched.)",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._lists.pop(name, None)
        self._persist()
        self._refresh_names()

    # ------------------------------------------------------------------
    # Folder editing within the selected list (auto-saved)
    # ------------------------------------------------------------------
    def _rebuild_folder_list(self) -> None:
        self.folder_list.clear()
        name = self._current_name()
        if name is None:
            return
        for raw in self._lists.get(name, []):
            missing = not Path(raw).is_dir()
            item = QListWidgetItem(
                raw + ("  (missing)" if missing else ""))
            item.setData(Qt.ItemDataRole.UserRole, raw)
            if missing:
                item.setToolTip("This folder was not found at its "
                                "remembered location.")
            self.folder_list.addItem(item)

    def _add_path(self, path) -> bool:
        """Append a folder to the SELECTED list (deduplicated by
        resolved path); auto-saves. Returns True if added."""
        name = self._current_name()
        if name is None:
            return False
        raw = str(path)
        try:
            key = str(Path(raw).resolve())
        except OSError:
            key = raw
        for existing in self._lists[name]:
            try:
                if str(Path(existing).resolve()) == key:
                    return False
            except OSError:
                if existing == raw:
                    return False
        self._lists[name].append(raw)
        self._persist()
        self._rebuild_folder_list()
        self._refresh_names(select=name)
        return True

    def _add_via_picker(self) -> None:
        name = self._current_name()
        if name is None:
            return
        paths = self._lists.get(name, [])
        start = (paths[-1] if paths
                 else getattr(self._settings, "last_directory", "")
                 or os.path.expanduser("~"))
        chosen = QFileDialog.getExistingDirectory(
            self, "Add dataset folder", start)
        if chosen:
            self._add_path(chosen)

    def _remove_selected(self) -> None:
        name = self._current_name()
        row = self.folder_list.currentRow()
        if name is None or row < 0:
            return
        raw = self.folder_list.item(row).data(
            Qt.ItemDataRole.UserRole)
        self._lists[name] = [p for p in self._lists[name] if p != raw]
        self._persist()
        self._rebuild_folder_list()
        self._refresh_names(select=name)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _confirm_missing(self, missing: list[str]) -> bool:
        listing = "\n".join(f"  \u2022 {m}" for m in missing)
        return QMessageBox.question(
            self, "Some Folders Are Missing",
            "These folders in the list were not found:\n\n"
            f"{listing}\n\n"
            "Load the available folders anyway? (The missing ones "
            "stay in the list unless you remove them.)",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) == QMessageBox.StandardButton.Yes

    def selected_roots(self) -> list[Path]:
        """The folders to load (existing only), valid after accept."""
        return list(self._roots)

    def accept(self) -> None:  # noqa: D102 - QDialog override
        name = self._current_name()
        if name is None:
            QMessageBox.information(
                self, "No List Selected",
                "Create or select a load list first.")
            return
        paths = self._lists.get(name, [])
        if not paths:
            QMessageBox.information(
                self, "Empty List",
                f'"{name}" has no folders yet \u2014 add at least '
                "one.")
            return
        existing = [p for p in paths if Path(p).is_dir()]
        missing = [p for p in paths if p not in existing]
        if not existing:
            QMessageBox.warning(
                self, "Nothing to Load",
                "None of the folders in this list exist. Remove or "
                "replace them, or plug the drive back in.")
            return
        if missing and not self._confirm_missing(missing):
            return
        self._settings.last_multi_list = name
        self._roots = [Path(p) for p in existing]
        super().accept()
