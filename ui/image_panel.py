"""
ui/image_panel.py

Center workspace: shows the current image, presence/orphan status,
progress, and the primary action buttons (Yes / No / Skip / Back /
Skip tag).

Layout (top to bottom):
  ┌──────────────────────────────────────────┐
  │            current_tag_name              │
  │   ✓ Tag present in file                  │
  │   Image 46 of 180 · 134 remaining        │
  │                                          │
  │  ┌──────────────────────────────────┐   │
  │  │                                  │   │
  │  │       [Image preview]            │   │  Click to zoom
  │  │                                  │   │
  │  └──────────────────────────────────┘   │
  │                                          │
  │       [✓ Yes (Y)]    [✗ No (N)]         │
  │   [Skip]  [Back]   [Skip tag]            │
  └──────────────────────────────────────────┘

Click anywhere on the image area opens a zoom dialog that shows the
image at large size, scaled to ~80% of the user's screen.

Image cache
-----------
A small LRU cache (5 entries max) holds the original QPixmap for the
current image and its recent neighbors. Scaling happens at paint time
since the panel size can change with the window. Original-pixmap cost
dominates (disk read + decode); rescale of an in-memory pixmap is ~5 ms
even for 4K images.

Synchronous loading is used throughout. A 4K JPEG loads in ~20-40 ms
which is below the threshold of perceptible lag. If profiling ever
shows otherwise, we'd move loading to a worker thread and emit a
ready-signal back to the main thread.

Missing-file handling
---------------------
If the .png/.jpg has been deleted between scan and display, we show
a placeholder text rather than crashing. The state's walk index
stays put; the user can use Back to step away.

Button enable/disable
---------------------
Yes / No / Skip / Skip-tag enable only when state.current_image is
not None. Back (undo) enables only when state.can_undo() is True.
This is refreshed on every state change.

Keyboard shortcuts
------------------
This widget exposes public action methods (trigger_yes, trigger_no,
trigger_skip, trigger_back, trigger_skip_tag). main_window wires
QShortcut objects to these. Button clicks call the same methods, so
the keyboard and mouse paths are identical.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QRect
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Fonts, Icons, Spacing, repolish
from core.state import SessionState, StateChange


# Max number of original-resolution pixmaps held in memory.
# A typical 4K image is ~30 MB as a QPixmap, so 5 entries is ~150 MB
# in the worst case — acceptable for a desktop dataset tool. Lower if
# memory pressure shows up in testing on real datasets.
IMAGE_CACHE_SIZE = 5

# Default keyboard hints displayed on button labels. These match the
# defaults in config/settings.py. main_window can call
# update_shortcut_labels() to refresh them when the user reassigns
# shortcuts in the Settings dialog.
DEFAULT_SHORTCUT_HINTS: dict[str, str] = {
    "yes":         "Y",
    "no":          "N",
    "skip_image":  "Space",
    "back":        "Bksp",
    "skip_tag":    "",
}


class ClickableImageLabel(QLabel):
    """QLabel that emits a callback on mouse click.

    Used for the main image display so clicking opens the zoom dialog.
    We use a callable attribute rather than a custom Qt signal because
    the consumer is a single, parent widget — no need for the signal
    overhead.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._on_click: Optional[callable] = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_click_handler(self, handler) -> None:
        self._on_click = handler

    def mousePressEvent(self, event) -> None:
        # Only react to left button. Right-click is reserved for any
        # future context menu and ignored for now.
        if event.button() == Qt.MouseButton.LeftButton and self._on_click:
            self._on_click()
        super().mousePressEvent(event)


# The zoom/pan convention lives in ui/zoom_view.py so the post
# inspector can share it verbatim instead of reimplementing it.
from ui.zoom_view import ZoomView as _ZoomView


