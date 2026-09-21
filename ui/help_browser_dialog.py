"""
ui/help_browser_dialog.py

The User Guide window: a category list on the left, the selected topic's
text on the right in a scrollable pane. Replaces the old linear paged
"Getting Started" dialog — a browser you can jump around in rather than
page straight through.

Content comes from ui/help_content.HELP_SECTIONS as (title, body) pairs.
Bodies may contain {placeholders} for keybinds (e.g. {yes}); this dialog
substitutes the user's CURRENT bindings from Settings at display time, so
the guide never shows a stale default after someone rebinds a key.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Fonts, Spacing
from ui.help_content import HELP_SECTIONS


# Map the {placeholder} names used in help bodies to the settings keys
# that hold their keybinds. Kept here so the content file stays pure text.
_KEY_PLACEHOLDERS = {
    "yes": "shortcuts/yes",
    "no": "shortcuts/no",
    "skip": "shortcuts/skip_image",
    "back": "shortcuts/back",
    "close_zoom": "shortcuts/close_zoom",
    "undo": "shortcuts/undo",
    "save_session": "shortcuts/save_session",
    "wheel_modifier": "shortcuts/wheel_nav_modifier",
}


class HelpBrowserDialog(QDialog):
    """Two-pane help browser: categories left, scrollable body right."""

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("TagWalker \u2014 User Guide")
        self.setMinimumSize(760, 520)
        self.resize(900, 600)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- Left: category list ---------------------------------------
        self._list = QListWidget()
        self._list.setObjectName("helpCategoryList")
        self._list.setFixedWidth(240)
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        for title, _ in HELP_SECTIONS:
            QListWidgetItem(title, self._list)
        self._list.currentRowChanged.connect(self._on_select)
        outer.addWidget(self._list)

        # --- Right: scrollable body ------------------------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(
            Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE)
        right_layout.setSpacing(Spacing.NORMAL)

        self._heading = QLabel("")
        f = self._heading.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        self._heading.setFont(f)
        self._heading.setWordWrap(True)
        right_layout.addWidget(self._heading)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._body = QLabel("")
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._body.setTextFormat(Qt.TextFormat.PlainText)
        # let the label sit at the top of the scroll area
        body_holder = QWidget()
        holder_layout = QVBoxLayout(body_holder)
        holder_layout.setContentsMargins(0, 0, Spacing.NORMAL, 0)
        holder_layout.addWidget(self._body)
        holder_layout.addStretch(1)
        self._scroll.setWidget(body_holder)

        right_layout.addWidget(self._scroll, 1)
        outer.addWidget(right, 1)

        # Select the first topic.
        if self._list.count():
            self._list.setCurrentRow(0)

    # -- behaviour --------------------------------------------------------

    def _on_select(self, row: int) -> None:
        if row < 0 or row >= len(HELP_SECTIONS):
            return
        title, body = HELP_SECTIONS[row]
        self._heading.setText(title)
        self._body.setText(self._resolve(body))
        # scroll back to top when switching topics
        self._scroll.verticalScrollBar().setValue(0)

    def _resolve(self, body: str) -> str:
        """Replace {placeholder} keybind tokens with the user's current
        bindings. Unknown placeholders are left as-is (defensive)."""
        text = body
        for name, key in _KEY_PLACEHOLDERS.items():
            token = "{" + name + "}"
            if token not in text:
                continue
            bind = self._current_key(key)
            text = text.replace(token, bind)
        return text

    def _current_key(self, settings_key: str) -> str:
        """Read a keybind from settings, with a readable fallback."""
        try:
            val = self._settings.get_shortcut(settings_key)
        except Exception:
            val = None
        if not val:
            # Some actions are button-only or unbound; show a word rather
            # than an empty gap.
            return "(unbound)"
        return str(val)
