"""Tag audit dialog — cross-reference dataset tags against Danbooru (F).

Opened from Tools → Audit Tags Against Danbooru. The flow:

  1. The dialog opens EMPTY with a scope dropdown and a Scan button.
     Nothing is read until the user clicks Scan (important for very large
     datasets — the user controls when the work happens).
  2. Scan loads the bundled tag database (once per session, ~0.5s) and
     cross-references every distinct dataset tag. Tags that are valid and
     in scope are omitted; problem tags populate a scrollable list.
  3. Each problem row offers FOUR mutually-exclusive actions (radio
     buttons): Do nothing / Fix / Rename / Delete.
       - Fix is available only for tags matching a known Danbooru alias;
         it shows the canonical target ("→ ponytail") and applies it.
       - Rename enables a free-text box for an arbitrary new name.
       - Delete removes the tag from every image.
     Mass-apply controls at the top set every row to Do nothing / Delete,
     or Fix-all (only the alias-matched rows).
  4. Confirm shows a final irreversible-warning prompt, then executes.

All writes go through the existing, atomic state mutators
(rename_tag_globally / delete_tag_globally), so each is durable and the
caption files are never left half-written.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Icons, Spacing, scrollbar_with_arrows_qss
from core import tag_database as tdb
from core import audit_exceptions_io as aex
from core.state import SessionState
from ui.image_preview_popup import show_image_preview


# Per-row action constants.
ACT_NOTHING = "nothing"
ACT_FIX = "fix"
ACT_RENAME = "rename"
ACT_DELETE = "delete"


class _AuditRow(QWidget):
    """One tag's row in the audit list: label + verdict + 4 radio actions.

    The four actions live in a QButtonGroup so exactly one is selected at
    a time (the mutually-exclusive behavior the user asked for, done the
    standard way). The Rename text box enables only when Rename is picked.
    Fix is present only when the tag has an alias target.

    Actions sit in FIXED-WIDTH columns at identical positions on every
    row, so the radios line up vertically whether or not a row has a Fix
    option (a row without Fix reserves an empty slot of the same width).
    """

    # Fixed action-column widths (px). Identical on every row so the
    # columns align across the whole list.
    COL_NOTHING = 96
    COL_RENAME = 78
    COL_RENAME_BOX = 130
    COL_DELETE = 70
    COL_FIX = 56
    COL_EXCEPT = 96

    def __init__(
        self,
        result: dict,
        on_view: Optional[Callable[[str], None]] = None,
        on_except: Optional[Callable[[str], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._result = result
        self._on_view = on_view
        self._on_except = on_except
        self.tag: str = result["tag"]
        self.verdict: str = result["verdict"]
        self.suggestion: Optional[str] = result.get("suggestion")
        self.count: int = result.get("count", 0)
        self._build()

    def _build(self) -> None:
        # Card-style row: a subtle elevated background with padding and a
        # rounded border, so each tag reads as its own unit instead of a
        # cramped flat line. Two columns: identity (left) and actions
        # (right, right-aligned).
        self.setObjectName("auditCard")
        self.setStyleSheet(
            f"#auditCard {{"
            f" background-color: {Colors.BG_PANEL};"
            f" border: 1px solid {Colors.BORDER_SUBTLE};"
            f" border-radius: 6px;"
            f"}}"
        )
        outer = QHBoxLayout(self)
        outer.setContentsMargins(
            Spacing.LOOSE, Spacing.NORMAL, Spacing.LOOSE, Spacing.NORMAL
        )
        outer.setSpacing(Spacing.LOOSE)

        # -- Left: tag identity (name on top, verdict note beneath) ------
        ident = QVBoxLayout()
        ident.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setSpacing(Spacing.NORMAL)
        tag_label = QLabel(self.tag)
        tag_label.setProperty("role", "mono")
        tag_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        tag_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        name_row.addWidget(tag_label)
        count_label = QLabel(
            f"{self.count} image{'s' if self.count != 1 else ''}"
        )
        count_label.setProperty("role", "tertiary")
        name_row.addWidget(count_label)

        # Mini "view image(s)" icon. Lives in the LEFT (flexible) column,
        # before the stretch, so it never disturbs the fixed-width action
        # columns on the right that keep every row's radios aligned.
        # Clicking it asks the dialog to open the image preview for this
        # tag's images (multi-image aware).
        if self._on_view is not None:
            view_btn = QToolButton()
            view_btn.setText(Icons.SEARCH)
            view_btn.setToolTip(
                f"View the image{'s' if self.count != 1 else ''} "
                f"containing '{self.tag}'"
            )
            view_btn.setAutoRaise(True)
            view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            view_btn.setFixedSize(24, 24)
            view_btn.clicked.connect(lambda: self._on_view(self.tag))
            name_row.addWidget(view_btn)

        name_row.addStretch(1)
        ident.addLayout(name_row)
        note_label = QLabel(self._verdict_note())
        note_label.setTextFormat(Qt.TextFormat.RichText)
        ident.addWidget(note_label)
        outer.addLayout(ident, 1)

        # -- Right: action group with FIXED-WIDTH COLUMNS ----------------
        # Each action gets its own fixed-width slot at the same position
        # on every row, so the radios line up vertically across all rows
        # regardless of whether a row has a Fix option. The Fix slot is
        # always reserved (placeholder when absent), so rows with and
        # without Fix stay aligned — this was the alignment break in the
        # screenshot. Order left→right: Do nothing, Rename (+box),
        # Delete, Fix (furthest right).
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self.radio_nothing = QRadioButton("Do nothing")
        self.radio_nothing.setChecked(True)  # safe default
        self.radio_nothing.setFixedWidth(self.COL_NOTHING)
        self._group.addButton(self.radio_nothing)

        self.radio_rename = QRadioButton("Rename")
        self.radio_rename.setFixedWidth(self.COL_RENAME)
        self._group.addButton(self.radio_rename)
        self.rename_box = QLineEdit()
        self.rename_box.setPlaceholderText("new name…")
        self.rename_box.setEnabled(False)
        self.rename_box.setFixedWidth(self.COL_RENAME_BOX)

        self.radio_delete = QRadioButton("Delete")
        self.radio_delete.setFixedWidth(self.COL_DELETE)
        self._group.addButton(self.radio_delete)

        # Fix slot — fixed width on every row. Present only for alias
        # rows; otherwise an empty placeholder of the same width keeps
        # alignment. The Fix radio carries only the word "Fix"; the
        # canonical target is shown in the verdict note on the left
        # ("known variant of navel_piercing"), so the slot stays narrow
        # and consistent.
        self.radio_fix: Optional[QRadioButton] = None
        fix_slot = QWidget()
        fix_slot.setFixedWidth(self.COL_FIX)
        fix_layout = QHBoxLayout(fix_slot)
        fix_layout.setContentsMargins(0, 0, 0, 0)
        if self.verdict == tdb.VERDICT_ALIAS and self.suggestion:
            self.radio_fix = QRadioButton("Fix")
            self.radio_fix.setToolTip(
                f"Replace '{self.tag}' with the canonical Danbooru tag "
                f"'{self.suggestion}' on every image."
            )
            self.radio_fix.setStyleSheet(
                f"QRadioButton {{ color: {Colors.SUCCESS_GREEN}; "
                f"font-weight: 600; }}"
            )
            self._group.addButton(self.radio_fix)
            fix_layout.addWidget(self.radio_fix)
        fix_layout.addStretch(1)

        actions = QHBoxLayout()
        actions.setSpacing(Spacing.NORMAL)
        actions.addWidget(self.radio_nothing)
        actions.addWidget(self.radio_rename)
        actions.addWidget(self.rename_box)
        actions.addWidget(self.radio_delete)
        actions.addWidget(fix_slot)

        # "Always OK" — a separate axis from the radios. Where "Do
        # nothing" leaves the tag flagged for next time, this adds it to
        # the persistent audit exception list so it's never flagged
        # again, and drops the row from the list immediately.
        self.btn_except = QPushButton("Always OK")
        self.btn_except.setToolTip(
            f"Stop flagging '{self.tag}'. Adds it to the audit exception "
            f"list (manage under Tools \u2192 Audit exceptions). Use this "
            f"for deliberate tags like studio or OC names."
        )
        self.btn_except.setFixedWidth(self.COL_EXCEPT)
        self.btn_except.clicked.connect(self._on_except_clicked)
        actions.addWidget(self.btn_except)

        outer.addLayout(actions, 0)

        # Enable the rename box only while Rename is selected; focus it
        # so the user can type immediately.
        self.radio_rename.toggled.connect(self._on_rename_toggled)

    def _on_except_clicked(self) -> None:
        """Hand the tag to the dialog, which adds it to the exception
        list and removes this row."""
        if self._on_except is not None:
            self._on_except(self.tag)

    def _on_rename_toggled(self, checked: bool) -> None:
        self.rename_box.setEnabled(checked)
        if checked:
            self.rename_box.setFocus()

    def _verdict_note(self) -> str:
        # A small colored dot + concise label, so the verdict reads at a
        # glance without a long sentence.
        if self.verdict == tdb.VERDICT_ALIAS and self.suggestion:
            return (
                f'<span style="color:{Colors.WARNING_AMBER};">●</span> '
                f'<span style="color:{Colors.TEXT_SECONDARY};">'
                f'known variant of <b style="color:{Colors.WARNING_AMBER};">'
                f'{self.suggestion}</b></span>'
            )
        if self.verdict == tdb.VERDICT_OUT_OF_SCOPE:
            cat = self._result.get("category")
            cat_name = tdb.CATEGORY_NAMES.get(cat, "?")
            return (
                f'<span style="color:{Colors.ACCENT_BLUE};">●</span> '
                f'<span style="color:{Colors.TEXT_SECONDARY};">'
                f'valid {cat_name} tag, outside scope</span>'
            )
        if self.verdict == tdb.VERDICT_COMPOUND:
            return (
                f'<span style="color:{Colors.SUCCESS_GREEN};">●</span> '
                f'<span style="color:{Colors.TEXT_SECONDARY};">'
                f'color compound — likely valid</span>'
            )
        # unknown
        return (
            f'<span style="color:{Colors.DANGER_RED};">●</span> '
            f'<span style="color:{Colors.TEXT_SECONDARY};">'
            f'not a known Danbooru tag</span>'
        )

    # -- mass-apply hooks ---------------------------------------------

    def set_action(self, action: str) -> None:
        """Set this row's selected action (used by mass-apply). Fix is
        ignored for rows without a Fix option."""
        if action == ACT_NOTHING:
            self.radio_nothing.setChecked(True)
        elif action == ACT_DELETE:
            self.radio_delete.setChecked(True)
        elif action == ACT_FIX and self.radio_fix is not None:
            self.radio_fix.setChecked(True)
        # ACT_RENAME isn't a mass action; ignored here.

    def selected_action(self) -> str:
        if self.radio_fix is not None and self.radio_fix.isChecked():
            return ACT_FIX
        if self.radio_rename.isChecked():
            return ACT_RENAME
        if self.radio_delete.isChecked():
            return ACT_DELETE
        return ACT_NOTHING

    def rename_target(self) -> str:
        return self.rename_box.text().strip()


class TagAuditDialog(QDialog):
    """The Tools → Audit Tags dialog. See module docstring for the flow."""

    def __init__(
        self, state: SessionState, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._rows: list[_AuditRow] = []
        self.setWindowTitle("Audit Tags Against Danbooru")
        self.setModal(True)
        # Sized for the POST-SCAN layout: once rows appear, each needs
        # room for the fixed action columns (Do nothing / Rename + box /
        # Delete / Fix ≈ 462px) plus the tag-identity column. The minimum
        # guarantees Delete and Fix are never clipped on the right; the
        # default opens a touch roomier.
        self.setMinimumSize(800, 480)
        self.resize(860, 620)
        self._build()

    def _open_tag_preview(self, tag: str) -> None:
        """Open the image preview for every image containing ``tag``.

        Wired to each audit row's mini view-icon. Gathers the tag's
        images from state and opens the multi-image-aware preview popup;
        the user pages through them with Prev/Next or the arrow keys.
        """
        images = self._state.get_images_with_tag(tag)
        if not images:
            return
        show_image_preview(images, self)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL
        )
        outer.setSpacing(Spacing.NORMAL)

        # Intro — concise.
        intro = QLabel(
            "Check every tag against the Danbooru tag list, then fix, "
            "rename, or delete the ones that don't match."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "secondary")
        outer.addWidget(intro)

        # Scope + Scan row.
        scope_row = QHBoxLayout()
        scope_row.setSpacing(Spacing.NORMAL)
        scope_label = QLabel("Treat as valid:")
        scope_label.setProperty("role", "secondary")
        scope_row.addWidget(scope_label)
        self.scope_combo = QComboBox()
        # Order: default first so it's the initial selection.
        self._scope_order = [
            tdb.SCOPE_DEFAULT,
            tdb.SCOPE_GENERAL_META,
            tdb.SCOPE_EVERYTHING,
        ]
        for key in self._scope_order:
            self.scope_combo.addItem(tdb.SCOPES[key][0], key)
        self.scope_combo.setToolTip(
            "Which Danbooru tag categories count as valid. Tags that are "
            "real but in an excluded category are listed as 'out of "
            "scope' so you can decide whether to keep them."
        )
        scope_row.addWidget(self.scope_combo, 1)
        self.btn_scan = QPushButton("Scan")
        self.btn_scan.setToolTip(
            "Load the tag database (first time only) and cross-reference "
            "your dataset."
        )
        self.btn_scan.clicked.connect(self._on_scan)
        scope_row.addWidget(self.btn_scan)
        outer.addLayout(scope_row)

        # Mass-apply header (hidden until a scan produces rows).
        self.mass_row = QWidget()
        mass_layout = QHBoxLayout(self.mass_row)
        mass_layout.setContentsMargins(0, 0, 0, 0)
        mass_layout.setSpacing(Spacing.NORMAL)
        mass_label = QLabel("Set all to:")
        mass_label.setProperty("role", "secondary")
        mass_layout.addWidget(mass_label)
        self.btn_all_nothing = QPushButton("Do nothing")
        self.btn_all_nothing.clicked.connect(
            lambda: self._mass_apply(ACT_NOTHING)
        )
        mass_layout.addWidget(self.btn_all_nothing)
        self.btn_all_fix = QPushButton("Fix all matches")
        self.btn_all_fix.setToolTip(
            "Set every row that has a known canonical match to Fix. "
            "Rows without a match are unaffected."
        )
        self.btn_all_fix.clicked.connect(lambda: self._mass_apply(ACT_FIX))
        mass_layout.addWidget(self.btn_all_fix)
        self.btn_all_delete = QPushButton("Delete all")
        self.btn_all_delete.clicked.connect(
            lambda: self._mass_apply(ACT_DELETE)
        )
        mass_layout.addWidget(self.btn_all_delete)
        mass_layout.addStretch(1)
        outer.addWidget(self.mass_row)
        self.mass_row.setVisible(False)

        # Filter bar (hidden until a scan produces rows). Lets the user
        # narrow the list by verdict type and by image count, so they can
        # focus on (say) only the high-confidence alias fixes, or only
        # the genuine unknowns. Valid color compounds are HIDDEN by
        # default — they're almost always fine and would just be noise.
        self.filter_row = QWidget()
        filt = QHBoxLayout(self.filter_row)
        filt.setContentsMargins(0, 0, 0, 0)
        filt.setSpacing(Spacing.NORMAL)
        show_label = QLabel("Show:")
        show_label.setProperty("role", "secondary")
        filt.addWidget(show_label)
        self.chk_alias = QCheckBox("Known variants")
        self.chk_alias.setChecked(True)
        self.chk_alias.toggled.connect(self._apply_filters)
        filt.addWidget(self.chk_alias)
        self.chk_unknown = QCheckBox("Unknown")
        self.chk_unknown.setChecked(True)
        self.chk_unknown.toggled.connect(self._apply_filters)
        filt.addWidget(self.chk_unknown)
        self.chk_oos = QCheckBox("Out of scope")
        self.chk_oos.setChecked(True)
        self.chk_oos.toggled.connect(self._apply_filters)
        filt.addWidget(self.chk_oos)
        self.chk_compound = QCheckBox("Color compounds")
        self.chk_compound.setChecked(False)  # hidden by default
        self.chk_compound.setToolTip(
            "Tags like 'pink_toenails' — a color/shade plus a real tag. "
            "Almost always valid, so hidden by default."
        )
        self.chk_compound.toggled.connect(self._apply_filters)
        filt.addWidget(self.chk_compound)
        filt.addSpacing(Spacing.LOOSE)
        count_label = QLabel("On ≥")
        count_label.setProperty("role", "secondary")
        filt.addWidget(count_label)
        self.spin_min_count = QSpinBox()
        self.spin_min_count.setRange(1, 9999)
        self.spin_min_count.setValue(1)
        self.spin_min_count.setSuffix(" img")
        self.spin_min_count.setToolTip(
            "Only show tags appearing on at least this many images."
        )
        self.spin_min_count.valueChanged.connect(self._apply_filters)
        filt.addWidget(self.spin_min_count)
        filt.addStretch(1)
        outer.addWidget(self.filter_row)
        self.filter_row.setVisible(False)

        # Scrollable result list.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # Classic up/down arrow buttons on this list's scrollbar, so a
        # long results list is easy to nudge with the mouse. Scoped to
        # this scroll area only (see scrollbar_with_arrows_qss docstring).
        self.scroll.setStyleSheet(scrollbar_with_arrows_qss())
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(
            2, Spacing.TIGHT, Spacing.NORMAL, Spacing.TIGHT
        )
        self.list_layout.setSpacing(Spacing.NORMAL)  # gap between cards
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_container)
        outer.addWidget(self.scroll, 1)

        # Status line (shown before/after scan).
        self.status_label = QLabel(
            "Choose a scope and click Scan to begin."
        )
        self.status_label.setProperty("role", "tertiary")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        # Confirm / Cancel.
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        # Confirm on the left, Cancel on the right (per request).
        self.btn_confirm = QPushButton("Confirm")
        self.btn_confirm.setProperty("role", "primary")
        self.btn_confirm.setEnabled(False)  # until a scan produces rows
        self.btn_confirm.clicked.connect(self._on_confirm)
        btn_row.addWidget(self.btn_confirm)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)
        outer.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def _on_scan(self) -> None:
        scope_key = self.scope_combo.currentData()
        # Load indicator: the first scan triggers the one-time DB load.
        db = tdb.get_database()
        if not db.is_loaded:
            self.status_label.setText("Loading tag database…")
            self.btn_scan.setEnabled(False)
            # Force the label to paint before the blocking load.
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()
            if not db.ensure_loaded():
                self.status_label.setText(
                    db.load_error or "Could not load the tag database."
                )
                self.btn_scan.setEnabled(True)
                return
            self.btn_scan.setEnabled(True)

        results = self._state.scan_tag_audit(scope_key)
        # Drop tags the user has marked "Always OK" so the audit stops
        # flagging them (studio names, OC tags, deliberate conventions).
        excepted = aex.load_exceptions()
        if excepted:
            results = [r for r in results if r["tag"].lower() not in excepted]
        self._populate(results)

    def _clear_rows(self) -> None:
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

    def _populate(self, results: list[dict]) -> None:
        self._clear_rows()
        if not results:
            self.mass_row.setVisible(False)
            self.btn_confirm.setEnabled(False)
            self.status_label.setText(
                "No problems found — every tag is a valid match within "
                "the selected scope."
            )
            return
        # Insert rows before the trailing stretch.
        insert_at = self.list_layout.count() - 1
        for result in results:
            row = _AuditRow(
                result,
                on_view=self._open_tag_preview,
                on_except=self._on_except_tag,
            )
            self.list_layout.insertWidget(insert_at, row)
            insert_at += 1
            self._rows.append(row)
        self.mass_row.setVisible(True)
        self.filter_row.setVisible(True)
        self.btn_confirm.setEnabled(True)
        n_alias = sum(
            1 for r in results if r["verdict"] == tdb.VERDICT_ALIAS
        )
        n_unknown = sum(
            1 for r in results if r["verdict"] == tdb.VERDICT_UNKNOWN
        )
        n_oos = sum(
            1 for r in results if r["verdict"] == tdb.VERDICT_OUT_OF_SCOPE
        )
        n_compound = sum(
            1 for r in results if r["verdict"] == tdb.VERDICT_COMPOUND
        )
        self._scan_summary = (
            f"{len(results)} tag(s) to review: "
            f"{n_alias} known variant(s), {n_unknown} unknown, "
            f"{n_oos} out of scope, {n_compound} color compound(s)."
        )
        self._apply_filters()
        # Make sure the list starts at the very top — otherwise the view
        # could open scrolled partway down with the first row clipped.
        bar = self.scroll.verticalScrollBar()
        if bar is not None:
            bar.setValue(0)

    def _mass_apply(self, action: str) -> None:
        for row in self._rows:
            # Only apply to currently-visible rows, so a mass action
            # respects the filter (e.g. "Delete all" while filtered to
            # Unknown only won't touch hidden out-of-scope rows).
            if row.isVisible():
                row.set_action(action)

    def _apply_filters(self) -> None:
        """Show/hide rows per the verdict checkboxes and min-count spin."""
        allowed: set[str] = set()
        if self.chk_alias.isChecked():
            allowed.add(tdb.VERDICT_ALIAS)
        if self.chk_unknown.isChecked():
            allowed.add(tdb.VERDICT_UNKNOWN)
        if self.chk_oos.isChecked():
            allowed.add(tdb.VERDICT_OUT_OF_SCOPE)
        if self.chk_compound.isChecked():
            allowed.add(tdb.VERDICT_COMPOUND)
        min_count = self.spin_min_count.value()
        visible = 0
        for row in self._rows:
            show = (
                row.verdict in allowed
                and row.count >= min_count
            )
            row.setVisible(show)
            if show:
                visible += 1
        hidden = len(self._rows) - visible
        suffix = (
            f"  (showing {visible}, {hidden} hidden by filters)"
            if hidden else f"  (showing all {visible})"
        )
        self.status_label.setText(
            getattr(self, "_scan_summary", "") + suffix
        )

    def _on_except_tag(self, tag: str) -> None:
        """Add a tag to the persistent audit exception list and drop its
        row(s) from the list so it isn't flagged here again."""
        if not aex.add_exception(tag):
            QMessageBox.warning(
                self, "Could not save exception",
                "The exception list couldn't be written. The tag is "
                "hidden for now but may reappear next session.",
            )
        key = tag.lower()
        for row in list(self._rows):
            if row.tag.lower() == key:
                row.setParent(None)
                row.deleteLater()
                self._rows.remove(row)
        if not self._rows:
            self.mass_row.setVisible(False)
            self.filter_row.setVisible(False)
            self.btn_confirm.setEnabled(False)
            self.status_label.setText(
                "No tags left to review — the rest are matches or "
                "exceptions."
            )
        else:
            self._apply_filters()

    # ------------------------------------------------------------------
    # Confirm / execute
    # ------------------------------------------------------------------

    def _on_confirm(self) -> None:
        # Gather planned operations.
        fixes: list[tuple[str, str]] = []     # (tag, canonical)
        renames: list[tuple[str, str]] = []   # (tag, new_name)
        deletes: list[str] = []
        bad_rename: list[str] = []
        for row in self._rows:
            action = row.selected_action()
            if action == ACT_FIX and row.suggestion:
                fixes.append((row.tag, row.suggestion))
            elif action == ACT_RENAME:
                target = row.rename_target()
                if not target:
                    bad_rename.append(row.tag)
                else:
                    renames.append((row.tag, target))
            elif action == ACT_DELETE:
                deletes.append(row.tag)

        if bad_rename:
            QMessageBox.warning(
                self, "Empty rename",
                "These tags are set to Rename but have no new name:\n\n"
                + ", ".join(bad_rename)
                + "\n\nEnter a name or change their action.",
            )
            return

        total = len(fixes) + len(renames) + len(deletes)
        if total == 0:
            QMessageBox.information(
                self, "Nothing to do",
                "No tags are set to Fix, Rename, or Delete. Set an action "
                "or Cancel.",
            )
            return

        # Final irreversible warning.
        parts = []
        if fixes:
            parts.append(f"Fix {len(fixes)} tag(s) to their canonical form")
        if renames:
            parts.append(f"Rename {len(renames)} tag(s)")
        if deletes:
            parts.append(f"Delete {len(deletes)} tag(s) from all images")
        summary = ";\n".join(parts)
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Confirm tag audit changes")
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setText("About to modify caption files across your dataset:")
        confirm.setInformativeText(
            summary
            + "\n\nThis rewrites caption files on disk and CANNOT be "
            "undone from here. Proceed?"
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        self._execute(fixes, renames, deletes)

    def _execute(
        self,
        fixes: list[tuple[str, str]],
        renames: list[tuple[str, str]],
        deletes: list[str],
    ) -> None:
        """Apply all planned operations via the atomic state mutators.

        Fix and Rename are both global renames under the hood. If a fix's
        canonical target already exists in the dataset, rename_tag_globally
        merges into it (its normal behavior), which is exactly right.
        Failures (locked files) are counted and reported.
        """
        applied = 0
        failed: list[str] = []
        # Fixes and renames are renames to a target.
        for tag, target in fixes + renames:
            try:
                n = self._state.rename_tag_globally(tag, target)
                if n > 0:
                    applied += 1
                else:
                    failed.append(tag)
            except OSError:
                failed.append(tag)
        for tag in deletes:
            try:
                n = self._state.delete_tag_globally(tag)
                if n > 0:
                    applied += 1
                else:
                    failed.append(tag)
            except OSError:
                failed.append(tag)

        if failed:
            QMessageBox.warning(
                self, "Audit partially completed",
                f"Applied {applied} change(s). {len(failed)} could not be "
                "completed (the tag may have already been changed, or "
                "files were locked):\n\n" + ", ".join(failed[:20])
                + ("…" if len(failed) > 20 else ""),
            )
        else:
            QMessageBox.information(
                self, "Audit complete",
                f"Applied {applied} change(s) across your dataset.",
            )
        self.accept()
