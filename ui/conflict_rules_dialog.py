"""Conflict Rules editor — create, edit, enable/disable, and delete the
rules used by the conflict scan (features C + D).

Opened from Tools → Edit Conflict Rules. Layout and feel mirror the other
Tools dialogs: a scrollable list of rule rows, each with an enable
toggle, a readable description, and Edit / Delete controls; a "New rule"
button opens a small sub-editor for the rule's type and tags (with
autocomplete, so tags are real Danbooru tags). Rules are saved to a JSON
file next to the app settings and persist across datasets.

This dialog only edits the ruleset. Running a scan and resolving
violations lives in conflict_scan_dialog.py — the two are separate the
way "set your preferences" and "do the work" are separate.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Spacing
from core import conflict_rules as cr
from core import conflict_rules_io as crio
from core import tag_io
from ui.tag_autocomplete import TagAutocomplete


class _RuleRow(QWidget):
    """One rule in the list: enable toggle + description + Edit/Delete."""

    def __init__(self, rule: cr.ConflictRule, on_edit, on_delete,
                 on_toggle, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.rule = rule
        self._on_edit = on_edit
        self._on_delete = on_delete
        self._on_toggle = on_toggle
        self._build()

    def _build(self) -> None:
        row = QHBoxLayout(self)
        row.setContentsMargins(
            Spacing.NORMAL, Spacing.TIGHT, Spacing.NORMAL, Spacing.TIGHT
        )
        row.setSpacing(Spacing.NORMAL)

        self.chk = QCheckBox()
        self.chk.setChecked(self.rule.enabled)
        self.chk.setToolTip("Enable/disable this rule for scans.")
        self.chk.toggled.connect(self._toggled)
        row.addWidget(self.chk)

        # Description: type badge + readable rule.
        ident = QVBoxLayout()
        ident.setSpacing(1)
        title = QLabel(self.rule.display_name())
        title.setStyleSheet("font-weight: 600;")
        # Wrap the title instead of letting it grow the row. A rule with
        # many or long tags produces a very long display name; without
        # wrapping the label expanded to fit it and pushed the Edit and
        # Delete buttons off the right edge of the window. Wrapping keeps
        # the buttons in view (the row grows taller, not wider), and the
        # full name is also on hover. A minimum width lets the label
        # shrink rather than demand the whole string's width.
        title.setWordWrap(True)
        title.setMinimumWidth(1)
        title.setToolTip(self.rule.display_name())
        title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        ident.addWidget(title)
        kind = ("mutually exclusive"
                if self.rule.rule_type == cr.RULE_EXCLUSION
                else "requirement")
        sub_bits = [kind]
        if self.rule.seeded:
            sub_bits.append("built-in")
        sub = QLabel("  \u00b7  ".join(sub_bits))
        sub.setProperty("role", "tertiary")
        ident.addWidget(sub)
        row.addLayout(ident, 1)

        # The action buttons keep a fixed size and are added with no
        # stretch, so the flexible description column (stretch=1)
        # absorbs all extra width and the buttons stay put.
        btn_edit = QPushButton("Edit")
        btn_edit.setFixedWidth(64)
        btn_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_edit.clicked.connect(lambda: self._on_edit(self.rule))
        row.addWidget(btn_edit, 0)
        btn_del = QPushButton("Delete")
        btn_del.setFixedWidth(72)
        btn_del.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_del.clicked.connect(lambda: self._on_delete(self.rule))
        row.addWidget(btn_del, 0)

    def _toggled(self, checked: bool) -> None:
        self.rule.enabled = checked
        self._on_toggle()


class _RuleEditDialog(QDialog):
    """Sub-dialog to create or edit a single rule."""

    def __init__(self, rule: Optional[cr.ConflictRule] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit rule" if rule else "New rule")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._editing = rule is not None
        self._rule = rule or cr.ConflictRule(rule_type=cr.RULE_EXCLUSION)
        self._autocompletes = []  # keep refs alive
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE
        )
        outer.setSpacing(Spacing.NORMAL)

        # Rule type selector.
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Type:"))
        self._type_group = QButtonGroup(self)
        self.radio_excl = QRadioButton("Mutually exclusive")
        self.radio_excl.setToolTip(
            "At most one of these tags should appear on an image.\n"
            "e.g. indoors / outdoors"
        )
        self.radio_req = QRadioButton("Requirement")
        self.radio_req.setToolTip(
            "If the trigger tag is present, the required tag must be too.\n"
            "e.g. cat_ears \u2192 animal_ears"
        )
        self._type_group.addButton(self.radio_excl)
        self._type_group.addButton(self.radio_req)
        type_row.addWidget(self.radio_excl)
        type_row.addWidget(self.radio_req)
        type_row.addStretch(1)
        outer.addLayout(type_row)
        if self._rule.rule_type == cr.RULE_REQUIREMENT:
            self.radio_req.setChecked(True)
        else:
            self.radio_excl.setChecked(True)
        self.radio_excl.toggled.connect(self._on_type_changed)

        # Optional name.
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name (optional):"))
        self.name_box = QLineEdit(self._rule.name)
        self.name_box.setPlaceholderText("auto from tags if blank")
        name_row.addWidget(self.name_box, 1)
        outer.addLayout(name_row)

        # --- Exclusion editor: a dynamic list of tag boxes -------------
        self.excl_widget = QWidget()
        excl_v = QVBoxLayout(self.excl_widget)
        excl_v.setContentsMargins(0, 0, 0, 0)
        excl_v.setSpacing(Spacing.TIGHT)
        excl_v.addWidget(QLabel(
            "Tags that should not co-occur (one per box):"
        ))
        self.tag_rows_host = QWidget()
        self.tag_rows = QVBoxLayout(self.tag_rows_host)
        self.tag_rows.setContentsMargins(0, 0, 0, 0)
        self.tag_rows.setSpacing(Spacing.TIGHT)
        excl_v.addWidget(self.tag_rows_host)
        btn_add_tag = QPushButton("+ Add tag")
        btn_add_tag.clicked.connect(lambda: self._add_tag_box(""))
        excl_v.addWidget(btn_add_tag, alignment=Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self.excl_widget)

        # --- Requirement editor: trigger -> requires -------------------
        self.req_widget = QWidget()
        req_v = QVBoxLayout(self.req_widget)
        req_v.setContentsMargins(0, 0, 0, 0)
        req_v.setSpacing(Spacing.TIGHT)
        trig_row = QHBoxLayout()
        trig_row.addWidget(QLabel("If image has:"))
        self.trigger_box = QLineEdit()
        self._attach_ac(self.trigger_box)
        trig_row.addWidget(self.trigger_box, 1)
        req_v.addLayout(trig_row)
        need_row = QHBoxLayout()
        need_row.addWidget(QLabel("it must also have:"))
        self.requires_box = QLineEdit()
        self._attach_ac(self.requires_box)
        need_row.addWidget(self.requires_box, 1)
        req_v.addLayout(need_row)
        outer.addWidget(self.req_widget)

        # Seed existing values.
        if self._rule.rule_type == cr.RULE_REQUIREMENT:
            self.trigger_box.setText(
                self._rule.tags[0] if self._rule.tags else ""
            )
            self.requires_box.setText(self._rule.requires)
        else:
            existing = self._rule.tags or ["", ""]
            for tg in existing:
                self._add_tag_box(tg)
        # Ensure exclusion has at least two boxes.
        while self.tag_rows.count() < 2:
            self._add_tag_box("")

        self._sync_type_visibility()

        # Buttons.
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        ok = QPushButton("Save")
        ok.setProperty("role", "primary")
        ok.clicked.connect(self._on_save)
        btn_row.addWidget(ok)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        outer.addLayout(btn_row)

    def _attach_ac(self, box: QLineEdit) -> None:
        self._autocompletes.append(TagAutocomplete(box))

    def _add_tag_box(self, value: str) -> None:
        host = QWidget()
        # Fixed vertical policy so rows don't stretch to fill space when
        # a sibling is removed (that was the layout-expands-weird bug).
        host.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        box = QLineEdit(value)
        box.setPlaceholderText("tag…")
        self._attach_ac(box)
        row.addWidget(box, 1)
        remove = QPushButton("\u2715")
        remove.setFixedWidth(28)
        remove.setToolTip("Remove this tag")
        remove.clicked.connect(lambda: self._remove_tag_box(host))
        row.addWidget(remove)
        host._tag_box = box  # stash for collection
        self.tag_rows.addWidget(host)

    def _remove_tag_box(self, host: QWidget) -> None:
        # Keep a minimum of two boxes for an exclusion set.
        if self.tag_rows.count() <= 2:
            return
        self.tag_rows.removeWidget(host)
        host.setParent(None)
        host.deleteLater()
        # Re-fit immediately so the surrounding labels/layout don't end up
        # stretched while the deleted widget waits to be reaped.
        self.tag_rows_host.adjustSize()
        self.adjustSize()

    def _on_type_changed(self, _checked: bool) -> None:
        self._sync_type_visibility()

    def _sync_type_visibility(self) -> None:
        excl = self.radio_excl.isChecked()
        self.excl_widget.setVisible(excl)
        self.req_widget.setVisible(not excl)
        self.adjustSize()

    def _collect_excl_tags(self) -> list[str]:
        tags = []
        for i in range(self.tag_rows.count()):
            host = self.tag_rows.itemAt(i).widget()
            if host is not None and hasattr(host, "_tag_box"):
                t = host._tag_box.text().strip()
                if t:
                    tags.append(t)
        return tags

    def _on_save(self) -> None:
        if self.radio_excl.isChecked():
            tags = self._collect_excl_tags()
            # Reject tags with format-breaking characters (comma, line
            # break) up front. A comma here usually means the user tried
            # to put several tags in one box — the rule would save but
            # never match anything, a confusing inert rule.
            bad = [t for t in tags if not tag_io.is_valid_tag(t)]
            if bad:
                QMessageBox.information(
                    self, "Invalid tag",
                    "Tags can't contain commas or line breaks — enter "
                    "one tag per box.\n\nProblem entries:\n  "
                    + "\n  ".join(bad[:6]),
                )
                return
            # de-dupe preserving order
            seen = set(); uniq = []
            for t in tags:
                if t.lower() not in seen:
                    seen.add(t.lower()); uniq.append(t)
            if len(uniq) < 2:
                QMessageBox.information(
                    self, "Need two tags",
                    "An exclusion rule needs at least two distinct tags.",
                )
                return
            self._rule.rule_type = cr.RULE_EXCLUSION
            self._rule.tags = uniq
            self._rule.requires = ""
        else:
            trig = self.trigger_box.text().strip()
            req = self.requires_box.text().strip()
            if not trig or not req:
                QMessageBox.information(
                    self, "Need both tags",
                    "A requirement rule needs a trigger tag and a "
                    "required tag.",
                )
                return
            # The required tag gets WRITTEN into caption files by the
            # conflict scan's "Add missing" fix, so a comma here would
            # make every fix silently impossible. Reject at the source.
            bad = [t for t in (trig, req) if not tag_io.is_valid_tag(t)]
            if bad:
                QMessageBox.information(
                    self, "Invalid tag",
                    "Tags can't contain commas or line breaks — enter "
                    "exactly one tag per box.\n\nProblem entries:\n  "
                    + "\n  ".join(bad),
                )
                return
            if trig.lower() == req.lower():
                QMessageBox.information(
                    self, "Same tag",
                    "The trigger and required tag must differ.",
                )
                return
            self._rule.rule_type = cr.RULE_REQUIREMENT
            self._rule.tags = [trig]
            self._rule.requires = req
        self._rule.name = self.name_box.text().strip()
        # A user-edited seed is no longer "built-in".
        if self._editing and self._rule.seeded:
            self._rule.seeded = False
        self.accept()

    def result_rule(self) -> cr.ConflictRule:
        return self._rule


class ConflictRulesDialog(QDialog):
    """The main rules-management dialog."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Conflict Rules")
        self.setModal(True)
        self.setMinimumSize(560, 460)
        self.resize(620, 560)
        self._rules = crio.load_rules()
        self._build()
        self._populate()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL
        )
        outer.setSpacing(Spacing.NORMAL)

        intro = QLabel(
            "Rules flag tag combinations that shouldn't occur together "
            "(or a tag that requires another). Built-in rules cover only "
            "certain logical contradictions \u2014 add your own for the "
            "rest. Changes save automatically."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "secondary")
        outer.addWidget(intro)

        top = QHBoxLayout()
        btn_new = QPushButton("+ New rule")
        btn_new.setProperty("role", "primary")
        btn_new.clicked.connect(self._on_new)
        top.addWidget(btn_new)
        top.addStretch(1)
        btn_restore = QPushButton("Restore built-in rules")
        btn_restore.setToolTip(
            "Re-add the built-in starter rules (won't duplicate ones "
            "already present)."
        )
        btn_restore.clicked.connect(self._on_restore_seeds)
        top.addWidget(btn_restore)
        outer.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(2, 2, Spacing.NORMAL, 2)
        self.list_layout.setSpacing(2)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        outer.addWidget(self.scroll, 1)

        self.empty_label = QLabel(
            "No rules yet. Click \u201c+ New rule\u201d to add one."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setProperty("role", "tertiary")
        outer.addWidget(self.empty_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close = QPushButton("Done")
        close.setProperty("role", "primary")
        close.clicked.connect(self.accept)
        btn_row.addWidget(close)
        outer.addLayout(btn_row)

    def _populate(self) -> None:
        # Clear existing rows (keep the trailing stretch).
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for i, rule in enumerate(self._rules):
            row = _RuleRow(
                rule, self._on_edit, self._on_delete, self._save
            )
            self.list_layout.insertWidget(i, row)
            if i < len(self._rules) - 1:
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setStyleSheet(f"color: {Colors.BORDER_SUBTLE};")
        self.empty_label.setVisible(not self._rules)
        self.scroll.setVisible(bool(self._rules))

    def _on_new(self) -> None:
        dlg = _RuleEditDialog(None, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._rules.append(dlg.result_rule())
            self._save()
            self._populate()

    def _on_edit(self, rule: cr.ConflictRule) -> None:
        dlg = _RuleEditDialog(rule, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._save()
            self._populate()

    def _on_delete(self, rule: cr.ConflictRule) -> None:
        confirm = QMessageBox.question(
            self, "Delete rule",
            f"Delete the rule \u201c{rule.display_name()}\u201d?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._rules = [r for r in self._rules if r is not rule]
        self._save()
        self._populate()

    def _on_restore_seeds(self) -> None:
        # Add any seed rule whose (type, tags, requires) isn't already
        # present, so restoring never duplicates.
        def sig(r):
            return (r.rule_type, tuple(t.lower() for t in r.tags),
                    r.requires.lower())
        have = {sig(r) for r in self._rules}
        added = 0
        for seed in cr.default_seed_rules():
            if sig(seed) not in have:
                self._rules.append(seed)
                added += 1
        if added:
            self._save()
            self._populate()
        QMessageBox.information(
            self, "Built-in rules",
            f"Added {added} built-in rule(s)." if added
            else "All built-in rules are already present.",
        )

    def _save(self) -> None:
        ok = crio.save_rules(self._rules)
        if not ok:
            QMessageBox.warning(
                self, "Could not save rules",
                "The conflict rules file couldn't be written. Your "
                "changes are kept for this session but may not persist.",
            )

    def rules(self) -> list:
        return list(self._rules)
