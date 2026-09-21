"""
ui/tag_tree.py

Left sidebar: tags grouped under their (first-occurring) subfolder.

Each tag appears EXACTLY ONCE in the tree, under the alphabetically-
first subfolder that contains it. This matches state.tree_order, the
same iteration order used by the auto-advance walk logic — the tree
visually represents that order.

Folder headers appear for every subfolder in the dataset (even folders
whose tags all first-occurred in earlier folders), so the user's
directory structure stays visible regardless of tag distribution.

Tree shape (example):
    📁 train       180 imgs
       ✓ 1girl     142
       ⏳ blue_hair 88
       ↷ outdoors  45  (skipped)
    📁 val         67 imgs
       ⏳ unique_to_val_tag  12

Tags in `val` that ALSO exist in `train` (like `1girl`) are NOT shown
under `val` — they live under `train`. Clicking the `train > 1girl`
row walks every `1girl` image across the entire dataset, not just
train's images.

The count column shows the GLOBAL number of images containing the tag
(not per-folder), reflecting the global nature of the audit job.

Right-click on a tag row brings up a context menu with two actions:
  - "Mark tag complete" — manual completion override (undoable)
  - "Delete tag globally..." — destructive, single-confirm

Update strategy
---------------
- tag_selected   : refresh selection highlight (no rebuild)
- tag_changed    : update icon for one tag's single row
- image_changed  : refresh that tag's global count (cheap)
- tree_rebuilt   : full rebuild (tag added/removed globally)
- filter_changed : ignored (filter affects queue, not tag inventory)

Performance
-----------
For a 200-tag dataset, the tree has ~200 + N_folders rows. QTreeWidget
handles this trivially. Per-event updates touch 1-N rows at most.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.accent_bar import AccentBarDelegate, CURRENT_ROLE
from config.theme import Colors, Fonts, Icons, Spacing, scrollbar_with_arrows_qss
from core.state import SessionState, StateChange, TagSortMode, TagStatus, _fold_for_match


# Custom item-data roles.
ROLE_KIND = Qt.ItemDataRole.UserRole       # "folder" or "tag"
ROLE_TAG_NAME = Qt.ItemDataRole.UserRole + 1
ROLE_SUBFOLDER = Qt.ItemDataRole.UserRole + 2


class _FolderProgressWidget(QWidget):
    """Folder-header row in the tag tree, with completion progress.

    Layout (three rows so nothing ever clips, regardless of column width):
        +--------------------------------------------+
        |  📁 folder_name                            |  <- row 1
        |  12/45 tags · 1000 images                  |  <- row 2
        |  [============         ]   27%             |  <- row 3
        +--------------------------------------------+

    Earlier single-line layout clipped the stats text when the tag-tree
    column was narrow. Splitting into rows fixes that and also lets each
    piece of information be read on its own — the user can glance at
    just the count, or just the bar, without parsing one long line.

    A folder with zero tag jobs (e.g. just images sharing tags whose
    first-occurrence folder is elsewhere) hides the bar row and the
    percent text, leaving only the name + image count visible.
    """

    BAR_HEIGHT = 6  # px — slightly thicker now that the bar has its own row

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 4, 4)
        layout.setSpacing(1)

        # Row 1: folder name (bold, slightly larger).
        self._name_label = QLabel()
        name_font = QFont()
        name_font.setPointSize(Fonts.SIZE_NORMAL)
        name_font.setBold(True)
        self._name_label.setFont(name_font)
        self._name_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
        self._name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction
        )
        # Don't truncate; allow Qt to clip to its bounds rather than
        # eliding the whole label.
        self._name_label.setWordWrap(False)
        layout.addWidget(self._name_label)

        # Row 2: counts ("12/45 tags · 1000 images"). Tertiary color, so
        # the name stays the dominant element.
        self._counts_label = QLabel()
        counts_font = QFont()
        counts_font.setPointSize(Fonts.SIZE_NORMAL)
        self._counts_label.setFont(counts_font)
        self._counts_label.setStyleSheet(f"color: {Colors.TEXT_TERTIARY};")
        layout.addWidget(self._counts_label)

        # Row 3: bar + percent. The bar stretches; the percent label sits
        # at the right and stays at its natural width.
        bar_row = QHBoxLayout()
        bar_row.setContentsMargins(0, 0, 0, 0)
        bar_row.setSpacing(6)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(self.BAR_HEIGHT)
        self._bar.setStyleSheet(
            f"QProgressBar {{ "
            f"  background-color: {Colors.BG_DEEPEST}; "
            f"  border: none; "
            f"  border-radius: 3px; "
            f"}} "
            f"QProgressBar::chunk {{ "
            f"  background-color: {Colors.SUCCESS_GREEN}; "
            f"  border-radius: 3px; "
            f"}}"
        )
        bar_row.addWidget(self._bar, 1)  # stretches

        self._percent_label = QLabel()
        pct_font = QFont()
        pct_font.setPointSize(Fonts.SIZE_NORMAL)
        pct_font.setBold(True)
        self._percent_label.setFont(pct_font)
        self._percent_label.setStyleSheet(f"color: {Colors.SUCCESS_GREEN};")
        self._percent_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # Reserve a small minimum so the bar doesn't jitter as the
        # percentage gains digits ("0%" -> "100%").
        self._percent_label.setMinimumWidth(40)
        bar_row.addWidget(self._percent_label)
        layout.addLayout(bar_row)

    def update_progress(
        self,
        folder_name: str,
        image_count: int,
        completed: int,
        total: int,
    ) -> None:
        """Refresh the displayed values.

        completed/total are tag counts (jobs done / total jobs in this
        folder). image_count is shown alongside. With total == 0, the
        bar + percent are hidden because percentage is undefined.
        """
        display_folder = folder_name if folder_name else "(root)"
        self._name_label.setText(f"{Icons.FOLDER}  {display_folder}")

        if total > 0:
            pct = int(round(100 * completed / total))
            self._bar.setValue(pct)
            self._bar.setVisible(True)
            self._percent_label.setVisible(True)
            self._percent_label.setText(f"{pct}%")
            self._counts_label.setText(
                f"{completed}/{total} tags  \u00b7  {image_count} images"
            )
        else:
            # No tag jobs in this folder. Hide the bar + percent; just
            # show the image count beneath the name.
            self._bar.setVisible(False)
            self._percent_label.setVisible(False)
            self._counts_label.setText(f"{image_count} images")


class _TagTreeWidget(QTreeWidget):
    """QTreeWidget with type-ahead that matches TAG NAMES, not the
    decorated row text. Field request: type-to-jump belongs on the tag
    list (every letter is free here), but Qt's native search compares
    the rendered text — which carries status glyphs/counts — so it
    never matched. The owner supplies a resolver mapping the typed
    prefix to the right item in visual order."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._resolver = None

    def set_typeahead_resolver(self, resolver) -> None:
        self._resolver = resolver

    def keyboardSearch(self, search: str) -> None:  # noqa: N802
        if self._resolver is None or not search:
            return
        try:
            item = self._resolver(search)
        except Exception:
            item = None
        if item is not None:
            # Normal selection semantics: jumping to a tag selects it
            # (and thus starts its walk), same as clicking it.
            self.setCurrentItem(item)
            self.scrollToItem(item)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Swallow Left / Right. By default a QTreeWidget collapses /
        # expands the current item on these keys, which — because the
        # user's fingers rest near the arrow keys while navigating — was
        # accidentally folding the folder header (there is only the one
        # root group, since subfolders aren't loaded, so folding it just
        # hid the whole list). Folding serves no purpose here, so Left /
        # Right do nothing. Up / Down still move the selection, and the
        # image queue owns the real image navigation.
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            event.ignore()
            return
        super().keyPressEvent(event)


