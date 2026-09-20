"""
ui/queue_panel.py

The right-side panel: image queue for the currently-walked tag.

Composition (top to bottom):
- FilterBar (filter mode, sort, orphan visibility)
- QListWidget showing the queue, with each row color-coded by state
- A small status line showing "N of M" position

Visual states for each row (precedence top to bottom):
- Currently viewing      blue background, bold font, 👁 prefix
- Orphan (no .txt)       red-tinted background, italic font, ∅ prefix
- Yes-decided            green background, ✓ prefix
- No-decided             red background, ✗ prefix
- Skipped                amber background, ↷ prefix
- Unprocessed            no background, no prefix

Orphan rows that happen to also be the currently-viewed row use the
blue background (viewing takes precedence as positional signal), but
keep italic font as a residual orphan indicator. The main image
panel already shows a large "no caption file" badge for orphans, so
the queue row doesn't need to scream it.

Performance
-----------
Each row is a plain QListWidgetItem with setBackground / setForeground /
setFont / setText / setData. No custom paint delegates. This keeps
the rendering path inside Qt's optimized native code; in design-time
benchmarks a 1000-item rebuild took ~30-50 ms.

On per-action restyle (Yes / No / Skip on one image), only that
single row is touched — O(1) — via the _items_by_path lookup
dictionary that mirrors the QListWidget contents.

Listener lifecycle and re-entry guard follow the same patterns
established in ui/filter_bar.py.
"""

from __future__ import annotations

from functools import lru_cache as _lru_cache

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QEvent, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QMenu,
)

from config.settings import Settings
from ui.accent_bar import AccentBarDelegate, CURRENT_ROLE
from config.theme import Colors, Fonts, Icons, Spacing, scrollbar_with_arrows_qss
from core.scanner import ImageEntry


