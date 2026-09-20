"""
ui/bookmark_button.py

One star button, used by all three surfaces.

The three kinds of bookmark do different things when opened — a post
reopens, a tag opens its wiki, a query re-runs the search — but the
control itself should not differ, so it lives here once. Each window
supplies two small functions: what is currently on screen, and what to
do when a saved entry is picked.

A dropdown rather than a panel: bookmarks are consulted occasionally,
and a permanent list would take space from the thing being looked at.
The menu is rebuilt as it opens, so it can never show a stale list
after another window has added something — all three share one store.

The menu shows only the most recent few. A menu grows with its
contents, and sixty entries already reach the bottom of a 1080p
screen; past that Qt falls back to scroll arrows, which is a miserable
way to find anything. Everything beyond that lives in the manager
window, which is also the only place a bookmark can be removed without
navigating to it first.
"""
from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QToolButton

STAR_ON = "\u2605"      # filled: this one is saved
STAR_OFF = "\u2606"     # hollow: it is not

# Keeps the dropdown a glance rather than a scroll. Chosen so the menu
# stays well short of the shortest screen anyone is likely to use.
MENU_LIMIT = 12

_WORDS = {
    "post": ("post", "Saved posts"),
    "tag": ("tag", "Saved tags"),
    "query": ("search", "Saved searches"),
}


class BookmarkButton(QToolButton):
    def __init__(self, kind: str, store, current, activate,
                 parent=None) -> None:
        """kind     : "post", "tag" or "query"
        store    : the shared BookmarkStore
        current  : () -> (value, label[, sort]) or None when there is
                   nothing on screen worth saving
        activate : (Bookmark) -> None, run when one is picked
        """
        super().__init__(parent)
        self._kind = kind
        self._store = store
        self._current = current
        self._activate = activate
        noun, plural = _WORDS.get(kind, ("item", "Saved"))
        self._noun, self._plural = noun, plural

        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolTip(
            f"{plural}. Save the {noun} you are looking at, or pick "
            "one to return to it.\n\nStored as plain JSON beside your "
            "settings, so they stay readable even if the program will "
            "not start.")
        self._menu = QMenu(self)
        self._menu.aboutToShow.connect(self._rebuild)
        self.setMenu(self._menu)
        self.refresh()

    # ------------------------------------------------------------------
    def _value(self):
        try:
            found = self._current()
        except Exception:
            return None
        if not found:
            return None
        value = str(found[0] or "").strip()
        return None if not value else found

    def refresh(self) -> None:
        """Star filled when what is on screen is already saved."""
        found = self._value()
        saved = bool(found
                     and self._store.contains(self._kind, found[0]))
        self.setText(STAR_ON if saved else STAR_OFF)

    def _toggle(self) -> None:
        found = self._value()
        if not found:
            return
        value = str(found[0])
        label = str(found[1]) if len(found) > 1 else value
        sort = str(found[2]) if len(found) > 2 else ""
        self._store.toggle(self._kind, value, label, sort)
        self.refresh()

    def _rebuild(self) -> None:
        self._menu.clear()
        found = self._value()
        if found:
            saved = self._store.contains(self._kind, str(found[0]))
            act = QAction(
                (f"Remove this {self._noun} from bookmarks" if saved
                 else f"Bookmark this {self._noun}"), self._menu)
            act.triggered.connect(self._toggle)
            self._menu.addAction(act)
        else:
            act = QAction("Nothing to bookmark yet", self._menu)
            act.setEnabled(False)
            self._menu.addAction(act)
        self._menu.addSeparator()

        entries = self._store.all(self._kind)
        if not entries:
            empty = self._menu.addSeparator()
            empty.setText(f"No {self._plural.lower()}")
            return
        # A disabled QAction is greyed out but STILL highlights on
        # hover, so "Saved tags" looked like one more bookmark you
        # could click. A separator with a title is a real section
        # heading: Qt draws it differently and never highlights it.
        title = (self._plural if len(entries) <= MENU_LIMIT
                 else f"{self._plural} \u2014 most recent")
        header = self._menu.addSeparator()
        header.setText(title)
        for entry in entries[:MENU_LIMIT]:
            text = entry.display()
            if len(text) > 60:
                text = text[:57] + "\u2026"
            item = QAction(text, self._menu)
            item.triggered.connect(
                lambda _checked=False, b=entry: self._open(b))
            self._menu.addAction(item)
        # Always offered, not only when the list is long: it is the
        # only route to removing something you are not looking at.
        self._menu.addSeparator()
        manage = QAction(
            f"All {self._plural.lower()} ({len(entries)})\u2026",
            self._menu)
        manage.triggered.connect(self.open_manager)
        self._menu.addAction(manage)

    def open_manager(self) -> None:
        from ui.bookmark_manager import BookmarkManager

        self._manager = BookmarkManager(
            self._kind, self._store, self._open, self.window())
        self._manager.show()
        self._manager.raise_()

    def _open(self, entry) -> None:
        try:
            self._activate(entry)
        except Exception:
            pass
        self.refresh()
