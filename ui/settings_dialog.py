"""
ui/settings_dialog.py

Modal preferences dialog.

Three logical sections, each in a QGroupBox:
1. Autosave — enabled flag, time interval (seconds), action interval.
2. Keyboard shortcuts — each remappable via QKeySequenceEdit, with a
   per-row reset button.
3. UI defaults — sort mode, filter mode, show-orphans for fresh sessions.

Bottom row buttons:
- OK            : commit changes and close.
- Cancel        : discard changes, close.
- Apply         : commit changes, keep dialog open.
- Reset…        : restore every setting to factory default (after
                  confirm). Dialog stays open; user can re-tweak.

Validation
----------
- Shortcut conflicts: two actions cannot share the same key sequence.
  On Apply / OK, we check for duplicates and refuse to commit if any
  are found, showing a small warning with the conflicting actions.
- Shortcut parse failures: handled by Settings.set_shortcut which
  returns False — we fall back to the last valid value silently.
- Negative intervals are clamped to 0 by Settings setters (validated
  there once, applied here automatically).

Dialog lifecycle
----------------
The dialog reads from Settings on construction (snapshot of current
values into the form). User edits are held in the form widgets, not
written back to Settings until OK or Apply. Cancel = close without
writing. After Apply, the form values become the new baseline so
subsequent Cancel from the same dialog instance discards only NEW
changes made after Apply.

Pattern
-------
Unlike the other UI widgets, this dialog talks to Settings rather
than SessionState. It does not need attach/detach. The dialog is
short-lived (modal, created on demand, destroyed on close), so we
don't need listener subscription either.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config.settings import DEFAULTS, SHORTCUT_KEYS, SHORTCUT_LABELS, Settings
from config.theme import Spacing, scrollbar_with_arrows_qss


# Combobox options for the default-mode dropdowns. Tuples of
# (display label, stored string value matching core.state enum values).
SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("A → Z", "alpha_asc"),
    ("Z → A", "alpha_desc"),
)

FILTER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("All images",                "all"),
    ("Only images with tag",      "has_tag"),
    ("Only images without tag",   "missing_tag"),
)


class _NoWheel(QObject):
    """Swallows wheel events aimed at value widgets.

    FIELD REPORT: scrolling the Preferences page silently changed
    whatever combo box or spin box the pointer happened to cross. In
    a scrollable settings page the wheel means "move the page", never
    "alter this setting" — and a settings change you did not intend
    and did not see is the worst kind.

    Installed on the widgets rather than the page, so the scroll area
    still receives the event and scrolls normally.
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Wheel and not obj.hasFocus():
            event.ignore()
            return True
        return False