class TagTree(QFrame):
    """Left sidebar showing the tag inventory grouped by subfolder."""

    # Right-click → Tag Reference (handled by the main window,
    # which owns the reference window).
    reference_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "panel")

        self._state: Optional[SessionState] = None

        # tag_name -> QTreeWidgetItem. Each tag has exactly ONE row.
        # Vocabulary-bucket cache (see _tag_bucket).
        self._tag_class_cache: dict[str, str] = {}
        self._tag_db_ready = False
        self._tag_db_failed = False
        self._tag_items: dict[str, QTreeWidgetItem] = {}
        # subfolder -> QTreeWidgetItem (folder header)
        self._folder_items: dict[str, QTreeWidgetItem] = {}

        # Guard so programmatic checkbox changes (entering/leaving
        # multi-select, restoring the selection after a rebuild) don't
        # bounce back through _on_item_changed into set_selected_tags.
        self._suspend_check_sync: bool = False

        self._build_ui()
        self._refresh_enabled()

    # ------------------------------------------------------------------
    # Public API: state binding
    # ------------------------------------------------------------------

    def attach(self, state: Optional[SessionState]) -> None:
        if state is self._state:
            return
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        self._state = state
        # A freshly-attached state starts in single-tag mode; reset the
        # toggle so the button matches (it would otherwise keep its old
        # checked state from a previous dataset). Block its signal so we
        # don't drive set_multi_select_mode during attach.
        self.multi_select_btn.blockSignals(True)
        try:
            self.multi_select_btn.setChecked(
                state.multi_select_mode if state is not None else False
            )
        finally:
            self.multi_select_btn.blockSignals(False)
        if state is not None:
            state.add_listener(self._on_state_change)
            # Sync the sort dropdown to the state's current tag sort
            # mode without firing _on_sort_changed (which would re-emit
            # and could loop). blockSignals guards the combo.
            self.sort_combo.blockSignals(True)
            try:
                for i in range(self.sort_combo.count()):
                    if self.sort_combo.itemData(i) == state.tag_sort_mode:
                        self.sort_combo.setCurrentIndex(i)
                        break
            finally:
                self.sort_combo.blockSignals(False)
            self._rebuild_tree()
        else:
            self._clear_tree()
        self._refresh_enabled()

    def detach(self) -> None:
        self.attach(None)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL,
        )
        layout.setSpacing(Spacing.NORMAL)

        header = QLabel("Tags")
        header.setProperty("role", "heading")
        layout.addWidget(header)

        # Live search box — filters visible tags as the user types.
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search tags…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_box)

        # Category filter dropdown (#8/#9). Mutually-exclusive views that
        # combine (AND) with the text search:
        #   - All tags
        #   - Completed only / Uncompleted only (by tag status)
        #   - Exact count: tags on exactly 1, 2, or 3 images (spotting
        #     rare one-offs on large datasets)
        cat_row = QHBoxLayout()
        cat_row.setSpacing(Spacing.TIGHT)
        cat_label = QLabel("Show:")
        cat_label.setProperty("role", "secondary")
        cat_row.addWidget(cat_label)
        self.category_filter = QComboBox()
        self.category_filter.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # (label, filter-key) — key drives _tag_passes_category_filter.
        self._category_options: list[tuple[str, str]] = [
            ("All tags", "all"),
            ("Completed", "completed"),
            ("Uncompleted", "uncompleted"),
            ("Skipped tags", "skipped"),
            ("On exactly 1 image", "eq1"),
            ("On exactly 2 images", "eq2"),
            ("On exactly 3 images", "eq3"),
            # Danbooru-vocabulary views (field request: inspect uniquely
            # made tokens). Classification reuses the audit's grammar,
            # so this filter and the audit can never disagree:
            #   custom      = not in the 201k DB at all (your inventions
            #                 — and typos, which is a feature)
            #   color_combo = <color/shade>_<registered tag> compounds
            #                 not individually registered
            # Canonical tags AND known aliases both count as "known";
            # spelling variants fold first ("long hair" == long_hair).
            ("Custom tags (not in Danbooru)", "custom_tags"),
            ("Color + known combos", "color_combos"),
        ]
        for label_text, key in self._category_options:
            self.category_filter.addItem(label_text, key)
        self.category_filter.currentIndexChanged.connect(
            self._on_category_filter_changed
        )
        cat_row.addWidget(self.category_filter, 1)
        layout.addLayout(cat_row)

        # Sort dropdown — controls tag ordering within each folder.
        sort_row = QHBoxLayout()
        sort_row.setSpacing(Spacing.TIGHT)
        sort_label = QLabel("Sort:")
        sort_label.setProperty("role", "secondary")
        sort_row.addWidget(sort_label)
        self.sort_combo = QComboBox()
        # The sort dropdown should NOT be the default target for arrow
        # keys. Previously, with nothing clicked on the page, focus sat
        # on this combo, so pressing Up/Down cycled the sort order
        # instead of moving through the tag list — surprising right
        # after finishing an image queue. ClickFocus means it only takes
        # focus (and thus only eats arrow keys) when explicitly clicked,
        # never via tab-cycling or as the page default. Arrow keys then
        # fall through to the tag tree, which is what the user expects.
        self.sort_combo.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # (display label, TagSortMode) — order defines dropdown order.
        self._sort_options: list[tuple[str, TagSortMode]] = [
            ("A \u2192 Z", TagSortMode.ALPHA_ASC),
            ("Z \u2192 A", TagSortMode.ALPHA_DESC),
            ("Most images", TagSortMode.COUNT_DESC),
            ("Fewest images", TagSortMode.COUNT_ASC),
        ]
        for label_text, mode in self._sort_options:
            self.sort_combo.addItem(label_text, mode)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        sort_row.addWidget(self.sort_combo, 1)
        layout.addLayout(sort_row)

        # Multi-select toggle. When on, tag rows grow checkboxes: tick
        # several tags and the queue shows the images that match the SET
        # (paired with the queue's filter dropdown — "only without tag"
        # finds images missing every ticked tag). The single-tag walk is
        # paused while this is on. Styled like the queue-panel toggles
        # (accent fill when engaged) for a consistent on/off read.
        self.multi_select_btn = QToolButton()
        self.multi_select_btn.setCheckable(True)
        self.multi_select_btn.setText("\u2611  Multi-select tags")
        self.multi_select_btn.setToolTip(
            "Tick several tags at once and browse the images that match "
            "the whole set.\n"
            "Pair with the queue's \"only without tag\" filter to find "
            "images MISSING every ticked tag — e.g. tick all hair-length "
            "tags to find images with no hair length set.\n"
            "While on, the normal one-tag-at-a-time walk is paused."
        )
        self.multi_select_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.multi_select_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self.multi_select_btn.setStyleSheet(
            f"QToolButton {{ border: 1px solid {Colors.BORDER_SUBTLE}; "
            f"border-radius: 3px; padding: 3px 6px; }}"
            f"QToolButton:checked {{ background-color: {Colors.ACCENT_BLUE}; "
            f"border: 1px solid {Colors.ACCENT_BLUE}; color: white; }}"
        )
        self.multi_select_btn.toggled.connect(self._on_multi_select_toggled)
        layout.addWidget(self.multi_select_btn)

        # Two-column tree: column 0 = name (icon + label), column 1 = count.
        self.tree = _TagTreeWidget()
        self.tree.set_typeahead_resolver(self._resolve_typeahead)
        # Classic up/down arrow buttons on the tree's scrollbar (global
        # theme hides them). Applied to the scrollbar object, not the
        # tree, so per-row tag-status colors and checkboxes are untouched.
        self.tree.verticalScrollBar().setStyleSheet(
            scrollbar_with_arrows_qss()
        )
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        h = self.tree.header()
        # Name column has priority: it takes the width its content needs
        # and is the LAST to elide. The count column is the flexible one
        # that absorbs leftover width (and yields/clips first when the
        # panel is narrowed) — so shrinking the sidebar squeezes the
        # count, not the tag name. Neither section is user-draggable.
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setAnimated(True)
        # Folder headers are NOT collapsible. There is only ever the one
        # root group (subfolders aren't loaded), so folding it would just
        # hide the whole tag list for no benefit. Turning off the expand
        # indicator removes the little fold arrow entirely, and root
        # decoration is dropped so no empty indicator column is drawn.
        # Headers are force-expanded on every build below.
        self.tree.setItemsExpandable(False)
        self.tree.setRootIsDecorated(False)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.itemClicked.connect(self._on_item_clicked)
        # Also select a tag when the current row changes via the keyboard
        # (Up/Down/Page/Home/End). Without this, arrow keys moved only
        # the tree's highlight, not the active tag — so navigating to the
        # "next tag" with the keyboard didn't actually start walking it.
        # Programmatic scroll-to-current uses scrollToItem (not
        # setCurrentItem), so this only fires on genuine user navigation;
        # we still guard with _updating for safety.
        self.tree.currentItemChanged.connect(self._on_current_item_changed)
        # Same left-edge bar as the queue, so "the program is here"
        # looks identical on both sides of the window.
        self._accent_delegate = AccentBarDelegate(self.tree)
        self.tree.setItemDelegateForColumn(0, self._accent_delegate)

        # Checkbox toggles in multi-select mode arrive as itemChanged.
        # The handler is guarded (mode + _suspend_check_sync) so the
        # style refreshes that also fire this signal are ignored.
        self.tree.itemChanged.connect(self._on_item_changed)

        # Right-click context menu for tag-level actions.
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)

        layout.addWidget(self.tree, 1)

        # Below the tree: a concise "jump to first pending tag" icon
        # button at the right, matching the queue list's jump-to-pending
        # glyph (#12).
        tag_btn_row = QHBoxLayout()
        tag_btn_row.setContentsMargins(0, 0, 0, 0)
        tag_btn_row.setSpacing(Spacing.TIGHT)
        self.btn_jump_pending_tag = QToolButton()
        self.btn_jump_pending_tag.setText("\u23ED")  # same glyph as queue
        self.btn_jump_pending_tag.setToolTip(
            "Jump to the first pending tag (from the top of the list) — "
            "a tag that still has undecided images.\nSkips completed and "
            "skipped tags."
        )
        self.btn_jump_pending_tag.setFixedWidth(28)
        self.btn_jump_pending_tag.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_jump_pending_tag.clicked.connect(
            self._on_jump_next_pending_tag
        )
        tag_btn_row.addStretch(1)
        tag_btn_row.addWidget(self.btn_jump_pending_tag)
        layout.addLayout(tag_btn_row)

        # Discoverability hint: tells users the right-click menu exists.
        # Muted color so it doesn't compete with the tag list.
        hint = QLabel("Right-click a tag for more options")
        hint.setProperty("role", "tertiary")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

    # ------------------------------------------------------------------
    # State change listener
    # ------------------------------------------------------------------

    def _on_state_change(self, change: StateChange) -> None:
        kind = change.kind
        if kind == "tree_rebuilt":
            self._rebuild_tree()
        elif kind == "tag_selected":
            self._refresh_selection_highlight()
        elif kind == "filter_changed":
            # filter_changed fires when the active view changes without a
            # tag click: entering/leaving browse mode, or a tag walk
            # ending. In those, current_tag may have gone to None, so the
            # blue tag highlight must clear — otherwise the previously
            # selected tag stays highlighted in browse mode even though no
            # tag is active. Re-styling every row against the (possibly
            # None) current_tag clears it. Skipped in multi-select mode,
            # which manages its own row styling and whose frequent
            # filter_changed events must not trigger a full restyle (it
            # can disturb the checkbox sync). Multi-select already keeps
            # current_tag None, so there is no stale tag highlight to fix
            # there anyway.
            if self._state is not None and not self._state.multi_select_mode:
                self._refresh_selection_highlight()
        elif kind == "tag_changed":
            if change.tag:
                self._refresh_tag_row(change.tag)
                # The tag's status (counts toward folder completion) may
                # have shifted. Update the folder it lives in. Cheap:
                # only the one folder, not the whole tree.
                folder = self._folder_for_tag(change.tag)
                if folder is not None:
                    self._refresh_folder_progress(folder)
                # If a status-based category filter is active (Completed /
                # Uncompleted), this tag may now belong on the other side
                # of the filter — re-apply so it shows/hides correctly.
                key = self.category_filter.currentData()
                if key in ("completed", "uncompleted"):
                    self._apply_search_filter()
        elif kind == "image_changed":
            # An image's tags may have shifted. Refresh the count for
            # whatever tag's row currently exists; cheap because we
            # iterate only tags affected by this image.
            if change.image_path is not None:
                self._refresh_counts_for_image(change.image_path)

    # ------------------------------------------------------------------
    # Building and clearing
    # ------------------------------------------------------------------

    def _clear_tree(self) -> None:
        self.tree.clear()
        self._tag_items.clear()
        self._folder_items.clear()

    def _rebuild_tree(self) -> None:
        """Full rebuild of folder headers and tag rows.

        Uses state.tree_order to place each tag exactly once under its
        first-occurring folder. All folders are shown as headers, even
        empty ones, to preserve directory-structure visibility.
        """
        if self._state is None:
            self._clear_tree()
            return

        # Preserve expansion state across rebuilds so the user's
        # collapse choices don't get clobbered.
        expanded_folders: set[str] = set()
        for sf, item in self._folder_items.items():
            if item.isExpanded():
                expanded_folders.add(sf)

        # Preserve selection + scroll too (field report: creating a new
        # tag rebuilt the tree and dumped the view to the bottom, losing
        # the user's place). Capture BEFORE the clear invalidates items.
        # The walk's current tag is the truth — the tree's currentItem
        # can be STALE (highlight syncs don't always route through it),
        # so it is only a fallback for tag-less states (e.g. multi-
        # select mode, where no single walk tag exists).
        prev_tag: Optional[str] = (
            self._state.current_tag if self._state is not None else None
        )
        if prev_tag is None:
            cur_item = self.tree.currentItem()
            if cur_item is not None:
                for t, it in self._tag_items.items():
                    if it is cur_item:
                        prev_tag = t
                        break
        prev_scroll = self.tree.verticalScrollBar().value()
        # If the dict was empty (first build), default everything to
        # expanded — the user can collapse later if they want.
        first_build = not expanded_folders and not self._folder_items

        self.tree.blockSignals(True)
        try:
            self._clear_tree()

            # Group state.tree_order by folder for fast lookup during
            # the per-folder-header iteration below.
            tags_by_folder: dict[str, list[str]] = {}
            for subfolder, tag in self._state.tree_order:
                tags_by_folder.setdefault(subfolder, []).append(tag)

            # Iterate every subfolder so the user sees their full
            # directory structure — including folders that have no
            # first-occurring tags of their own.
            for subfolder in self._state.subfolders:
                folder_item = self._make_folder_item(subfolder)
                self.tree.addTopLevelItem(folder_item)
                self._folder_items[subfolder] = folder_item
                # Span column 0 across all columns so the progress
                # widget (which contains its own right-aligned stats)
                # gets the full row width.
                folder_item.setFirstColumnSpanned(True)

                for tag in tags_by_folder.get(subfolder, []):
                    tag_item = self._make_tag_item(tag, subfolder)
                    folder_item.addChild(tag_item)
                    self._tag_items[tag] = tag_item

                # Install the folder progress widget AFTER the item is
                # in the tree (setItemWidget requires the item to have
                # been added). Initial values are computed and pushed in
                # one call.
                self._install_folder_progress_widget(subfolder, folder_item)

                # Always expanded: folder headers are no longer
                # collapsible (see setItemsExpandable(False) in setup), so
                # tags must always be visible. The remembered-expansion
                # bookkeeping is retained above harmlessly but no longer
                # gates visibility.
                folder_item.setExpanded(True)

            # After rebuild, restore the current-tag highlight if any.
            self._refresh_selection_highlight()
            # A rebuild creates fresh tag rows without checkboxes; if
            # multi-select is on, re-add them and restore the ticks from
            # the state's selection. Signals are already blocked here.
            if self._state is not None and self._state.multi_select_mode:
                self._suspend_check_sync = True
                try:
                    self._set_checkboxes_now(True)
                finally:
                    self._suspend_check_sync = False
        finally:
            self.tree.blockSignals(False)

        # Re-apply any active search filter so it survives the rebuild
        # (sort change, count change, etc.). Done outside the signal
        # block since it only toggles item visibility.
        self._apply_search_filter()

        # Restore the user's place. If the previously-selected tag still
        # exists, re-select it (signals blocked — this is a visual
        # restore, NOT a walk restart) and keep it in view. Otherwise
        # fall back to the raw scroll offset, clamped.
        if prev_tag is not None and prev_tag in self._tag_items:
            item = self._tag_items[prev_tag]
            self.tree.blockSignals(True)
            try:
                self.tree.setCurrentItem(item)
            finally:
                self.tree.blockSignals(False)
            self.tree.scrollToItem(item)  # default EnsureVisible
        else:
            bar = self.tree.verticalScrollBar()
            bar.setValue(min(prev_scroll, bar.maximum()))

    # ------------------------------------------------------------------
    # Item factories
    # ------------------------------------------------------------------

    def _make_folder_item(self, subfolder: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        item.setData(0, ROLE_KIND, "folder")
        item.setData(0, ROLE_SUBFOLDER, subfolder)

        # No setText: the embedded _FolderProgressWidget owns the visible
        # row content (folder name, stats, progress bar). We leave the
        # underlying item's text blank so the default tree renderer
        # doesn't draw any redundant label behind the widget.

        # Folders aren't directly selectable — only their children matter.
        flags = item.flags() & ~Qt.ItemFlag.ItemIsSelectable
        item.setFlags(flags)
        return item

    def _install_folder_progress_widget(
        self,
        subfolder: str,
        folder_item: QTreeWidgetItem,
    ) -> None:
        """Attach a _FolderProgressWidget to a folder row and seed its
        initial values. Called once per folder during _rebuild_tree."""
        widget = _FolderProgressWidget()
        self.tree.setItemWidget(folder_item, 0, widget)
        self._refresh_folder_progress(subfolder)

    def _refresh_folder_progress(self, subfolder: str) -> None:
        """Recompute completion stats for a folder and push them into
        its embedded widget. Cheap (O(tags_in_folder)), so it's safe to
        call on every tag_changed event.

        Folder completion = # tags with status COMPLETED / # total tags
        whose first-occurrence folder is this one. Tag jobs that live
        elsewhere in the tree don't count here even if some of their
        images live in this folder, because the tag tree groups tags by
        first occurrence, and progress should match that grouping.
        """
        if self._state is None:
            return
        folder_item = self._folder_items.get(subfolder)
        if folder_item is None:
            return
        widget = self.tree.itemWidget(folder_item, 0)
        if not isinstance(widget, _FolderProgressWidget):
            return

        # Tags whose first-occurrence folder is this one.
        tags_here = [
            t for (sf, t) in self._state.tree_order if sf == subfolder
        ]
        total = len(tags_here)
        completed = sum(
            1 for t in tags_here
            if self._state.get_tag_status(t) == TagStatus.COMPLETED
        )
        image_count = len(
            self._state._images_by_subfolder.get(subfolder, [])
        )
        # For the root group (empty subfolder), show the dataset's own
        # folder name rather than a generic "(root)" — or "multiple
        # folders" when several were loaded and merged. Subfolders (a
        # non-empty name) are shown as-is. (Subfolders are not currently
        # loaded — images inside them are ignored — so in practice this
        # is the single root header, but the per-subfolder path stays
        # correct if that ever changes.)
        display = subfolder if subfolder else self._root_display_name()
        widget.update_progress(display, image_count, completed, total)

    def _root_display_name(self) -> str:
        """The label for the root (top-level) group: the loaded dataset's
        folder name, or 'multiple folders' when several were loaded and
        merged into one dataset. Falls back to '(root)' only if there is
        no state to name."""
        if self._state is None:
            return "(root)"
        roots = getattr(self._state, "roots", None) or [self._state.root]
        if len(roots) > 1:
            return "multiple folders"
        return roots[0].name if roots and roots[0] is not None else "(root)"

    def _folder_for_tag(self, tag: str) -> Optional[str]:
        """Reverse-lookup: which subfolder does this tag belong to in
        tree_order? Returns None if the tag isn't in the tree."""
        if self._state is None:
            return None
        for sf, t in self._state.tree_order:
            if t == tag:
                return sf
        return None

    def _make_tag_item(
        self,
        tag: str,
        subfolder: str,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        item.setData(0, ROLE_KIND, "tag")
        item.setData(0, ROLE_TAG_NAME, tag)
        item.setData(0, ROLE_SUBFOLDER, subfolder)
        self._apply_tag_style(item, tag)
        return item

    def _apply_tag_style(
        self,
        item: QTreeWidgetItem,
        tag: str,
    ) -> None:
        """Set icon, text, count, and styling for a tag row."""
        if self._state is None:
            return

        status = self._state.get_tag_status(tag)
        icon = self._status_icon(status)
        color = self._status_color(status)

        item.setText(0, f"{icon}  {tag}")
        # Global count: number of files dataset-wide containing this
        # tag. Reflects the audit job's true size (clicking the tag
        # walks every such image).
        cnt = self._state.get_tag_count_global(tag)
        item.setText(1, f"{cnt}")
        # Right-align the count: the count column now stretches to fill
        # leftover width, so right-alignment keeps the number pinned to
        # the row's right edge instead of floating after the name.
        item.setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        item.setForeground(0, QBrush(QColor(color)))
        item.setForeground(1, QBrush(QColor(Colors.TEXT_TERTIARY)))

        # Currently-walked tag: blue background tint.
        is_current = (self._state.current_tag == tag)
        # For the accent-bar delegate. Distinct from Qt's selection,
        # which follows the last click and can point elsewhere.
        item.setData(0, CURRENT_ROLE, bool(is_current))
        if is_current:
            bg = QBrush(QColor(Colors.ACCENT_BLUE_BG))
            font = QFont()
            font.setPointSize(Fonts.SIZE_NORMAL)
            font.setBold(True)
            item.setBackground(0, bg)
            item.setBackground(1, bg)
            item.setFont(0, font)
            item.setFont(1, font)
        else:
            item.setBackground(0, QBrush())
            item.setBackground(1, QBrush())
            font = QFont()
            font.setPointSize(Fonts.SIZE_NORMAL)
            item.setFont(0, font)
            item.setFont(1, font)

    @staticmethod
    def _status_icon(status: TagStatus) -> str:
        if status == TagStatus.COMPLETED:
            return Icons.CHECK
        if status == TagStatus.SKIPPED:
            return Icons.SKIP
        return Icons.HOURGLASS

    @staticmethod
    def _status_color(status: TagStatus) -> str:
        if status == TagStatus.COMPLETED:
            return Colors.SUCCESS_GREEN
        if status == TagStatus.SKIPPED:
            return Colors.WARNING_AMBER
        return Colors.TEXT_PRIMARY

    # ------------------------------------------------------------------
    # Per-event refresh routines
    # ------------------------------------------------------------------

    def _refresh_selection_highlight(self) -> None:
        """Re-apply styling to every tag row so the current-tag highlight
        moves to the right place (or clears if no tag selected).

        Two visuals track the active tag and BOTH must follow it: the
        blue background tint (our own, via _apply_tag_style) and Qt's own
        selection indicator (the accent bar / selected row on the tree
        widget). When a tag is active we tint and scroll to it; when none
        is (browse mode, or a walk that just ended) we also CLEAR Qt's
        selection, or the last-clicked tag row keeps its selection
        indicator even though no tag is being walked. The clear is done
        under blockSignals so it doesn't bounce through
        currentItemChanged and re-start a walk.
        """
        if self._state is None:
            return
        for tag, item in self._tag_items.items():
            self._apply_tag_style(item, tag)

        cur = self._state.current_tag
        if cur is not None:
            # Scroll the currently-walked tag into view.
            item = self._tag_items.get(cur)
            if item is not None:
                self.tree.scrollToItem(item)
        elif self._state.browse_mode:
            # Browse mode: no tag is active, so drop Qt's row selection or
            # the last-clicked tag row keeps its selection indicator (the
            # accent bar / selected-row look) even though nothing is being
            # walked. Restricted to browse mode on purpose — the walk-end
            # no-tag state is left as it was, and multi-select's selection
            # is its checkboxes, not the current row. Cleared under
            # blockSignals so it doesn't bounce through currentItemChanged
            # and re-start a walk.
            if self.tree.currentItem() is not None:
                self.tree.blockSignals(True)
                try:
                    self.tree.setCurrentItem(None)
                    self.tree.clearSelection()
                finally:
                    self.tree.blockSignals(False)

    def _refresh_tag_row(self, tag: str) -> None:
        """Update icon, count, and color for one tag's row."""
        item = self._tag_items.get(tag)
        if item is not None:
            self._apply_tag_style(item, tag)

    def _refresh_counts_for_image(self, image_path: Path) -> None:
        """When an image's tags change, the global count of each
        affected tag may have changed. Refresh those specific tag rows.

        Cheaper than refreshing every tag: we touch only tags currently
        in this image's caption file. For an image with 10 tags, that's
        10 row updates.
        """
        if self._state is None:
            return
        # Style refreshes below call setText, which fires itemChanged.
        # That must NOT be mistaken for the user ticking a checkbox — in
        # multi-select that would recompute the selection from the rows
        # mid-refresh and, if a checkbox momentarily lags, wipe it (and
        # with it the finder's queue). Suspend the check-sync for the
        # duration of this programmatic refresh. (Save/restore so it
        # nests safely inside any outer suspend.)
        prev_suspend = self._suspend_check_sync
        self._suspend_check_sync = True
        try:
            # We don't know which tags actually changed (added vs removed)
            # without before/after info. Refresh both the current tag-list
            # AND any tag we have in the tree that may have been removed.
            # Simplest correct approach: refresh the current tag (whose
            # count is most likely to have changed) and any tags currently
            # in the image. For removed tags we may miss the count update,
            # but only briefly: the next per-image change covers it, and
            # tree_rebuilt covers structural changes.
            if self._state.current_tag is not None:
                self._refresh_tag_row(self._state.current_tag)
            for tag in self._state.get_image_tags(image_path):
                self._refresh_tag_row(tag)
        finally:
            self._suspend_check_sync = prev_suspend

    # ------------------------------------------------------------------
    # User interactions
    # ------------------------------------------------------------------

    def _on_sort_changed(self, _index: int) -> None:
        """User picked a different tag sort order from the dropdown."""
        if self._state is None:
            return
        mode = self.sort_combo.currentData()
        if isinstance(mode, TagSortMode):
            self._state.set_tag_sort_mode(mode)

    def _on_search_changed(self, _text: str) -> None:
        """Live-filter the tag tree as the user types in the search box."""
        self._apply_search_filter()

    def _on_category_filter_changed(self, _index: int) -> None:
        """User picked a category filter (All / Completed / Uncompleted /
        rare-by-count). Re-apply the combined filter."""
        self._apply_search_filter()

    def _tag_bucket(self, tag: str) -> str:
        """Vocabulary bucket for a tag: "known" (canonical or alias,
        after folding — "long hair" == long_hair), "color_combo"
        (<color>_<registered tag> not itself registered, e.g.
        light_blue_thighhighs), or "custom" (not in the database:
        unique tokens and typos alike).

        Uses the audit's classifier so the two features always agree.
        Results are cached for the session — classification is a pure
        function of the tag string and the immutable database. On the
        first use the 201k database loads (~0.5s, wait cursor); if it
        can't load, warn ONCE, fall back to "known" for everything, and
        flip the dropdown back to All from outside the filter loop."""
        cached = self._tag_class_cache.get(tag)
        if cached is not None:
            return cached
        if self._tag_db_failed:
            return "known"
        from core import tag_database as tdb

        db = tdb.get_database()
        if not self._tag_db_ready:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                ok = db.ensure_loaded()
            finally:
                QApplication.restoreOverrideCursor()
            if not ok:
                self._tag_db_failed = True
                QMessageBox.warning(
                    self, "Tag database unavailable",
                    "The Danbooru tag database could not be loaded, so "
                    "the Custom / Color-combo views can't classify "
                    "tags.\n\nCheck the audit's tag-database setting.",
                )
                # Escape the filter loop before touching the combo.
                QTimer.singleShot(0, self._reset_category_filter_to_all)
                return "known"
            self._tag_db_ready = True
        bucket = tdb.bucket_for(tag)
        self._tag_class_cache[tag] = bucket
        return bucket

    def _reset_category_filter_to_all(self) -> None:
        for i in range(self.category_filter.count()):
            if self.category_filter.itemData(i) == "all":
                self.category_filter.setCurrentIndex(i)
                break

    def _tag_passes_category_filter(self, tag: str) -> bool:
        """True if `tag` passes the current category-filter dropdown.

        Keys: all / completed / uncompleted / skipped / eq1 / eq2 / eq3.
        Completion uses the tag's status (manually-complete or all
        images decided counts as completed). "skipped" matches tags the
        user skipped wholesale. Rare-by-count uses the global image count
        for the tag.
        """
        if self._state is None:
            return True
        key = self.category_filter.currentData()
        if key in (None, "all"):
            return True
        if key == "completed":
            return self._state.get_tag_status(tag) == TagStatus.COMPLETED
        if key == "uncompleted":
            return self._state.get_tag_status(tag) != TagStatus.COMPLETED
        if key == "skipped":
            # Tags the user skipped WHOLESALE (record_skip_tag →
            # TagStatus.SKIPPED), i.e. "I'm not auditing this category".
            # This is the per-tag skip, distinct from the queue's
            # per-image "Only skipped images" filter.
            return self._state.get_tag_status(tag) == TagStatus.SKIPPED
        if key == "custom_tags":
            return self._tag_bucket(tag) == "custom"
        if key == "color_combos":
            return self._tag_bucket(tag) == "color_combo"
        if key in ("eq1", "eq2", "eq3"):
            exact = {"eq1": 1, "eq2": 2, "eq3": 3}[key]
            return self._state.get_tag_count_global(tag) == exact
        return True

    def _apply_search_filter(self) -> None:
        """Apply the combined search + category filter.

        Two filters AND together. A tag row is visible iff:
          - search query is empty OR the tag name contains the query
            (case-insensitive substring), AND
          - the category-filter dropdown passes (All / Completed /
            Uncompleted / on exactly N images).

        Folders with no visible tag children are hidden when ANY filter
        is active, so the tree stays compact. With all filters off,
        all folders show (even empty ones) for navigation context.

        Re-applied after every rebuild so the filters persist across
        sort changes, decision-driven recomputes, etc.
        """
        query = self.search_box.text().strip().lower()
        category_key = self.category_filter.currentData()
        any_filter_active = (
            query != ""
            or category_key not in (None, "all")
        )

        for subfolder, folder_item in self._folder_items.items():
            visible_children = 0
            for i in range(folder_item.childCount()):
                child = folder_item.child(i)
                tag = child.data(0, ROLE_TAG_NAME)
                if not isinstance(tag, str):
                    continue
                matches_search = (query == "" or query in tag.lower())
                matches_category = self._tag_passes_category_filter(tag)
                visible = matches_search and matches_category
                child.setHidden(not visible)
                if visible:
                    visible_children += 1
            # Folder visibility: with no active filter, always show
            # folders (they provide navigation context even when empty
            # of matches); with any filter on, hide folders with zero
            # visible children to keep the view tidy.
            if not any_filter_active:
                folder_item.setHidden(False)
            else:
                folder_item.setHidden(visible_children == 0)

    def _resolve_typeahead(self, search: str):
        """Type-ahead resolver for the tree widget: first VISIBLE tag
        (visual order) whose name starts with the typed prefix,
        case-insensitive. Returns the item or None."""
        prefix = search.lower()
        item_to_tag = {id(it): t for t, it in self._tag_items.items()}
        for item in self._visible_tag_items_in_order():
            tag = item_to_tag.get(id(item))
            if tag is not None and tag.lower().startswith(prefix):
                return item
        return None

    def _visible_tag_items_in_order(self) -> list[QTreeWidgetItem]:
        """Return all currently-visible tag rows in top-to-bottom tree
        order (respecting folder grouping and the active filters).
        Hidden rows (filtered out or in collapsed folders) are excluded.
        """
        result: list[QTreeWidgetItem] = []
        root = self.tree.invisibleRootItem()
        for fi in range(root.childCount()):
            folder = root.child(fi)
            if folder.isHidden():
                continue
            for ci in range(folder.childCount()):
                child = folder.child(ci)
                if child.isHidden():
                    continue
                if child.data(0, ROLE_KIND) == "tag":
                    result.append(child)
        return result

    def _on_jump_next_pending_tag(self) -> None:
        """Jump to the FIRST pending tag from the top of the visible list
        — a tag whose status is PENDING (still has undecided images).
        Always scans from the top, regardless of the current selection,
        so it reliably takes the user to "the next thing that needs work"
        rather than something relative to where they are. Skips COMPLETED
        and SKIPPED tags. Tag-side counterpart of the queue's
        jump-to-pending button (#12).
        """
        if self._state is None:
            return
        items = self._visible_tag_items_in_order()
        if not items:
            return
        for item in items:
            tag = item.data(0, ROLE_TAG_NAME)
            if not isinstance(tag, str):
                continue
            if self._state.get_tag_status(tag) == TagStatus.PENDING:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return
        # No pending tag among visible rows — silent no-op.

    def focus_tree(self) -> None:
        """Give keyboard focus to the tag tree and ensure a row is
        current, so arrow keys immediately navigate tags.

        Called when a walk ends: the user just finished a tag's image
        queue and will likely press Down to move to the next tag. We put
        focus on the tree and, if no row is current, anchor on the
        currently-walked tag's row (or the first tag) so the first arrow
        press moves predictably.
        """
        self.tree.setFocus()
        if self.tree.currentItem() is not None:
            return
        anchor = None
        if self._state is not None and self._state.current_tag is not None:
            anchor = self._tag_items.get(self._state.current_tag)
        if anchor is None and self._tag_items:
            anchor = next(iter(self._tag_items.values()), None)
        if anchor is not None:
            self.tree.blockSignals(True)
            try:
                self.tree.setCurrentItem(anchor)
            finally:
                self.tree.blockSignals(False)

    def _on_current_item_changed(self, current, previous) -> None:
        """The current tree row changed via keyboard (or programmatically).
        If it's a tag row, select that tag so keyboard navigation starts
        walking it — matching the flow "finish an image queue, press Down
        to move to the next tag." Folder rows are ignored.

        Programmatic rebuilds call setCurrentItem inside blockSignals, so
        this doesn't fire for them. We also guard against re-selecting
        the already-current tag (idempotence) to avoid resetting the walk
        position when the tree merely re-syncs its highlight.
        """
        if self._state is None or current is None:
            return
        # In multi-select mode, arrow keys only move the highlight; they
        # must NOT start a single-tag walk (there is none in this mode).
        # Spacebar still toggles the focused row's checkbox via Qt.
        if self._state.multi_select_mode:
            return
        kind = current.data(0, ROLE_KIND)
        if kind != "tag":
            return
        tag = current.data(0, ROLE_TAG_NAME)
        if not isinstance(tag, str):
            return
        # Don't re-select the tag we're already on (would reset the walk
        # to its first pending image unexpectedly).
        if self._state.current_tag == tag:
            return
        self._state.select_tag(tag)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """Click on a tag row selects that tag. Click on a folder header
        enters browse mode for that folder — listing its images with no
        active tag, so fresh uncaptioned images can be captioned from
        zero (Yes/No stay disabled; only caption editing is live)."""
        if self._state is None:
            return
        # In multi-select mode, a row click is for the checkbox, not for
        # starting a walk. Qt toggles the checkbox itself (and fires
        # itemChanged); suppress single-tag selection here.
        if self._state.multi_select_mode:
            return
        kind = item.data(0, ROLE_KIND)
        if kind == "folder":
            # Folder header -> browse that folder. The ROLE_SUBFOLDER
            # value is the same key the state's images_by_subfolder uses
            # (both come from state.subfolders), so it scopes correctly.
            subfolder = item.data(0, ROLE_SUBFOLDER)
            if isinstance(subfolder, str):
                self._state.enter_browse_mode(subfolder)
            return
        if kind != "tag":
            return
        tag = item.data(0, ROLE_TAG_NAME)
        if isinstance(tag, str):
            # currentItemChanged fires before itemClicked on a click and
            # already selected this tag (setting current_tag). Without
            # this guard the click re-selects it, emitting a second
            # tag_selected — the "Selected tag" line was logged twice.
            if self._state.current_tag == tag:
                return
            self._state.select_tag(tag)

    # ------------------------------------------------------------------
    # Multi-select mode (tick a set of tags; queue filters by the set)
    # ------------------------------------------------------------------

    def _on_multi_select_toggled(self, checked: bool) -> None:
        """Enter/leave multi-select mode. Tells the state (which switches
        the queue between single-tag walk and tag-SET filtering), then
        shows or hides the per-row checkboxes to match."""
        if self._state is None:
            return
        self._state.set_multi_select_mode(checked)
        # set_multi_select_mode cleared the selection when leaving, so
        # _set_checkboxes_now(False) just removes the boxes; when entering
        # it restores any selection the state already holds (normally
        # none, but defensive).
        self.tree.blockSignals(True)
        self._suspend_check_sync = True
        try:
            self._set_checkboxes_now(checked)
        finally:
            self._suspend_check_sync = False
            self.tree.blockSignals(False)

    def _set_checkboxes_now(self, on: bool) -> None:
        """Add or remove checkboxes on every tag row. When turning on,
        restore the ticked state from the state's current selection.

        Does NOT manage tree signals itself — callers block them (so this
        is safe to call from inside _rebuild_tree's blockSignals block).

        Performance: column 0 is ResizeToContents, which recomputes its
        width by scanning EVERY row on each item change. Mutating every
        row's checkbox under that mode is O(n^2) — ~14s each way on a
        ~1,800-tag tree. So we pin the column to a fixed width (Interactive
        mode) and freeze repaints for the bulk pass, then restore
        ResizeToContents once at the end (a single recompute). This takes
        the toggle from tens of seconds to instant.
        """
        selected: set[str] = set()
        if on and self._state is not None:
            selected = {t.lower() for t in self._state.get_selected_tags()}

        header = self.tree.header()
        prev_width = self.tree.columnWidth(0)
        prev_updates = self.tree.updatesEnabled()
        self.tree.setUpdatesEnabled(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, prev_width)
        try:
            for tag, item in self._tag_items.items():
                self._set_item_checkable(item, on, tag.lower() in selected)
        finally:
            header.setSectionResizeMode(
                0, QHeaderView.ResizeMode.ResizeToContents
            )
            self.tree.setUpdatesEnabled(prev_updates)

    @staticmethod
    def _set_item_checkable(
        item: QTreeWidgetItem, checkable: bool, checked: bool
    ) -> None:
        """Toggle a tag row's checkbox. Setting CheckStateRole is what
        actually draws the box; clearing that data removes it."""
        flags = item.flags()
        if checkable:
            item.setFlags(flags | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0,
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
            )
        else:
            item.setFlags(flags & ~Qt.ItemFlag.ItemIsUserCheckable)
            # Remove the check-state data so no checkbox is drawn.
            item.setData(0, Qt.ItemDataRole.CheckStateRole, None)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        """A tag row changed. In multi-select mode this is a checkbox
        toggle: recompute the ticked set and push it to the state.

        Guarded against programmatic changes (_suspend_check_sync) and
        against the style refreshes that also emit itemChanged (those
        leave the check states untouched, so set_selected_tags would
        no-op anyway — the guard just avoids the wasted pass)."""
        if self._suspend_check_sync or self._state is None:
            return
        if not self._state.multi_select_mode:
            return
        if item.data(0, ROLE_KIND) != "tag":
            return
        self._state.set_selected_tags(self._collect_checked_tags())

    def _collect_checked_tags(self) -> list[str]:
        """Tags whose row checkbox is currently ticked, in tree order.
        Includes rows hidden by the category filter — a tick persists
        even when the row is filtered out of view."""
        result: list[str] = []
        for tag, item in self._tag_items.items():
            if item.checkState(0) == Qt.CheckState.Checked:
                result.append(tag)
        return result

    def _on_context_menu(self, position) -> None:
        """Right-click menu on tag rows. Folder headers get no menu."""
        if self._state is None:
            return
        item = self.tree.itemAt(position)
        if item is None or item.data(0, ROLE_KIND) != "tag":
            return
        tag = item.data(0, ROLE_TAG_NAME)
        if not isinstance(tag, str):
            return

        menu = QMenu(self)

        ref_action = QAction(f"Tag Reference for '{tag}'…", self)
        ref_action.triggered.connect(
            lambda: self.reference_requested.emit(tag))
        menu.addAction(ref_action)
        menu.addSeparator()

        status = self._state.get_tag_status(tag)

        # Mark complete (only useful if not already completed).
        if status != TagStatus.COMPLETED:
            mark_action = QAction(f"Mark '{tag}' complete", self)
            mark_action.triggered.connect(
                lambda: self._state.mark_tag_complete(tag) if self._state else None
            )
            menu.addAction(mark_action)

        # Skip tag — "come back to this later". Only offered if the tag
        # isn't already skipped. Acts on this specific tag regardless of
        # which tag is currently being walked.
        if status != TagStatus.SKIPPED:
            skip_action = QAction(f"Skip '{tag}' (review later)", self)
            skip_action.triggered.connect(
                lambda: self._state.record_skip_tag(tag) if self._state else None
            )
            menu.addAction(skip_action)

        menu.addSeparator()

        # Rename / merge. Less destructive than Delete (everything is
        # undoable) but still global, so we put it before Delete.
        rename_action = QAction(f"Rename '{tag}'... (or merge into existing tag)", self)
        rename_action.triggered.connect(
            lambda: self._confirm_and_rename_tag(tag)
        )
        menu.addAction(rename_action)

        # Destructive: delete tag globally. Visually distinct via the
        # confirm dialog. Placed at the bottom of the menu, after a
        # separator, to reduce accidental selection.
        delete_action = QAction(f"Delete tag '{tag}' globally...", self)
        delete_action.triggered.connect(lambda: self._confirm_and_delete_tag(tag))
        menu.addAction(delete_action)

        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _confirm_and_rename_tag(self, old_tag: str) -> None:
        """Prompt the user for a new name, warn if it would merge into
        an existing tag, then perform the global rename.

        The merge case (target tag already exists) gets an explicit
        second confirmation so the user can't accidentally collapse two
        distinct tags. The dataset isn't recoverable from a merge
        without undo, and merges that affect many files might exhaust
        the user's expectation of undo even though it's still one entry.
        """
        if self._state is None:
            return
        from PySide6.QtWidgets import QInputDialog
        new_tag, ok = QInputDialog.getText(
            self, "Rename tag",
            f"Rename '{old_tag}' to:",
            text=old_tag,
        )
        if not ok:
            return
        new_tag = new_tag.strip()
        if not new_tag or new_tag == old_tag:
            return

        # Split case: the user typed commas, e.g. renaming a malformed
        # merged tag "1girl solo" into the correct "1girl, solo". This
        # is a one-to-many split, not a rename. Parse the parts and route
        # to split_tag_globally. (A normal rename never contains a comma,
        # since a comma can't be part of a single tag.)
        if "," in new_tag:
            parts = [p.strip() for p in new_tag.split(",") if p.strip()]
            if not parts:
                return
            if parts == [old_tag]:
                return
            source_count = self._state.get_tag_count_global(old_tag)
            confirm = QMessageBox.question(
                self, "Split tag?",
                f"Split '{old_tag}' into {len(parts)} tags "
                f"({', '.join(parts)}) across {source_count} image(s)?\n\n"
                f"'{old_tag}' will be replaced by these tags on every "
                f"image that has it. Note: any Yes/No decisions you made "
                f"on '{old_tag}' will NOT carry over — the new tags start "
                f"fresh, since the old merged tag wasn't a real concept.\n\n"
                f"Undoable via Back.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            modified = self._state.split_tag_globally(old_tag, parts)
            failures = self._state.last_write_failures
            shown = ", ".join(failures[:15]) + (
                " \u2026" if len(failures) > 15 else "")
            if modified == 0:
                QMessageBox.warning(
                    self, "Split failed",
                    ("Every target file was locked or read-only and could "
                     f"not be written:\n\n{shown}\n\nClose anything using "
                     "them and try again.") if failures else
                    "No files were modified. One of the new tags may be "
                    "invalid, or the tag may not exist.",
                )
            elif failures:
                QMessageBox.warning(
                    self, "Split partially completed",
                    f"Split '{old_tag}' on {modified} file(s).\n\n"
                    f"{len(failures)} file(s) could not be written (locked "
                    f"or read-only) and still have the old tag:\n\n{shown}"
                    "\n\nClose anything using them and retry to finish.",
                )
            return

        # If the target already exists, this is a merge — confirm.
        is_merge = new_tag in self._state.all_tags
        if is_merge:
            target_count = self._state.get_tag_count_global(new_tag)
            source_count = self._state.get_tag_count_global(old_tag)
            confirm = QMessageBox.question(
                self, "Merge tags?",
                f"'{new_tag}' already exists on {target_count} images.\n\n"
                f"This will MERGE '{old_tag}' ({source_count} images) "
                f"into '{new_tag}'. Every image with '{old_tag}' will "
                f"be retagged with '{new_tag}', and '{old_tag}' will be "
                f"removed.\n\nUndoable via Back, but only as a single "
                f"action. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        # Pure rename also confirms, but more lightly.
        else:
            source_count = self._state.get_tag_count_global(old_tag)
            confirm = QMessageBox.question(
                self, "Rename tag?",
                f"Rename '{old_tag}' to '{new_tag}' across "
                f"{source_count} images?\n\nUndoable via Back.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        modified = self._state.rename_tag_globally(old_tag, new_tag)
        failures = self._state.last_write_failures
        shown = ", ".join(failures[:15]) + (" \u2026" if len(failures) > 15 else "")
        if modified == 0:
            QMessageBox.warning(
                self, "Rename failed",
                ("Every target file was locked or read-only and could not "
                 f"be written, so nothing changed:\n\n{shown}\n\nClose "
                 "anything using them and try again.") if failures else
                "No files were modified. The tag may not exist.",
            )
        elif failures:
            # Some files renamed, others couldn't be written (after retry).
            # The old tag remains on those, so name them and let the user
            # retry rather than assume it fully completed.
            QMessageBox.warning(
                self, "Rename partially completed",
                f"Renamed '{old_tag}' on {modified} file(s).\n\n"
                f"{len(failures)} file(s) could not be written (locked or "
                f"read-only) and still have the old tag:\n\n{shown}\n\n"
                "Close anything using them and retry to finish the rename.",
            )

    def _confirm_and_delete_tag(self, tag: str) -> None:
        """Single explicit confirmation dialog showing the file count,
        then delete the tag globally."""
        if self._state is None:
            return
        count = self._state.get_tag_count_global(tag)
        msg = QMessageBox(self)
        msg.setWindowTitle("Delete tag")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText(f"Remove '{tag}' from {count} caption file(s)?")
        msg.setInformativeText(
            "This rewrites the affected .txt files immediately.\n\n"
            "You can undo with Ctrl+Z, but undo is lost when the app closes."
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if msg.exec() == QMessageBox.StandardButton.Yes:
            modified = self._state.delete_tag_globally(tag)
            failures = self._state.last_write_failures
            # Warn if some files couldn't be written even after retries
            # (locked / read-only). The tag remains on those files, so
            # name them and let the user retry.
            if failures:
                shown = ", ".join(failures[:15]) + (
                    " \u2026" if len(failures) > 15 else "")
                QMessageBox.warning(
                    self,
                    "Delete partially completed",
                    f"Removed '{tag}' from {modified} file(s).\n\n"
                    f"{len(failures)} file(s) could not be written (locked "
                    f"or read-only) and still have the tag:\n\n{shown}\n\n"
                    "Close anything using them and retry the delete.",
                )

    # ------------------------------------------------------------------
    # Enabled state
    # ------------------------------------------------------------------

    def _refresh_enabled(self) -> None:
        enabled = self._state is not None
        self.tree.setEnabled(enabled)
        self.sort_combo.setEnabled(enabled)
        self.search_box.setEnabled(enabled)
        self.multi_select_btn.setEnabled(enabled)
