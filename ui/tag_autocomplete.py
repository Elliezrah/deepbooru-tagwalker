"""
ui/tag_autocomplete.py

Tag autocomplete — a small suggestion popup attachable to a QLineEdit.

Attach to any QLineEdit to get a non-intrusive dropdown of canonical
Danbooru tags as the user types, ranked by popularity. Accept a suggestion
with click, Tab, or Up/Down + Enter; plain Enter commits exactly what you
typed (so short unique tags aren't overridden by the first suggestion).

Design goals (the "right touch" the user asked for):
  * Small and quiet: a short list (max ~8), only appears after 2+
    characters, positioned just under the box, dismissed on Esc / focus
    loss / empty input.
  * Never auto-replaces: the box only changes when the user explicitly
    picks an item. Typing is never overridden.
  * Cheap to open: the database loads lazily, in a BACKGROUND thread, on
    the first focus of the box — so the ~0.5s load never freezes typing
    and there's no startup cost. Until it's ready, the popup simply shows
    nothing (the box stays fully usable as a plain text field).
"""

from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import Qt, QObject, QEvent, Signal, QTimer, QPoint
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QFrame,
)

from config.theme import Colors
from core import tag_database as tdb
from config.settings import Settings


class TagAutocomplete(QObject):
    """Attaches an autocomplete popup to a QLineEdit.

    Usage:
        self._ac = TagAutocomplete(self.my_line_edit)
    Keep a reference (it's parented to the line edit, but holding it is
    clearer). The popup is a frameless QListWidget shown beneath the box.
    """

    # Emitted (from the worker thread) when the background load finishes.
    _loaded = Signal()

    MIN_CHARS = 2
    MAX_ITEMS = 8
    DEBOUNCE_MS = 90

    def __init__(self, line_edit: QLineEdit,
                 apply_entry_format: bool = True) -> None:
        super().__init__(line_edit)
        self._edit = line_edit
        self._db = tdb.get_database()
        self._load_started = False
        self._popup: Optional[QListWidget] = None
        # When True, suggestions are shown and inserted in the user's
        # chosen tag-entry format (underscores/spaces). Post Browser sets
        # this False: its search box is Danbooru query syntax (space =
        # tag separator, tags use underscores), NOT the caption format.
        self._apply_entry_format = apply_entry_format

        # Debounce so we don't recompute on every fast keystroke.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DEBOUNCE_MS)
        self._timer.timeout.connect(self._update_suggestions)

        self._edit.textEdited.connect(self._on_text_edited)
        self._edit.installEventFilter(self)
        self._loaded.connect(self._on_db_loaded)

    def _ensure_popup(self) -> QListWidget:
        """Create the popup list on first use (keeps __init__ cheap and
        avoids constructing a top-level window until actually needed)."""
        if self._popup is not None:
            return self._popup
        # Parent the popup to the line edit (not None). It's still a
        # top-level Tool window via the flags below, but giving it a
        # parent inside the current window makes it a transient child of
        # that window — so when the line edit lives in an *application-
        # modal* dialog (conflict rules, audit exceptions, both shown via
        # exec()), the modal grab lets mouse clicks reach the popup.
        # Without a parent the popup is an independent top-level window,
        # which a modal dialog blocks from receiving mouse input — that's
        # why suggestions could only be chosen with the keyboard there.
        popup = QListWidget(self._edit)
        popup.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        popup.setAttribute(
            Qt.WidgetAttribute.WA_ShowWithoutActivating, True
        )
        popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        popup.setFrameShape(QFrame.Shape.Box)
        popup.setUniformItemSizes(True)
        popup.setStyleSheet(
            f"QListWidget {{"
            f" background-color: {Colors.BG_INPUT};"
            f" border: 1px solid {Colors.ACCENT_BLUE};"
            f" border-radius: 4px;"
            f" outline: none;"
            f" padding: 2px;"
            f"}}"
            f"QListWidget::item {{ padding: 3px 6px; color: "
            f"{Colors.TEXT_PRIMARY}; }}"
            f"QListWidget::item:selected {{"
            f" background-color: {Colors.ACCENT_BLUE};"
            f" color: white; border-radius: 2px;"
            f"}}"
        )
        popup.itemClicked.connect(self._on_item_picked)
        self._popup = popup
        return popup

    # ------------------------------------------------------------------
    # Background database load
    # ------------------------------------------------------------------

    def _ensure_load_started(self) -> None:
        """Kick off the DB load in a background thread (once)."""
        if self._load_started or self._db.is_loaded:
            return
        self._load_started = True

        def worker():
            self._db.ensure_loaded()
            # Signal is thread-safe for queued cross-thread delivery.
            self._loaded.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _on_db_loaded(self) -> None:
        # If the user has already typed something, refresh now that data
        # is available.
        if self._edit.hasFocus() and len(self._edit.text()) >= self.MIN_CHARS:
            self._update_suggestions()

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # Qt keeps delivering events to installed filters while the
        # widget tree is being torn down, by which point this object's
        # Python attributes may already be gone. Raising inside a Qt
        # virtual callback is undefined behaviour rather than a caught
        # exception — under enough accumulated windows it segfaults —
        # so a half-destroyed filter must decline quietly instead.
        edit = getattr(self, "_edit", None)
        if edit is None:
            return False
        if obj is edit:
            et = event.type()
            if et == QEvent.Type.FocusIn:
                self._ensure_load_started()
            elif et == QEvent.Type.FocusOut:
                # The line edit loses focus when the user clicks the popup
                # (it's a separate top-level window). If we hid the popup
                # on every focus-out, that click would dismiss the popup on
                # mouse-DOWN, before the mouse-up could land on an item —
                # so itemClicked never fired and mouse selection appeared
                # broken (keyboard still worked because it never changes
                # focus). So: if the cursor is over the popup, the user is
                # picking from it — leave it up and let the click commit.
                if not self._cursor_over_popup():
                    self._hide_popup()
            elif et == QEvent.Type.KeyPress and (
                self._popup is not None and self._popup.isVisible()
            ):
                if self._handle_popup_key(event):
                    return True
        return super().eventFilter(obj, event)

    def _cursor_over_popup(self) -> bool:
        """True if the mouse cursor is over the visible popup — i.e. the
        user is in the middle of clicking a suggestion, so the popup must
        stay up long enough for the click to land."""
        popup = self._popup
        if popup is None or not popup.isVisible():
            return False
        # The popup is a top-level window, so its geometry is already in
        # global coordinates — directly comparable to the global cursor.
        return popup.geometry().contains(QCursor.pos())

    def _handle_popup_key(self, event) -> bool:
        key = event.key()
        if key == Qt.Key.Key_Down:
            row = min(self._popup.currentRow() + 1, self._popup.count() - 1)
            self._popup.setCurrentRow(max(0, row))
            return True
        if key == Qt.Key.Key_Up:
            row = self._popup.currentRow() - 1
            self._popup.setCurrentRow(max(0, row))
            return True
        if key == Qt.Key.Key_Tab:
            # Tab accepts the suggestion (the highlighted one, or the
            # first if none is highlighted).
            item = self._popup.currentItem() or (
                self._popup.item(0) if self._popup.count() else None
            )
            if item is not None:
                self._apply_choice(item.text())
                return True
            return False
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Enter commits the TYPED text by default, so the user can add
            # a short unique tag (e.g. "wa") without it being replaced by
            # the first suggestion ("water"). A suggestion is accepted on
            # Enter only if the user explicitly navigated to one with
            # Up/Down (arrow + Enter). Otherwise hide the popup and let
            # the line edit's normal Enter add exactly what was typed.
            if self._popup.currentRow() >= 0 and self._popup.currentItem():
                self._apply_choice(self._popup.currentItem().text())
                return True
            self._hide_popup()
            return False
        if key == Qt.Key.Key_Escape:
            self._hide_popup()
            return True
        return False

    def _on_text_edited(self, _text: str) -> None:
        # textEdited fires only on user input (not programmatic setText),
        # so applying a choice won't re-trigger the popup.
        self._ensure_load_started()
        self._timer.start()

    # ------------------------------------------------------------------
    # Suggestions
    # ------------------------------------------------------------------

    def current_term(self) -> tuple[str, int, int]:
        """(term, start, end) for the word the cursor sits in.

        FIELD REPORT: only the first tag ever got suggestions. The
        whole box was matched as one string, so once a space was
        typed "1girl sol" matched nothing and the popup went quiet
        for every term after the first. Search boxes here are
        space-separated lists, so the unit to complete is the term
        under the cursor.

        BUT: that space-splitting is only right for the Post Browser's
        space-separated SEARCH box (apply_entry_format=False). The tag-
        entry boxes (add-tag, conflict rules, audit exceptions, tag
        reference) each hold ONE tag, and in spaces mode a multi-word tag
        like "long hair" contains a space that is INTRA-tag, not a
        separator. Splitting there would complete only "hair". So for
        entry-format contexts the whole box is a single term.
        """
        text = self._edit.text()
        if self._apply_entry_format:
            # Single-tag box: the entire contents are one term.
            return text, 0, len(text)
        cursor = self._edit.cursorPosition()
        start = text.rfind(" ", 0, cursor) + 1
        end = text.find(" ", cursor)
        if end == -1:
            end = len(text)
        return text[start:end], start, end

    def _update_suggestions(self) -> None:
        text = self.current_term()[0].strip()
        if len(text) < self.MIN_CHARS or not self._db.is_loaded:
            self._hide_popup()
            return
        results = self._db.suggest(text, self.MAX_ITEMS)
        # Don't show a single suggestion identical to what's typed.
        if not results or (len(results) == 1 and results[0] == text.lower()):
            self._hide_popup()
            return
        popup = self._ensure_popup()
        popup.clear()
        for r in results:
            # Show the item in the format it will be inserted in, so the
            # popup preview matches the result. Insertion re-applies the
            # same formatting from the canonical tag, so display and
            # insert stay consistent even though the item text is shown
            # formatted here.
            QListWidgetItem(self._format_for_entry(r), popup)
        # No default highlight: with nothing selected, Enter commits the
        # typed text. The user must Tab or arrow-down to pick a suggestion.
        popup.setCurrentRow(-1)
        self._show_popup()

    def _show_popup(self) -> None:
        popup = self._ensure_popup()
        # Size and position the popup just beneath the line edit.
        rows = min(popup.count(), self.MAX_ITEMS)
        row_h = popup.sizeHintForRow(0) if popup.count() else 20
        # sizeHintForRow can return -1 before the view is laid out; guard
        # against a negative/zero height that would make the popup
        # collapse or render at a stray size.
        if row_h <= 0:
            row_h = 20
        popup.setFixedHeight(rows * row_h + 8)
        popup.setFixedWidth(max(self._edit.width(), 180))

        # Default position: directly beneath the box. But if that would
        # push the popup off the bottom of the screen (e.g. the add-tag
        # box near the window's bottom edge), flip it ABOVE the box so it
        # stays fully visible.
        below = self._edit.mapToGlobal(self._edit.rect().bottomLeft())
        pos = below
        screen = self._edit.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            popup_h = popup.height()
            if below.y() + popup_h > avail.bottom():
                above = self._edit.mapToGlobal(self._edit.rect().topLeft())
                flipped_y = above.y() - popup_h
                # Only flip if there's genuinely more room above; else keep
                # it below so it never lands off the top of the screen.
                if flipped_y >= avail.top():
                    pos = QPoint(below.x(), flipped_y)
        popup.move(pos)
        if not popup.isVisible():
            popup.show()

    def _hide_popup(self) -> None:
        if self._popup is not None:
            if self._popup.isVisible():
                self._popup.hide()
            self._popup.clear()

    def _on_item_picked(self, item: QListWidgetItem) -> None:
        self._apply_choice(item.text())

    def _format_for_entry(self, tag: str) -> str:
        """Apply the user's chosen tag-entry format to a canonical
        (underscore) suggestion, unless this instance opts out (Post
        Browser). Reads the setting live so toggling Preferences affects
        the next keystroke without a restart. Emoticons are protected by
        format_tag_for_entry itself."""
        if not self._apply_entry_format:
            return tag
        try:
            use_spaces = Settings().tags_use_spaces
        except Exception:
            return tag
        return tdb.format_tag_for_entry(tag, use_spaces)

    def _apply_choice(self, tag: str) -> None:
        # Replace only the term under the cursor, leaving the rest of
        # the query intact — these boxes hold space-separated lists,
        # so overwriting the whole line would destroy earlier terms.
        # setText (not insert) so textEdited does not fire and reopen
        # the popup.
        tag = self._format_for_entry(tag)
        _old, start, end = self.current_term()
        full = self._edit.text()
        self._edit.setText(full[:start] + tag + full[end:])
        self._edit.setCursorPosition(start + len(tag))
        self._hide_popup()
        self._edit.setFocus()
