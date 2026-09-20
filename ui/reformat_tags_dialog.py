"""
ui/reformat_tags_dialog.py

Tools -> Reformat Tags: bulk convert underscores <-> spaces in tags.

A dataset-wide caption rewrite, made safe to look at before it runs:

  * Pick a direction (underscores -> spaces, or the reverse).
  * Every tag that would change is listed as `old -> new`, ticked by
    default. Untick anything you want left alone.
  * Emoticon / symbol tags (^_^, o_o, >_<) are pulled into a separate
    "Protected" section and UNticked by default, so they can't be mangled
    unless you deliberately tick one.
  * Conversions that would merge into an existing tag are flagged.
  * Apply runs the whole thing as ONE undoable step.

The transform is per-tag (handled by SessionState.build_reformat_map), so
the comma separators between tags are never touched.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Spacing, repolish, scrollbar_with_arrows_qss
from core import reformat_guard

_PAIR_ROLE = Qt.ItemDataRole.UserRole + 1


class ReformatTagsDialog(QDialog):
    """Bulk underscore<->space reformat with a per-tag preview + guard."""

    def __init__(self, state, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state = state
        self._prog = None
        self.setWindowTitle("Reformat Tags")
        self.resize(560, 560)
        self._build()
        self._reload()

    # ---- construction -------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(Spacing.NORMAL)

        intro = QLabel(
            "Convert the underscores and spaces inside your tags across the "
            "whole dataset \u2014 e.g. \u201cblack_elbow_gloves\u201d \u2194 "
            "\u201cblack elbow gloves\u201d. Only the text inside each tag "
            "changes; the commas between tags are left alone. Review the "
            "list below, untick anything you want to keep, then Apply. The "
            "whole change is a single Undo."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "secondary")
        outer.addWidget(intro)

        # Direction.
        dir_row = QHBoxLayout()
        self.rb_to_spaces = QRadioButton("Underscores \u2192 spaces")
        self.rb_to_unders = QRadioButton("Spaces \u2192 underscores")
        self.rb_to_spaces.setChecked(True)
        self._dir_group = QButtonGroup(self)
        self._dir_group.addButton(self.rb_to_spaces)
        self._dir_group.addButton(self.rb_to_unders)
        self.rb_to_spaces.toggled.connect(self._reload)
        dir_row.addWidget(self.rb_to_spaces)
        dir_row.addWidget(self.rb_to_unders)
        dir_row.addStretch(1)
        outer.addLayout(dir_row)

        # Summary line.
        self.lbl_summary = QLabel("")
        self.lbl_summary.setProperty("role", "secondary")
        outer.addWidget(self.lbl_summary)

        # Main (convert) list.
        self.list_main = QListWidget()
        self.list_main.verticalScrollBar().setStyleSheet(
            scrollbar_with_arrows_qss()
        )
        outer.addWidget(self.list_main, 3)

        # Protected (skipped-by-default) section.
        self.lbl_protected = QLabel(
            "Protected (emoticons & symbols) \u2014 skipped by default. "
            "Tick one only if you really want it converted:"
        )
        self.lbl_protected.setWordWrap(True)
        self.lbl_protected.setProperty("role", "secondary")
        outer.addWidget(self.lbl_protected)
        self.list_protected = QListWidget()
        self.list_protected.verticalScrollBar().setStyleSheet(
            scrollbar_with_arrows_qss()
        )
        outer.addWidget(self.list_protected, 1)

        # Select-all / none for the main list (convenience on big sets).
        sel_row = QHBoxLayout()
        self.btn_all = QPushButton("Tick all")
        self.btn_all.setProperty("role", "secondary")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none = QPushButton("Untick all")
        self.btn_none.setProperty("role", "secondary")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        sel_row.addWidget(self.btn_all)
        sel_row.addWidget(self.btn_none)
        sel_row.addStretch(1)
        outer.addLayout(sel_row)

        # Bottom buttons.
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setProperty("role", "primary")
        repolish(self.btn_apply)
        self.btn_apply.setDefault(True)
        self.btn_apply.clicked.connect(self._on_apply)
        btn_row.addWidget(self.btn_apply)
        outer.addLayout(btn_row)

    # ---- helpers ------------------------------------------------------
    def _to_spaces(self) -> bool:
        return self.rb_to_spaces.isChecked()

    def _make_item(self, old: str, new: str, note: str, checked: bool) -> QListWidgetItem:
        label = f"{old}  \u2192  {new}"
        if note:
            label += f"    {note}"
        item = QListWidgetItem(label)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        item.setData(_PAIR_ROLE, (old, new))
        return item

    def _reload(self) -> None:
        # toggled fires for both buttons; only rebuild once.
        if self.sender() is self.rb_to_unders:
            return
        self.list_main.clear()
        self.list_protected.clear()

        rmap = self._state.build_reformat_map(self._to_spaces())
        n_protected = 0
        for old in sorted(rmap.keys()):
            new = rmap[old]
            if reformat_guard.is_protected_tag(old):
                self.list_protected.addItem(
                    self._make_item(old, new, "(emoticon / symbol)", checked=False)
                )
                n_protected += 1
            else:
                merges = self._state.get_tag_count_global(new) > 0
                note = "\u2014 merges into existing tag" if merges else ""
                self.list_main.addItem(
                    self._make_item(old, new, note, checked=True)
                )

        n_main = self.list_main.count()
        if n_main == 0 and n_protected == 0:
            self.lbl_summary.setText(
                "Every tag is already in this format \u2014 nothing to convert."
            )
            self.btn_apply.setEnabled(False)
        else:
            self.lbl_summary.setText(
                f"{n_main} tag(s) will change. "
                f"{n_protected} protected tag(s) skipped."
            )
            self.btn_apply.setEnabled(True)

        # Hide the protected section entirely when there's nothing in it.
        has_protected = n_protected > 0
        self.lbl_protected.setVisible(has_protected)
        self.list_protected.setVisible(has_protected)

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.list_main.count()):
            self.list_main.item(i).setCheckState(state)

    def _collect_checked(self) -> dict:
        rmap: dict = {}
        for lst in (self.list_main, self.list_protected):
            for i in range(lst.count()):
                item = lst.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    old, new = item.data(_PAIR_ROLE)
                    rmap[old] = new
        return rmap

    # ---- apply --------------------------------------------------------
    def _on_apply(self) -> None:
        rmap = self._collect_checked()
        if not rmap:
            QMessageBox.information(
                self, "Reformat Tags",
                "Nothing is ticked, so there's nothing to convert.",
            )
            return
        n = len(rmap)
        confirm = QMessageBox.question(
            self, "Reformat Tags",
            f"Convert {n} tag(s) across the dataset now?\n\n"
            "This rewrites the affected caption files. It's a single Undo "
            "if you change your mind.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return

        # Busy feedback. The apply blocks the UI thread, so if we ran it
        # inline the dialog would only half-paint before the freeze. Show
        # the dialog, then hand control back to the event loop so it can
        # FULLY paint, and only then (next tick) run the work. (Threading
        # the apply isn't safe — it emits state changes that touch widgets
        # on this thread.)
        self._prog = QProgressDialog(
            "Reformatting tags across the dataset\u2026\n"
            "This can take a moment on a large set.",
            None, 0, 0, self,
        )
        self._prog.setWindowTitle("Reformat Tags")
        self._prog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._prog.setMinimumDuration(0)
        self._prog.setAutoClose(False)
        self._prog.setAutoReset(False)
        self._prog.show()
        # Defer until the dialog has actually rendered. The 80 ms gives the
        # window manager time to map + paint it before the event loop locks.
        QTimer.singleShot(80, lambda: self._do_apply(rmap))

    def _do_apply(self, rmap: dict) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            res = self._state.apply_tag_rename_map(rmap)
        finally:
            QApplication.restoreOverrideCursor()
            if self._prog is not None:
                self._prog.close()
                self._prog = None

        msg = (f"Done \u2014 reformatted {res['tags']} tag(s) across "
               f"{res['images']} image(s).")
        failed = res.get("failed") or []
        if failed:
            shown = ", ".join(failed[:10]) + (" \u2026" if len(failed) > 10 else "")
            msg += ("\n\n\u26a0 " + f"{len(failed)} file(s) could not be written "
                    "(open elsewhere, read-only, or locked by antivirus / cloud "
                    "sync) and were left unchanged:\n" + shown +
                    "\n\nClose anything using them and run it again.")
        msg += "\n\nUse Edit \u2192 Undo to reverse the whole change."
        QMessageBox.information(self, "Reformat Tags", msg)

        # Offer to sync the tag-entry format to the direction just
        # converted, so new tags typed/autocompleted match the dataset.
        # NEVER automatic — the user may convert for one reason but want
        # entry a certain way. Only ask when the setting isn't already in
        # that direction (and only if any tags actually changed).
        if res.get("tags"):
            self._maybe_offer_format_sync()
        self.accept()

    def _maybe_offer_format_sync(self) -> None:
        """After a conversion, offer (never force) to switch the tag-entry
        format setting to match the direction just applied."""
        try:
            from config.settings import Settings
            settings = Settings()
        except Exception:
            return
        to_spaces = self._to_spaces()
        target = "spaces" if to_spaces else "underscores"
        # Already in that direction — nothing to offer.
        if settings.tag_entry_format == target:
            return
        human_dir = "spaces" if to_spaces else "underscores"
        example = "long hair" if to_spaces else "long_hair"
        resp = QMessageBox.question(
            self, "Switch tag entry format?",
            f"You converted your dataset to {human_dir}.\n\n"
            f"Switch tag entry and autocomplete to {human_dir} too, so new "
            f"tags you type match (e.g. \u201c{example}\u201d)?\n\n"
            "You can change this any time in Preferences \u2192 Tag entry.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if resp == QMessageBox.StandardButton.Yes:
            settings.tag_entry_format = target
