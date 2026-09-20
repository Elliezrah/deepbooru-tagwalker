"""
ui/filter_bar.py

Filter, sort, and orphan-visibility controls for the image queue.

Lives at the top of the right-side queue panel (see ui/queue_panel.py).
Reflects current SessionState settings and writes user changes back to
the state. Also listens to state notifications so that programmatic
changes (e.g. from a session load that restores filter mode) keep the
widget UI in sync.

This is the first UI widget written, and it establishes patterns that
the rest of the UI follows:

1. Widgets construct empty (no state binding yet).
2. attach(state) binds them to a SessionState. Replaceable on reload.
3. detach() unbinds without destroying the widget.
4. Signal loops are guarded with a single `_updating` flag.
5. Combobox items store their semantic value as UserData, not by
   position — reordering options is safe.
6. Disabled state is automatic when no state is attached.

Why attach/detach instead of state-in-constructor: the user can open
a new dataset at any time, which creates a fresh SessionState. Without
attach/detach the widget would keep listening to the old (dead) state
and never see updates from the new one. Recreating widgets on every
dataset open would mean flicker and lost focus. The attach pattern
swaps the state under the same widget instance — clean.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from config.theme import Spacing
from core.state import FilterMode, SessionState, SortMode, StateChange


class FilterBar(QWidget):
    """Filter, sort, and orphan-visibility controls.

    Layout (vertical, top to bottom):
        Filter: [combobox]
        Sort:   [combobox]
        [ ] Show images without .txt

    Public surface:
        attach(state)  — bind to a SessionState (or None to unbind)
        detach()       — alias for attach(None)
    """

    # Combobox options. Tuples of (display label, semantic value).
    # The label is what the user sees; the value is stored as
    # Qt UserData on each item, so item ordering can change without
    # breaking the mapping.
    #
    # Two label sets, same semantic values: the filter's meaning shifts
    # between single-tag mode (keyed to the CURRENT walk tag) and
    # multi-select mode (set logic over the TICKED tags). Two field
    # reports traced back to the old ambiguous "with tag" wording —
    # users reasonably read it as "with whatever tag I typed in the
    # search box". The labels now say exactly which tags they mean, and
    # _relabel_for_mode swaps them live when multi-select toggles.
    FILTER_OPTIONS: tuple[tuple[str, FilterMode], ...] = (
        ("All images",                        FilterMode.ALL),
        ("Only images WITH current tag",      FilterMode.HAS_TAG),
        ("Only images WITHOUT current tag",   FilterMode.MISSING_TAG),
        ("Only skipped (current tag)",        FilterMode.SKIPPED_ONLY),
        ("Only over token limit",             FilterMode.OVER_TOKEN_LIMIT),
    )

    FILTER_OPTIONS_MULTI: tuple[tuple[str, FilterMode], ...] = (
        ("All images",                        FilterMode.ALL),
        ("With ALL ticked tags",              FilterMode.HAS_TAG),
        ("With NONE of the ticked tags",      FilterMode.MISSING_TAG),
        ("Skipped for ANY ticked tag",        FilterMode.SKIPPED_ONLY),
        ("Only over token limit",             FilterMode.OVER_TOKEN_LIMIT),
    )

    SORT_OPTIONS: tuple[tuple[str, SortMode], ...] = (
        ("A → Z",          SortMode.ALPHA_ASC),
        ("Z → A",          SortMode.ALPHA_DESC),
        ("Most tags",      SortMode.TAG_COUNT_DESC),
        ("Fewest tags",    SortMode.TAG_COUNT_ASC),
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state: Optional[SessionState] = None
        # Whether the Filter dropdown is locked (grouping mode). Sort
        # remains usable.
        self._filter_locked: bool = False
        # Re-entry guard. When we update widget values programmatically
        # (e.g. from a state change), Qt fires the same change signals
        # we use to detect user edits. Without this flag we'd loop:
        # widget change → state.set_filter_mode → state emits → we
        # call _sync_from_state → widget change → ...
        self._updating: bool = False

        self._build_ui()
        self._refresh_enabled()

    # ------------------------------------------------------------------
    # Public API: state binding
    # ------------------------------------------------------------------

    def attach(self, state: Optional[SessionState]) -> None:
        """Bind this widget to a SessionState.

        Idempotent: calling with the currently-bound state is a no-op.
        Switching to a different state automatically unsubscribes
        from the previous one's listeners.
        """
        if state is self._state:
            return
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        self._state = state
        if state is not None:
            state.add_listener(self._on_state_change)
            self._sync_from_state()
        self._refresh_enabled()

    def detach(self) -> None:
        """Unbind from the current state. The widget becomes inert
        but is not destroyed; calling attach() again rebinds it."""
        self.attach(None)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        # Tight margins: this bar lives inside a panel that already
        # has its own padding, so we don't want a double border.
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.TIGHT)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.setSpacing(Spacing.NORMAL)
        filter_label = QLabel("Filter:")
        filter_label.setProperty("role", "secondary")
        filter_row.addWidget(filter_label)
        self.filter_combo = QComboBox()
        for label, mode in self.FILTER_OPTIONS:
            # The mode enum is stored on the item itself (UserData).
            # We look it up via itemData() rather than relying on index.
            self.filter_combo.addItem(label, mode)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_change)
        filter_row.addWidget(self.filter_combo, 1)  # stretch=1 fills row
        outer.addLayout(filter_row)

        # Sort row
        sort_row = QHBoxLayout()
        sort_row.setSpacing(Spacing.NORMAL)
        sort_label = QLabel("Sort:")
        sort_label.setProperty("role", "secondary")
        sort_row.addWidget(sort_label)
        self.sort_combo = QComboBox()
        for label, mode in self.SORT_OPTIONS:
            self.sort_combo.addItem(label, mode)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_change)
        sort_row.addWidget(self.sort_combo, 1)
        outer.addLayout(sort_row)

        # Orphan-visibility checkbox.
        # The label is what was in the original mockup ("Show no-txt")
        # but slightly fuller for clarity. The intent is to reveal
        # images that have no caption file alongside their tagged
        # siblings.
        self.orphans_check = QCheckBox("Show images without .txt")
        self.orphans_check.setToolTip(
            "Reveal images that have no caption file. These are "
            "highlighted in red in the queue and can be acted on "
            "to create a caption file with the current tag."
        )
        self.orphans_check.stateChanged.connect(self._on_orphans_change)
        outer.addWidget(self.orphans_check)

    # ------------------------------------------------------------------
    # Signal handlers (widget → state)
    # ------------------------------------------------------------------

    def _on_filter_change(self, index: int) -> None:
        if self._updating or self._state is None:
            return
        mode = self.filter_combo.itemData(index)
        if isinstance(mode, FilterMode):
            self._state.set_filter_mode(mode)

    def _on_sort_change(self, index: int) -> None:
        if self._updating or self._state is None:
            return
        mode = self.sort_combo.itemData(index)
        if isinstance(mode, SortMode):
            self._state.set_sort_mode(mode)

    def _on_orphans_change(self, _state_value: int) -> None:
        # The argument is Qt.CheckState as int (0=Unchecked, 2=Checked).
        # We ignore it and read isChecked() directly — clearer at the
        # call site than translating CheckState values.
        if self._updating or self._state is None:
            return
        self._state.set_show_orphans(self.orphans_check.isChecked())

    # ------------------------------------------------------------------
    # State change listener (state → widget)
    # ------------------------------------------------------------------

    def _on_state_change(self, change: StateChange) -> None:
        """Called by SessionState when something changes.

        "filter_changed" is the main trigger — set_filter_mode,
        set_sort_mode, set_show_orphans, and mode entry/exit all emit it.
        "tag_selected" also matters: picking a tag LEAVES browse mode, and
        the browse-only hiding of the tag-based filter options must be
        undone when that happens (tag_selected does not itself emit
        filter_changed). Other change kinds don't affect our UI.
        """
        if change.kind in ("filter_changed", "tag_selected"):
            self._sync_from_state()

    # ------------------------------------------------------------------
    # Internal: pull current state into widget values
    # ------------------------------------------------------------------

    def _sync_from_state(self) -> None:
        """Update widget values to match the bound state.

        Used on attach() and whenever state changes externally. Wraps
        each set in the re-entry guard so the signals fired by setCurrentIndex /
        setChecked don't loop back into the widget→state handlers.
        """
        if self._state is None:
            return
        self._updating = True
        try:
            self._relabel_for_mode(self._state.multi_select_mode)
            self._apply_browse_filter_visibility(self._state.browse_mode)
            self._select_combo_by_data(self.filter_combo, self._state.filter_mode)
            self._select_combo_by_data(self.sort_combo, self._state.sort_mode)
            self.orphans_check.setChecked(self._state.show_orphans)
        finally:
            self._updating = False

    def _apply_browse_filter_visibility(self, browse: bool) -> None:
        """Show or hide the tag-based filter options for browse mode.

        The tag-based filters (Only WITH / Only WITHOUT / Only skipped)
        key on an active tag, which browse mode does not have, so they are
        hidden while browsing — leaving "All images" and "Only over token
        limit" (the two tag-independent options, both of which work in
        browse mode). Rows are hidden in the popup AND disabled in the
        model as a fallback, the same technique set_grouping_mode uses for
        the sort options. Leaving browse restores every row. If the active
        filter is a now-hidden tag-based one when browse begins, it is
        snapped back to ALL so the visible selection stays valid.
        """
        from core.state import FilterMode
        tag_based = (FilterMode.HAS_TAG, FilterMode.MISSING_TAG,
                     FilterMode.SKIPPED_ONLY)
        if browse and self._state is not None:
            if self._state.filter_mode in tag_based:
                # Snap to ALL so a hidden option isn't left selected. This
                # calls back into the state, which re-emits filter_changed;
                # the _updating guard on the outer _sync_from_state keeps
                # that from looping into the widget handlers.
                self._state.set_filter_mode(FilterMode.ALL)
        view = self.filter_combo.view()
        model = self.filter_combo.model()
        for i in range(self.filter_combo.count()):
            mode = self.filter_combo.itemData(i)
            hide = browse and mode in tag_based
            view.setRowHidden(i, hide)
            item = model.item(i)
            if item is not None:
                item.setEnabled(not hide)

    def _relabel_for_mode(self, multi: bool) -> None:
        """Swap the filter labels to match what they actually mean right
        now: single-tag mode filters against the CURRENT walk tag;
        multi-select filters with set logic over the TICKED tags. The
        semantic UserData on each item never changes — only the words."""
        options = self.FILTER_OPTIONS_MULTI if multi else self.FILTER_OPTIONS
        for label, mode in options:
            for i in range(self.filter_combo.count()):
                if self.filter_combo.itemData(i) == mode:
                    if self.filter_combo.itemText(i) != label:
                        self.filter_combo.setItemText(i, label)
                    break

    @staticmethod
    def _select_combo_by_data(combo: QComboBox, value) -> None:
        """Set combobox to the item whose UserData equals `value`.

        No-op if no item matches. Uses identity-aware equality so that
        enum members compare correctly across reloads (Python enum
        members are singletons within a single import; we don't span
        imports here).
        """
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    def show_defaults(self, filter_mode, sort_mode, show_orphans: bool) -> None:
        """Set the combos to the given defaults WITHOUT a bound state.

        Called at startup so the bar reflects the user's saved default
        sort/filter immediately, instead of the hardcoded first item.
        Otherwise the combo shows index 0 (A-Z) at launch and then
        'flips' to the real default when the first dataset loads. Guarded
        so the programmatic set doesn't emit into the widget→state path.
        """
        self._updating = True
        try:
            self._select_combo_by_data(self.filter_combo, filter_mode)
            self._select_combo_by_data(self.sort_combo, sort_mode)
            self.orphans_check.setChecked(show_orphans)
        finally:
            self._updating = False

    def _refresh_enabled(self) -> None:
        """Enable controls when a state is bound, disable otherwise."""
        has_state = self._state is not None
        self.filter_combo.setEnabled(has_state)
        self.sort_combo.setEnabled(has_state)
        self.orphans_check.setEnabled(has_state)

    def set_grouping_mode(self, grouping: bool) -> None:
        """Adjust the sort options for queue grouping.

        In grouping mode only A-Z / Z-A make sense as a GROUP order, so
        the tag-count sorts ("Most tags" / "Fewest tags") are hidden. The
        Filter dropdown and orphan toggle stay fully usable — grouping is
        a view over the (already filtered) queue, so filtering works
        normally. If the current sort is a now-hidden tag-count mode when
        grouping turns on, snap it to A-Z so the visible selection is
        valid.
        """
        if grouping and self._state is not None:
            from core.state import SortMode
            if self._state.sort_mode in (
                SortMode.TAG_COUNT_DESC, SortMode.TAG_COUNT_ASC
            ):
                self._state.set_sort_mode(SortMode.ALPHA_ASC)
        # Show/hide the tag-count sort items (indices 2 and 3).
        view = self.sort_combo.view()
        model = self.sort_combo.model()
        for i, (_label, mode) in enumerate(self.SORT_OPTIONS):
            from core.state import SortMode
            is_count = mode in (
                SortMode.TAG_COUNT_DESC, SortMode.TAG_COUNT_ASC
            )
            hide = grouping and is_count
            # Hide the row in the popup and disable it as a fallback.
            view.setRowHidden(i, hide)
            item = model.item(i)
            if item is not None:
                item.setEnabled(not hide)
