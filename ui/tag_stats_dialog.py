"""
ui/tag_stats_dialog.py

Tools → Export Tag Statistics.

Field feature: produce a machine-and-AI-readable summary of the
dataset's tag distribution so a language model can reason about it —
above all the token-count-to-image-count RATIO, which predicts whether
unique trigger tokens will train reliably.

Filtering reuses the vocabulary buckets (tag_database.bucket_for, the
same classifier behind the tree filter and the audit): export
everything, or only custom tokens, or only color+known combos, in any
combination. Output is Markdown — save to file or copy straight to the
clipboard for pasting into an AI chat.

NSFW suppression is deliberately NOT implemented yet (design under
discussion with the user).
"""
from __future__ import annotations

import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.state import SessionState

BUCKET_TITLES = {
    "known": "Known tags (canonical & aliases)",
    "color_combo": "Color + known combos (unregistered compounds)",
    "custom": "Custom tags (not in Danbooru)",
}


def build_stats_report(
    state: SessionState,
    buckets: set[str],
    db_ok: bool,
) -> str:
    """Build the Markdown statistics report. Pure (no UI) for tests.

    `buckets` selects which vocabulary sections to include; ignored
    when db_ok is False (everything lands in one unclassified section).
    Ratio definition is stated in the report so an AI reader can't
    misinterpret it: images-with-tag / total-images (orphans included
    in the denominator; the captioned count is given so it can be
    recomputed against captioned-only if preferred).
    """
    total_images = len(state.all_images)
    # Three honest buckets (a field report caught the old label lying:
    # an EMPTY caption file was being reported as "no caption file").
    no_txt: list[str] = []
    empty_txt: list[str] = []
    for img in state.all_images:
        if not state._has_txt.get(img.image_path, False):
            no_txt.append(img.image_path.name)
        elif not state.get_image_tags(img.image_path):
            empty_txt.append(img.image_path.name)
    captioned = total_images - len(no_txt) - len(empty_txt)

    def _named(names: list[str]) -> str:
        """'3' or, when few, '3 (a.png, b.png, c.png)' so the user can
        actually FIND them in a large dataset."""
        if not names or len(names) > 8:
            return str(len(names))
        return f"{len(names)} ({', '.join(sorted(names))})"

    counts = dict(state._tag_counts)
    if db_ok:
        from core import tag_database as tdb
        by_bucket: dict[str, list[str]] = {
            "known": [], "color_combo": [], "custom": []}
        for t in counts:
            by_bucket[tdb.bucket_for(t)].append(t)
    else:
        by_bucket = {"unclassified": list(counts)}

    lines: list[str] = []
    lines.append("# TagWalker Tag Statistics")
    lines.append("")
    lines.append(
        f"Generated: {datetime.datetime.now().isoformat(timespec='seconds')}")
    # Multi-folder loads list EVERY root (field report: the header
    # named only the first of three loaded folders).
    roots = getattr(state, "roots", None) or [state.root]
    if len(roots) == 1:
        lines.append(f"Dataset root: {roots[0]}")
    else:
        lines.append(f"Dataset roots ({len(roots)}):")
        for r in roots:
            lines.append(f"  - {r}")
    lines.append(
        f"Images: {total_images}  \u00b7  with tags: {captioned}  "
        f"\u00b7  empty caption file: {_named(empty_txt)}  "
        f"\u00b7  no caption file: {_named(no_txt)}")
    if db_ok:
        lines.append(
            f"Unique tags: {len(counts)}  ("
            f"known {len(by_bucket['known'])}  \u00b7  "
            f"color-combo {len(by_bucket['color_combo'])}  \u00b7  "
            f"custom {len(by_bucket['custom'])})")
        included = [BUCKET_TITLES[b] for b in
                    ("known", "color_combo", "custom") if b in buckets]
        lines.append("Included below: " + "; ".join(included))
    else:
        lines.append(f"Unique tags: {len(counts)}")
        lines.append(
            "NOTE: the Danbooru vocabulary database was unavailable, so "
            "tags are NOT classified into known/combo/custom below.")
    lines.append("")
    lines.append("## How to read this (for AI reviewers)")
    lines.append("")
    lines.append(
        "`ratio` = images carrying the tag / total images "
        f"({total_images}). A low ratio on a unique/custom token means "
        "few training examples — it is unlikely to be learned "
        "reliably. A ratio near 1.00 on a non-global tag suggests "
        "over-tagging. `custom` tags are the dataset author's own "
        "tokens (or typos); `color_combo` tags follow booru "
        "color+noun grammar without being individually registered.")
    lines.append("")

    if db_ok:
        section_order = [b for b in ("known", "color_combo", "custom")
                         if b in buckets]
    else:
        section_order = ["unclassified"]

    for b in section_order:
        tags = by_bucket.get(b, [])
        title = BUCKET_TITLES.get(b, "All tags (unclassified)")
        lines.append(f"## {title} \u2014 {len(tags)} tags")
        lines.append("")
        if not tags:
            lines.append("(none)")
            lines.append("")
            continue
        lines.append("| tag | images | ratio |")
        lines.append("|---|---:|---:|")
        for t in sorted(tags, key=lambda x: (-counts[x], x)):
            n = counts[t]
            ratio = n / total_images if total_images else 0.0
            lines.append(f"| {t} | {n} | {ratio:.3f} |")
        lines.append("")
    return "\n".join(lines)