class ZoomDialog(QDialog):
    """Modal dialog that displays the current image at large size.

    Opens fitted to ~80% of the screen. Mouse wheel zooms in/out
    (anchored under the cursor), left-drag pans when zoomed in.
    Double-click or Esc closes. Ctrl+0 / the "Fit" key resets zoom.
    """

    def __init__(self, pixmap: QPixmap, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Zoom — scroll to zoom, drag to pan, double-click to close")
        self.setModal(True)

        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.resize(int(avail.width() * 0.8), int(avail.height() * 0.8))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem(pixmap)
        self._scene.addItem(self._item)
        self._view = _ZoomView(self._scene, self)
        layout.addWidget(self._view, 1)

        # The same on-screen control hint the image-inspection popup
        # shows, for consistency: the window title already says this,
        # but a title bar is easy to miss and vanishes when maximised,
        # so the guidance is repeated where the eye already is. A little
        # vertical padding keeps it off the image edge.
        hint = QLabel("Scroll to zoom \u00b7 drag to pan \u00b7 "
                      "Ctrl+0 to fit \u00b7 double-click or Esc to close")
        hint.setProperty("role", "tertiary")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setContentsMargins(0, Spacing.TIGHT, 0, Spacing.TIGHT)
        layout.addWidget(hint)

        # Fit the image to the view once the dialog has its size. We
        # defer fitting to showEvent because the view's viewport size
        # isn't final until the dialog is shown.
        self._pixmap_rect = self._item.boundingRect()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Fit on first show.
        self._view.reset_zoom(self._pixmap_rect)

    def mouseDoubleClickEvent(self, event) -> None:
        # Double-click anywhere closes the dialog. Single click is
        # reserved for starting a pan-drag.
        self.close()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.key() == Qt.Key.Key_0 and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            # Ctrl+0 resets zoom to fit.
            self._view.reset_zoom(self._pixmap_rect)
        else:
            super().keyPressEvent(event)


class _SquareThumb(QWidget):
    """A thumbnail tile that always paints as a centered square, filling
    its allocated cell by center-cropping the source image.

    Crucially, it sets only a SMALL minimum size (so it never forces the
    center panel to stay wide — the bug where the window couldn't shrink)
    and paints itself to whatever size the grid gives it. The source
    pixmap is loaded once; painting center-crops and scales it to a
    square each repaint, so tiles stay perfectly square at any panel
    size.
    """

    def __init__(self, path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from PySide6.QtGui import QPixmap
        self._src = QPixmap(str(path))
        self._name = path.name
        # Tiny minimum so the grid (and the whole window) can shrink
        # freely; the tile just gets smaller. This is the fix for the
        # panel refusing to shrink past the old fixed thumbnail size.
        self.setMinimumSize(24, 24)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        from PySide6.QtGui import QPainter, QColor, QPen
        w, h = self.width(), self.height()
        side = min(w, h)
        if side <= 0:
            return  # nothing to paint into (collapsed) — avoid bad state
        painter = QPainter(self)
        if not painter.isActive():
            return
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            ox = (w - side) // 2
            oy = (h - side) // 2
            target = QRect(ox, oy, side, side)
            painter.fillRect(self.rect(), QColor(Colors.BG_INPUT))
            if self._src.isNull():
                painter.setPen(QPen(QColor(Colors.TEXT_TERTIARY)))
                painter.drawText(
                    self.rect(), Qt.AlignmentFlag.AlignCenter, self._name
                )
            else:
                sw, sh = self._src.width(), self._src.height()
                crop = min(sw, sh)
                if crop > 0:
                    sx = (sw - crop) // 2
                    sy = (sh - crop) // 2
                    painter.drawPixmap(
                        target, self._src, QRect(sx, sy, crop, crop)
                    )
            painter.setPen(QPen(QColor(Colors.BORDER_SUBTLE)))
            painter.drawRect(target.adjusted(0, 0, -1, -1))
        finally:
            painter.end()


class GroupPreviewGrid(QWidget):
    """Paged thumbnail grid shown in the center panel when a group header
    is selected (Feature B).

    Shows up to grid_cols*grid_cols thumbnails of the group's images at
    once. Groups larger than one page get ‹ › paging controls. Only the
    current page's thumbnails are held in memory (the previous page's
    pixmaps are dropped on page change), so memory stays bounded
    regardless of group size. Thumbnails are scaled from the source
    images on the calling thread; group sizes are small (one page) so the
    cost is modest, and we only reload on group/page change, never on
    every repaint.

    This is intentionally a STATIC paged grid, not an auto-slideshow:
    no timers, no animation — the user pages manually. That avoids the
    timer-lifecycle fragility of a slideshow and keeps the decision
    moment calm.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._paths: list = []
        self._page = 0
        self._cols = 2  # default 2x2 (largest, cleanest tiles)
        # Match the single-image view's minimum so toggling grouping
        # doesn't change how far the centre workspace can shrink. The
        # single image label uses a 120px min height and no hard min
        # width; mirror that here.
        self.setMinimumSize(0, 120)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.NORMAL)

        # Title / count line.
        self._title = QLabel("")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setProperty("role", "secondary")
        outer.addWidget(self._title)

        # The grid of thumbnails.
        self._grid_host = QWidget()
        self._grid_host.setMinimumSize(0, 0)
        self._grid_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._grid = QGridLayout(self._grid_host)
        self._grid.setSpacing(6)
        self._grid.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._grid_host, 1)

        # Paging controls.
        page_row = QHBoxLayout()
        page_row.addStretch(1)
        self._btn_prev = QPushButton("\u2039")  # ‹
        self._btn_prev.setFixedWidth(36)
        self._btn_prev.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_prev.clicked.connect(self._prev_page)
        page_row.addWidget(self._btn_prev)
        self._page_label = QLabel("")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_label.setProperty("role", "tertiary")
        self._page_label.setMinimumWidth(60)
        page_row.addWidget(self._page_label)
        self._btn_next = QPushButton("\u203a")  # ›
        self._btn_next.setFixedWidth(36)
        self._btn_next.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_next.clicked.connect(self._next_page)
        page_row.addWidget(self._btn_next)
        page_row.addStretch(1)
        outer.addLayout(page_row)

    def set_grid_size(self, cols: int) -> None:
        """Set the grid dimension (2, 3, 4, or 5). Re-renders if showing.
        2x2 gives the largest, cleanest tiles; 5x5 the most-at-once."""
        cols = max(2, min(5, int(cols)))
        if cols != self._cols:
            self._cols = cols
            self._page = 0
            if self._paths:
                self._render_page()

    def _per_page(self) -> int:
        return self._cols * self._cols

    def show_group(self, base: str, paths: list) -> None:
        """Display a group's images (list of Paths), starting at page 0."""
        self._paths = list(paths)
        self._page = 0
        self._base = base
        self._render_page()

    def _prev_page(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._render_page()

    def _next_page(self) -> None:
        last = (len(self._paths) - 1) // self._per_page()
        if self._page < last:
            self._page += 1
            self._render_page()

    def _clear_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _render_page(self) -> None:
        """Render the current page as square tiles in a uniform grid.

        Tiles are _SquareThumb widgets that center-crop to a square and
        scale to whatever the grid gives them. We give every grid row and
        column equal stretch so the cells are uniform, and the tiles
        themselves only claim a tiny minimum — so the grid (and the whole
        window) can shrink freely. Only this page's tiles exist at once.
        """
        self._clear_grid()
        per = self._per_page()
        total = len(self._paths)
        n_pages = max(1, (total + per - 1) // per)
        start = self._page * per
        page_paths = self._paths[start:start + per]

        cols = self._cols
        for idx, path in enumerate(page_paths):
            r, c = divmod(idx, cols)
            tile = _SquareThumb(path)
            self._grid.addWidget(tile, r, c)

        # Equal stretch on every row/column so cells are uniform squares
        # regardless of how many tiles the last page holds. Reset any
        # stretch from a previous (larger) grid first.
        for i in range(6):
            self._grid.setRowStretch(i, 1 if i < cols else 0)
            self._grid.setColumnStretch(i, 1 if i < cols else 0)

        self._title.setText(
            f"{getattr(self, '_base', '')}  —  {total} images"
        )
        self._page_label.setText(f"Page {self._page + 1} / {n_pages}")
        self._btn_prev.setEnabled(self._page > 0)
        self._btn_next.setEnabled(self._page < n_pages - 1)
        single = n_pages <= 1
        self._btn_prev.setVisible(not single)
        self._btn_next.setVisible(not single)
        self._page_label.setVisible(not single)


class ImagePanel(QFrame):
    """Center workspace widget."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "panel")

        self._state: Optional[SessionState] = None

        # Reference to the queue panel, set by main_window after both
        # panels exist (see ImagePanel.set_queue_panel). Used by the
        # Yes/No triggers to detect multi-selection and dispatch to
        # batch operations (B6). None if not wired (single-image only).
        self._queue_panel = None

        # path -> QPixmap LRU cache. Keys are absolute image paths.
        self._cache: "OrderedDict[Path, QPixmap]" = OrderedDict()
        # Track what's currently displayed so we know whether a state
        # change actually changed the displayed image (vs just status).
        self._displayed_path: Optional[Path] = None
        # Original pixmap of the currently-displayed image (for zoom).
        self._displayed_pixmap: Optional[QPixmap] = None

        self._build_ui()
        self._refresh_enabled()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach(self, state: Optional[SessionState]) -> None:
        if state is self._state:
            return
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        self._state = state
        if state is not None:
            state.add_listener(self._on_state_change)
            self._refresh_display()
        else:
            self._clear_display()
        self._refresh_enabled()

    def detach(self) -> None:
        self.attach(None)

    # Public action methods. main_window wires keyboard shortcuts to
    # these so button clicks and key presses share one implementation.
    def trigger_yes(self) -> None:
        # In multi-select, "Yes/Add" stamps the ticked tags onto the
        # selected rows (or this image) rather than recording a single-tag
        # decision. _multi_trigger handles the row-vs-current targeting.
        if self._state is not None and self._state.multi_select_mode:
            self._multi_trigger(add=True)
            return
        self._trigger_decision(yes=True)

    def trigger_no(self) -> None:
        # In multi-select, "No/Remove" pulls the ticked tags OFF the
        # selected rows (or this image) — the mirror of Add. Outside
        # multi-select it's the usual single-tag rejection.
        if self._state is not None and self._state.multi_select_mode:
            self._multi_trigger(add=False)
            return
        self._trigger_decision(yes=False)

    def _confirm_multi_remove(self, selected) -> bool:
        """Confirm a multi-select batch REMOVE, naming exactly which tags
        will be pulled and from how many images.

        Multi-select remove strips EVERY ticked tag from every selected
        row at once. Without a prompt, a tag left ticked from an earlier
        step gets removed as silent collateral of an intended removal —
        an easy, hard-to-notice way to lose a whole tag across the set.
        This states the concrete effect and only counts images that
        actually carry each tag, so the numbers are honest.
        """
        if self._state is None:
            return False
        tags = list(self._state.get_selected_tags())
        if not tags or not selected:
            return False
        sel = set(selected)
        # Per-tag: how many of the selected images actually have it.
        lines = []
        total_removals = 0
        for t in tags:
            n = sum(
                1 for p in sel
                if t in self._state._image_tags.get(p, [])
            )
            total_removals += n
            lines.append(f"  • {t} — from {n} image(s)")
        if total_removals == 0:
            return True  # nothing to remove; let it no-op quietly
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Remove tags from selected images?")
        box.setText(
            f"This will remove the following tag(s) from "
            f"{len(sel)} selected image(s):")
        box.setInformativeText(
            "\n".join(lines)
            + "\n\nThis affects EVERY ticked tag, not just one. "
              "It can be reversed with Undo (this pass).")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _multi_trigger(self, add: bool) -> None:
        """Apply a multi-select Add (Yes) or Remove (No) to the right
        target, mirroring _trigger_decision's targeting:

        - >1 rows selected, or exactly 1 row selected that ISN'T the
          current walk image (a deliberate Ctrl-click) -> batch the ticked
          tags onto/off those rows, WITHOUT advancing the walk.
        - otherwise (no selection, or the one selected row IS the current
          image -> normal walking) -> act on the current image and advance,
          exactly like single-image Add/No.
        """
        if self._state is None:
            return
        selected = (
            self._queue_panel.selected_image_paths()
            if self._queue_panel is not None else []
        )
        current = self._state.current_image
        current_path = current.image_path if current is not None else None
        try:
            row_batch = (
                len(selected) > 1
                or (len(selected) == 1 and selected[0] != current_path)
            )
            if row_batch:
                if add:
                    self._state.multi_batch_add_selected(selected)
                else:
                    if not self._confirm_multi_remove(selected):
                        return
                    self._state.multi_batch_remove_selected(selected)
            else:
                if add:
                    self._state.multi_add_selected_to_current()
                else:
                    self._state.multi_remove_selected_from_current()
        except OSError as e:
            self._show_write_error(e)
        # Ensure the pass tally + button states reflect the change even if
        # no later event fires (e.g. a no-op removal).
        self._refresh_enabled()

    def _trigger_decision(self, yes: bool) -> None:
        """Dispatch a Yes/No to the right target.

        Targeting rules:
        - >1 rows selected → batch decision on all of them, no walk
          advance (B6).
        - exactly 1 row selected AND it's a DIFFERENT image than the
          current walk position → the user Ctrl-clicked a specific row
          (Ctrl-click is deliberately non-jumping), so the decision
          must apply to THAT row, not the walked image. We route it as a
          one-element batch so the walk doesn't advance away from a row
          the user didn't navigate to. Without this, the decision would
          silently land on current_image — the wrong image — and the
          walk would jump.
        - otherwise (no selection, or the single selected row IS the
          current walk row → normal walking) → the familiar
          walk-advancing record_yes / record_no on the current image.
        """
        if self._state is None:
            return
        from core.state import Decision
        decision = Decision.YES if yes else Decision.NO

        # Group-header batch path (Feature B): if grouping is on and a
        # group header is the current row, apply the decision to ALL
        # images in that group, then auto-advance to the next group
        # header. The per-image walk is untouched.
        if self._queue_panel is not None:
            group_members = self._queue_panel.selected_group_members()
            if group_members:
                acted_key = self._queue_panel._selected_header_key
                try:
                    self._state.record_batch_decisions(
                        group_members, decision,
                        undo_context={"group_key": acted_key},
                    )
                except OSError as e:
                    self._show_write_error(e)
                    return
                self._queue_panel.advance_to_next_group_header()
                return

        if self._queue_panel is not None:
            selected = self._queue_panel.selected_image_paths()
        else:
            selected = []

        current = self._state.current_image
        current_path = current.image_path if current is not None else None
        try:
            if len(selected) > 1:
                # Batch path: apply to all selected, no walk advance.
                self._state.record_batch_decisions(selected, decision)
            elif len(selected) == 1 and selected[0] != current_path:
                # Single deliberate (Ctrl-click) selection on a row that
                # isn't the walked image: decide THAT row, don't advance.
                self._state.record_batch_decisions(selected, decision)
            else:
                # Single-image path (current image): the familiar
                # walk-advancing record_yes / record_no.
                if yes:
                    self._state.record_yes()
                else:
                    self._state.record_no()
        except OSError as e:
            self._show_write_error(e)

    def set_queue_panel(self, queue_panel) -> None:
        """Wire the queue panel reference so batch ops can read its
        current selection. Called once by main_window after both panels
        exist.
        """
        self._queue_panel = queue_panel

    def trigger_skip_image(self) -> None:
        """Skip the right target image(s).

        Same targeting rules as Yes/No (see _trigger_decision):
        - group header selected → SKIP the whole group, then auto-advance
          to the next group header (matches batch Yes/No so the grouped
          and standard walks behave the same way);
        - >1 selected → batch skip;
        - exactly 1 selected that differs from the walked image → skip
          that row (no advance);
        - otherwise → single-image skip on the current image with walk
          advance.
        """
        if self._state is None:
            return
        if self._state.multi_select_mode:
            # Skip/Next is removed in multi-select: its button is hidden and
            # Space does nothing here. Move forward by tagging (Add/Remove
            # advance the current image) or by clicking a row in the queue.
            return
        # Multi-select: Skip is pure navigation to the next matching image
        # (nothing is recorded — there's no single tag).
        if self._state.multi_select_mode:
            self._state.multi_advance()
            return
        # Group-header batch path (Feature B): skip every image in the
        # selected group as a unit, then advance to the next group. Without
        # this, Skip in group mode fell through to the single-image path
        # and skipped only the current image, not the group.
        if self._queue_panel is not None:
            group_members = self._queue_panel.selected_group_members()
            if group_members:
                acted_key = self._queue_panel._selected_header_key
                try:
                    self._state.record_batch_skip_image(
                        group_members,
                        undo_context={"group_key": acted_key},
                    )
                except OSError as e:
                    self._show_write_error(e)
                    return
                self._queue_panel.advance_to_next_group_header()
                return
        selected = (
            self._queue_panel.selected_image_paths()
            if self._queue_panel is not None else []
        )
        current = self._state.current_image
        current_path = current.image_path if current is not None else None
        if len(selected) > 1:
            self._state.record_batch_skip_image(selected)
        elif len(selected) == 1 and selected[0] != current_path:
            self._state.record_batch_skip_image(selected)
        else:
            self._state.record_skip_image()

    def set_undo_handler(self, handler) -> None:
        """Install a callback invoked by the Back button / Backspace
        instead of calling state.undo() directly.

        main_window provides its _action_undo here so the Back path goes
        through the same failure-warning logic as the Edit → Undo menu
        item. Without this, a failed undo revert (e.g. a locked .txt on
        a granular-edit undo) would be silently swallowed on the Back
        path while the menu path warns — an inconsistency that lets disk
        and memory diverge without the user knowing.
        """
        self._undo_handler = handler

    def trigger_back(self) -> None:
        # Back / Backspace = Undo the last action, in BOTH normal and
        # multi-select mode. Multi-select no longer has Next/Prev
        # navigation buttons — use the queue list to move around — so here
        # it behaves exactly like the normal-mode undo.
        handler = getattr(self, "_undo_handler", None)
        if handler is not None:
            handler()
        elif self._state is not None:
            # Fallback (no handler wired, e.g. in isolated tests): undo
            # directly. The result is still checked so a standalone
            # panel doesn't claim a failed undo succeeded.
            self._state.undo()

    def trigger_skip_tag(self) -> None:
        if self._state is not None:
            self._state.record_skip_tag()

    def _on_undo_pass(self) -> None:
        """Reverse every change (adds and removes) made in the current
        multi-select pass, after a confirmation so a stray click can't wipe
        a pass of work. The state rebuilds the finder and emits
        filter_changed; we refresh our own buttons + tally afterwards so the
        tally resets once the pass is empty again."""
        if self._state is None:
            return
        pending = self._state.multi_pass_image_count()
        if pending <= 0:
            return
        noun = "image" if pending == 1 else "images"
        resp = QMessageBox.question(
            self,
            "Undo all changes this pass",
            f"Reverse every tag change made to {pending} {noun} since you "
            "entered multi-select mode?\n\nThis undoes the whole pass at "
            "once and can't be redone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        n = self._state.undo_all_multi_pass()
        self._refresh_enabled()
        if n > 0:
            err = self._state.last_undo_error
            if err:
                self._show_write_error(err)

    def update_shortcut_labels(self, hints: dict[str, str]) -> None:
        """Update keyboard hint suffixes on button labels.

        Called by main_window after settings load or after the user
        reassigns shortcuts. `hints` maps action name to hint string
        ("Y", "Ctrl+Shift+P", "" for unbound).
        """
        self._shortcut_hints = dict(hints)
        self._refresh_button_labels()
        # Skip-tag no longer has a button here (moved to tag tree
        # context menu), so there's no label to update for it.

    def _refresh_button_labels(self) -> None:
        """Set the action-button text and tooltips, adapting to
        multi-select mode. In multi-select the buttons say what they
        actually do there — "＋ Add tag(s)", "Next", "Prev" — instead of
        the single-tag-walk labels, so the user never has to guess what
        "Yes" means with no tag selected."""
        hints = getattr(self, "_shortcut_hints", {})
        # Capture the original (single-tag) tooltips once, the first time
        # this runs (always in normal mode, before any multi toggle), so
        # we can restore them when leaving multi-select.
        if not hasattr(self, "_orig_tips"):
            self._orig_tips = {
                "yes": self.btn_yes.toolTip(),
                "no": self.btn_no.toolTip(),
                "skip": self.btn_skip.toolTip(),
                "back": self.btn_back.toolTip(),
            }
        multi = self._state is not None and self._state.multi_select_mode
        if multi:
            self.btn_yes.setText(self._compose_btn_label(
                "＋ Add tag(s)", "", hints.get("yes", "")))
            self.btn_yes.setToolTip(
                "Add the ticked tag(s) to the selected rows (or this "
                "image), then move on")
            self.btn_no.setText(self._compose_btn_label(
                "－ Remove tag(s)", "", hints.get("no", "")))
            self.btn_no.setToolTip(
                "Remove the ticked tag(s) from the selected rows (or this "
                "image)")
            self.btn_skip.setText(self._compose_btn_label(
                "Next", "", hints.get("skip_image", "")))
            self.btn_skip.setToolTip("Go to the next matching image")
            self.btn_back.setText(self._compose_btn_label(
                "Undo", "", hints.get("back", "")))
            self.btn_back.setToolTip("Undo the last Add or Remove")
        else:
            self.btn_yes.setText(self._compose_btn_label(
                "Yes", Icons.CHECK, hints.get("yes", "")))
            self.btn_yes.setToolTip(self._orig_tips["yes"])
            self.btn_no.setText(self._compose_btn_label(
                "No", Icons.CROSS, hints.get("no", "")))
            self.btn_no.setToolTip(self._orig_tips["no"])
            self.btn_skip.setText(self._compose_btn_label(
                "Skip", "", hints.get("skip_image", "")))
            self.btn_skip.setToolTip(self._orig_tips["skip"])
            self.btn_back.setText(self._compose_btn_label(
                "Back", "", hints.get("back", "")))
            self.btn_back.setToolTip(self._orig_tips["back"])

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE,
        )
        outer.setSpacing(Spacing.NORMAL)

        # ---- Header: tag name + presence + progress, full-width ----
        # These three labels span the full image-panel width so they
        # stay centered relative to the entire panel. The swirl widget
        # used to live here as a floating child in the top-right
        # corner, but it was prone to z-order issues (hiding behind
        # the image's colored border) and obscured the image at small
        # window widths. It's now in main_window's bottom area, next
        # to the action log — see ui/main_window.py for placement.
        self.label_tag_name = QLabel("(select a tag)")
        self.label_tag_name.setProperty("role", "heading")
        self.label_tag_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.label_tag_name)

        self.label_presence = QLabel("")
        self.label_presence.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.label_presence)
        # When False (set by the queue panel in grouping mode), the
        # presence indicator stays hidden — a group's images can be mixed,
        # so a single "tag present / not present" flag would be misleading.
        self._presence_indicator_visible = True

        self.label_progress = QLabel("")
        self.label_progress.setProperty("role", "tertiary")
        self.label_progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.label_progress)

        # One-line status for multi-select mode. Hidden during normal
        # walking; shown only while multi-select is active.
        self.label_multi_hint = QLabel("Multi-selection mode enabled.")
        self.label_multi_hint.setWordWrap(True)
        self.label_multi_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_multi_hint.setStyleSheet(
            f"QLabel {{"
            f" background-color: {Colors.ACCENT_BLUE_BG};"
            f" color: {Colors.TEXT_PRIMARY};"
            f" border: 1px solid {Colors.ACCENT_BLUE};"
            f" border-radius: 4px; padding: 5px 8px;"
            f"}}"
        )
        self.label_multi_hint.setVisible(False)
        outer.addWidget(self.label_multi_hint)

        # ---- Image area ----
        # Expanding size policy with stretch=1 in the layout below so
        # the image takes whatever space remains after header and
        # button rows. minimumHeight prevents collapse when the
        # window is shrunk.
        self.label_image = ClickableImageLabel()
        self.label_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # A modest floor so the image area never collapses to nothing,
        # but small enough that the whole window can fit on a short
        # screen (e.g. a 1366x768 laptop, fullscreened, after the
        # taskbar and title bar). The image still expands to fill all
        # available space above this floor via the Expanding policy.
        self.label_image.setMinimumHeight(120)
        self.label_image.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.label_image.set_click_handler(self._on_image_click)

        # Group preview grid (Feature B) — shown in place of the single
        # image when a group header is selected in the queue. A stacked
        # widget swaps between the two without disturbing the rest of the
        # layout. Page 0 = single image (normal walk), page 1 = grid.
        self.group_grid = GroupPreviewGrid()
        self._image_stack = QStackedWidget()
        self._image_stack.addWidget(self.label_image)   # index 0
        self._image_stack.addWidget(self.group_grid)    # index 1
        self._image_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        outer.addWidget(self._image_stack, 1)  # stretch=1 takes remaining

        # ---- Primary buttons row ----
        primary_row = QHBoxLayout()
        primary_row.setSpacing(Spacing.LOOSE)
        primary_row.addStretch(1)
        self.btn_yes = QPushButton(self._compose_btn_label("Yes", Icons.CHECK, "Y"))
        self.btn_yes.setProperty("role", "success")
        repolish(self.btn_yes)
        self.btn_yes.setToolTip(
            "Confirm this image SHOULD have the current tag.\n"
            "Adds the tag to the .txt file if missing, then advances."
        )
        self.btn_yes.clicked.connect(self.trigger_yes)
        primary_row.addWidget(self.btn_yes)

        self.btn_no = QPushButton(self._compose_btn_label("No", Icons.CROSS, "N"))
        self.btn_no.setProperty("role", "danger")
        repolish(self.btn_no)
        self.btn_no.setToolTip(
            "This image should NOT have the current tag.\n"
            "Removes the tag from the .txt file if present, then advances."
        )
        self.btn_no.clicked.connect(self.trigger_no)
        primary_row.addWidget(self.btn_no)
        primary_row.addStretch(1)
        outer.addLayout(primary_row)

        # ---- Secondary buttons row ----
        # Note: the "Skip tag" button used to live here. It has been
        # moved to the tag tree's right-click context menu, alongside
        # Mark Complete / Rename / Delete, so all tag-level operations
        # cluster in one place and this row stays uncluttered.
        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(Spacing.NORMAL)
        secondary_row.addStretch(1)
        self.btn_skip = QPushButton(self._compose_btn_label("Skip", "", "Space"))
        self.btn_skip.setProperty("role", "secondary")
        repolish(self.btn_skip)
        self.btn_skip.setToolTip(
            "Skip this image for now without changing its tags.\n"
            "It stays unprocessed so you can come back to it."
        )
        self.btn_skip.clicked.connect(self.trigger_skip_image)
        secondary_row.addWidget(self.btn_skip)

        self.btn_back = QPushButton(self._compose_btn_label("Back", "", "Bksp"))
        self.btn_back.setProperty("role", "secondary")
        repolish(self.btn_back)
        self.btn_back.setToolTip(
            "Undo the last action and return to the image you just\n"
            "decided on, so you can re-tag it."
        )
        self.btn_back.clicked.connect(self.trigger_back)
        secondary_row.addWidget(self.btn_back)
        secondary_row.addStretch(1)
        outer.addLayout(secondary_row)

        # ---- Multi-select "Undo all this pass" row ----
        # A safety net for bulk tagging: while stamping ticked tags onto
        # many images in multi-select, this shows a running tally and a
        # one-click reverse for everything done THIS pass. Hidden entirely
        # outside multi-select and whenever the pass is empty, so it never
        # clutters normal walking. _refresh_enabled drives its text and
        # visibility; the pass resets on leaving multi-select.
        self._multi_undo_row = QWidget()
        undo_row = QHBoxLayout(self._multi_undo_row)
        undo_row.setContentsMargins(0, 0, 0, 0)
        undo_row.setSpacing(Spacing.NORMAL)
        undo_row.addStretch(1)
        self.label_pass_count = QLabel("")
        self.label_pass_count.setProperty("role", "tertiary")
        self.label_pass_count.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        undo_row.addWidget(self.label_pass_count)
        self.btn_undo_pass = QPushButton("\u21a9  Undo all")
        self.btn_undo_pass.setProperty("role", "secondary")
        repolish(self.btn_undo_pass)
        self.btn_undo_pass.setToolTip(
            "Reverse EVERY tag change (added or removed) during this\n"
            "multi-select pass, and bring the affected images back into\n"
            "the finder. Asks for confirmation first."
        )
        self.btn_undo_pass.clicked.connect(self._on_undo_pass)
        undo_row.addWidget(self.btn_undo_pass)
        undo_row.addStretch(1)
        self._multi_undo_row.setVisible(False)
        outer.addWidget(self._multi_undo_row)

    @staticmethod
    def _compose_btn_label(text: str, icon: str, shortcut_hint: str) -> str:
        """Build a button label like 'Yes (Y)' or '✓  Yes' or just 'Yes'.

        Icon (optional) goes first, then text, then shortcut hint in
        parentheses if provided.
        """
        parts: list[str] = []
        if icon:
            parts.append(icon)
        parts.append(text)
        label = "  ".join(parts) if icon else text
        if shortcut_hint:
            label += f" ({shortcut_hint})"
        return label

    # ------------------------------------------------------------------
    # State change listener (state → widget)
    # ------------------------------------------------------------------

    def _on_state_change(self, change: StateChange) -> None:
        kind = change.kind
        # Every event that could affect what's shown triggers a full
        # display refresh. This is simpler than partial updates and
        # the refresh is cheap (no disk read unless the image actually
        # changed).
        if kind == "walk_ended" and change.extra == "tag_complete":
            # A tag finished and the user's setting is "stop". Show a
            # completion message naming the tag, instead of silently
            # jumping to the next one. current_tag is now None so the
            # action buttons disable, preventing accidental tagging of
            # the next set.
            self._show_tag_complete(change.tag)
            self._refresh_presence_label()
            self._refresh_progress_label()
            self._refresh_enabled()
            return
        if kind in (
            "tag_selected",
            "walk_advanced",
            "walk_ended",
            "tree_rebuilt",
            "tag_changed",  # batch decisions
            "filter_changed",
        ):
            self._refresh_display()
        elif kind == "image_changed":
            # Could be the current image's tags shifting (refresh
            # presence badge AND the state border), or another image
            # we don't display. Also refresh progress label since
            # granular edits can clear decisions on the current tag.
            if (change.image_path is not None
                    and self._state is not None
                    and self._state.current_image is not None
                    and self._state.current_image.image_path == change.image_path):
                self._refresh_presence_label()
                self._refresh_border()
            # Decisions may have changed (granular edit cleared
            # stale decisions). Refresh the progress label so the
            # decided/remaining count stays correct.
            self._refresh_progress_label()
            # A granular add/remove is undoable, so it changes
            # state.can_undo() — the Back (Undo) button's enabled state
            # must be refreshed. Without this, the VERY FIRST edit of a
            # session (when Back starts disabled) leaves Back disabled
            # even though there's now something to undo, so pressing it
            # does nothing; a later action that goes through
            # _refresh_display() would re-enable it, which is why "any
            # other action makes undo work again". Refreshing here fixes
            # the first-edit case. (Other events above route through
            # _refresh_display, which already calls _refresh_enabled.)
            self._refresh_enabled()

    # ------------------------------------------------------------------
    # Display refresh
    # ------------------------------------------------------------------

    def show_group_preview(self, preview: Optional[tuple]) -> None:
        """Switch the center panel between single-image and group-grid.

        preview is (base, [paths]) to show the grid for a selected group
        header, or None to return to the normal single-image view.
        """
        if preview is None:
            self._image_stack.setCurrentIndex(0)  # single image
            return
        base, paths = preview
        self.group_grid.show_group(base, paths)
        self._image_stack.setCurrentIndex(1)  # grid

    def set_group_grid_size(self, cols: int) -> None:
        """Set the preview grid dimension (3/4/5) from preferences."""
        self.group_grid.set_grid_size(cols)

    def _refresh_display(self) -> None:
        """Update every visual element to match current state."""
        if self._state is None:
            self._clear_display()
            return

        cur_tag = self._state.current_tag
        cur_image = self._state.current_image

        # Tag name header.
        if cur_tag is None:
            self.label_tag_name.setText("(select a tag)")
            # End-of-tree case: we have no current tag but there ARE
            # tags in the dataset. Show the completion message in the
            # image area.
            if self._state.all_tags:
                self._show_end_of_tree()
            else:
                self._clear_image_area()
        else:
            self.label_tag_name.setText(cur_tag)

        # Image and presence.
        if cur_image is None:
            if cur_tag is not None:
                # Tag selected but queue is empty (filter excludes all).
                self.label_image.setText("No images match the current filter.")
                self.label_image.setPixmap(QPixmap())
                self._displayed_path = None
                self._displayed_pixmap = None
            self._refresh_presence_label()
            self._refresh_progress_label()
            self._refresh_enabled()
            self._refresh_border()
            return

        # Real image to display.
        path = cur_image.image_path
        if path != self._displayed_path:
            self._load_and_display_image(path)
        # else: same image still displayed, no reload needed (cheap).

        self._refresh_presence_label()
        self._refresh_progress_label()
        self._refresh_enabled()
        self._refresh_border()

    def _refresh_border(self) -> None:
        """Color the image border to reflect the current image's tag
        state at a glance.

        The border gives peripheral feedback during fast tagging so the
        user doesn't have to look up at the presence label between every
        decision. Color meanings:

        - GREEN  : current image has the current tag (Yes would confirm)
        - RED    : current image does NOT have the current tag (Yes
                   would add it, No would skip it)
        - AMBER  : current image is an orphan, no caption file
        - none   : nothing meaningful to show (no tag selected, walk
                   ended, no image, etc.)

        Border colors are theme-overridable (Pass C); see
        Colors.STATE_BORDER_* in config/theme.py.
        """
        if (self._state is None
                or self._state.current_image is None
                or self._state.current_tag is None):
            # No meaningful state — clear the border. We use an empty
            # stylesheet rather than a transparent border so the label
            # falls back to whatever the global theme defines (which is
            # currently no border).
            self.label_image.setStyleSheet("")
            return

        path = self._state.current_image.image_path
        if not self._state.has_caption_file(path):
            color = Colors.STATE_BORDER_ORPHAN
        elif self._state.is_tag_in_image(path, self._state.current_tag):
            color = Colors.STATE_BORDER_HAS_TAG
        else:
            color = Colors.STATE_BORDER_MISSING_TAG

        # 5px gives clear peripheral visibility without dominating the
        # image. A 1px inner padding keeps the image from rendering
        # flush against the colored edge, which looks tidier when the
        # image has a matching color along its edge.
        self.label_image.setStyleSheet(
            f"QLabel {{ border: 5px solid {color}; padding: 1px; }}"
        )

    def set_presence_indicator_visible(self, visible: bool) -> None:
        """Show/hide the 'Tag present / not in file' indicator. Hidden in
        grouping mode where a group's images may be mixed."""
        self._presence_indicator_visible = visible
        self.label_presence.setVisible(visible)
        if not visible:
            self.label_presence.setText("")

    def _refresh_presence_label(self) -> None:
        """Update the presence badge ('Tag present', 'Tag not in file',
        or 'No caption file'). Suppressed entirely when the indicator is
        hidden (grouping mode)."""
        if not self._presence_indicator_visible:
            self.label_presence.setText("")
            return
        if self._state is None:
            self.label_presence.setText("")
            return
        img = self._state.current_image
        tag = self._state.current_tag
        if img is None or tag is None:
            self.label_presence.setText("")
            return

        if not self._state.has_caption_file(img.image_path):
            self.label_presence.setText(f"{Icons.NO_FILE}  No caption file")
            self.label_presence.setProperty("role", "danger")
        elif self._state.is_tag_in_image(img.image_path, tag):
            self.label_presence.setText(f"{Icons.CHECK}  Tag present in file")
            self.label_presence.setProperty("role", "success")
        else:
            self.label_presence.setText(f"{Icons.CROSS}  Tag not in file")
            self.label_presence.setProperty("role", "danger")
        repolish(self.label_presence)

    def _refresh_progress_label(self) -> None:
        """Show 'N decided of M · K remaining' for the current tag.

        Previous version used walk_index as the numerator, which was
        misleading — moving back/forward in the queue without making
        decisions shouldn't shift the counter. Now the counter
        reflects actual progress (Yes or No decisions; skip-image is
        not counted as a decision but is tallied separately in the
        file state panel).
        """
        if self._state is None or self._state.current_tag is None:
            self.label_progress.setText("")
            return
        size = self._state.walk_size
        if size == 0:
            self.label_progress.setText("Queue is empty")
            return
        # Count YES / NO decisions on the current tag's queue.
        # Cheap (one lookup per queue item) and matches the swirl's
        # completion semantics so the two visuals always agree.
        # Shared with the progress bar and the walk status line: all
        # three wanted the same number and each recomputed it over the
        # whole queue on every keypress.
        decided = self._state.current_queue_decided()
        remaining = size - decided
        self.label_progress.setText(
            f"{decided} decided of {size}"
            + (f"  \u00b7  {remaining} remaining" if remaining > 0 else "")
        )

    def _clear_display(self) -> None:
        self.label_tag_name.setText("(open a directory)")
        self.label_presence.setText("")
        self.label_progress.setText("")
        self._clear_image_area()

    def _clear_image_area(self) -> None:
        self.label_image.setPixmap(QPixmap())
        self.label_image.setText("")
        self.label_image.setStyleSheet("")  # clear any state-border
        self._displayed_path = None
        self._displayed_pixmap = None

    def _show_end_of_tree(self) -> None:
        self.label_image.setPixmap(QPixmap())
        self.label_image.setText(f"{Icons.DONE}  Tree walk complete")
        self.label_image.setStyleSheet("")  # clear any state-border
        self._displayed_path = None
        self._displayed_pixmap = None

    def _show_tag_complete(self, completed_tag: Optional[str]) -> None:
        """Show the per-tag completion message when the user's setting
        is "stop on tag complete".

        The header swaps from "select a tag" to a celebratory line that
        names the tag, and the image area is replaced with a clear
        multi-line message guiding the user to pick the next tag. The
        action buttons are disabled by _refresh_enabled because
        current_tag is now None — that also physically prevents the
        "kept tagging without noticing" workflow hazard.
        """
        tag_label = completed_tag or "this tag"
        self.label_tag_name.setText(f"\u2728  Tag complete")
        self.label_presence.setText("")
        self.label_progress.setText("")
        self.label_image.setPixmap(QPixmap())
        self.label_image.setText(
            f"\u2728  Tag complete!\n"
            f"Every image for '{tag_label}' has been reviewed.\n"
            f"Pick another tag to continue."
        )
        # No meaningful tag state to show — clear the border too.
        self.label_image.setStyleSheet("")
        self._displayed_path = None
        self._displayed_pixmap = None

    # ------------------------------------------------------------------
    # Image loading + cache
    # ------------------------------------------------------------------

    def _load_and_display_image(self, path: Path) -> None:
        """Load (or fetch from cache) and display an image."""
        pixmap = self._get_pixmap(path)
        if pixmap is None or pixmap.isNull():
            self.label_image.setPixmap(QPixmap())
            # Distinguish "file is gone" from "file is here but unreadable"
            # (corrupt bytes / unsupported encoding). The old message always
            # said "deleted externally", which is misleading and unhelpful
            # when the file is sitting right there but won't decode — a
            # common case when auditing scraped datasets.
            try:
                present = path.is_file()
            except OSError:
                present = False
            if present:
                self.label_image.setText(
                    f"\u26a0  Couldn't read image:\n{path.name}\n"
                    f"(file is present but corrupt or an unsupported format)"
                )
            else:
                self.label_image.setText(
                    f"\u26a0  Image file not found:\n{path.name}\n"
                    f"(may have been deleted externally)"
                )
            self._displayed_path = path
            self._displayed_pixmap = None
            return

        self._displayed_path = path
        self._displayed_pixmap = pixmap
        # Clear text so it doesn't show through transparent areas.
        self.label_image.setText("")
        self._paint_scaled_pixmap()

    def _get_pixmap(self, path: Path) -> Optional[QPixmap]:
        """Return the cached pixmap for `path`, loading from disk on miss.

        Cache uses LRU eviction: on a hit, move the entry to the end
        (most recently used); on a miss, load and append; evict from
        the front when over IMAGE_CACHE_SIZE.
        """
        if path in self._cache:
            # Mark as recently used by removing and re-inserting.
            self._cache.move_to_end(path)
            return self._cache[path]

        # Miss: load from disk. QPixmap.load returns False on failure
        # (file missing, format unsupported); the resulting pixmap
        # is null. We cache null pixmaps too so we don't retry a
        # missing file on every refresh.
        pixmap = QPixmap()
        try:
            ok = pixmap.load(str(path))
        except Exception:
            ok = False
        if not ok:
            # Don't cache the null pixmap — if the user creates the
            # file later (or restores it), we want to pick it up.
            return pixmap

        self._cache[path] = pixmap
        # Evict oldest if over capacity. OrderedDict.popitem(last=False)
        # removes from the front.
        while len(self._cache) > IMAGE_CACHE_SIZE:
            self._cache.popitem(last=False)
        return pixmap

    def _paint_scaled_pixmap(self) -> None:
        """Scale the displayed pixmap to fit the label and paint it.

        Called from _load_and_display_image and from resizeEvent.
        """
        if self._displayed_pixmap is None or self._displayed_pixmap.isNull():
            return
        target = self.label_image.size()
        if target.width() <= 0 or target.height() <= 0:
            return
        scaled = self._displayed_pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.label_image.setPixmap(scaled)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Re-paint the current image at the new size. Cheap because
        # the original pixmap is in memory; we only re-scale.
        self._paint_scaled_pixmap()

    # ------------------------------------------------------------------
    # Click → zoom
    # ------------------------------------------------------------------

    def _on_image_click(self) -> None:
        if self._displayed_pixmap is None or self._displayed_pixmap.isNull():
            return
        dlg = ZoomDialog(self._displayed_pixmap, self)
        dlg.exec()

    # ------------------------------------------------------------------
    # Error display
    # ------------------------------------------------------------------

    def _show_write_error(self, error: OSError) -> None:
        """Disk write failed during Yes/No. Show a brief, dismissable
        error in the presence label.

        We avoid a modal dialog for this because file-write failures
        are usually transient (locked by antivirus, etc.). Showing
        the error inline lets the user retry without ceremony.
        """
        self.label_presence.setText(f"⚠  Write failed: {error}")
        self.label_presence.setProperty("role", "danger")
        repolish(self.label_presence)

    # ------------------------------------------------------------------
    # Enabled state
    # ------------------------------------------------------------------

    def _refresh_enabled(self) -> None:
        has_state = self._state is not None
        has_image = (
            has_state
            and self._state is not None
            and self._state.current_image is not None
        )
        can_undo = (
            has_state and self._state is not None and self._state.can_undo()
        )
        # In multi-select mode the buttons take on add/remove/navigation
        # roles: Yes = add the ticked tag(s), No = remove the ticked
        # tag(s), Skip = next image, Back = previous image (navigation,
        # not undo). Yes/No act on the selected rows if any, else the
        # current image.
        in_multi = (
            has_state
            and self._state is not None
            and self._state.multi_select_mode
        )
        in_browse = (
            has_state
            and self._state is not None
            and self._state.browse_mode
        )
        if in_browse:
            # Browse mode has no active tag: there is nothing to decide,
            # so Yes/No/Skip are HIDDEN entirely (not merely disabled) to
            # keep the panel uncluttered — the row's surrounding stretches
            # recenter whatever remains, exactly as multi-select does when
            # it hides Skip. The underlying actions are already inert here
            # (record_yes/no/skip all no-op with no tag), so hiding is
            # purely cosmetic and cannot strand a phantom action even via
            # a keyboard shortcut. Back stays visible and live as UNDO:
            # file-state caption edits push undo entries the same as
            # anywhere, so Backspace must revert the last caption change.
            self.btn_yes.setVisible(False)
            self.btn_no.setVisible(False)
            self.btn_skip.setVisible(False)
            self.btn_back.setVisible(True)
            self.btn_back.setEnabled(can_undo)
        elif in_multi:
            self.btn_yes.setVisible(True)
            self.btn_no.setVisible(True)
            self.btn_yes.setEnabled(has_image)
            self.btn_no.setEnabled(has_image)
            # Skip/Next is removed in multi-select; hiding it leaves the
            # row's two stretches around btn_back, centering it. Back is
            # now Undo, enabled only when there's something to undo.
            self.btn_skip.setVisible(False)
            self.btn_back.setVisible(True)
            self.btn_back.setEnabled(can_undo)
        else:
            action_enabled = has_image
            self.btn_yes.setVisible(True)
            self.btn_no.setVisible(True)
            self.btn_yes.setEnabled(action_enabled)
            self.btn_no.setEnabled(action_enabled)
            self.btn_skip.setVisible(True)
            self.btn_skip.setEnabled(action_enabled)
            self.btn_back.setVisible(True)
            self.btn_back.setEnabled(can_undo)

        # Buttons relabel themselves for the active mode.
        self._refresh_button_labels()
        # How-to strip is visible only in multi-select mode.
        if hasattr(self, "label_multi_hint"):
            self.label_multi_hint.setVisible(bool(in_multi))
        # Multi-select pass: running tally + one-click "Undo all". Shown
        # only in multi-select once at least one image has been stamped.
        if hasattr(self, "_multi_undo_row"):
            pass_count = 0
            if in_multi and self._state is not None:
                pass_count = self._state.multi_pass_image_count()
            if in_multi:
                # Visible for the whole pass so the row doesn't pop in (and
                # shove the buttons around) the moment the first edit lands.
                # The button just enables and the tally fills in. Covers
                # both adds and removes now.
                if pass_count > 0:
                    noun = "image" if pass_count == 1 else "images"
                    self.label_pass_count.setText(
                        f"{pass_count} {noun} changed this pass"
                    )
                else:
                    self.label_pass_count.setText("No changes yet this pass")
                self.btn_undo_pass.setEnabled(pass_count > 0)
                self._multi_undo_row.setVisible(True)
            else:
                self._multi_undo_row.setVisible(False)