class _QueueListWidget(QListWidget):
    """QListWidget whose automatic scroll-into-view can be suppressed.

    Qt's view scrolls the current/clicked item fully into view from
    inside its own mouse handling (via scrollTo), independently of the
    autoScroll property and of our explicit scrollToItem calls. On an
    off-center click that internal scroll nudges the viewport a few px,
    shifting which row sits under the cursor and desyncing the selection
    highlight from the clicked row. Overriding scrollTo lets us veto that
    nudge for the duration of a click while still allowing our own
    deliberate scrollToItem (which we route through the base class).
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._scroll_locked = False

    def set_scroll_locked(self, locked: bool) -> None:
        self._scroll_locked = locked

    def scrollTo(self, index, hint=QListWidget.ScrollHint.EnsureVisible):  # noqa: N802
        if self._scroll_locked:
            return  # veto Qt's internal click-nudge
        super().scrollTo(index, hint)

    def force_scroll_to_item(self, item, hint) -> None:
        """Deliberate, code-initiated scroll that bypasses the lock."""
        was = self._scroll_locked
        self._scroll_locked = False
        try:
            self.scrollToItem(item, hint)
        finally:
            self._scroll_locked = was

    # -- Report: type-ahead stole letters -------------------------------
    def keyboardSearch(self, search: str) -> None:  # noqa: N802
        """Disable Qt's built-in type-to-jump. Global shortcuts (Y/N/
        Space/…) already claim most letters, so the leftover keys jumped
        the selection unpredictably on mispresses — worse than useless.
        Deliberate no-op; tag-name type-ahead lives on the tag tree,
        where every letter is available."""
        return

    # -- Feature: modifier + mouse wheel steps the selection ------------
    def set_wheel_nav_modifier_getter(self, getter) -> None:
        """Install a callable returning the configured modifier name
        ('Shift' / 'Ctrl' / 'Alt' / 'Disabled'). Read per-event so a
        change in Settings applies immediately, no restart."""
        self._wheel_nav_getter = getter

    _WHEEL_MODIFIERS = {
        "Shift": Qt.KeyboardModifier.ShiftModifier,
        "Ctrl": Qt.KeyboardModifier.ControlModifier,
        "Alt": Qt.KeyboardModifier.AltModifier,
    }

    def wheelEvent(self, event) -> None:  # noqa: N802
        name = None
        getter = getattr(self, "_wheel_nav_getter", None)
        if getter is not None:
            try:
                name = getter()
            except Exception:
                name = None
        mod = self._WHEEL_MODIFIERS.get(name or "")
        if mod is not None and (event.modifiers() & mod):
            dy = event.angleDelta().y()
            if dy != 0:
                step = -1 if dy > 0 else 1     # wheel up = previous image
                row = self.currentRow()
                n = self.count()
                nxt = row + step
                # Skip non-selectable rows (group headers in grouped view).
                while 0 <= nxt < n:
                    it = self.item(nxt)
                    if it is not None and bool(
                        it.flags() & Qt.ItemFlag.ItemIsSelectable
                    ):
                        break
                    nxt += step
                if 0 <= nxt < n and nxt != row:
                    # currentItemChanged fires -> the panel's normal
                    # click-navigation path walks to that image. Mark
                    # this transition as WHEEL NAVIGATION: the handler's
                    # Ctrl/Shift guard (which protects click/arrow
                    # multi-selection) must not swallow it — the user is
                    # holding the modifier BECAUSE it's the wheel-nav
                    # key (field report: highlight scrolled, image
                    # didn't follow).
                    self._wheel_nav_in_progress = True
                    try:
                        self.setCurrentRow(nxt)
                    finally:
                        self._wheel_nav_in_progress = False
            event.accept()
            return
        super().wheelEvent(event)
from core.state import Decision, SessionState, StateChange
from ui.filter_bar import FilterBar


# ImageEntry is stored as item UserData under this role. Using a custom
# role keeps it separate from Qt's reserved roles.
IMAGE_ENTRY_ROLE = Qt.ItemDataRole.UserRole
# Group rows (name-pattern grouping, Feature B) carry their NameGroup
# under this role; image rows leave it None. Lets us tell a group-header
# row apart from an image row in click handlers.
GROUP_ROLE = Qt.ItemDataRole.UserRole + 1
# A group's expanded/collapsed state is stored per-row so it survives a
# rebuild (we re-read it into the panel's _expanded_bases set).
GROUP_EXPANDED_ROLE = Qt.ItemDataRole.UserRole + 2


# Cache of generated presence-dot icons, keyed by hex color string.
# These are small filled circles used as the row icon for undecided
# images: green = tag present, red = tag missing. Generated once per
# color (per theme palette) and reused. Painting a tiny pixmap is cheap
# but caching avoids doing it for every row on every restyle.
_DOT_ICON_CACHE: dict[str, QIcon] = {}
_DOT_ICON_SIZE = 12  # px; small, unobtrusive


def _dot_icon(hex_color: str) -> QIcon:
    """Return a cached small filled-circle QIcon in the given color.

    Used as the presence indicator on undecided queue rows so the dot
    can be colored independently of the row's (neutral) text — Qt list
    items can't color individual characters, but they can carry a
    separately-colored icon.
    """
    cached = _DOT_ICON_CACHE.get(hex_color)
    if cached is not None:
        return cached
    pix = QPixmap(_DOT_ICON_SIZE, _DOT_ICON_SIZE)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(hex_color))
    # Inset by 2px so the circle has a little breathing room.
    painter.drawEllipse(2, 2, _DOT_ICON_SIZE - 4, _DOT_ICON_SIZE - 4)
    painter.end()
    icon = QIcon(pix)
    _DOT_ICON_CACHE[hex_color] = icon
    return icon


@_lru_cache(maxsize=50_000)
def _stem_of(path) -> str:
    """pathlib parses the name on every .stem access, and a rebuild
    asks once per row. Small — about 1.4 ms per 2,400 rows — but the
    paths never change, so recomputing is pure waste."""
    return path.stem


class QueuePanel(QFrame):
    """Image queue panel for the currently-walked tag."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Mark this frame as a panel so the stylesheet styles it.
        self.setProperty("role", "panel")

        self._state: Optional[SessionState] = None
        self._updating: bool = False

        # image_path -> QListWidgetItem. Lets _restyle_one run in O(1).
        self._items_by_path: dict[Path, QListWidgetItem] = {}

        # Name-pattern grouping (Feature B). When True, the queue is shown
        # as collapsible group headers (base_(0), base_(1), … families)
        # with indented image children, plus ungrouped images. The walk
        # itself stays per-image; grouping is purely visual + a batch-
        # decide convenience. _expanded_bases holds the (subfolder, base)
        # keys currently expanded so the state survives list rebuilds.
        self._grouping_enabled: bool = False
        self._expanded_bases: set[tuple[str, str]] = set()
        # (subfolder, base) -> list[ImageEntry] for the current queue's
        # groups; rebuilt each time the list is rebuilt.
        self._groups_by_key: dict[tuple[str, str], list] = {}
        # Last image the walk was on, for the group-boundary stop rule.
        self._prev_walk_path = None
        # The (subfolder, base) key of the group HEADER the user has
        # currently selected, or None when an image row is the active
        # selection. This is the fix for header selection being yanked
        # back to the walked image: when a header is selected, the
        # current-row indicator keeps the highlight on the header instead
        # of forcing it onto the walked image.
        self._selected_header_key = None
        # Rows whose style is stale. See _restyle_changed_rows().
        self._dirty_rows: set = set()
        self._last_current_path = None
        # When True, _refresh_current_indicator skips the
        # scroll-into-view/center step. Set during a USER CLICK on a row:
        # the clicked row is already visible under the cursor, and
        # recentering the viewport there moves a different row under the
        # click point — desyncing the selection highlight from the row
        # (the "highlight jumps on off-center clicks" bug). Programmatic
        # navigation (keyboard, undo, jump-to-pending) leaves this False
        # so off-screen targets still scroll into view.
        self._suppress_scroll = False
        # One-shot flag: set when the user clicks a single image row in
        # group mode, so the next refresh shows that single image instead
        # of the group's grid preview.
        self._view_single_image = False
        # Optional back-reference to the image panel so we can trigger the
        # center-panel group preview when a group header is selected.
        self._image_panel = None
        # Optional back-reference to the file-state panel so we can switch
        # it to a read-only group summary when a group header is selected.
        self._file_state_panel = None
        # The image path at which we last enforced a group-boundary stop.
        # Prevents trapping the user: once we've stopped at a group's last
        # member, deciding it AGAIN is allowed to advance normally (the
        # user has consciously chosen to move on past the boundary).
        self._boundary_stopped_at = None

        # The tag whose queue is currently displayed. Tracked separately
        # from state.current_tag because state.current_tag becomes None
        # when a walk ends (e.g. tag-complete stop), but the queue panel
        # still shows that tag's images and they need correct styling
        # (green for Yes, etc.). Without this, the moment the walk ends
        # the styling falls back to UNPROCESSED and the green ticks
        # disappear, even though the decisions in state are correct.
        self._displayed_tag: Optional[str] = None

        self._build_ui()
        self._refresh_enabled()

    # ------------------------------------------------------------------
    # Public API: state binding
    # ------------------------------------------------------------------

    def attach(self, state: Optional[SessionState]) -> None:
        """Bind to a SessionState. See ui/filter_bar.py for the pattern."""
        if state is self._state:
            return
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        self._state = state
        # The filter bar lives inside us, so we forward attach to it.
        self.filter_bar.attach(state)
        if state is not None:
            state.add_listener(self._on_state_change)
            self._rebuild_list()
        else:
            self._clear_list()
        self._refresh_enabled()

    def detach(self) -> None:
        self.attach(None)

    def set_image_panel(self, image_panel) -> None:
        """Wire the image panel so we can drive its center-panel group
        preview when a group header is selected."""
        self._image_panel = image_panel

    def set_file_state_panel(self, panel) -> None:
        """Wire the file-state panel so we can switch it to a group
        summary (read-only) when a group header is selected in group
        mode, and back to the normal per-image view otherwise."""
        self._file_state_panel = panel

    def selected_image_paths(self) -> list:
        """Return the image paths of all currently-selected rows.

        Used by batch operations (B6): the image panel's Yes/No
        triggers check this to decide between single-image and
        multi-image behavior.

        An empty list means no rows are selected (panel just opened, or
        the walk's current row is the only "selected" one through Qt's
        currentItem). A single-element list means single selection
        (clicked one row). Multiple elements indicate Ctrl/Shift-built
        multi-selection.
        """
        out = []
        for item in self.list_widget.selectedItems():
            entry = item.data(IMAGE_ENTRY_ROLE)
            if entry is not None:
                out.append(entry.image_path)
        return out

    # ------------------------------------------------------------------
    # Group batch-decide (Feature B)
    # ------------------------------------------------------------------

    def selected_group_members(self) -> Optional[list]:
        """If grouping is on AND a group header is the active selection,
        return that group's member image paths; otherwise None.

        Used by the decision handler to route a Yes/No on a selected
        group header into a batch decision over the whole group.
        """
        if not self._grouping_enabled:
            return None
        key = self._selected_header_key
        if key is None:
            return None
        members = self._groups_by_key.get(key)
        if not members:
            return None
        return [e.image_path for e in members]

    def selected_group_preview(self) -> Optional[tuple]:
        """If a group header is the active selection, return
        (base, [paths]) for the center-panel preview grid; else None."""
        if not self._grouping_enabled:
            return None
        key = self._selected_header_key
        if key is None:
            return None
        members = self._groups_by_key.get(key)
        if not members:
            return None
        return (key[1], [e.image_path for e in members])

    def reselect_group_after_undo(self) -> None:
        """Called after a successful undo. If the reverted action was a
        group batch op, the state carries its group key in
        last_undo_context — re-select that group so the
        selection/highlight/file-state follow the undone group, exactly as
        standard mode returns the walk to the undone image. Reading the
        key from the undo entry itself (rather than a UI-side marker)
        keeps it in lockstep with the real undo stack, so undoing several
        group actions in a row walks the selection back group-by-group.
        No-ops in flat mode, if there's no group context, or if the group
        no longer exists."""
        if self._state is None or not self._grouping_enabled:
            return
        ctx = self._state.last_undo_context
        if not ctx:
            return
        key = ctx.get("group_key")
        if key is None or key not in self._groups_by_key:
            return
        item = None
        for i in range(self.list_widget.count()):
            if self.list_widget.item(i).data(GROUP_ROLE) == key:
                item = self.list_widget.item(i)
                break
        if item is not None:
            self._select_header(key, item)
            self.list_widget.scrollToItem(item)

    def advance_to_next_group_header(self) -> None:
        """After a group batch-decide, move selection to the next group
        that still has PENDING work — skipping over groups that are
        already fully decided (every member YES or NO for the current
        tag). This keeps the user moving through real work instead of
        landing on already-finished groups.

        Search order: from the row after the current header to the end,
        pick the first group with a pending member. If none of the later
        groups have pending work, fall back to the immediate next header
        (so the user still moves forward); if there's no next header at
        all, stay on the current group. Updates the tracked header key,
        highlight, and centre-panel preview.
        """
        cur_row = -1
        if self._selected_header_key is not None:
            for i in range(self.list_widget.count()):
                if (self.list_widget.item(i).data(GROUP_ROLE)
                        == self._selected_header_key):
                    cur_row = i
                    break
        n = self.list_widget.count()
        tag = self._displayed_tag

        def group_has_pending(key) -> bool:
            # "Pending" = has an UNDECIDED (UNPROCESSED) member. SKIPPED
            # members don't count as pending (a skip is a deliberate
            # set-aside), so auto-advance skips over fully-decided AND
            # fully-skipped groups — the same definition jump-to-pending
            # uses, keeping the two behaviours consistent.
            if tag is None:
                return True
            for e in self._groups_by_key.get(key, []):
                d = self._state.get_decision(e.image_path, tag)
                if d == Decision.UNPROCESSED:
                    return True
            return False

        first_next = None  # immediate next header, as a fallback
        target = None
        for i in range(cur_row + 1, n):
            it = self.list_widget.item(i)
            key = it.data(GROUP_ROLE)
            if key is None:
                continue
            if first_next is None:
                first_next = (i, it, key)
            if group_has_pending(key):
                target = (i, it, key)
                break

        chosen = target or first_next
        if chosen is not None:
            i, it, key = chosen
            self._selected_header_key = key
            self.list_widget.blockSignals(True)
            try:
                self.list_widget.setCurrentItem(it)
            finally:
                self.list_widget.blockSignals(False)
            self.list_widget.scrollToItem(it)
            self._highlight_selected_header()
            if self._image_panel is not None:
                self._image_panel.show_group_preview(
                    self.selected_group_preview()
                )
            return
        # No next header at all — keep the current group selected; refresh
        # its highlight (its aggregate colour changed after the decision).
        self._highlight_selected_header()
        if self._image_panel is not None:
            self._image_panel.show_group_preview(
                self.selected_group_preview()
            )

    def select_first_pending_group(self) -> bool:
        """Select the first group that still has undecided images.

        FIELD REPORT: switching to group mode left the walk running on
        single images until a group header was clicked, and changing
        tag while grouped did the same. Both had the same cause —
        nothing selected a group, and a group walk needs a selected
        group to walk.

        advance_to_next_group_header() could not serve: it starts from
        the row AFTER the current one, which is right for advancing
        and wrong for arriving. This starts at the top.

        Returns True if a group was selected.
        """
        if not self._grouping_enabled:
            return False
        tag = self._displayed_tag

        def has_pending(key) -> bool:
            if tag is None:
                return True
            for entry in self._groups_by_key.get(key, []):
                if (self._state.get_decision(entry.image_path, tag)
                        == Decision.UNPROCESSED):
                    return True
            return False

        first = None
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            key = item.data(GROUP_ROLE)
            if key is None:
                continue
            if first is None:
                first = (item, key)
            if has_pending(key):
                first = (item, key)
                break

        if first is None:
            return False
        item, key = first
        self._selected_header_key = key
        self.list_widget.blockSignals(True)
        try:
            self.list_widget.setCurrentItem(item)
        finally:
            self.list_widget.blockSignals(False)
        self.list_widget.scrollToItem(item)
        self._highlight_selected_header()
        if self._image_panel is not None:
            self._image_panel.show_group_preview(
                self.selected_group_preview())
        return True

    def _group_key_of_image(self, image_path) -> Optional[tuple]:
        """Return the (subfolder, base) group key an image belongs to, or
        None if the image isn't part of any detected group.

        O(1) via the reverse index built during grouping. This used to
        scan every group and member, and it runs twice per
        image-to-image step, so on a large grouped set it was two full
        scans per keypress."""
        return getattr(self, "_group_key_by_image", {}).get(image_path)

    def _enforce_group_boundary(self, prev_path, new_path) -> bool:
        """Group auto-advance boundary rule (Feature B).

        When walking image-by-image INSIDE a group, deciding the last
        member must NOT carry the walk across into the next group's first
        image. If the walk just advanced from an image in group A to an
        image in a DIFFERENT group B (or to an ungrouped image), and the
        previous image was the LAST member of group A, snap the walk back
        to that last member so it stops at the boundary.

        Returns True if it corrected (snapped back), False otherwise.

        Only applies in grouping mode and only when the previous image
        was inside an expanded group. Crossing between ungrouped images,
        or normal movement within the same group, is left alone.
        """
        if not self._grouping_enabled or self._state is None:
            return False
        if prev_path is None or new_path is None:
            return False
        prev_key = self._group_key_of_image(prev_path)
        if prev_key is None:
            return False  # previous wasn't in a group — no boundary
        new_key = self._group_key_of_image(new_path)
        if new_key == prev_key:
            return False  # still within the same group — fine
        # We crossed out of group A. Only stop if prev was the LAST member
        # of group A (i.e. the user was at the group's end and an advance
        # would leave it). If prev wasn't the last member, the crossing
        # shouldn't happen during normal in-group walking, but guard
        # anyway.
        members = self._groups_by_key.get(prev_key, [])
        if members and members[-1].image_path == prev_path:
            prev_expanded = prev_key in self._expanded_bases
            # EXPANDED group (report item O): HARD stop. The user is
            # reviewing members one by one, so deciding the last member
            # must never auto-cross into the next group — every repeated
            # decision keeps the walk parked on that last image. To move
            # on they act deliberately: click the next group/image, or a
            # group batch Yes/No (both jump the walk directly and bypass
            # this stop, which fires only on decision-driven advance).
            #
            # COLLAPSED group: gentler stop-once-then-pass. Members aren't
            # individually visible, so we stop a single time at the
            # boundary, then a further decision is allowed through — this
            # avoids parking a user speeding through collapsed groups.
            if not prev_expanded and self._boundary_stopped_at == prev_path:
                # Collapsed group, already stopped once: allow passage.
                self._boundary_stopped_at = None
                return False
            queue = self._state.get_current_queue()
            for i, e in enumerate(queue):
                if e.image_path == prev_path:
                    self._boundary_stopped_at = prev_path
                    self._state.jump_to_queue_index(i)
                    return True
        return False

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL,
        )
        layout.setSpacing(Spacing.NORMAL)

        # Header label.
        header = QLabel("Queue")
        header.setProperty("role", "heading")
        layout.addWidget(header)

        # The filter bar (lives inside the queue panel for compactness).
        self.filter_bar = FilterBar(self)
        layout.addWidget(self.filter_bar)

        # Search row + jump-to-pending button. The search input filters
        # the visible queue by filename substring; walk advances only
        # through filtered images. The jump button finds the first
        # pending (UNPROCESSED) image in the current queue and jumps
        # the walk there — used when the user loses track of their
        # position. Mini button: this is occasional-use, shouldn't
        # demand attention.
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(Spacing.TIGHT)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Filter by name or tags (e.g. 1boy, 1girl)…"
        )
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setToolTip(
            "Filter the queue by filename or tag.\n"
            "-tag = exclude (e.g. -long_hair)\n"
            "~tag, ~tag = any of (e.g. ~smile, ~light_smile)\n"
            "tag, tag = all of.  Spaces match underscores.\n"
            "Tip: multi-select in the tag tree = has-ALL / has-NONE "
            "filters."
        )
        self.search_input.textChanged.connect(self._on_search_changed)
        search_row.addWidget(self.search_input, 1)
        # Exact-match toggle: flips the search box between partial
        # (substring of name/tag — the default) and exact tag matching,
        # so "hand" can be made to match only the tag "hand", not
        # "hand_on_hip". Comma-separated queries stay exact regardless.
        self.btn_exact = QToolButton()
        self.btn_exact.setCheckable(True)
        self.btn_exact.setText("=")
        self.btn_exact.setToolTip(
            "Exact tag match.\n"
            "On: matches only images whose tag is exactly what you typed "
            "(\"hand\" finds \"hand\", not \"hand_on_hip\").\n"
            "Off (default): matches partial text in filenames and tags.\n"
            "Comma-separated lists are always matched exactly."
        )
        self.btn_exact.setFixedWidth(28)
        self.btn_exact.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_exact.setStyleSheet(
            f"QToolButton {{ border: 1px solid transparent; "
            f"border-radius: 3px; }}"
            f"QToolButton:checked {{ background-color: {Colors.ACCENT_BLUE}; "
            f"border: 1px solid {Colors.ACCENT_BLUE}; }}"
        )
        self.btn_exact.toggled.connect(self._on_exact_toggled)
        search_row.addWidget(self.btn_exact)
        # Lock toggle (#14): keep the current search filter and selected
        # image position even when the active tag changes, for quickly
        # inspecting the same image(s) across different tags.
        self.btn_lock = QToolButton()
        self.btn_lock.setCheckable(True)
        self.btn_lock.setText("\U0001F513")  # open padlock (unlocked default)
        self.btn_lock.setToolTip(
            "Lock the current image filter and position across tag "
            "changes.\nUseful for inspecting the same image(s) under "
            "different tags. Click again to unlock."
        )
        self.btn_lock.setFixedWidth(28)
        self.btn_lock.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Clear checked-state visual: when engaged, the button gets an
        # accent background + border and a border so it's obviously "on"
        # rather than the near-invisible default pressed look. Without
        # this the lock state was very hard to read.
        self.btn_lock.setStyleSheet(
            f"QToolButton {{ border: 1px solid transparent; "
            f"border-radius: 3px; }}"
            f"QToolButton:checked {{ background-color: {Colors.ACCENT_BLUE}; "
            f"border: 1px solid {Colors.ACCENT_BLUE}; }}"
        )
        self.btn_lock.toggled.connect(self._on_lock_toggled)
        search_row.addWidget(self.btn_lock)
        # Grouping toggle (Feature B): flip the flat queue into collapsible
        # name-pattern groups (base_(0), base_(1), … families produced by
        # Windows multi-rename). Off by default.
        self.btn_group = QToolButton()
        self.btn_group.setCheckable(True)
        self.btn_group.setText("\U0001F5C2")  # card-index-dividers glyph
        self.btn_group.setToolTip(
            "Group images by name pattern.\n"
            "Files renamed together in Windows (name_(0), name_(1), …) "
            "are grouped so you can decide a whole set at once.\n"
            "Click again to return to a flat list."
        )
        self.btn_group.setFixedWidth(28)
        self.btn_group.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_group.setStyleSheet(
            f"QToolButton {{ border: 1px solid transparent; "
            f"border-radius: 3px; }}"
            f"QToolButton:checked {{ background-color: {Colors.ACCENT_BLUE}; "
            f"border: 1px solid {Colors.ACCENT_BLUE}; }}"
        )
        self.btn_group.toggled.connect(self._on_group_toggled)
        search_row.addWidget(self.btn_group)
        # Jump button: small, low-visual-weight. Tooltip explains.
        # Standard Qt icon would change with theme, but a glyph keeps
        # it consistent across themes.
        self.btn_jump_pending = QPushButton("\u23ED")  # "next track" glyph
        self.btn_jump_pending.setToolTip(
            "Jump to next pending image\n(first image without a decision yet)"
        )
        # Fixed small size — same height as the search input.
        self.btn_jump_pending.setFixedWidth(28)
        self.btn_jump_pending.clicked.connect(self._on_jump_pending_clicked)
        search_row.addWidget(self.btn_jump_pending)

        # Zero-result diagnosis: when an active search matches nothing,
        # a silent empty list is undiagnosable (field report: an
        # impostor tag — near-typo or invisible Unicode — looked like
        # the searched tag but wasn't it). This label names the nearest
        # real tags so the culprit is visible immediately. Hidden
        # whenever there are results or no search.
        self.search_hint = QLabel("")
        self.search_hint.setWordWrap(True)
        self.search_hint.setStyleSheet(
            f"color: {Colors.WARNING_AMBER}; font-style: italic;"
        )
        self.search_hint.setVisible(False)
        layout.addLayout(search_row)
        layout.addWidget(self.search_hint)

        # The actual list. uniformItemSizes=True is a Qt optimization
        # hint that lets the widget skip per-item size queries — speeds
        # up large lists noticeably.
        self.list_widget = _QueueListWidget()
        # Wheel-nav modifier is read live from Settings on every
        # wheel event, so a change in Preferences applies instantly.
        self._app_settings = Settings()
        self.list_widget.set_wheel_nav_modifier_getter(
            lambda: self._app_settings.wheel_nav_modifier
        )
        # Classic up/down arrow buttons on the queue's scrollbar (the
        # global theme hides them). Applied to the scrollbar object, not
        # the list widget, so the per-row state colors are untouched.
        self.list_widget.verticalScrollBar().setStyleSheet(
            scrollbar_with_arrows_qss()
        )
        self.list_widget.setUniformItemSizes(True)
        # A bar down the left edge of the current row: it survives the
        # green/red/amber decision backgrounds and Qt's own selection
        # highlight, which a background tint cannot.
        self._accent_delegate = AccentBarDelegate(self.list_widget)
        self.list_widget.setItemDelegate(self._accent_delegate)

        # Right-click a row for its properties. Explorer-shaped,
        # because that is the gesture people already have for "tell me
        # about this file".
        self.list_widget.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(
            self._show_row_menu)
        # Extended selection: plain click selects single, Ctrl+click
        # toggles, Shift+click range-selects. Enables batch operations
        # (B6) — the user can multi-select rows and press Yes/No to
        # apply to all of them.
        self.list_widget.setSelectionMode(
            self.list_widget.SelectionMode.ExtendedSelection
        )
        # Disable Qt's automatic scroll-current-into-view. setCurrentItem
        # otherwise scrolls on its own, on TOP of our explicit
        # scrollToItem — and that auto-scroll ignored our click
        # suppression, recentering the viewport on an off-center click and
        # desyncing the highlight. With autoScroll off, ALL scrolling goes
        # through our guarded scrollToItem in _refresh_current_indicator.
        self.list_widget.setAutoScroll(False)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.itemDoubleClicked.connect(
            self._on_item_double_clicked
        )
        # Also react to the CURRENT item changing via the keyboard
        # (arrow keys, Page Up/Down, Home/End). Without this, keyboard
        # navigation moved only Qt's highlight while the walked image
        # stayed put — the displayed image didn't follow the selection.
        # Programmatic selection syncs block signals, so this only fires
        # on genuine user navigation.
        self.list_widget.currentItemChanged.connect(
            self._on_current_item_changed
        )
        # Event filter on the viewport: a MOUSE PRESS sets the
        # scroll-suppression flag BEFORE the resulting currentItemChanged
        # (and its indicator refresh) runs, so a click never recenters the
        # viewport under the cursor (the highlight-jump bug). The flag is
        # cleared on the next event-loop turn, so subsequent programmatic
        # navigation scrolls normally. Keyboard navigation never sets it.
        self.list_widget.viewport().installEventFilter(self)
        layout.addWidget(self.list_widget, 1)

        # Position label at the bottom. It must NOT dictate the panel's
        # minimum width — a long status string (e.g. dataset-scaled group
        # counts) was forcing the queue panel un-shrinkable. Allow it to
        # shrink below its text width; Qt will elide. A tiny minimum width
        # plus Ignored horizontal size policy detaches its text length
        # from the panel's minimum size.
        self.status_label = QLabel("No tag selected")
        self.status_label.setProperty("role", "tertiary")
        self.status_label.setMinimumWidth(0)
        sp = self.status_label.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self.status_label.setSizePolicy(sp)
        layout.addWidget(self.status_label)

    def _on_lock_toggled(self, checked: bool) -> None:
        """Toggle the cross-tag search/position lock (#14). Swaps the
        glyph (open/closed padlock) in addition to the checked-state
        background so the state is unmistakable."""
        self.btn_lock.setText("\U0001F512" if checked else "\U0001F513")
        if self._state is not None:
            self._state.set_search_locked(checked)

    def _on_exact_toggled(self, checked: bool) -> None:
        """Toggle exact-tag matching for the queue search box. The state
        re-filters the queue if a search is active and emits
        filter_changed, which our listener turns into a list rebuild."""
        if self._state is None or self._updating:
            return
        self._state.set_queue_search_exact(checked)

    # ------------------------------------------------------------------
    # State change listener (state → widget)
    # ------------------------------------------------------------------

    def _on_state_change(self, change: StateChange) -> None:
        """Route state-change notifications to the appropriate handler.

        Rebuild events that change the queue shape:
        - tag_selected     : new tag, new queue
        - filter_changed   : same tag, possibly different visible images
        - tree_rebuilt     : a tag was deleted; current tag may be gone

        Restyle events that don't change the queue shape:
        - walk_advanced    : current row moved; restyle to refresh icons
        - image_changed    : one image's decision/tags changed; restyle it
        - walk_ended       : if extra="end_of_tree", clear list entirely
        """
        kind = change.kind
        if kind in ("tag_selected", "filter_changed", "tree_rebuilt"):
            # A sort/filter/tag change re-drives the queue. In group mode,
            # an active group-header selection is normally stale after
            # such a change, so we clear it and let the highlight follow
            # the walked image. EXCEPTION: when the lock is engaged on a
            # tag change, the user is deliberately holding their place
            # across tags, so we keep the selected group (it's restored
            # after the rebuild in _rebuild_list/_refresh). filter_changed
            # (sort/filter) always clears it — the queue genuinely
            # reorders/filters there.
            locked = (
                hasattr(self, "btn_lock") and self.btn_lock.isChecked()
            )
            keep_header = (
                self._grouping_enabled
                and kind == "tag_selected"
                and locked
                and self._selected_header_key is not None
            )
            if (self._grouping_enabled and not keep_header
                    and kind in ("filter_changed", "tag_selected")):
                if self._selected_header_key is not None:
                    self._selected_header_key = None
                    if self._image_panel is not None:
                        self._image_panel.show_group_preview(None)
            self._sync_search_box()
            self._rebuild_list()
            # If we kept a header selection under lock, re-assert it after
            # the rebuild so its highlight + preview are restored.
            if keep_header:
                self._select_header_after_rebuild(self._selected_header_key)
            elif (self._grouping_enabled
                  and kind in ("tag_selected", "filter_changed")):
                # The header was just cleared above. Pick the first
                # group of the new tag, or the walk silently reverts to
                # single images.
                self.select_first_pending_group()
        elif kind == "walk_advanced":
            # Group-boundary stop (Feature B): only for DECISION-driven
            # auto-advance (extra="auto_advance"). Manual jumps/clicks/nav
            # also emit walk_advanced but must NEVER be blocked — that's
            # how the user deliberately crosses a boundary after a stop.
            if self._grouping_enabled and self._state is not None:
                new_img = self._state.current_image
                new_path = new_img.image_path if new_img is not None else None
                if (change.extra == "auto_advance"
                        and self._enforce_group_boundary(
                            self._prev_walk_path, new_path)):
                    # Snapped back: the walk is now parked on the boundary
                    # image, NOT new_path. Track THAT as prev so a further
                    # decision is recognised as crossing the boundary again
                    # (this is what makes the expanded-group HARD stop hold,
                    # and the collapsed stop-once fire correctly). Without
                    # it, prev moved past the boundary and the next decision
                    # sailed straight through.
                    cur = self._state.current_image
                    self._prev_walk_path = (
                        cur.image_path if cur is not None else new_path
                    )
                    return
                self._prev_walk_path = new_path
            self._refresh_current_indicator()
        elif kind == "image_changed":
            if change.image_path is not None:
                if self._grouping_enabled:
                    # A single-image decision change (e.g. an UNDO of one
                    # image, or a single Yes/No while an image is the
                    # active view) should make the highlight follow that
                    # image — not stay stuck on a previously-selected
                    # group header. So if a header was selected but the
                    # walk is now on an image, release the header so
                    # _refresh_current_indicator tracks the walked image.
                    # (A GROUP batch decide emits tag_changed, not
                    # image_changed, so this branch won't fire for it and
                    # the header stays selected for group auto-advance.)
                    if (self._selected_header_key is not None
                            and self._state is not None
                            and self._state.current_image is not None):
                        self._selected_header_key = None
                        if self._image_panel is not None:
                            self._image_panel.show_group_preview(None)
                    self._rebuild_list()
                else:
                    self._restyle_one(change.image_path)
                    # Also flag it for the next indicator refresh: the
                    # walk usually moves on, so this row must be
                    # repainted without its current-row styling.
                    self.note_row_dirty(change.image_path)
        elif kind == "queue_rebuilt":
            # The state re-filtered itself — after a batch decision
            # under a membership filter, the rows that no longer match
            # must go.
            self._rebuild_list()
        elif kind == "tag_changed":
            # A tag's state changed across (potentially) many images —
            # e.g. a GROUP BATCH DECIDE, which emits tag_changed (not
            # image_changed). In grouping mode the group-header aggregate
            # colors depend on those decisions, so rebuild to recolor them
            # immediately. (Without this, headers only recolored on the
            # next unrelated rebuild, e.g. expanding — the reported bug.)
            # FIELD BUG: flat mode did nothing here, on the reasoning
            # that a tag decision does not change which images are in
            # the queue. That holds only with no filter.
            #
            # Batch-answering Yes while filtered to "images WITHOUT
            # this tag" makes every one of them a match for the
            # opposite filter: they should leave the list at once. The
            # captions were written correctly, but the rows sat there
            # unstyled and unticked, which reads as the operation
            # having silently failed.
            if self._grouping_enabled:
                self._rebuild_list()
            elif self._filter_depends_on_tag():
                self._rebuild_list()
            else:
                # Membership is unchanged, but the colours and marks
                # are not. Restyling every row is O(queue), which is
                # fine here: a batch decision is a deliberate act, not
                # something that happens on every keypress.
                self._restyle_all_rows()
        elif kind == "walk_ended":
            self._sync_search_box()
            if change.extra == "end_of_tree":
                self._rebuild_list()
            else:
                self._refresh_current_indicator()

    # ------------------------------------------------------------------
    # Building and clearing the list
    # ------------------------------------------------------------------

    def _clear_list(self) -> None:
        """Remove all items. Used on detach or when no tag is selected."""
        self.list_widget.clear()
        self._items_by_path.clear()
        self._displayed_tag = None
        self.status_label.setText("No tag selected")

    def _refresh_search_hint(self) -> None:
        """Show a diagnosis under the search box when an active search
        matches nothing: name the nearest real tags (with counts), so an
        impostor tag — a near-typo or one carrying invisible characters —
        is exposed instead of leaving a silently empty list."""
        if self._state is None:
            self.search_hint.setVisible(False)
            return
        query = self._state.queue_search
        if not query or self._state.get_current_queue():
            self.search_hint.setVisible(False)
            return
        near = self._state.suggest_similar_tags(query, limit=3)
        if near:
            names = ",  ".join(f"{t}  ({n})" for t, n in near)
            self.search_hint.setText(
                f"No matches for \u201c{query}\u201d. Similar tags in this "
                f"dataset: {names}"
            )
        else:
            self.search_hint.setText(
                f"No matches for \u201c{query}\u201d \u2014 no similar tag "
                "exists in this dataset."
            )
        self.search_hint.setVisible(True)

    def _rebuild_list(self) -> None:
        """Recompute and repopulate the entire list from current state.

        Cost: O(N) where N is queue size. The cost is dominated by
        QListWidget's internal layout, not our per-item work. For
        N <= 5000, this completes in well under 100 ms.

        We block signals during the rebuild — Qt fires currentRowChanged
        signals during clear/add cycles that we don't want to react to
        (they're not user actions).
        """
        if self._state is None:
            self._clear_list()
            return

        self.list_widget.blockSignals(True)
        try:
            self.list_widget.clear()
            self._items_by_path.clear()

            queue = self._state.get_current_queue()
            current_tag = self._state.current_tag
            multi = self._state.multi_select_mode
            browse = self._state.browse_mode

            # Bail to an empty list only when there's genuinely nothing to
            # show. Multi-select and browse mode both run with current_tag
            # None by design, so a None tag must not clear the list in
            # either — only a truly empty queue does. In single-tag mode,
            # a None tag still means "no tag selected / end of walk".
            if not queue or (current_tag is None and not multi
                             and not browse):
                # _displayed_tag is intentionally left at its previous
                # value here. If the walk just ended via tag-complete,
                # _rebuild_list isn't called (only _refresh_current_
                # indicator is), so this clear branch only runs on a
                # real "no tag at all" event — at which point clearing
                # the displayed tag is correct.
                self._displayed_tag = None
                if multi:
                    # Two different "empty" situations need two different
                    # nudges: nothing ticked yet (guide them to tick), vs
                    # ticked but nothing matches the active filter.
                    if not self._state.get_selected_tags():
                        self.status_label.setText(
                            "Tick one or more tags to find images"
                        )
                    else:
                        self.status_label.setText(
                            "No images match the ticked tags"
                        )
                elif browse:
                    # Browsing a folder, but nothing to show — the only
                    # way this happens is the show-without-txt toggle
                    # hiding uncaptioned images when every image here is
                    # uncaptioned. Say so instead of "Tree walk complete"
                    # or "No tag selected" (both true-ish but unhelpful
                    # mid-browse).
                    self.status_label.setText(
                        "No captioned images here \u2014 enable "
                        "\u201cshow images without .txt\u201d to see them"
                    )
                elif current_tag is None and self._state.all_tags:
                    self.status_label.setText(
                        f"{Icons.DONE} Tree walk complete"
                    )
                elif self._state.queue_search:
                    # A tag IS selected; the active search emptied the
                    # queue. Say so (the old text claimed "No tag
                    # selected", which is false and undiagnosable) and
                    # let the hint label below name the nearest real
                    # tags.
                    self.status_label.setText("0 images match the search")
                else:
                    self.status_label.setText("No tag selected")
                self._refresh_search_hint()
                return

            # Real queue: single-tag walk, or multi-select matches.
            # In multi-select mode _displayed_tag stays None (there's no
            # single tag); _apply_style and the indicator handle that.
            self._displayed_tag = current_tag

            if self._grouping_enabled:
                self._populate_grouped(queue)
            else:
                for entry in queue:
                    item = QListWidgetItem()
                    item.setData(IMAGE_ENTRY_ROLE, entry)
                    self.list_widget.addItem(item)
                    self._items_by_path[entry.image_path] = item
                    self._apply_style(item, entry)

            self._refresh_current_indicator()
            # Grouping status: keep it SHORT and fixed-length. A status
            # string whose length scales with dataset size (group counts,
            # pending counts) was forcing the panel's minimum width wide
            # enough to show the whole line — so a large dataset made the
            # queue panel un-shrinkable. A terse constant avoids that.
            if self._grouping_enabled:
                if not self._groups_by_key:
                    self.status_label.setText(
                        "No name-pattern groups found"
                    )
                else:
                    self.status_label.setText("Grouped view")
        finally:
            self.list_widget.blockSignals(False)
        # After every rebuild (flat or grouped), refresh the zero-result
        # search diagnosis — it depends on the queue we just rendered.
        self._refresh_search_hint()

    # ------------------------------------------------------------------
    # Name-pattern grouping (Feature B)
    # ------------------------------------------------------------------

    def _on_group_toggled(self, checked: bool) -> None:
        """Toggle name-pattern grouping. Rebuilds the list in the chosen
        mode. uniformItemSizes is disabled while grouping because group
        headers and image rows differ in height.

        Grouping is a VIEW over the same walk/queue, not a separate mode:
        the queue it groups is already filtered and sorted by the state,
        so Filter (All / with tag / without tag) keeps working — a
        filtered queue simply yields groups built from the visible images.
        Only the tag-count sort options are hidden in group mode (they
        don't define a group order); A→Z / Z→A reorder the groups."""
        self._grouping_enabled = checked
        self.list_widget.setUniformItemSizes(not checked)
        # Tell the filter bar to show only the sort options that make
        # sense for grouping (A-Z / Z-A), but keep Filter fully usable.
        self.filter_bar.set_grouping_mode(checked)
        # Hide the image panel's "tag present / not present" indicator in
        # group mode — a group's images can be mixed, so a single
        # presence flag is misleading.
        if self._image_panel is not None:
            self._image_panel.set_presence_indicator_visible(not checked)
        if not checked:
            # Leaving grouping mode: return the center panel to the normal
            # single-image view (we may have been showing a group's grid).
            if self._image_panel is not None:
                self._image_panel.show_group_preview(None)
            self._boundary_stopped_at = None
            self._selected_header_key = None
        self._rebuild_list()
        if checked:
            # Arriving in group mode must START the group walk. Without
            # this the list was grouped but the walk stayed on single
            # images until a header happened to be clicked.
            self.select_first_pending_group()

    def _populate_grouped(self, queue: list) -> None:
        """Render the queue as collapsible name-pattern groups followed
        by ungrouped images.

        Grouping is computed PER SUBFOLDER (a group never crosses
        folders). Within each subfolder, images matching base_(n) cluster
        into groups (2+ members); the rest are ungrouped. The display
        order is: for each subfolder in queue order, its groups (sorted
        by base) then its ungrouped images, preserving the queue's
        original ordering as much as possible.

        Group header rows carry the (subfolder, base) key; image rows
        carry their ImageEntry. Expanded groups show their image children
        indented beneath the header; collapsed groups show only the
        header.
        """
        from core.name_grouping import group_images
        from core.state import SortMode

        self._groups_by_key = {}
        # Reverse index: image path -> its group key. Built as the
        # groups are, so looking up which group an image belongs to is
        # O(1). Without it, _group_key_of_image scanned every group and
        # every member on each call, and it is called twice per
        # image-to-image navigation step — so on a large grouped set
        # arrow-keying did two full-dataset scans per keypress.
        self._group_key_by_image = {}

        # Determine group ordering direction from the state's sort mode.
        # Only A-Z / Z-A are meaningful for ordering groups; the tag-count
        # sorts don't define a group order, so they fall back to A-Z.
        descending = (
            self._state is not None
            and self._state.sort_mode == SortMode.ALPHA_DESC
        )

        # Partition the queue by subfolder while preserving first-seen
        # subfolder order, then order subfolders by name in the chosen
        # direction so the whole grouped view follows A-Z / Z-A.
        by_sub: dict[str, list] = {}
        for entry in queue:
            sub = getattr(entry, "subfolder", "")
            by_sub.setdefault(sub, []).append(entry)
        sub_order = sorted(by_sub.keys(), key=str.lower, reverse=descending)

        for sub in sub_order:
            items = by_sub[sub]
            groups, ungrouped = group_images(
                items, sub, lambda e: _stem_of(e.image_path)
            )
            # Order this subfolder's groups by base in the chosen
            # direction (group_images returns them base-ascending).
            groups = sorted(
                groups, key=lambda g: g.base.lower(), reverse=descending
            )
            for g in groups:
                key = (g.subfolder, g.base)
                self._groups_by_key[key] = list(g.members)
                for member in g.members:
                    self._group_key_by_image[member.image_path] = key
                self._add_group_header(key, g.base, g.members)
                if key in self._expanded_bases:
                    for entry in g.members:
                        self._add_image_row(entry, indented=True)
            for entry in ungrouped:
                self._add_image_row(entry, indented=False)

        # If a previously-selected header no longer exists (group
        # dissolved, filter changed), drop the stale selection.
        if (self._selected_header_key is not None
                and self._selected_header_key not in self._groups_by_key):
            self._selected_header_key = None

        # If grouping is on but nothing grouped, tell the user why — the
        # feature needs the Windows multi-rename filename format.
        if not self._groups_by_key:
            self.status_label.setText(
                "No name-pattern groups found. Grouping needs files named "
                "name_(0), name_(1), … (Windows multi-rename format)."
            )

    def _add_group_header(self, key, base: str, members) -> None:
        """Add a group-header row for (subfolder, base)."""
        item = QListWidgetItem()
        item.setData(GROUP_ROLE, key)
        expanded = key in self._expanded_bases
        item.setData(GROUP_EXPANDED_ROLE, expanded)
        arrow = "\u25be" if expanded else "\u25b8"  # ▾ open / ▸ closed
        n = len(members)
        item.setText(f"{arrow}  {base}   ({n} images)")
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setData(IMAGE_ENTRY_ROLE, None)
        self.list_widget.addItem(item)
        # Style the header to reflect the group's aggregate decision
        # state for the current tag (all-yes / all-no / mixed / pending).
        self._style_group_header(item, members)

    def _add_image_row(self, entry, indented: bool) -> None:
        """Add a normal image row (optionally indented under a group)."""
        item = QListWidgetItem()
        item.setData(IMAGE_ENTRY_ROLE, entry)
        self.list_widget.addItem(item)
        self._items_by_path[entry.image_path] = item
        self._apply_style(item, entry)
        if indented:
            # Prefix with spaces for a simple indent (uniformItemSizes is
            # already off in grouping mode, so this is safe).
            item.setText("      " + item.text())

    def _style_group_header(self, item, members) -> None:
        """Color a group header by its members' aggregate decision state
        for the current tag: all decided same way, mixed, or pending."""
        if self._state is None or self._displayed_tag is None:
            return
        tag = self._displayed_tag
        decisions = set()
        for entry in members:
            decisions.add(
                self._state.get_decision(entry.image_path, tag)
            )
        # Aggregate: drive the header background subtly.
        if decisions == {Decision.YES}:
            item.setForeground(QColor(Colors.SUCCESS_GREEN))
        elif decisions == {Decision.NO}:
            item.setForeground(QColor(Colors.DANGER_RED))
        elif Decision.UNPROCESSED in decisions and len(decisions) == 1:
            item.setForeground(QColor(Colors.TEXT_PRIMARY))
        else:
            # Mixed.
            item.setForeground(QColor(Colors.WARNING_AMBER))

    def _restyle_one(self, image_path: Path) -> None:
        """Re-apply styling to a single row. O(1) via the lookup dict.

        Called when one image's decision or tags changed. Doesn't touch
        other rows.
        """
        item = self._items_by_path.get(image_path)
        if item is None:
            return  # image isn't in current queue
        entry = item.data(IMAGE_ENTRY_ROLE)
        if isinstance(entry, ImageEntry):
            self._apply_style(item, entry)

    def _multi_filter_description(self) -> str:
        """Plain-language description of what the multi-select queue is
        showing, so Has-vs-Missing isn't something the user has to reason
        out from a dropdown. Kept concise — the status label elides on a
        narrow panel, and the 'X of N' position is shown first so it never
        gets pushed out."""
        from core.state import FilterMode
        mode = self._state.filter_mode if self._state is not None else None
        if mode == FilterMode.MISSING_TAG:
            return "missing the ticked tags"
        if mode == FilterMode.HAS_TAG:
            return "have all the ticked tags"
        if mode == FilterMode.SKIPPED_ONLY:
            return "skipped for the ticked tags"
        return "all images"

    def _show_row_menu(self, pos) -> None:
        """Context menu for one queue row."""
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        entry = item.data(IMAGE_ENTRY_ROLE)
        if not isinstance(entry, ImageEntry):
            # A group header, which has no single file to describe.
            return
        menu = QMenu(self.list_widget)
        act_props = menu.addAction("Properties…")
        act_meta = menu.addAction("Meta info…")
        chosen = menu.exec(self.list_widget.mapToGlobal(pos))
        if chosen is act_props:
            self._show_properties(entry)
        elif chosen is act_meta:
            self._show_metadata(entry)

    def _show_properties(self, entry) -> None:
        from ui.image_properties_dialog import ImagePropertiesDialog

        dialog = ImagePropertiesDialog(entry, self._state, self)
        dialog.exec()

    def _show_metadata(self, entry) -> None:
        # Separate from Properties on purpose: generation metadata is
        # often dense (full prompt + settings, or a whole ComfyUI graph),
        # so it gets its own focused dialog rather than crowding the
        # properties sheet.
        from ui.image_metadata_dialog import ImageMetadataDialog

        dialog = ImageMetadataDialog(entry, self)
        dialog.exec()

    def _filter_depends_on_tag(self) -> bool:
        """True when the current filter decides membership by whether
        an image carries the current tag."""
        from core.state import FilterMode

        if self._state is None:
            return False
        return self._state.filter_mode in (
            FilterMode.HAS_TAG, FilterMode.MISSING_TAG,
            FilterMode.SKIPPED_ONLY)

    def _restyle_all_rows(self) -> None:
        """Repaint every row. For batch operations only."""
        for _path, item in self._items_by_path.items():
            entry = item.data(IMAGE_ENTRY_ROLE)
            if isinstance(entry, ImageEntry):
                self._apply_style(item, entry)

    def _restyle_changed_rows(self) -> None:
        """Restyle only the rows whose appearance can have changed.

        MEASURED: this used to restyle EVERY row on every decision.
        A Yes/No changes two rows — the one just decided and the new
        current one — but on a 1,200-image queue it restyled 1,201,
        calling _apply_style 72,060 times per 60 decisions. The cost
        scaled linearly with the dataset: 5 ms per keypress at 100
        images, 27 ms at 1,200, 55 ms at 2,400.

        The module's own docstring predicted it — "tracking previous
        index explicitly would be a future optimization if profiling
        ever shows this as a hotspot" — and profiling did.

        Three rows can change: the row that was current before, the
        row that is current now, and any row whose decision was just
        recorded. The last is tracked by _note_decided(); the first
        two are remembered here. Anything else is repainted by the
        events that actually alter it (a rebuild, a filter change).
        """
        changed = set(self._dirty_rows)
        self._dirty_rows.clear()
        if self._last_current_path is not None:
            changed.add(self._last_current_path)
        current = None
        if self._state is not None and self._state.current_image:
            current = self._state.current_image.image_path
            changed.add(current)
        self._last_current_path = current

        for path in changed:
            item = self._items_by_path.get(path)
            if item is None:
                continue
            entry = item.data(IMAGE_ENTRY_ROLE)
            if isinstance(entry, ImageEntry):
                self._apply_style(item, entry)

    def note_row_dirty(self, image_path) -> None:
        """Mark one row as needing a restyle on the next refresh.

        Called when a decision is recorded: that row's colour and
        prefix change, and it is usually NOT the row that becomes
        current afterwards.
        """
        if image_path is not None:
            self._dirty_rows.add(image_path)

    def _refresh_current_indicator(self) -> None:
        """Update which row is marked as currently viewing.

        Implementation note: rather than tracking "previously current"
        explicitly, we restyle all visible items in the queue. At queue
        sizes we care about (up to a few thousand), per-item restyle is
        cheap (~5 microseconds) so a full pass is still under 50 ms.
        Tracking previous index explicitly would be a future
        optimization if profiling ever shows this as a hotspot.
        """
        if self._state is None:
            return

        # Consume the one-shot scroll-suppression flag set by a mouse
        # press. Using a local and clearing the instance flag immediately
        # means a single indicator refresh honours the suppression and
        # every later (programmatic) refresh scrolls normally — no risk of
        # the flag sticking on.
        suppress_scroll = self._suppress_scroll
        self._suppress_scroll = False
        # One-shot: did the user just click a single image row? If so the
        # group-mode refresh keeps the single image instead of the group
        # grid. Consume it here so it applies to exactly this refresh.
        view_single = self._view_single_image
        self._view_single_image = False
        # When suppressing, snapshot the scrollbar position up front and
        # restore it at the end. Skipping our own scrollToItem isn't
        # enough: Qt's view nudges a partially-clipped current item into
        # view internally when setCurrentItem runs (even with autoScroll
        # off), creeping the viewport a few px per click — which shifts
        # which row sits under the cursor and desyncs the next click.
        # Restoring the exact scroll value pins the viewport so the
        # clicked row stays put.
        _scroll_keep = (
            self.list_widget.verticalScrollBar().value()
            if suppress_scroll else None
        )

        self._restyle_changed_rows()

        # GROUPED MODE selection sync — unified with standard mode.
        #
        # The blue highlight follows the WALKED IMAGE (state.current_image)
        # exactly as in flat mode, EXCEPT when the user has explicitly
        # selected a group header to batch-decide it (_selected_header_key
        # set). A header selection is transient: any walk move (undo,
        # sort, jump-to-pending, clicking an image) clears it elsewhere,
        # so by the time we're here with it still set, the header really
        # is the active selection and we keep the highlight there.
        if self._grouping_enabled and self._selected_header_key is not None:
            self._highlight_selected_header()
            for i in range(self.list_widget.count()):
                it = self.list_widget.item(i)
                if it.data(GROUP_ROLE) == self._selected_header_key:
                    self.list_widget.blockSignals(True)
                    try:
                        self.list_widget.setCurrentItem(it)
                    finally:
                        self.list_widget.blockSignals(False)
                    break
            members = self._groups_by_key.get(self._selected_header_key, [])
            if members and self._displayed_tag is not None:
                yes = no = pend = 0
                for e in members:
                    d = self._state.get_decision(
                        e.image_path, self._displayed_tag
                    )
                    if d == Decision.YES:
                        yes += 1
                    elif d == Decision.NO:
                        no += 1
                    else:
                        pend += 1
                self.status_label.setText(
                    f"Group \u201c{self._selected_header_key[1]}\u201d: "
                    f"{len(members)} image(s) \u2014 "
                    f"{yes} yes, {no} no, {pend} pending"
                )
            # Keep the file-state panel showing this group's summary.
            self._push_group_summary(self._selected_header_key)
            if _scroll_keep is not None:
                self.list_widget.verticalScrollBar().setValue(_scroll_keep)
            return

        # No active header selection: highlight the walked image's row.
        # If the image is hidden inside a COLLAPSED group, do NOT
        # auto-expand it (that caused collapsed groups to spring open on
        # every sort/filter change). Instead select the group's header so
        # the highlight still tracks where the walk is, without disturbing
        # the user's expand/collapse state.
        if self._grouping_enabled:
            cur_img = self._state.current_image
            if cur_img is not None:
                cur_item = self._items_by_path.get(cur_img.image_path)
                if cur_item is not None:
                    self.list_widget.blockSignals(True)
                    try:
                        self.list_widget.setCurrentItem(cur_item)
                    finally:
                        self.list_widget.blockSignals(False)
                    if not suppress_scroll:
                        self.list_widget.force_scroll_to_item(
                            cur_item,
                            QListWidget.ScrollHint.PositionAtCenter,
                        )
                else:
                    # Image is in a collapsed group: highlight the header.
                    key = self._group_key_of_image(cur_img.image_path)
                    if key is not None:
                        for i in range(self.list_widget.count()):
                            it = self.list_widget.item(i)
                            if it.data(GROUP_ROLE) == key:
                                self.list_widget.blockSignals(True)
                                try:
                                    self.list_widget.setCurrentItem(it)
                                finally:
                                    self.list_widget.blockSignals(False)
                                if not suppress_scroll:
                                    self.list_widget.force_scroll_to_item(
                                        it,
                                        QListWidget.ScrollHint.EnsureVisible,
                                    )
                                break
                # Update BOTH centre panel and file-state panel so neither
                # shows stale single-image content after entering group
                # mode / changing filter, sort, or tag. If the walked image
                # belongs to a group, show that GROUP'S preview grid AND the
                # group summary; if it's an ungrouped image, show it as a
                # single image with the normal per-image file state.
                # EXCEPTION: when the user just clicked a single image row
                # to view it, keep the single image + per-image state.
                gkey = self._group_key_of_image(cur_img.image_path)
                in_group = (gkey is not None and gkey in self._groups_by_key)
                group_expanded = in_group and gkey in self._expanded_bases
                # Show the single walked image (not the group's grid) when
                # the user clicked a row, when the image is ungrouped, OR
                # when its group is EXPANDED. In an expanded group the walk
                # sits on one member that's already visible in the list, so
                # the grid preview is wrong (report item P) — the user
                # wants to see the member the walk advanced to. Collapsed
                # groups still show the grid, since their members aren't
                # visible in the list and the header stands in for them.
                if view_single or not in_group or group_expanded:
                    if self._image_panel is not None:
                        self._image_panel.show_group_preview(None)
                    self._clear_group_summary()
                else:
                    members = self._groups_by_key[gkey]
                    if self._image_panel is not None:
                        self._image_panel.show_group_preview(
                            (gkey[1], [e.image_path for e in members])
                        )
                    # Keep the file-state panel's group summary in sync
                    # with the group whose grid is shown.
                    self._push_group_summary(gkey)
            else:
                # No current image (empty/!ended): clear to single view.
                if self._image_panel is not None:
                    self._image_panel.show_group_preview(None)
                self._clear_group_summary()
            if _scroll_keep is not None:
                self.list_widget.verticalScrollBar().setValue(_scroll_keep)
            return

        # ---- FLAT MODE (unchanged) ----
        # Not in group mode: ensure the file-state panel is in its normal
        # per-image view (a leftover group summary would be stale).
        self._clear_group_summary()
        # Scroll the current row into view and sync Qt's native
        # selection to the walked image. Without this, Qt's own
        # selection highlight stays on whatever row the user last
        # clicked, so the visible "selected" row drifts out of sync
        # with the walk position. We block signals around setCurrentItem
        # so it doesn't re-fire itemClicked / currentItemChanged and
        # loop back into the state.
        cur_img = self._state.current_image
        if cur_img is not None:
            cur_item = self._items_by_path.get(cur_img.image_path)
            if cur_item is not None:
                self.list_widget.blockSignals(True)
                try:
                    self.list_widget.setCurrentItem(cur_item)
                finally:
                    self.list_widget.blockSignals(False)
                if not suppress_scroll:
                    self.list_widget.force_scroll_to_item(
                        cur_item,
                        QListWidget.ScrollHint.PositionAtCenter,
                    )
        else:
            # No current image (end of walk / empty queue): clear the
            # native selection so no stale row stays highlighted.
            self.list_widget.blockSignals(True)
            try:
                self.list_widget.clearSelection()
                self.list_widget.setCurrentItem(None)
            finally:
                self.list_widget.blockSignals(False)
        # Status line.
        size = self._state.walk_size
        if size > 0:
            pos = f"{Icons.PIN} {self._state.walk_index + 1} of {size}"
            if self._state.multi_select_mode:
                # Spell out the active filter so the user always knows
                # whether they're looking at images that HAVE or are
                # MISSING the ticked tags.
                self.status_label.setText(
                    f"{pos} \u2014 {self._multi_filter_description()}"
                )
            else:
                self.status_label.setText(pos)
        else:
            self.status_label.setText("Queue is empty")
        if _scroll_keep is not None:
            self.list_widget.verticalScrollBar().setValue(_scroll_keep)

    def _apply_style(self, item: QListWidgetItem, entry: ImageEntry) -> None:
        """Compute and apply the visual style for a single row.

        Precedence (background and main visual signal):
        1. Currently viewing → blue
        2. Orphan (no .txt)  → tinted danger background, italic
        3. Yes-decided       → green
        4. No-decided        → red
        5. Skipped           → amber
        6. Default           → no tint

        The icon prefix follows the same precedence (one icon per row,
        to avoid visual clutter). Orphans that are NOT the current row
        also get italic font as a residual orphan indicator.
        """
        if self._state is None:
            return

        path = entry.image_path
        is_current = (
            self._state.current_image is not None
            and self._state.current_image.image_path == path
        )
        is_orphan = not self._state.has_caption_file(path)
        # Use _displayed_tag, not state.current_tag, because
        # state.current_tag becomes None when the walk ends (e.g.
        # tag-complete stop), but this queue panel is still showing
        # that tag's images. Without this, all rows would lose their
        # decision-based styling the moment the walk ends.
        decision = (
            self._state.get_decision(path, self._displayed_tag)
            if self._displayed_tag is not None
            else Decision.UNPROCESSED
        )

        # ---- Choose background, text-prefix glyph, and presence dot --
        # Two distinct visual channels:
        #   * text-prefix glyph (eye / no-file / check / cross / skip)
        #     for current, orphan, and decided states — colored text
        #   * a separately-colored circle ICON for UNDECIDED rows that
        #     indicates tag presence (green = has tag, red = missing)
        #     while the text stays neutral white
        # Only one channel is active per row: decided/current/orphan use
        # the text glyph; undecided rows use the presence-dot icon.
        bg_color: Optional[str] = None
        icon_glyph: str = ""
        dot_icon: Optional[QIcon] = None
        text_color: str = Colors.TEXT_PRIMARY  # neutral default
        if self._state.multi_select_mode:
            # Multi-select has no single displayed tag, so the old
            # _displayed_tag check always read False and painted every row
            # red — even Has-mode rows that DO carry the ticked tags. A row
            # is "green" when the image has ALL the ticked tags: Has-mode
            # rows go green, Missing-mode rows red, All-mode mixed.
            _sel = self._state.get_selected_tags()
            has_tag_in_image = bool(_sel) and all(
                self._state.is_tag_in_image(path, t) for t in _sel
            )
        else:
            has_tag_in_image = (
                self._displayed_tag is not None
                and self._state.is_tag_in_image(path, self._displayed_tag)
            )
        if is_current:
            # Do NOT paint a manual blue background here. Qt's native
            # selection (synced to the walked image in
            # _refresh_current_indicator) is the single source of the blue
            # highlight. Painting our own ACCENT_BLUE_BG on top created a
            # SECOND, independently-triggered blue that could land on a
            # different row than the native selection during the
            # click→walk_advanced sequence — the "highlight jumps to
            # another image" desync. The eye glyph + bold still mark the
            # current row distinctly without competing with selection.
            icon_glyph = Icons.EYE
            text_color = Colors.TEXT_PRIMARY
        elif is_orphan:
            bg_color = Colors.DANGER_BG
            icon_glyph = Icons.NO_FILE
            text_color = Colors.DANGER_RED
        elif decision == Decision.YES:
            bg_color = Colors.SUCCESS_BG
            icon_glyph = Icons.CHECK
            text_color = Colors.SUCCESS_GREEN
        elif decision == Decision.NO:
            bg_color = Colors.DANGER_BG
            icon_glyph = Icons.CROSS
            text_color = Colors.DANGER_RED
        elif decision == Decision.SKIPPED:
            bg_color = Colors.WARNING_BG
            icon_glyph = Icons.SKIP
            text_color = Colors.WARNING_AMBER
        else:
            # Unprocessed: presence dot as a colored ICON; text neutral.
            dot_icon = _dot_icon(
                Colors.SUCCESS_GREEN if has_tag_in_image else Colors.DANGER_RED
            )
            text_color = Colors.TEXT_PRIMARY

        # ---- Apply background ----
        if bg_color is not None:
            item.setBackground(QBrush(QColor(bg_color)))
        else:
            item.setBackground(QBrush())

        # ---- Icon (presence dot) ----
        # Set for undecided rows, cleared otherwise (so a decided row
        # doesn't keep a stale dot after undo→redo cycles).
        if dot_icon is not None:
            item.setIcon(dot_icon)
        else:
            item.setIcon(QIcon())

        # ---- Foreground (text color) ----
        item.setForeground(QBrush(QColor(text_color)))

        # ---- Font (bold for current, italic for orphan) ----
        font = QFont()
        font.setPointSize(Fonts.SIZE_SMALL)
        font.setBold(is_current)
        font.setItalic(is_orphan)
        item.setFont(font)

        # Marks the row for the accent-bar delegate. Separate from
        # Qt's selection, which tracks the last CLICK and is allowed
        # to point somewhere else entirely.
        item.setData(CURRENT_ROLE, bool(is_current))

        # ---- Text ----
        # Filename without extension keeps the row compact. For
        # subfolders, the subfolder prefix shows alongside the name
        # so users can tell which folder this image belongs to.
        filename = _stem_of(entry.image_path)
        if entry.subfolder:
            display = f"{entry.subfolder}/{filename}"
        else:
            display = filename
        prefix = f"{icon_glyph}  " if icon_glyph else ""
        item.setText(f"{prefix}{display}")

    # ------------------------------------------------------------------
    # User interaction
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 (Qt naming)
        """Suppress the click-recenter scroll. On a mouse press in the
        list viewport we (a) set _suppress_scroll so our own indicator
        refresh skips its scrollToItem, and (b) lock the list's internal
        scrollTo so Qt's own click-nudge can't creep the viewport (the
        few-px shift that moved a different row under the cursor and
        desynced the highlight on off-center clicks). We release the lock
        on the matching release/leave so later deliberate navigation
        scrolls normally."""
        if obj is self.list_widget.viewport():
            et = event.type()
            if et == QEvent.Type.MouseButtonPress:
                self._suppress_scroll = True
                self.list_widget.set_scroll_locked(True)
            elif et in (
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.Leave,
            ):
                self.list_widget.set_scroll_locked(False)
        return super().eventFilter(obj, event)

    def _clear_suppress_scroll(self) -> None:
        self._suppress_scroll = False
        self.list_widget.set_scroll_locked(False)

    def _on_current_item_changed(self, current, previous) -> None:
        """The current row changed — via keyboard arrows/Page/Home/End,
        or programmatically. Drive the walk to the new row so the
        displayed image follows keyboard navigation (the reported bug:
        arrows moved the highlight but not the image).

        Guards:
        - Programmatic selection syncs call setCurrentItem inside
          blockSignals(True), so this handler doesn't fire for them.
        - Ctrl/Shift held → the user is extending a multi-selection for
          a batch op; don't jump the walk (it would collapse the
          selection back to one row). WHEEL navigation is exempt: it
          sets _wheel_nav_in_progress on the list widget, because its
          configured modifier is held as part of the gesture itself.
        - A plain mouse click also triggers this (Qt updates current
          before emitting itemClicked); jumping here is correct and
          _on_item_clicked's jump becomes redundant but harmless because
          jump_to_queue_index to the same index is idempotent.
        - In grouping mode, group-header rows aren't images; landing on
          one via keyboard does not drive the walk (handled by click for
          batch-decide instead).
        """
        if self._state is None or current is None:
            return
        # Group header row: select it (keyboard navigation landed here).
        # Mark it active, highlight it, show its preview, don't walk.
        if current.data(GROUP_ROLE) is not None:
            self._selected_header_key = current.data(GROUP_ROLE)
            self._highlight_selected_header()
            if self._image_panel is not None:
                self._image_panel.show_group_preview(
                    self.selected_group_preview()
                )
            return
        # Image row: clear any header selection and return the center
        # panel to single-image mode.
        self._selected_header_key = None
        if self._image_panel is not None:
            self._image_panel.show_group_preview(None)
        from PySide6.QtWidgets import QApplication
        modifiers = QApplication.keyboardModifiers()
        if (
            not getattr(self.list_widget, "_wheel_nav_in_progress", False)
            and modifiers & (
                Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.ControlModifier
            )
        ):
            # Ctrl/Shift + click/arrows extends a multi-selection for a
            # batch op — don't jump the walk. WHEEL navigation is exempt
            # (flag set by _QueueListWidget.wheelEvent): its modifier is
            # the gesture itself, and the whole point is the image
            # following each tick in real time (field report: the
            # highlight scrolled but the main image never updated,
            # because holding Shift for the wheel tripped this guard).
            return
        index = self._queue_index_of_item(current)
        if index is None:
            return
        # Avoid redundant work if we're already on this image.
        if self._state.current_image is not None:
            cur_item = self._items_by_path.get(
                self._state.current_image.image_path
            )
            if cur_item is current:
                return
        self._state.jump_to_queue_index(index)

    def _queue_index_of_item(self, item: QListWidgetItem):
        """Map a list row to its index in the STATE QUEUE (not the list
        row number, which in grouping mode includes header rows). Returns
        None for non-image rows or if not found."""
        entry = item.data(IMAGE_ENTRY_ROLE)
        if not isinstance(entry, ImageEntry):
            return None
        if self._state is None:
            return None
        queue = self._state.get_current_queue()
        target = entry.image_path
        for i, e in enumerate(queue):
            if e.image_path == target:
                return i
        return None

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        """User clicked a row (single click).

        - Group header: SELECT it — keep it highlighted, show its preview
          grid, and arm it for batch Yes/No. A single click does NOT
          expand/collapse (that's double-click); selecting and expanding
          are deliberately separate so clicking a header to act on the
          whole group doesn't also reshuffle the list.
        - Image row: jump the walk to that image on a plain click. Ctrl/
          Shift clicks are reserved for multi-selection (batch ops).
        """
        if self._state is None:
            return
        key = item.data(GROUP_ROLE)
        if key is not None:
            from PySide6.QtWidgets import QApplication
            modifiers = QApplication.keyboardModifiers()
            if modifiers & (
                Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.ControlModifier
            ):
                return
            self._select_header(key, item)
            return
        # Image row: selecting an image clears any header selection and
        # returns to normal walking.
        from PySide6.QtWidgets import QApplication
        modifiers = QApplication.keyboardModifiers()
        if modifiers & (
            Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.ControlModifier
        ):
            return
        self._selected_header_key = None
        if self._image_panel is not None:
            self._image_panel.show_group_preview(None)
        # The user deliberately clicked a single image row to view it —
        # the next indicator refresh must keep the SINGLE image shown, not
        # snap back to the group's grid preview (which is the default for
        # toggle/filter/sort/tag-change). One-shot.
        self._view_single_image = True
        index = self._queue_index_of_item(item)
        if index is not None:
            self._state.jump_to_queue_index(index)

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        """Double-click on a group header expands/collapses it. (On an
        image row, double-click is a no-op beyond the single-click jump
        that already fired.)

        The expand/collapse PRESERVES the current selection context: if
        this header was the active selection, it stays selected (grid
        preview kept); if an image was the active view, expanding or
        collapsing the group does NOT yank the view to the group preview
        — the image stays shown. (Forcing header re-selection on every
        toggle was the bug where collapsing a group while viewing one of
        its images flipped the centre panel back to the grid.)"""
        if self._state is None:
            return
        key = item.data(GROUP_ROLE)
        if key is None:
            return
        header_was_selected = (self._selected_header_key == key)
        self._toggle_group(key)
        if header_was_selected:
            # Restore this header's selection + preview after the rebuild.
            self._select_header_after_rebuild(key)
        # Otherwise leave selection as-is: the rebuild's
        # _refresh_current_indicator already restored the walked image's
        # selection and the centre panel stays in single-image mode.

    def _select_header(self, key, item) -> None:
        """Mark a group header as the active selection: highlight it,
        show its preview grid, and remember it so the current-row
        indicator won't yank the highlight onto the walked image."""
        self._selected_header_key = key
        # Make the header the current item (so keyboard nav continues
        # from here and the model is robust even when invoked directly).
        self.list_widget.blockSignals(True)
        try:
            self.list_widget.setCurrentItem(item)
        finally:
            self.list_widget.blockSignals(False)
        # Paint the blue 'current' background on the header now.
        self._highlight_selected_header()
        if self._image_panel is not None:
            self._image_panel.show_group_preview(
                self.selected_group_preview()
            )
        self._push_group_summary(key)

    def _push_group_summary(self, key) -> None:
        """Tell the file-state panel to show this group's read-only
        summary (#2a)."""
        if self._file_state_panel is None:
            return
        members = self._groups_by_key.get(key, [])
        tag = self._displayed_tag
        self._file_state_panel.show_group_summary(key[1], members, tag)

    def _clear_group_summary(self) -> None:
        """Return the file-state panel to its normal per-image view."""
        if self._file_state_panel is not None:
            self._file_state_panel.clear_group_summary()

    def _select_header_after_rebuild(self, key) -> None:
        """After a rebuild (e.g. expand/collapse), re-find the header row
        for `key` and restore its selection + highlight. If the group no
        longer exists (dissolved under a new tag's filter), clear the
        selection and fall back to following the walked image."""
        if key not in self._groups_by_key:
            self._selected_header_key = None
            if self._image_panel is not None:
                self._image_panel.show_group_preview(None)
            self._refresh_current_indicator()
            return
        self._selected_header_key = key
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            if it.data(GROUP_ROLE) == key:
                self.list_widget.blockSignals(True)
                try:
                    self.list_widget.setCurrentItem(it)
                finally:
                    self.list_widget.blockSignals(False)
                break
        self._highlight_selected_header()
        if self._image_panel is not None:
            self._image_panel.show_group_preview(
                self.selected_group_preview()
            )

    def _highlight_selected_header(self) -> None:
        """Give the currently-selected group header the blue 'current'
        background, and clear it from any other header."""
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            k = it.data(GROUP_ROLE)
            if k is None:
                continue
            if k == self._selected_header_key:
                it.setBackground(QBrush(QColor(Colors.ACCENT_BLUE_BG)))
            # Non-selected headers keep their aggregate styling (handled
            # in _style_group_header during rebuild); don't clear here.

    def _toggle_group(self, key) -> None:
        """Expand or collapse a group header and rebuild the list."""
        if key in self._expanded_bases:
            self._expanded_bases.discard(key)
        else:
            self._expanded_bases.add(key)
        self._rebuild_list()

    def _on_search_changed(self, text: str) -> None:
        """User edited the search box. Push to state, which rebuilds
        the queue and emits filter_changed; our state listener will
        rebuild the visible list in response. Empty text clears the
        filter and restores the full queue.

        Guarded by _updating so programmatic syncs (when the state
        clears the search on a tag change) don't loop back here.
        """
        if self._state is None or self._updating:
            return
        self._state.set_queue_search(text)

    def _sync_search_box(self) -> None:
        """Set the search box text to match the state's queue_search.

        Called on tag changes, where the state clears the search. The
        _updating guard prevents the resulting textChanged signal from
        bouncing back into set_queue_search and emitting a redundant
        filter_changed.
        """
        if self._state is None:
            return
        desired = self._state.queue_search
        if self.search_input.text() == desired:
            return
        self._updating = True
        try:
            self.search_input.setText(desired)
        finally:
            self._updating = False

    def _on_jump_pending_clicked(self) -> None:
        """Jump to the first UNDECIDED image/group from the top.

        "Pending" here means strictly UNPROCESSED — an image that has been
        given no decision at all. SKIPPED images do NOT count: a skip is a
        deliberate decision to set the image aside, so jump-to-pending must
        not land back on it (the reported bug). YES/NO are decided and also
        excluded.

        In GROUPING mode, selects the first group header that has any
        UNPROCESSED member (so you can batch-decide it), or the first
        UNPROCESSED ungrouped image — whichever comes first in the list.
        In flat mode, jumps the walk to the first UNPROCESSED image.
        """
        if self._state is None or self._state.current_tag is None:
            return
        tag = self._state.current_tag

        if self._grouping_enabled:
            self._jump_pending_grouped(tag)
            return

        queue = self._state.get_current_queue()
        for i, img in enumerate(queue):
            d = self._state.get_decision(img.image_path, tag)
            if d == Decision.UNPROCESSED:
                self._state.jump_to_queue_index(i)
                return
        # Nothing undecided (all YES/NO/SKIPPED) — no jump target. Silent
        # no-op is fine.

    def _jump_pending_grouped(self, tag) -> None:
        """Grouping-mode jump-to-pending: walk the visible rows top to
        bottom; select the first group header whose group has an
        UNPROCESSED member, or the first UNPROCESSED ungrouped image.
        SKIPPED does not count as pending (see _on_jump_pending_clicked)."""
        def is_pending(path):
            return self._state.get_decision(path, tag) == Decision.UNPROCESSED

        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            key = it.data(GROUP_ROLE)
            if key is not None:
                members = self._groups_by_key.get(key, [])
                if any(is_pending(e.image_path) for e in members):
                    # Select this group header (batch-decide target).
                    self._select_header(key, it)
                    self.list_widget.scrollToItem(it)
                    return
            else:
                entry = it.data(IMAGE_ENTRY_ROLE)
                if entry is not None and is_pending(entry.image_path):
                    # Ungrouped pending image: clear header selection and
                    # walk to it.
                    self._selected_header_key = None
                    if self._image_panel is not None:
                        self._image_panel.show_group_preview(None)
                    idx = self._queue_index_of_item(it)
                    if idx is not None:
                        self._state.jump_to_queue_index(idx)
                    self.list_widget.scrollToItem(it)
                    return
        # Nothing undecided — no-op.

    # ------------------------------------------------------------------
    # Enabled state
    # ------------------------------------------------------------------

    def _refresh_enabled(self) -> None:
        has_state = self._state is not None
        self.list_widget.setEnabled(has_state)
