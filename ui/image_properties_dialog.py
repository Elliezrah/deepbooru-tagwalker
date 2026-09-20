"""
ui/image_properties_dialog.py

Everything known about one image and its caption, in one place.

Modelled on the Windows file properties sheet because that is the
shape people already know: label on the left, value on the right,
grouped under headings, nothing to interpret. The additions are the
ones this program can make and Explorer cannot — the caption's token
count against the training limits, and what the caption contains.

Read-only by construction. Nothing here writes, renames or deletes.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from config.theme import Colors, Fonts, Spacing

# Caption limits, matching the rest of the program.
SOFT_LIMIT = 150
HARD_LIMIT = 225


def _human_size(n: int) -> str:
    """Bytes, the way a file manager writes them: a rounded figure with
    the exact byte count beside it, because both get used."""
    if n < 1024:
        return f"{n:,} bytes"
    units = ["KB", "MB", "GB"]
    size = float(n)
    unit = units[0]
    for unit in units:
        size /= 1024.0
        if size < 1024.0:
            break
    return f"{size:,.1f} {unit}  ({n:,} bytes)"


class ImagePropertiesDialog(QDialog):
    """A properties sheet for one image."""

    def __init__(self, entry, state, parent=None) -> None:
        super().__init__(parent)
        self._entry = entry
        self._state = state
        self._rows: list[tuple[str, str]] = []

        path = Path(entry.image_path)
        self.setWindowTitle(f"{path.name} — Properties")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.NORMAL, Spacing.NORMAL,
                                  Spacing.NORMAL, Spacing.NORMAL)
        layout.setSpacing(Spacing.TIGHT)

        name = QLabel(f"<b>{path.name}</b>")
        name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(name)
        layout.addWidget(self._rule())

        self._build(layout, path)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        copy = QPushButton("Copy all")
        copy.setToolTip(
            "Copy every field below as text \u2014 useful when "
            "reporting something odd about one image.")
        copy.clicked.connect(self._copy_all)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------
    def _rule(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {Colors.BORDER_SUBTLE};")
        return line

    def _heading(self, layout, text: str, first: bool = False) -> None:
        label = QLabel(f"<b>{text}</b>")
        label.setContentsMargins(0, 0 if first else 12, 0, 2)
        layout.addWidget(label)

    def _row(self, layout, label: str, value: str,
             colour: str = "", tip: str = "") -> None:
        """One label/value pair, aligned like a properties sheet."""
        self._rows.append((label, value))
        row = QHBoxLayout()
        row.setSpacing(Spacing.TIGHT)
        left = QLabel(label)
        left.setProperty("role", "secondary")
        left.setFixedWidth(132)
        left.setAlignment(Qt.AlignmentFlag.AlignTop
                          | Qt.AlignmentFlag.AlignLeft)
        row.addWidget(left)
        shown = (f"<span style='color:{colour};'>{value}</span>"
                 if colour else value)
        right = QLabel(shown)
        right.setWordWrap(True)
        right.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(right, 1)
        layout.addLayout(row)
        if tip:
            left.setToolTip(tip)
            right.setToolTip(tip)

    # ------------------------------------------------------------------
    def _build(self, layout, path: Path) -> None:
        entry = self._entry

        self._heading(layout, "Image", first=True)
        self._row(layout, "Type",
                  f"{path.suffix.lstrip('.').upper()} image "
                  f"({path.suffix.lower()})")
        width, height, note = self._dimensions(path)
        if width:
            megapixels = (width * height) / 1_000_000
            self._row(layout, "Dimensions",
                      f"{width:,} \u00d7 {height:,} px "
                      f"({megapixels:.1f} MP)")
            ratio = self._aspect(width, height)
            self._row(layout, "Aspect ratio", ratio)
        elif note:
            self._row(layout, "Dimensions", note, Colors.TEXT_SECONDARY)
        self._row(layout, "Size", self._size_of(path))

        self._heading(layout, "Location")
        folder = getattr(entry, "subfolder", "") or ""
        self._row(layout, "Folder", folder or "(dataset root)")
        self._row(layout, "Full path", str(path))

        self._heading(layout, "Caption")
        txt = Path(entry.txt_path)
        if not txt.exists():
            self._row(layout, "File", "none \u2014 no .txt beside this "
                                      "image", Colors.WARNING_AMBER)
            return
        self._row(layout, "File", txt.name)
        self._row(layout, "Size", self._size_of(txt))

        tags = []
        if self._state is not None:
            tags = self._state.get_image_tags(entry.image_path) or []
        self._row(layout, "Tags", f"{len(tags):,}")

        tokens = self._tokens(tags)
        if tokens is not None:
            if tokens > HARD_LIMIT:
                colour = Colors.DANGER_RED
                suffix = f"  \u2014 over the {HARD_LIMIT}-token limit"
            elif tokens > SOFT_LIMIT:
                colour = Colors.WARNING_AMBER
                suffix = f"  \u2014 approaching {HARD_LIMIT}"
            else:
                colour = ""
                suffix = ""
            self._row(
                layout, "CLIP tokens", f"{tokens:,}{suffix}", colour,
                tip=("Counted with the same tokeniser your trainer "
                     "uses. Past the limit the tail is cut, and with "
                     "caption shuffling on a different tail is cut "
                     "every step."))
        if tags:
            preview = ", ".join(tags[:12])
            if len(tags) > 12:
                preview += f", \u2026 (+{len(tags) - 12} more)"
            self._row(layout, "Contents", preview)

        self._heading(layout, "This session")
        self._row(layout, "Decisions recorded", self._decisions())

    # ------------------------------------------------------------------
    def _dimensions(self, path: Path):
        """(width, height, note). Pillow reads the header only, so this
        does not decode the whole image."""
        try:
            from PIL import Image
        except ImportError:
            return (0, 0, "unavailable \u2014 the Pillow imaging "
                          "library is missing from this build")
        try:
            with Image.open(path) as im:
                return (im.width, im.height, "")
        except Exception:
            return (0, 0, "could not be read")

    def _aspect(self, width: int, height: int) -> str:
        from math import gcd

        divisor = gcd(width, height) or 1
        w, h = width // divisor, height // divisor
        if w > 40 or h > 40:
            # An awkward ratio reduces to something unreadable like
            # 1327:996; the decimal is more use than the fraction.
            return f"{width / height:.2f} : 1"
        shape = ("square" if w == h
                 else "landscape" if w > h else "portrait")
        return f"{w}:{h}  ({shape})"

    def _size_of(self, path: Path) -> str:
        try:
            return _human_size(path.stat().st_size)
        except OSError:
            return "unreadable"

    def _tokens(self, tags: list):
        if not tags:
            return 0
        try:
            from core import clip_token_counter as ctc
            return int(ctc.count_tokens(", ".join(tags)))
        except Exception:
            return None

    def _decisions(self) -> str:
        """How many tags have been answered for this image, which is
        the one thing Explorer could never tell you."""
        if self._state is None:
            return "\u2014"
        try:
            from core.state import Decision
            path = self._entry.image_path
            counts = {"yes": 0, "no": 0, "skipped": 0}
            for (image, _tag), decision in self._state._decisions.items():
                if image != path:
                    continue
                if decision == Decision.YES:
                    counts["yes"] += 1
                elif decision == Decision.NO:
                    counts["no"] += 1
                elif decision == Decision.SKIPPED:
                    counts["skipped"] += 1
        except Exception:
            return "\u2014"
        if not any(counts.values()):
            return "none yet"
        parts = [f"{n} {name}" for name, n in counts.items() if n]
        return ", ".join(parts)

    def _copy_all(self) -> None:
        lines = [f"{label}: {value}" for label, value in self._rows]
        QGuiApplication.clipboard().setText("\n".join(lines))
