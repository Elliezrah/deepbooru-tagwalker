"""
ui/image_metadata_dialog.py

Shows the Stable-Diffusion generation metadata embedded in an image —
the prompt, negative prompt, and settings it was made with. Reached from
the queue's right-click menu as "Meta info…", kept separate from
"Properties…" on purpose: generation metadata can be long and dense
(a full prompt plus a settings line, or an entire ComfyUI graph), and
folding it into the properties sheet would bury the quick facts that
sheet exists to show. Two entries, each focused.

Read-only. Copy buttons let the user lift the prompt or the whole blob
out to reuse it; nothing here writes to the image or the caption.

The actual reading/parsing lives in core.metadata_reader; this file only
lays the result out.
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
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Fonts, Spacing
from core.metadata_reader import read_generation_metadata


class ImageMetadataDialog(QDialog):
    """Generation-metadata viewer for one image."""

    def __init__(self, entry, parent=None) -> None:
        super().__init__(parent)
        self._entry = entry
        path = Path(entry.image_path)
        self.setWindowTitle(f"{path.name} \u2014 Meta info")
        # Wider than tall: the two-column "book" layout puts the parsed
        # prompt/negative/settings on the left and the raw blob on the
        # right, so the window stays a sane height instead of running off
        # the screen as one long vertical stack.
        self.setMinimumWidth(720)
        self.setMinimumHeight(360)
        self.resize(860, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.NORMAL, Spacing.NORMAL,
                                  Spacing.NORMAL, Spacing.NORMAL)
        layout.setSpacing(Spacing.TIGHT)

        name = QLabel(f"<b>{path.name}</b>")
        name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(name)

        md = read_generation_metadata(path)

        source = QLabel(
            f"Source: {md.source}" if md.found and md.source
            else "Generation metadata")
        source.setProperty("role", "tertiary")
        layout.addWidget(source)
        layout.addWidget(self._rule())

        if not md.found:
            self._build_empty(layout, md.note)
        else:
            self._build_found(layout, md)

        # Close button, right-aligned.
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        close.setDefault(True)
        btn_row.addWidget(close)
        layout.addLayout(btn_row)

    # -- states -----------------------------------------------------------

    def _build_empty(self, layout: QVBoxLayout, note: str) -> None:
        msg = QLabel(note or "No generation metadata found in this image.")
        msg.setWordWrap(True)
        msg.setProperty("role", "secondary")
        layout.addWidget(msg)
        layout.addStretch(1)

    def _build_found(self, layout: QVBoxLayout, md) -> None:
        # Book layout: parsed, human-readable data on the LEFT (prompt,
        # negative, settings), the dense raw blob on the RIGHT. A splitter
        # divides them so the user can widen either side; each side scrolls
        # independently, so nothing can stretch the window vertically.
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- Left page: parsed data, in its own scroll area -------------
        left_inner = QWidget()
        left = QVBoxLayout(left_inner)
        left.setContentsMargins(0, 0, Spacing.NORMAL, 0)
        left.setSpacing(Spacing.TIGHT)

        if md.prompt:
            left.addWidget(self._heading("Prompt"))
            left.addWidget(self._text_block(md.prompt, height=110))
            left.addWidget(self._copy_button("Copy prompt", md.prompt))

        if md.negative:
            left.addWidget(self._heading("Negative prompt"))
            left.addWidget(self._text_block(md.negative, height=70))
            left.addWidget(self._copy_button("Copy negative", md.negative))

        if md.fields:
            left.addWidget(self._heading("Settings"))
            left.addWidget(self._fields_block(md.fields))

        if md.note:
            note = QLabel(md.note)
            note.setWordWrap(True)
            note.setProperty("role", "tertiary")
            left.addWidget(note)

        left.addStretch(1)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setWidget(left_inner)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        splitter.addWidget(left_scroll)

        # --- Right page: raw metadata, fills its side ------------------
        right_inner = QWidget()
        right = QVBoxLayout(right_inner)
        right.setContentsMargins(Spacing.NORMAL, 0, 0, 0)
        right.setSpacing(Spacing.TIGHT)
        if md.raw:
            right.addWidget(self._heading("Raw metadata"))
            # No fixed height — let it fill the column and scroll inside.
            raw_box = QPlainTextEdit()
            raw_box.setPlainText(md.raw)
            raw_box.setReadOnly(True)
            f = raw_box.font()
            f.setFamily(getattr(Fonts, "MONO", "monospace")
                        if hasattr(Fonts, "MONO") else "monospace")
            raw_box.setFont(f)
            raw_box.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            right.addWidget(raw_box, 1)
            right.addWidget(self._copy_button("Copy raw", md.raw))
        else:
            empty = QLabel("(no raw blob)")
            empty.setProperty("role", "tertiary")
            right.addWidget(empty)
            right.addStretch(1)
        splitter.addWidget(right_inner)

        # Left a touch narrower than right by default; both adjustable.
        splitter.setSizes([360, 460])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter, 1)

    # -- widgets ----------------------------------------------------------

    def _heading(self, text: str) -> QLabel:
        lbl = QLabel(text)
        f = lbl.font()
        f.setBold(True)
        lbl.setFont(f)
        return lbl

    def _text_block(self, text: str, height: int,
                    mono: bool = False) -> QPlainTextEdit:
        box = QPlainTextEdit()
        box.setPlainText(text)
        box.setReadOnly(True)
        box.setFixedHeight(height)
        if mono:
            f = box.font()
            f.setFamily(getattr(Fonts, "MONO", "monospace")
                        if hasattr(Fonts, "MONO") else "monospace")
            box.setFont(f)
        box.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        return box

    def _fields_block(self, fields: list[tuple[str, str]]) -> QFrame:
        frame = QFrame()
        grid = QVBoxLayout(frame)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        for k, v in fields:
            row = QHBoxLayout()
            row.setSpacing(Spacing.NORMAL)
            key = QLabel(f"{k}:")
            key.setProperty("role", "secondary")
            key.setMinimumWidth(120)
            key.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            val = QLabel(v)
            val.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setWordWrap(True)
            row.addWidget(key)
            row.addWidget(val, 1)
            grid.addLayout(row)
        return frame

    def _copy_button(self, label: str, payload: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setProperty("role", "tertiary")

        def _copy() -> None:
            cb = QGuiApplication.clipboard()
            if cb is not None:
                cb.setText(payload)
                btn.setText("Copied")
                # revert label shortly after, so it reads as feedback
                from PySide6.QtCore import QTimer
                QTimer.singleShot(1200, lambda: btn.setText(label))

        btn.clicked.connect(_copy)
        # Keep copy buttons compact / left-aligned via a wrapper is
        # overkill; the button's natural size is fine in this column.
        btn.setMaximumWidth(160)
        return btn

    def _rule(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line
