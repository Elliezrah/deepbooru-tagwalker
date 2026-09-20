"""Conflict scan dialog — find and resolve rule violations (C + D).

Opened from Tools → Scan for Tag Conflicts. The flow mirrors the Danbooru
audit dialog so it feels familiar:

  1. Opens with a short summary and a Scan button. Nothing is read until
     the user clicks Scan.
  2. Scan runs the enabled conflict rules over every image (alias-aware)
     and lists each violation as a row.
  3. Each row offers aligned actions:
       - EXCLUSION  ("has both indoors and outdoors"): a CHECKBOX per
         conflicting tag — "Remove indoors" / "Remove outdoors". More
         than one can be ticked, so an image flagged for several
         mutually-exclusive tags can have several removed at once.
         Ticking nothing leaves the image unchanged.
       - REQUIREMENT ("has cat_ears, missing animal_ears"): an "Add
         animal_ears" checkbox; leave it unticked to do nothing.
     A per-rule batch control at the top can set every row of a given
     exclusion rule to remove a chosen tag, or every requirement row to
     add its missing tag — for when the user is confident.
  4. Confirm applies the chosen actions. Each edit is atomic and
     individually undoable (via Back); resolving many at once creates
     one undo entry per edit, which the confirm prompt notes.

All writes go through the existing per-image mutators
(remove_tag_from_image / add_tag_to_image), so files are never left
half-written.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Spacing, scrollbar_with_arrows_qss
from core import conflict_rules as cr
from core import conflict_rules_io as crio
from core import tag_io
from core.state import SessionState


# Per-row resolution actions.
ACT_NOTHING = "nothing"
ACT_REMOVE = "remove"   # exclusion: remove a chosen tag
ACT_ADD = "add"         # requirement: add the missing tag


class _ViolationRow(QWidget):
    """One violation row with aligned resolution controls.

    Fixed-width action columns keep the controls aligned across rows
    (the same approach that fixed the audit dialog's alignment): the
    left side describes the violation; the right side holds the action
    radios at consistent x-positions.
    """

    COL_NOTHING = 96

    def __init__(self, violation: cr.Violation,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.violation = violation
        self._build()

    def _build(self) -> None:
        # Vertical layout: description on top (full width), then the
        # action radios on their own row beneath. This guarantees the
        # "Remove <tag>" radios never run off the right edge of the
        # window regardless of how many conflicting tags a row has or how
        # long their names are (the horizontal layout was the clipping
        # bug). A separator line keeps rows visually distinct.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Spacing.LOOSE, Spacing.NORMAL, Spacing.LOOSE, Spacing.NORMAL
        )
        outer.setSpacing(Spacing.TIGHT)

        v = self.violation
        # ---- Top: image name + a small "View" button + description ----
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(Spacing.NORMAL)
        img_label = QLabel(v.image_path.name)
        img_label.setProperty("role", "mono")
        img_label.setStyleSheet("font-weight: 600;")
        img_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        name_row.addWidget(img_label, 1)
        view_btn = QPushButton("\U0001F50D View")  # magnifier glyph
        view_btn.setToolTip("Open this image in a popup to inspect it")
        view_btn.setFixedHeight(24)
        view_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        view_btn.clicked.connect(self._on_view_image)
        name_row.addWidget(view_btn, 0)
        outer.addLayout(name_row)
        desc = self._desc_label()
        desc.setWordWrap(True)
        outer.addWidget(desc)

        # ---- Action controls on their own row -----------------------
        # Exclusion violations offer a checkbox per conflicting tag, so
        # more than one tag can be removed from the same image (an image
        # flagged for three mutually-exclusive tags may need two of them
        # gone, not just one — the old single-select radio group could
        # not express that). Requirement violations have a single "Add"
        # action. "Do nothing" is the absence of any tick, so there is
        # no separate control for it.
        actions = QHBoxLayout()
        actions.setSpacing(Spacing.LOOSE)
        actions.setContentsMargins(Spacing.NORMAL, 0, 0, 0)

        self._tag_boxes: dict[str, QCheckBox] = {}
        if v.rule.rule_type == cr.RULE_EXCLUSION:
            # Dedupe while preserving first-seen order: the same tag can
            # appear twice in `present` (e.g. a tag and its alias both
            # resolving to it), which previously produced two identical
            # "Remove <tag>" controls.
            seen: set[str] = set()
            for tag in v.present:
                if tag in seen:
                    continue
                seen.add(tag)
                cb = QCheckBox(f"Remove {tag}")
                cb.setStyleSheet(
                    f"QCheckBox {{ color: {Colors.DANGER_RED}; }}"
                )
                actions.addWidget(cb)
                self._tag_boxes[tag] = cb
        else:
            cb = QCheckBox(f"Add {v.missing}")
            cb.setStyleSheet(
                f"QCheckBox {{ color: {Colors.SUCCESS_GREEN}; "
                f"font-weight: 600; }}"
            )
            actions.addWidget(cb)
            self._tag_boxes[v.missing] = cb
        actions.addStretch(1)
        outer.addLayout(actions)

    def _on_view_image(self) -> None:
        """Open the flagged image in a popup so the user can judge whether
        the conflict is fair before choosing an action."""
        from ui.image_preview_popup import show_image_preview
        show_image_preview(self.violation.image_path, self)

    def _desc_label(self) -> QLabel:
        v = self.violation
        if v.rule.rule_type == cr.RULE_EXCLUSION:
            tags = " + ".join(
                f"<b style='color:{Colors.DANGER_RED};'>{t}</b>"
                for t in v.present
            )
            txt = (
                f"<span style='color:{Colors.DANGER_RED};'>\u25cf</span> "
                f"<span style='color:{Colors.TEXT_SECONDARY};'>has "
                f"{tags} \u2014 rule \u201c{v.rule.display_name()}\u201d"
                f"</span>"
            )
        else:
            txt = (
                f"<span style='color:{Colors.WARNING_AMBER};'>\u25cf</span> "
                f"<span style='color:{Colors.TEXT_SECONDARY};'>has "
                f"<b>{v.trigger}</b> but is missing "
                f"<b style='color:{Colors.SUCCESS_GREEN};'>{v.missing}</b>"
                f"</span>"
            )
        lbl = QLabel(txt)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        return lbl

    # -- mass-apply helpers --------------------------------------------
    def set_nothing(self) -> None:
        # Clear every tick: "do nothing" is the absence of any action.
        for cb in self._tag_boxes.values():
            cb.setChecked(False)

    def set_remove_tag(self, tag: str) -> None:
        """For exclusion rows: tick 'Remove <tag>' if this row has it.

        Additive (does not clear other ticks), so applying a mass
        "remove X" across rows composes with any per-row choices the
        user already made.
        """
        cb = self._tag_boxes.get(tag)
        if cb is not None:
            cb.setChecked(True)

    def set_add_missing(self) -> None:
        """For requirement rows: tick the Add action."""
        if self.violation.rule.rule_type == cr.RULE_REQUIREMENT:
            cb = self._tag_boxes.get(self.violation.missing)
            if cb is not None:
                cb.setChecked(True)

    def chosen_actions(self):
        """Return a list of (kind, tag) for every ticked action.

        Multiple for an exclusion row where several tags are ticked;
        one for a requirement row; empty when nothing is ticked.
        """
        out = []
        is_exclusion = (
            self.violation.rule.rule_type == cr.RULE_EXCLUSION)
        for tag, cb in self._tag_boxes.items():
            if cb.isChecked():
                out.append(
                    (ACT_REMOVE, tag) if is_exclusion else (ACT_ADD, tag))
        return out


class ConflictScanDialog(QDialog):
    """Scan the dataset for rule violations and resolve them."""

    def __init__(self, state: SessionState,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state = state
        self._rows: list[_ViolationRow] = []
        self.setWindowTitle("Scan for Tag Conflicts")
        self.setModal(True)
        self.setMinimumSize(820, 480)
        self.resize(880, 620)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE
        )
        outer.setSpacing(Spacing.NORMAL)

        intro = QLabel(
            "Check every image against your conflict rules, then resolve "
            "the violations. Matching is alias-aware. Edit the rules from "
            "Tools \u2192 Edit Conflict Rules."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "secondary")
        outer.addWidget(intro)

        # Scan control row.
        scan_row = QHBoxLayout()
        self.btn_scan = QPushButton("Scan")
        self.btn_scan.setProperty("role", "primary")
        self.btn_scan.clicked.connect(self._on_scan)
        scan_row.addWidget(self.btn_scan)
        self.scan_note = QLabel("")
        self.scan_note.setProperty("role", "tertiary")
        scan_row.addWidget(self.scan_note)
        scan_row.addStretch(1)
        outer.addLayout(scan_row)

        # Mass-apply row (populated after a scan).
        self.mass_row = QHBoxLayout()
        self.mass_label = QLabel("Set all to:")
        self.mass_label.setProperty("role", "secondary")
        self.mass_row.addWidget(self.mass_label)
        self.btn_all_nothing = QPushButton("Do nothing")
        self.btn_all_nothing.clicked.connect(
            lambda: self._mass_nothing()
        )
        self.mass_row.addWidget(self.btn_all_nothing)
        self.btn_all_fix_req = QPushButton("Add all missing (requirements)")
        self.btn_all_fix_req.setToolTip(
            "For every requirement violation, select 'Add' the missing "
            "tag. Exclusion conflicts still need a per-image choice."
        )
        self.btn_all_fix_req.clicked.connect(lambda: self._mass_add_req())
        self.mass_row.addWidget(self.btn_all_fix_req)
        self.mass_row.addStretch(1)
        outer.addLayout(self.mass_row)
        self._set_mass_visible(False)

        # Results scroll list.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        # Classic up/down arrow buttons on this list's scrollbar (scoped
        # to this scroll area only). Same treatment as the audit dialog.
        self.scroll.setStyleSheet(scrollbar_with_arrows_qss())
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(
            2, Spacing.TIGHT, Spacing.NORMAL, Spacing.TIGHT
        )
        self.list_layout.setSpacing(Spacing.NORMAL)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        outer.addWidget(self.scroll, 1)

        self.status_label = QLabel(
            "Click Scan to check the dataset against your rules."
        )
        self.status_label.setProperty("role", "secondary")
        outer.addWidget(self.status_label)

        # Confirm / Cancel (Confirm left, Cancel right — matches audit).
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_confirm = QPushButton("Apply")
        self.btn_confirm.setProperty("role", "primary")
        self.btn_confirm.setEnabled(False)
        self.btn_confirm.clicked.connect(self._on_confirm)
        btn_row.addWidget(self.btn_confirm)
        self.btn_cancel = QPushButton("Close")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)
        outer.addLayout(btn_row)

    def _set_mass_visible(self, visible: bool) -> None:
        for w in (self.mass_label, self.btn_all_nothing,
                  self.btn_all_fix_req):
            w.setVisible(visible)

    def _on_scan(self) -> None:
        rules = crio.load_rules()
        enabled = [r for r in rules if r.enabled]
        if not enabled:
            QMessageBox.information(
                self, "No rules",
                "There are no enabled conflict rules. Add some from "
                "Tools \u2192 Edit Conflict Rules first.",
            )
            return
        self.btn_scan.setEnabled(False)
        self.scan_note.setText("Scanning\u2026")
        QDialog.repaint(self)
        try:
            violations = self._state.scan_conflicts(enabled)
        finally:
            self.btn_scan.setEnabled(True)
            self.scan_note.setText("")
        self._populate(violations)

    def _populate(self, violations: list) -> None:
        # Clear old rows.
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._rows = []

        for v in violations:
            row = _ViolationRow(v)
            self.list_layout.insertWidget(
                self.list_layout.count() - 1, row
            )
            self._rows.append(row)

        n = len(violations)
        n_excl = sum(
            1 for v in violations
            if v.rule.rule_type == cr.RULE_EXCLUSION
        )
        n_req = n - n_excl
        if n == 0:
            self.status_label.setText(
                "No conflicts found \u2014 every image satisfies your "
                "rules."
            )
        else:
            self.status_label.setText(
                f"{n} violation(s): {n_excl} exclusion, {n_req} "
                f"requirement. Choose an action per row, then Apply."
            )
        self.btn_confirm.setEnabled(n > 0)
        self._set_mass_visible(n > 0)
        bar = self.scroll.verticalScrollBar()
        if bar is not None:
            bar.setValue(0)

    def _mass_nothing(self) -> None:
        for row in self._rows:
            row.set_nothing()

    def _mass_add_req(self) -> None:
        for row in self._rows:
            row.set_add_missing()

    def _on_confirm(self) -> None:
        # Gather chosen actions. A single exclusion row can now
        # contribute several removals, so each row yields a list.
        actions = []  # (image_path, kind, tag)
        for row in self._rows:
            for kind, tag in row.chosen_actions():
                actions.append((row.violation.image_path, kind, tag))
        if not actions:
            QMessageBox.information(
                self, "Nothing selected",
                "No actions chosen. Select 'Remove' or 'Add' on the "
                "rows you want to resolve.",
            )
            return
        n_remove = sum(1 for _, k, _ in actions if k == ACT_REMOVE)
        n_add = sum(1 for _, k, _ in actions if k == ACT_ADD)
        confirm = QMessageBox.question(
            self, "Apply resolutions",
            f"Apply {len(actions)} change(s)?\n\n"
            f"\u2022 Remove a tag from {n_remove} image(s)\n"
            f"\u2022 Add a tag to {n_add} image(s)\n\n"
            "Each change is individually undoable via Back.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        applied = 0
        failed_files: list[str] = []
        invalid_adds: set[str] = set()
        for image_path, kind, tag in actions:
            try:
                if kind == ACT_REMOVE:
                    if self._state.remove_tag_from_image(image_path, tag):
                        applied += 1
                elif kind == ACT_ADD:
                    # A rule's required tag can be malformed if the rules
                    # file was hand-edited (the editor rejects these, but
                    # the file is user-editable JSON). add_tag_to_image
                    # would just return False — which used to drop the fix
                    # silently. Detect it and tell the user which rule tag
                    # to repair instead.
                    if not tag_io.is_valid_tag(tag):
                        invalid_adds.add(tag)
                        continue
                    if self._state.add_tag_to_image(image_path, tag):
                        applied += 1
            except OSError:
                # The per-image edit already retried a transient lock before
                # raising, so this is a persistent failure (locked or
                # read-only). Record the file by name so the user knows
                # exactly what to fix, rather than just a count.
                failed_files.append(image_path.name)
        if invalid_adds:
            shown = ", ".join(repr(t) for t in sorted(invalid_adds)[:5])
            QMessageBox.warning(
                self, "Rule tag can't be written",
                f"These required tag(s) contain commas or line breaks and "
                f"can't be added to caption files:\n\n{shown}\n\n"
                "Fix the rule in Tools \u2192 Edit Conflict Rules (one tag "
                "per box), then re-scan.",
            )
        if failed_files:
            shown = ", ".join(failed_files[:15]) + (
                " \u2026" if len(failed_files) > 15 else "")
            QMessageBox.warning(
                self, "Some changes failed",
                f"Applied {applied} change(s). {len(failed_files)} could "
                f"not be written (locked or read-only):\n\n{shown}\n\n"
                "The rest were written. Close anything using those files "
                "and re-scan to finish.",
            )
        self.accept()