class TagStatsDialog(QDialog):
    def __init__(self, state: SessionState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("Export Tag Statistics")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Export per-tag image counts and count/image ratios\n"
            "as Markdown — for AI-assisted dataset review."
        ))

        # Load the vocabulary DB once for classification.
        from core import tag_database as tdb
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._db_ok = tdb.get_database().ensure_loaded()
        finally:
            QApplication.restoreOverrideCursor()

        self.chk_known = QCheckBox(BUCKET_TITLES["known"])
        self.chk_combo = QCheckBox(BUCKET_TITLES["color_combo"])
        self.chk_custom = QCheckBox(BUCKET_TITLES["custom"])
        for c in (self.chk_known, self.chk_combo, self.chk_custom):
            c.setChecked(True)
            layout.addWidget(c)
        if not self._db_ok:
            for c in (self.chk_known, self.chk_combo, self.chk_custom):
                c.setEnabled(False)
            layout.addWidget(QLabel(
                "Vocabulary database unavailable — the export will "
                "contain all tags, unclassified."))

        row = QHBoxLayout()
        self.btn_save = QPushButton("Export to File\u2026")
        self.btn_save.clicked.connect(self._export_file)
        self.btn_copy = QPushButton("Copy to Clipboard")
        self.btn_copy.clicked.connect(self._copy)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_copy)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        layout.addLayout(row)

    def _buckets(self) -> set[str]:
        b: set[str] = set()
        if self.chk_known.isChecked():
            b.add("known")
        if self.chk_combo.isChecked():
            b.add("color_combo")
        if self.chk_custom.isChecked():
            b.add("custom")
        return b

    def _build(self) -> str | None:
        buckets = self._buckets()
        if self._db_ok and not buckets:
            QMessageBox.information(
                self, "Nothing Selected",
                "Tick at least one tag group to export.")
            return None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return build_stats_report(self._state, buckets, self._db_ok)
        finally:
            QApplication.restoreOverrideCursor()

    def _export_file(self) -> None:
        report = self._build()
        if report is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Tag Statistics",
            "tagwalker_tag_stats.md", "Markdown (*.md);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(report)
        except OSError as exc:
            QMessageBox.warning(self, "Export Failed", str(exc))
            return
        QMessageBox.information(
            self, "Exported", f"Statistics written to:\n{path}")

    def _copy(self) -> None:
        report = self._build()
        if report is None:
            return
        QApplication.clipboard().setText(report)
        QMessageBox.information(
            self, "Copied",
            "Statistics copied — paste them into your AI chat.")