class SettingsDialog(QDialog):
    """Modal preferences dialog."""

    def __init__(
        self,
        settings: Settings,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings: Settings = settings
        # Per-shortcut QKeySequenceEdit widgets, indexed by setting key
        # ("shortcuts/yes" etc.) so we can iterate them for validation
        # and commit.
        self._shortcut_edits: dict[str, QKeySequenceEdit] = {}

        self.setWindowTitle("Settings")
        self.setModal(True)
        # Reasonable starting size — most settings fit without scrolling
        # on a 1080p display. A minimum WIDTH stops the dialog from
        # opening horizontally cramped: without it, the scroll area let
        # the dialog shrink narrower than the form needs, so labels and
        # controls looked squeezed until the user widened it manually
        # (the reported annoyance). Height can still shrink (the form
        # scrolls vertically).
        self.setMinimumWidth(560)
        self.resize(560, 640)

        self._build_ui()
        self._load_from_settings()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE,
        )
        outer.setSpacing(Spacing.LOOSE)

        # ScrollArea around the form content — keeps the dialog usable
        # at small window sizes (or with many shortcuts in the future).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # Never scroll horizontally — the form is a fixed-width column;
        # a horizontal scrollbar only appears when the dialog is too
        # narrow, which the minimum width now prevents anyway.
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # Classic up/down arrow buttons on the preferences scrollbar, to
        # match the audit/conflict dialogs (the global theme hides them).
        scroll.verticalScrollBar().setStyleSheet(scrollbar_with_arrows_qss())

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(Spacing.SECTION)

        inner_layout.addWidget(self._build_autosave_group())
        inner_layout.addWidget(self._build_shortcuts_group())
        inner_layout.addWidget(self._build_defaults_group())
        inner_layout.addWidget(self._build_tag_entry_group())
        inner_layout.addWidget(self._build_danbooru_group())
        inner_layout.addWidget(self._build_appearance_group())
        inner_layout.addStretch(1)

        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # Small info footer showing where the INI file lives. Useful
        # for power users who want to back up or hand-edit.
        path_label = QLabel(f"Settings file: {self._settings.file_path()}")
        path_label.setProperty("role", "tertiary")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        outer.addWidget(path_label)

        # Bottom button row: OK / Cancel + a "Reset" button.
        # Apply was removed because (1) it didn't trigger the live
        # main_window settings hook properly (changes weren't taking
        # effect until OK anyway) and (2) the OK/Cancel pair already
        # covers every use case — there's no scenario where someone
        # wants to commit changes without closing the dialog.
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._button_box.accepted.connect(self._on_ok)
        self._button_box.rejected.connect(self.reject)

        # Reset button: added separately and aligned to the left so it
        # visually separates from the standard OK/Cancel cluster.
        reset_btn = QPushButton("Reset to defaults…")
        reset_btn.clicked.connect(self._on_reset)
        self._button_box.addButton(
            reset_btn,
            QDialogButtonBox.ButtonRole.ResetRole,
        )

        outer.addWidget(self._button_box)

    def _build_autosave_group(self) -> QGroupBox:
        group = QGroupBox("Autosave")
        layout = QFormLayout(group)
        layout.setSpacing(Spacing.NORMAL)

        # "Enable autosave" checkbox with a clickable "?" help button
        # beside it. Clicking the button opens a plain-language popup
        # explaining what autosave does and what it does NOT do.
        enable_row = QHBoxLayout()
        enable_row.setSpacing(Spacing.TIGHT)
        self._autosave_enabled = QCheckBox("Enable autosave")
        self._autosave_enabled.setToolTip(
            "Autosave only fires after you've chosen a session file "
            "via File → Save Session As…"
        )
        enable_row.addWidget(self._autosave_enabled)

        help_btn = QToolButton()
        help_btn.setText("?")
        help_btn.setToolTip("What is autosave?")
        help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        help_btn.clicked.connect(self._show_autosave_help)
        enable_row.addWidget(help_btn)
        enable_row.addStretch(1)

        enable_container = QWidget()
        enable_container.setLayout(enable_row)
        layout.addRow("", enable_container)

        self._autosave_seconds = QSpinBox()
        self._autosave_seconds.setRange(0, 3600)
        self._autosave_seconds.setSuffix(" sec")
        self._autosave_seconds.setSpecialValueText("disabled")
        self._autosave_seconds.setToolTip(
            "Time-based autosave interval. 0 = disable time-based saving."
        )
        layout.addRow("Save every", self._autosave_seconds)

        self._autosave_actions = QSpinBox()
        self._autosave_actions.setRange(0, 1000)
        self._autosave_actions.setSuffix(" actions")
        self._autosave_actions.setSpecialValueText("disabled")
        self._autosave_actions.setToolTip(
            "Action-based autosave interval. 0 = disable action-based saving."
        )
        layout.addRow("Save after", self._autosave_actions)

        return group

    def _show_autosave_help(self) -> None:
        """Explain autosave in plain language via a popup."""
        msg = QMessageBox(self)
        msg.setWindowTitle("About autosave")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText("What autosave does")
        msg.setInformativeText(
            "Autosave periodically writes your <b>review progress</b> "
            "(which tags you've reviewed, your decisions, and your place "
            "in the walk) to a session file, so you can close the program "
            "and pick up later.\n\n"
            "It saves in two ways, and you can use either or both:\n"
            "  • Every N seconds (time-based)\n"
            "  • After every N actions (action-based)\n"
            "Set either interval to 0 to turn that one off.\n\n"
            "Autosave only runs after you've chosen where to save, via "
            "File → Save Session As…. Until then it stays idle.\n\n"
            "Important: autosave only affects your <b>review session</b>. "
            "Your actual caption (.txt) edits are written to disk "
            "immediately whenever you press Yes or No — they are never at "
            "risk, with or without autosave."
        )
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()

    def _build_shortcuts_group(self) -> QGroupBox:
        group = QGroupBox("Keyboard shortcuts")
        layout = QFormLayout(group)
        layout.setSpacing(Spacing.NORMAL)

        # Iterate in the order defined by SHORTCUT_LABELS (which is
        # insertion-ordered in Python 3.7+, so the dialog rows appear
        # in the same order as the labels dict).
        for key, label in SHORTCUT_LABELS.items():
            row = QHBoxLayout()
            row.setSpacing(Spacing.TIGHT)

            edit = QKeySequenceEdit()
            # Allow only one chord per shortcut. The default of 4 is
            # for multi-chord sequences like "Ctrl+K, Ctrl+S" which
            # we don't want here.
            edit.setMaximumSequenceLength(1)
            self._shortcut_edits[key] = edit
            row.addWidget(edit, 1)

            # Per-row reset button. Compact, just an icon-style label.
            reset_btn = QPushButton("↺")
            reset_btn.setToolTip(f"Reset to default ({DEFAULTS[key] or 'unbound'})")
            reset_btn.setFixedWidth(28)
            reset_btn.clicked.connect(
                lambda _checked=False, k=key: self._reset_one_shortcut(k)
            )
            row.addWidget(reset_btn)

            container = QWidget()
            container.setLayout(row)
            layout.addRow(label, container)

        # Wheel navigation is a modifier CHOICE, not a key sequence —
        # its own combo row (field request: step through the queue with
        # modifier + mouse wheel; configurable, default Shift since
        # Ctrl+wheel is image zoom).
        self._wheel_nav_combo = QComboBox()
        for text, value in (
            ("Shift + wheel", "Shift"),
            ("Ctrl + wheel", "Ctrl"),
            ("Alt + wheel", "Alt"),
            ("Disabled", "Disabled"),
        ):
            self._wheel_nav_combo.addItem(text, value)
        layout.addRow("Queue wheel navigation", self._wheel_nav_combo)

        return group

    def _build_defaults_group(self) -> QGroupBox:
        group = QGroupBox("Default UI state for new sessions")
        layout = QFormLayout(group)
        layout.setSpacing(Spacing.NORMAL)

        self._default_sort = QComboBox()
        for label, value in SORT_OPTIONS:
            self._default_sort.addItem(label, value)
        layout.addRow(self._subhead("Queue defaults", first=True))
        layout.addRow("Sort mode:", self._default_sort)

        self._default_filter = QComboBox()
        for label, value in FILTER_OPTIONS:
            self._default_filter.addItem(label, value)
        layout.addRow("Filter mode:", self._default_filter)

        self._default_show_orphans = QCheckBox(
            "Show images without .txt by default"
        )
        layout.addRow("", self._default_show_orphans)

        self._default_auto_yes = QCheckBox(
            "Auto-confirm images that already have the tag"
        )
        self._default_auto_yes.setToolTip(
            "When walking a tag, automatically mark Yes on images that "
            "already contain it, stopping only on images that are missing "
            "it. Speeds up audits where you mostly trust existing tags.\n"
            "Auto-confirmed decisions are undoable like any other."
        )
        layout.addRow("", self._default_auto_yes)

        # When a tag is fully reviewed, two choices: stop and show a
        # completion message (safer — the user always notices the
        # transition), or auto-advance to the next pending tag.
        self._default_tag_complete = QComboBox()
        self._tag_complete_options: list[tuple[str, str]] = [
            ("Stop and show completion message", "stop"),
            ("Auto-advance to next tag",          "advance"),
        ]
        for label_text, value in self._tag_complete_options:
            self._default_tag_complete.addItem(label_text, value)
        self._default_tag_complete.setToolTip(
            "What to do when every image for a tag has been reviewed.\n"
            "\"Stop\" prevents accidentally continuing to tag the next "
            "set without noticing the transition."
        )
        layout.addRow("When a tag is finished:", self._default_tag_complete)

        # Master on/off for the co-occurrence hints panel, independent of
        # the source/threshold below. Default on.
        self._default_show_cooccur = QCheckBox("Show co-occurrence hints")
        self._default_show_cooccur.setToolTip(
            "Show the \u201cOften appears with\u2026\u201d panel in the "
            "file-state view, which suggests tags that commonly accompany "
            "the tag you're auditing.\n"
            "Turn off to hide it entirely (the source and threshold below "
            "then have no effect)."
        )
        layout.addRow(self._subhead("Co-occurrence hints"))
        layout.addRow("", self._default_show_cooccur)

        # Co-occurrence hints source: bundled official Danbooru data
        # (default) or the user's own dataset.
        self._default_cooccur_source = QComboBox()
        self._default_cooccur_source.addItem("Danbooru (official data)", "danbooru")
        self._default_cooccur_source.addItem("My dataset", "dataset")
        self._default_cooccur_source.setToolTip(
            "Where the file-state co-occurrence hints come from. "
            "'Danbooru' uses the bundled official co-occurrence data "
            "(authoritative, Ochiai-ranked \u2014 recommended). 'My dataset' "
            "computes co-occurrence from your own loaded images, useful "
            "for checking the internal consistency of your own tagging."
        )
        layout.addRow("Source:", self._default_cooccur_source)

        # Co-occurrence "too common" filter percentage. Drives the
        # hint-suppression for ubiquitous tags. Default 50%. Only the
        # "My dataset" source uses it — the Danbooru source is
        # Ochiai-ranked and ignores this cutoff.
        self._default_cooccur_too_common = QSpinBox()
        self._default_cooccur_too_common.setRange(0, 100)
        self._default_cooccur_too_common.setSuffix(" %")
        self._default_cooccur_too_common.setToolTip(
            "Applies only when hints come from 'My dataset'.\n"
            "Suppresses tags that appear on more than this percentage of "
            "all your images (ubiquitous tags like '1girl' or 'sweat'). "
            "Default 50%.\n"
            "The 'Danbooru' source is Ochiai-ranked and ignores this."
        )
        layout.addRow(
            "Suppress dataset tags above:",
            self._default_cooccur_too_common,
        )

        # Image grouping: preview grid size (Feature B). Bounded choices.
        self._default_group_grid = QComboBox()
        self._default_group_grid.addItem("2 \u00d7 2  (4 images)", 2)
        self._default_group_grid.addItem("3 \u00d7 3  (9 images)", 3)
        self._default_group_grid.addItem("4 \u00d7 4  (16 images)", 4)
        self._default_group_grid.addItem("5 \u00d7 5  (25 images)", 5)
        self._default_group_grid.setToolTip(
            "How many image thumbnails to show at once in the center "
            "preview when a name-pattern group is selected. Larger "
            "grids show more but each thumbnail is smaller. Groups with "
            "more images than the grid get \u2039 \u203a paging.\n"
            "Default 3\u00d73."
        )
        layout.addRow(self._subhead("Group review"))
        layout.addRow("Grid size:", self._default_group_grid)

        self._editor_fullscreen = QCheckBox(
            "Always open Image Editor maximised")
        self._editor_fullscreen.setToolTip(
            "The image editor is a place you settle into for a whole "
            "batch, so it opens filling the screen by default. Turn "
            "this off to open it as a normal-sized window.")
        layout.addRow(self._subhead("Image editor"))
        layout.addRow("", self._editor_fullscreen)

        return group

    def _build_tag_entry_group(self) -> QGroupBox:
        """Tag entry format: underscores (default) or spaces. Controls the
        format tags are written in when typed/autocompleted. Matching and
        lookup work in both formats regardless."""
        group = QGroupBox("Tag entry")
        layout = QFormLayout(group)
        layout.setSpacing(Spacing.NORMAL)

        self._tag_entry_format = QComboBox()
        self._tag_entry_format.addItem("Underscores  (long_hair)", "underscores")
        self._tag_entry_format.addItem("Spaces  (long hair)", "spaces")
        self._tag_entry_format.setToolTip(
            "The format tags are written in when you type or autocomplete "
            "them.\n"
            "\u2022 Underscores: the Danbooru canonical form (long_hair).\n"
            "\u2022 Spaces: for space-separated datasets (long hair).\n\n"
            "Lookups, autocomplete matching, and the Tag Referencer work "
            "in BOTH formats regardless of this setting \u2014 it only "
            "changes the text that gets inserted. Emoticons like ^_^ are "
            "never converted.\n\n"
            "The Reformat Tags tool can offer to change this for you after "
            "it converts your dataset."
        )
        layout.addRow("Tag format:", self._tag_entry_format)

        note = QLabel(
            "Applies to tag entry and autocomplete. The Post Browser search "
            "box is unaffected \u2014 it uses Danbooru search syntax, where a "
            "space separates two tags."
        )
        note.setProperty("role", "tertiary")
        note.setWordWrap(True)
        layout.addRow("", note)

        # Caption tokenizer + token limit — drive the file-state "over
        # limit" badge and the "over token limit" queue filter. The count
        # is tokenizer-specific: SDXL uses CLIP, the Flux targets use their
        # own encoders (T5 / Qwen3 / Mistral), so the same caption counts
        # differently. The tokenizer selector picks the trainer family; the
        # limit selector offers only the limits valid for that family.
        from core import multi_tokenizer as mt

        self._tokenizer_target = QComboBox()
        for t in mt.all_targets():
            self._tokenizer_target.addItem(t.label, t.key)
            self._tokenizer_target.setItemData(
                self._tokenizer_target.count() - 1, t.blurb,
                Qt.ItemDataRole.ToolTipRole)
        self._tokenizer_target.setToolTip(
            "Which trainer family the caption is counted for.\n"
            "\u2022 SDXL counts CLIP content tokens (chunks of 75).\n"
            "\u2022 Flux.1 counts T5-XXL tokens; Flux.2 Klein counts Qwen3 "
            "tokens; Flux.2 Dev counts Mistral tokens \u2014 each caps at "
            "512.\n"
            "\u2022 Krea 2 and Anima share Flux.2 Klein's Qwen3 tokenizer, "
            "so choose Klein for them.\n"
            "The same caption counts differently under each tokenizer, so "
            "pick the one you train with. (Hover an option for detail.)")

        self._token_limit = QComboBox()
        self._token_limit.setToolTip(
            "The caption token limit for the chosen tokenizer.\n"
            "\u2022 A caption AT the limit still trains cleanly; strictly "
            "OVER it spills past what the trainer keeps.\n"
            "\u2022 SDXL: 225 (the default) = three full 75-token chunks "
            "(3\u00d775), the common ceiling.\n"
            "\u2022 Flux: 512 is the encoder cap (longer prompts are "
            "truncated); 256 is a tighter optional budget.\n\n"
            "Used by the file-state token badge and the \u201conly over "
            "token limit\u201d queue filter.")

        # Repopulate the limit choices whenever the tokenizer changes, and
        # keep the current selection if it's still valid for the new
        # tokenizer (else fall back to that tokenizer's default).
        self._tokenizer_target.currentIndexChanged.connect(
            self._on_tokenizer_target_changed)

        layout.addRow(self._subhead("Caption tokenizer & token limit"))
        layout.addRow("Tokenizer:", self._tokenizer_target)
        layout.addRow("Token limit:", self._token_limit)

        return group

    def _repopulate_token_limits(self, target_key: str,
                                 prefer_limit: Optional[int] = None) -> None:
        """Fill the limit combo with the limits valid for `target_key`.
        Selects `prefer_limit` if valid, else the target's default."""
        from core import multi_tokenizer as mt
        target = mt.get_target(target_key)
        self._token_limit.blockSignals(True)
        self._token_limit.clear()
        for v in target.limits:
            self._token_limit.addItem(str(v), v)
        want = (prefer_limit if (prefer_limit in target.limits)
                else target.default_limit)
        idx = self._token_limit.findData(want)
        self._token_limit.setCurrentIndex(idx if idx >= 0 else 0)
        self._token_limit.blockSignals(False)

    def _on_tokenizer_target_changed(self, _index: int) -> None:
        key = self._tokenizer_target.currentData()
        # Preserve the current limit across the switch if still valid.
        cur = self._token_limit.currentData()
        self._repopulate_token_limits(key, cur)

    def _build_danbooru_group(self) -> QGroupBox:
        """Danbooru lookups (Tag Reference): master opt-in, images
        sub-option, and the two INDEPENDENT cache clears (spec
        decisions 4/7/10)."""
        group = QGroupBox("Danbooru lookups (Tag Reference)")
        gl = QVBoxLayout(group)
        gl.addWidget(self._subhead("Lookups", first=True))
        self._dan_enabled = QCheckBox(
            "Enable Danbooru lookups (description + curated examples)")
        gl.addWidget(self._dan_enabled)
        self._dan_images = QCheckBox(
            "Show example images (hidden until hover)")
        gl.addWidget(self._dan_images)
        self._dan_reveal = QCheckBox(
            "\u2003Show them straight away instead of on hover")
        self._dan_reveal.setToolTip(
            "Skips the hover-to-reveal step, and removes the "
            "\u201cReveal all\u201d button since it would have "
            "nothing left to do. Faster to browse; less discreet.")
        gl.addWidget(self._dan_reveal)
        dan_note = QLabel(
            "Off by default \u2014 with lookups off the app makes no "
            "network requests at all. Only the tag name is ever sent; "
            "never your images, captions or paths. Example images are "
            "the wiki editors' picks and CAN include explicit "
            "content; ratings on Danbooru are user-assigned, so any "
            "filtering is best-effort.")
        dan_note.setWordWrap(True)
        gl.addWidget(dan_note)
        self._dan_enabled.toggled.connect(self._dan_images.setEnabled)
        self._dan_enabled.toggled.connect(self._sync_dan_reveal)
        self._dan_images.toggled.connect(self._sync_dan_reveal)
        from core import danbooru_api as _dapi

        gl.addWidget(self._subhead("Cached images"))
        cache_row = QHBoxLayout()
        cache_row.addWidget(QLabel("Keep downloaded images:"))
        self._img_cache_mb = QComboBox()
        for mb, label, _why in _dapi.DISK_CACHE_CHOICES:
            self._img_cache_mb.addItem(label, mb)
        self._img_cache_mb.currentIndexChanged.connect(
            self._show_cache_note)
        cache_row.addWidget(self._img_cache_mb, 1)
        gl.addLayout(cache_row)
        self._cache_note = QLabel("")
        self._cache_note.setWordWrap(True)
        gl.addWidget(self._cache_note)

        from core import export_naming

        gl.addWidget(self._subhead("Saving posts to disk"))
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("File names:"))
        self._export_naming = QComboBox()
        for key, label, example in export_naming.MODES:
            self._export_naming.addItem(label, key)
        self._export_naming.setToolTip(
            "How a post you download is named.\n\nA folder of "
            "post ids sorts by upload order, not by when you saved "
            "them, which makes finding a recent download hard. A "
            "leading date and time sorts newest-last and stays "
            "unique.\n\nThe post id is kept in every mode: it is the "
            "only part that identifies the source without doubt.")
        self._export_naming.currentIndexChanged.connect(
            self._show_naming_example)
        name_row.addWidget(self._export_naming, 1)
        gl.addLayout(name_row)
        self._naming_example = QLabel("")
        self._naming_example.setWordWrap(True)
        gl.addWidget(self._naming_example)
        self._dan_export_original = QCheckBox(
            "Always export images with original resolution")
        self._dan_export_original.setToolTip(
            "Danbooru shows a shrunk copy for speed. With this on, "
            "exports fetch the untouched upload instead \u2014 larger "
            "files, but the real thing.")
        gl.addWidget(self._dan_export_original)

        export_row = QHBoxLayout()
        export_row.addWidget(QLabel("Export folder:"))
        self._dan_export_dir = QLineEdit()
        self._dan_export_dir.setReadOnly(True)
        self._dan_export_dir.setPlaceholderText(
            "not set \u2014 choose a folder to enable exporting")
        export_row.addWidget(self._dan_export_dir, 1)
        btn_browse = QPushButton("Browse\u2026")
        btn_browse.clicked.connect(self._pick_dan_export_dir)
        export_row.addWidget(btn_browse)
        gl.addLayout(export_row)


        gl.addWidget(self._subhead("Post browser"))
        cols_row = QHBoxLayout()
        cols_row.addWidget(QLabel("Thumbnails per row:"))
        self._browser_cols = QComboBox()
        for n in (4, 5, 6, 8, 10):
            self._browser_cols.addItem(str(n), n)
        self._browser_cols.setToolTip(
            "How many thumbnails sit side by side in the post "
            "browser.\n\nThe page size stays at 20 results either "
            "way; this only changes how they are arranged. Five fits "
            "the default window without scrolling.")
        cols_row.addWidget(self._browser_cols)
        cols_row.addStretch(1)
        gl.addLayout(cols_row)

        gl.addWidget(self._subhead("Discover"))
        disc_note = QLabel(
            "The \U0001F3B2 button in the Tag "
            "Referencer picks a random tag and looks it up. Its "
            "controls live here so that button can stay a single "
            "press.")
        disc_note.setWordWrap(True)
        gl.addWidget(disc_note)
        disc_row = QHBoxLayout()
        disc_row.addWidget(QLabel("Offer:"))
        self._disc_scope = QComboBox()
        self._disc_scope.addItem("General tags only", "general")
        self._disc_scope.addItem("All tag types", "all")
        self._disc_scope.setToolTip(
            "General tags are the descriptive vocabulary you caption "
            "with. Artist, character and copyright names are not, "
            "which is why they are excluded by default.")
        disc_row.addWidget(self._disc_scope)
        disc_row.addWidget(QLabel("Minimum posts:"))
        self._disc_min = QSpinBox()
        self._disc_min.setRange(0, 5000000)
        self._disc_min.setSingleStep(100)
        self._disc_min.setToolTip(
            "A tag on fewer posts than this is never offered.\n\n"
            "This is the setting that matters: a random tag out of a "
            "hundred thousand is almost always a one-off name nobody "
            "has used. Raise it to meet only well-established "
            "vocabulary; lower it to go spelunking.")
        disc_row.addWidget(self._disc_min)
        disc_row.addStretch(1)
        gl.addLayout(disc_row)
        self._disc_norepeat = QCheckBox(
            "Do not repeat a tag within a session")
        gl.addWidget(self._disc_norepeat)
        self._disc_skip_owned = QCheckBox(
            "Skip tags already in my dataset")
        self._disc_skip_owned.setToolTip(
            "Turns browsing into gap-finding: only tags your captions "
            "have never used are offered.")
        gl.addWidget(self._disc_skip_owned)

        gl.addWidget(self._subhead("Blocked content"))
        block_note = QLabel(
            "Applies to images fetched "
            "from Danbooru only. Your own dataset is never filtered.<br>"
            "One rule per line. All terms on a line must match; "
            "\u201c-\u201d inverts a term. Delete a line to allow it "
            "again; \u201cReset to defaults\u201d restores this list.")
        block_note.setWordWrap(True)
        gl.addWidget(block_note)
        self._dan_block_custom = QPlainTextEdit()
        self._dan_block_custom.setFixedHeight(150)
        gl.addWidget(self._dan_block_custom)


        cache_row = QHBoxLayout()
        self._dan_text_size = QLabel("")
        self._btn_clear_text = QPushButton("Clear text cache")
        self._btn_clear_text.clicked.connect(self._clear_dan_text)
        self._dan_img_size = QLabel("")
        self._btn_clear_img = QPushButton("Clear image cache")
        self._btn_clear_img.clicked.connect(self._clear_dan_images)
        cache_row.addWidget(self._btn_clear_text)
        cache_row.addWidget(self._dan_text_size)
        cache_row.addSpacing(16)
        cache_row.addWidget(self._btn_clear_img)
        cache_row.addWidget(self._dan_img_size)
        cache_row.addStretch(1)
        gl.addLayout(cache_row)
        return group

    def _build_appearance_group(self) -> QGroupBox:
        """Appearance: theme (restart-required) and swirl visualization.

        Theme is read at app startup and baked into the stylesheet — so
        a theme change in this dialog persists but does NOT repaint the
        running app. The dialog notes "Restart required" next to the
        theme dropdown so users know what to expect.

        Swirl visibility and color scheme are live: they take effect
        immediately on dialog close (no restart needed).
        """
        from config.theme import THEME_NAMES
        # The tag database lives here for historical reasons — it is
        # not an appearance setting, so the group is named for what it
        # actually holds and both halves carry a heading.
        group = QGroupBox("Appearance and tag data")
        layout = QFormLayout(group)
        layout.setSpacing(Spacing.NORMAL)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # ---- Theme selector ----
        # Dropdown of available themes; restart-required notice next
        # to it. We use a horizontal row to keep label, combobox, and
        # notice together visually.
        from PySide6.QtWidgets import QHBoxLayout
        layout.addRow(self._subhead("Appearance", first=True))
        theme_row = QHBoxLayout()
        theme_row.setContentsMargins(0, 0, 0, 0)
        theme_row.setSpacing(Spacing.NORMAL)
        self._appearance_theme = QComboBox()
        for name in THEME_NAMES:
            self._appearance_theme.addItem(name, name)
        self._appearance_theme.setToolTip(
            "Switching themes takes effect on next launch."
        )
        theme_row.addWidget(self._appearance_theme)
        notice = QLabel("(Restart required)")
        notice.setProperty("role", "tertiary")
        theme_row.addWidget(notice)
        theme_row.addStretch(1)
        layout.addRow("Theme:", theme_row)

        # ---- Swirl visibility ----
        self._appearance_show_swirl = QCheckBox(
            "Show convergence visualization (swirl) on main task page"
        )
        self._appearance_show_swirl.setToolTip(
            "The 200x200 widget next to the image. Shows per-tag\n"
            "completion as a galaxy-like spiral. Takes effect\n"
            "immediately."
        )
        layout.addRow("", self._appearance_show_swirl)

        # ---- Audit tag-database snapshot (restart-required) ----
        # Which Danbooru CSV the "Audit against Danbooru" tool checks
        # against. Different SDXL base models learned different tag-era
        # vocabularies; matching the snapshot to your model avoids false
        # "wrong tag" flags. Presets come from tag_database.CSV_PRESETS
        # (only those whose file exists in resources/ are listed); a
        # custom CSV can be browsed for.
        from core import tag_database as tdb
        csv_row = QHBoxLayout()
        csv_row.setContentsMargins(0, 0, 0, 0)
        csv_row.setSpacing(Spacing.NORMAL)
        self._audit_csv = QComboBox()
        for key, label, _purpose in tdb.available_csvs():
            self._audit_csv.addItem(label, key)
        self._audit_csv.setToolTip(
            "Which Danbooru tag snapshot the audit checks against.\n"
            "Match it to your base model's era. Restart required."
        )
        self._audit_csv.currentIndexChanged.connect(
            self._on_audit_csv_changed
        )
        csv_row.addWidget(self._audit_csv, 1)
        browse_csv_btn = QPushButton("Browse\u2026")
        browse_csv_btn.setToolTip("Use a custom Danbooru tag CSV file.")
        browse_csv_btn.clicked.connect(self._on_browse_audit_csv)
        csv_row.addWidget(browse_csv_btn)
        csv_notice = QLabel("(Restart required)")
        csv_notice.setProperty("role", "tertiary")
        csv_row.addWidget(csv_notice)
        layout.addRow(self._subhead("Tag database"))
        layout.addRow("Snapshot:", csv_row)

        self._audit_csv_purpose = QLabel("")
        self._audit_csv_purpose.setProperty("role", "tertiary")
        self._audit_csv_purpose.setWordWrap(True)
        layout.addRow("", self._audit_csv_purpose)

        return group

    def _on_audit_csv_changed(self, *_args) -> None:
        """Show the purpose of the currently-selected tag-database CSV."""
        from core import tag_database as tdb
        data = self._audit_csv.itemData(self._audit_csv.currentIndex())
        purpose = ""
        if isinstance(data, str):
            preset = tdb.CSV_PRESETS.get(data)
            if preset is not None:
                purpose = preset[2]
            elif data:
                purpose = f"Custom file: {data}"
        self._audit_csv_purpose.setText(purpose)

    def _on_browse_audit_csv(self) -> None:
        """Pick a custom CSV file and add/select it in the dropdown."""
        import os
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a Danbooru tag CSV", "",
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        idx = self._audit_csv.findData(path)
        if idx < 0:
            self._audit_csv.addItem(f"Custom: {os.path.basename(path)}", path)
            idx = self._audit_csv.count() - 1
        self._audit_csv.setCurrentIndex(idx)

    @staticmethod
    def _subhead(text: str, first: bool = False) -> QLabel:
        """A heading inside a settings group.

        FIELD REPORT: the Danbooru page ran as one flat list, so a
        hover-images checkbox was followed straight by a caching
        dropdown with nothing to say the subject had changed. Long
        groups need internal headings or every setting looks like a
        continuation of the one above it.

        Spaced above but not below, so a heading sits with the
        controls it introduces rather than floating between them.
        """
        label = QLabel(f"<b>{text}</b>")
        label.setContentsMargins(0, 0 if first else 14, 0, 2)
        return label

    def _show_cache_note(self) -> None:
        """Explain the choice, and show what is on disk right now.

        A megabyte figure means little on its own; "currently 140 MB,
        3,500 images" makes the setting concrete.
        """
        from core import danbooru_api as _dapi

        chosen = self._img_cache_mb.currentData()
        why = ""
        for mb, _label, note in _dapi.DISK_CACHE_CHOICES:
            if mb == chosen:
                why = note
                break
        used = ""
        try:
            cache = _dapi.ImageCache(
                self._settings.cache_dir("danbooru_images"))
            size = cache.size_bytes()
            if size:
                count = len(list(cache.dir.glob("*.bin")))
                used = (f"<br><b>Currently using {size/1024/1024:.0f} "
                        f"MB</b> for {count:,} image(s).")
        except Exception:
            used = ""
        self._cache_note.setText(
            f"{why}{used}<br><i>Images are always kept in memory "
            "while the program is open, so browsing stays fast "
            "whatever you choose here.</i>")

    def _show_naming_example(self) -> None:
        """Show a real example: the mode names mean little on their
        own, and a filename is immediately recognisable."""
        from core import export_naming

        key = self._export_naming.currentData()
        for mode, _label, example in export_naming.MODES:
            if mode == key:
                self._naming_example.setText(
                    f"<i>example:</i> {example}")
                return
        self._naming_example.setText("")

    def _install_wheel_guard(self) -> None:
        """Every value widget on the page stops responding to the
        wheel unless it has been clicked into first."""
        self._wheel_guard = _NoWheel(self)
        targets = (self.findChildren(QComboBox)
                   + self.findChildren(QSpinBox))
        for widget in targets:
            widget.installEventFilter(self._wheel_guard)
            # Without this a focused combo still eats wheel events
            # meant for the page.
            widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _select_audit_csv(self, value: str) -> None:
        """Select the stored CSV choice (adding a custom entry if the
        value is a path not already in the list)."""
        import os
        idx = self._audit_csv.findData(value)
        if idx < 0 and value and os.path.isabs(value):
            self._audit_csv.addItem(
                f"Custom: {os.path.basename(value)}", value
            )
            idx = self._audit_csv.findData(value)
        self._audit_csv.setCurrentIndex(idx if idx >= 0 else 0)
        self._on_audit_csv_changed()

    # ------------------------------------------------------------------
    # Settings ↔ form transfer
    # ------------------------------------------------------------------

    def _load_from_settings(self) -> None:
        """Read current Settings values into the form widgets.

        Called once at construction and again after Reset.
        """
        # Autosave
        self._autosave_enabled.setChecked(self._settings.autosave_enabled)
        self._autosave_seconds.setValue(self._settings.autosave_interval_seconds)
        self._autosave_actions.setValue(self._settings.autosave_interval_actions)

        # Shortcuts
        for key, edit in self._shortcut_edits.items():
            value = self._settings.get_shortcut(key)
            edit.setKeySequence(QKeySequence.fromString(value) if value
                                else QKeySequence())
        self._select_combo_by_data(
            self._wheel_nav_combo, self._settings.wheel_nav_modifier
        )
        self._editor_fullscreen.setChecked(
            self._settings.image_editor_fullscreen)

        # UI defaults
        self._select_combo_by_data(
            self._default_sort, self._settings.default_sort_mode
        )
        self._select_combo_by_data(
            self._default_filter, self._settings.default_filter_mode
        )
        self._default_show_orphans.setChecked(self._settings.default_show_orphans)
        self._default_auto_yes.setChecked(self._settings.default_auto_yes)
        self._select_combo_by_data(
            self._default_tag_complete,
            self._settings.default_tag_complete_behavior,
        )
        self._default_cooccur_too_common.setValue(
            self._settings.default_cooccur_too_common_pct
        )
        self._select_combo_by_data(
            self._default_cooccur_source, self._settings.default_cooccur_source
        )
        self._default_show_cooccur.setChecked(
            self._settings.default_show_cooccur_hints
        )
        gi = self._default_group_grid.findData(
            self._settings.default_group_grid_cols
        )
        self._default_group_grid.setCurrentIndex(gi if gi >= 0 else 0)

        # Tag entry format
        self._select_combo_by_data(
            self._tag_entry_format, self._settings.tag_entry_format
        )
        # Caption tokenizer + token limit. Set the tokenizer first, then
        # populate the dependent limit choices and select the stored limit.
        tk = self._tokenizer_target.findData(self._settings.tokenizer_target)
        self._tokenizer_target.blockSignals(True)
        self._tokenizer_target.setCurrentIndex(tk if tk >= 0 else 0)
        self._tokenizer_target.blockSignals(False)
        self._repopulate_token_limits(
            self._tokenizer_target.currentData(), self._settings.token_limit)

        # Appearance
        # Select current theme in dropdown by matching the stored name.
        cur_theme = self._settings.theme
        for i in range(self._appearance_theme.count()):
            if self._appearance_theme.itemData(i) == cur_theme:
                self._appearance_theme.setCurrentIndex(i)
                break
        self._appearance_show_swirl.setChecked(self._settings.show_swirl)
        self._dan_enabled.setChecked(
            self._settings.danbooru_lookups_enabled)
        self._dan_images.setChecked(self._settings.danbooru_show_images)
        self._dan_images.setEnabled(self._dan_enabled.isChecked())
        self._dan_reveal.setChecked(
            self._settings.danbooru_reveal_by_default)
        self._dan_export_dir.setText(self._settings.danbooru_export_dir)
        self._dan_export_original.setChecked(
            self._settings.danbooru_export_original)
        self._dan_block_custom.setPlainText(
            self._settings.danbooru_block_custom)
        mi = self._img_cache_mb.findData(
            self._settings.danbooru_image_cache_mb)
        self._img_cache_mb.setCurrentIndex(max(0, mi))
        self._show_cache_note()
        ni = self._export_naming.findData(
            self._settings.danbooru_export_naming)
        self._export_naming.setCurrentIndex(max(0, ni))
        self._show_naming_example()
        ci = self._browser_cols.findData(
            self._settings.post_browser_columns)
        self._browser_cols.setCurrentIndex(max(0, ci))
        idx = self._disc_scope.findData(self._settings.discovery_scope)
        self._disc_scope.setCurrentIndex(max(0, idx))
        self._disc_min.setValue(self._settings.discovery_min_count)
        self._disc_norepeat.setChecked(
            self._settings.discovery_no_repeat)
        self._disc_skip_owned.setChecked(
            self._settings.discovery_skip_owned)
        self._sync_dan_reveal()
        self._refresh_dan_cache_sizes()
        self._select_audit_csv(self._settings.tag_database_choice)
        self._install_wheel_guard()

    def _commit_to_settings(self) -> bool:
        """Write form values to Settings. Returns False if validation
        rejected the commit (e.g. shortcut conflicts). The caller
        decides whether to close the dialog based on the return value.
        """
        # First: detect shortcut conflicts. Two actions cannot share
        # the same key chord, or one of them silently becomes
        # unreachable.
        seen: dict[str, list[str]] = {}
        for key, edit in self._shortcut_edits.items():
            seq_str = edit.keySequence().toString()
            if seq_str:  # empty == unbound, allowed to repeat
                seen.setdefault(seq_str, []).append(key)

        conflicts = {seq: keys for seq, keys in seen.items() if len(keys) > 1}
        if conflicts:
            lines: list[str] = []
            for seq, keys in conflicts.items():
                action_labels = ", ".join(
                    SHORTCUT_LABELS.get(k, k) for k in keys
                )
                lines.append(f"• '{seq}' is bound to: {action_labels}")
            QMessageBox.warning(
                self,
                "Shortcut conflicts",
                "Cannot save: the following shortcuts are bound to more "
                "than one action.\n\n" + "\n".join(lines),
            )
            return False

        # Autosave
        self._settings.autosave_enabled = self._autosave_enabled.isChecked()
        self._settings.autosave_interval_seconds = self._autosave_seconds.value()
        self._settings.autosave_interval_actions = self._autosave_actions.value()

        # Shortcuts. Settings.set_shortcut rejects invalid input
        # silently (returns False) — for shortcuts captured by
        # QKeySequenceEdit this should never happen in practice.
        for key, edit in self._shortcut_edits.items():
            self._settings.set_shortcut(key, edit.keySequence().toString())
        self._settings.wheel_nav_modifier = self._wheel_nav_combo.currentData()
        self._settings.image_editor_fullscreen = (
            self._editor_fullscreen.isChecked())

        # UI defaults
        sort_value = self._default_sort.itemData(self._default_sort.currentIndex())
        if isinstance(sort_value, str):
            self._settings.default_sort_mode = sort_value
        filter_value = self._default_filter.itemData(self._default_filter.currentIndex())
        if isinstance(filter_value, str):
            self._settings.default_filter_mode = filter_value
        self._settings.default_show_orphans = (
            self._default_show_orphans.isChecked()
        )
        self._settings.default_auto_yes = (
            self._default_auto_yes.isChecked()
        )
        tc_value = self._default_tag_complete.itemData(
            self._default_tag_complete.currentIndex()
        )
        if isinstance(tc_value, str):
            self._settings.default_tag_complete_behavior = tc_value
        self._settings.default_cooccur_too_common_pct = (
            self._default_cooccur_too_common.value()
        )
        src_value = self._default_cooccur_source.itemData(
            self._default_cooccur_source.currentIndex()
        )
        if isinstance(src_value, str):
            self._settings.default_cooccur_source = src_value
        self._settings.default_show_cooccur_hints = (
            self._default_show_cooccur.isChecked()
        )
        grid_value = self._default_group_grid.itemData(
            self._default_group_grid.currentIndex()
        )
        if isinstance(grid_value, int):
            self._settings.default_group_grid_cols = grid_value

        # Tag entry format
        fmt_value = self._tag_entry_format.currentData()
        if fmt_value in ("underscores", "spaces"):
            self._settings.tag_entry_format = fmt_value

        # Caption tokenizer + token limit. Set the tokenizer FIRST — its
        # setter snaps the stored limit into the new target's range — then
        # set the explicit limit chosen in the combo.
        tgt_value = self._tokenizer_target.currentData()
        if isinstance(tgt_value, str):
            self._settings.tokenizer_target = tgt_value
        tok_value = self._token_limit.currentData()
        if isinstance(tok_value, int):
            self._settings.token_limit = tok_value

        # Appearance
        theme_value = self._appearance_theme.itemData(
            self._appearance_theme.currentIndex()
        )
        if isinstance(theme_value, str):
            self._settings.theme = theme_value
        self._settings.show_swirl = self._appearance_show_swirl.isChecked()
        self._settings.danbooru_lookups_enabled = \
            self._dan_enabled.isChecked()
        self._settings.danbooru_show_images = \
            self._dan_images.isChecked()
        self._settings.danbooru_reveal_by_default = \
            self._dan_reveal.isChecked()
        self._settings.danbooru_export_dir = \
            self._dan_export_dir.text()
        self._settings.danbooru_export_original = \
            self._dan_export_original.isChecked()
        self._settings.danbooru_block_custom = \
            self._dan_block_custom.toPlainText()
        self._settings.danbooru_image_cache_mb = \
            self._img_cache_mb.currentData()
        self._settings.danbooru_export_naming = \
            self._export_naming.currentData()
        self._settings.post_browser_columns = \
            self._browser_cols.currentData()
        self._settings.discovery_scope = \
            self._disc_scope.currentData()
        self._settings.discovery_min_count = self._disc_min.value()
        self._settings.discovery_no_repeat = \
            self._disc_norepeat.isChecked()
        self._settings.discovery_skip_owned = \
            self._disc_skip_owned.isChecked()
        csv_value = self._audit_csv.itemData(self._audit_csv.currentIndex())
        if isinstance(csv_value, str) and csv_value:
            self._settings.tag_database_choice = csv_value

        # Flush to disk now so the new values survive a crash before
        # the next sync.
        self._settings.sync()
        return True

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_ok(self) -> None:
        if self._commit_to_settings():
            self.accept()
        # else: validation failed, dialog stays open

    def _on_reset(self) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("Reset settings")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText("Reset all settings to their default values?")
        msg.setInformativeText(
            "This affects autosave behavior, keyboard shortcuts, and UI "
            "defaults. Window size and last-used directory are NOT reset."
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if msg.exec() == QMessageBox.StandardButton.Yes:
            self._settings.reset_all_to_defaults()
            self._load_from_settings()

    def _reset_one_shortcut(self, key: str) -> None:
        """Reset one shortcut to its default. Affects the form widget
        only; not committed to Settings until the user clicks Apply
        or OK."""
        default = DEFAULTS.get(key, "")
        edit = self._shortcut_edits.get(key)
        if edit is None:
            return
        edit.setKeySequence(
            QKeySequence.fromString(default) if default else QKeySequence()
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _select_combo_by_data(combo: QComboBox, value: str) -> None:
        """Select the combobox entry whose UserData matches `value`."""
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    # ------------------------------------------------------------------
    # Danbooru caches (Tag Reference) — two INDEPENDENT stores, each
    # with its own clear (spec decision 7).
    # ------------------------------------------------------------------
    def _dan_caches(self):
        from core.danbooru_api import ImageCache, TextCache
        return (TextCache(self._settings.cache_dir("danbooru_text")),
                ImageCache(self._settings.cache_dir("danbooru_images")))

    def _pick_dan_export_dir(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        start = self._dan_export_dir.text() or ""
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose an export folder", start)
        if chosen:
            self._dan_export_dir.setText(chosen)

    def _sync_dan_reveal(self) -> None:
        """Reveal-by-default only means anything when images are on."""
        self._dan_reveal.setEnabled(
            self._dan_enabled.isChecked()
            and self._dan_images.isChecked())

    def _refresh_dan_cache_sizes(self) -> None:
        text, img = self._dan_caches()

        def fmt(n: int) -> str:
            if n >= 1024 * 1024:
                return f"{n / (1024 * 1024):.1f} MB"
            return f"{n / 1024:.0f} KB" if n else "empty"

        self._dan_text_size.setText(fmt(text.size_bytes()))
        self._dan_img_size.setText(fmt(img.size_bytes()))

    def _clear_dan_text(self) -> None:
        text, _ = self._dan_caches()
        text.clear()
        self._refresh_dan_cache_sizes()

    def _clear_dan_images(self) -> None:
        _, img = self._dan_caches()
        img.clear()
        self._refresh_dan_cache_sizes()
