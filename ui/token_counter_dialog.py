"""
ui/token_counter_dialog.py

Stats → Token Counter (opens in its own window, non-modal).

Field feature: keep captions under the trainer's token limit. The
tokenizer is selectable — SDXL counts CLIP BPE tokens, the Flux
targets count with their own encoders (T5-XXL / Qwen3 / Mistral) via
core.multi_tokenizer, with NO approximation (a limit checker that
guessed would be worse than none). Lists the images whose captions
exceed the selected threshold for the chosen tokenizer.

Thresholds depend on the tokenizer: SDXL offers the CLIP chunk sizes
75 / 150 / 225; the Flux targets offer 256 / 512 (512 is the encoder
cap). Counts are cached per open/refresh; switching threshold
refilters instantly, switching tokenizer recounts. Refresh recounts
after caption edits.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QMenu,
    QMessageBox,
    QToolButton,
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from pathlib import Path

from core import dataset_marker as marker
from core import multi_tokenizer as mt
from core.state import SessionState

# Back-compat alias: the SDXL (CLIP) limits, still used as the default
# summary columns in the exported report. Per-tokenizer thresholds now
# come from core.multi_tokenizer; this tuple is the SDXL set.
THRESHOLDS = mt.get_target(mt.TARGET_SDXL).limits  # (75, 150, 225)

HELP_TEXT = """How token counting works

The tokenizer is selectable, because different trainer families use
different text encoders and the same caption counts differently:

• SDXL — CLIP BPE. Fed to the encoder in chunks of 75 content tokens
  (150 / 225 = two / three chunks). BOS/EOS excluded.
• Flux.1 — T5-XXL (SentencePiece).
• Flux.2 Klein — Qwen3 (byte-level BPE).
• Flux.2 Dev — Mistral-Small-3.2.
  The Flux encoders cap the sequence at 512 (longer prompts are
  truncated); 256 is a tighter optional budget.

Each mode counts with that encoder's REAL tokenizer, so the number
matches what the trainer sees — no cross-tokenizer approximation.

Rules of thumb under CLIP (exact examples; other tokenizers differ):
• Common words are usually 1 token: smile = 1, solo = 1.
• Every underscore is its OWN token: long_hair = 3
  (long + _ + hair); a_very_long_tag_name = 9.
• Every comma between tags = 1 token.
• Digits count one by one: 1girl = 2 (1 + girl); 12345 = 5.
• Rare or invented words split into several pieces:
  thighhighs = 3, aoi_my_oc = 5.
• Kaomoji and punctuation runs are tokens too: ^_^ counts.
Byte-level BPE (Qwen3) and SentencePiece (T5/Mistral) split words on
their own learned rules, so their counts for the same caption will
differ from CLIP — which is exactly why the tokenizer is selectable.

To reduce a caption's count: remove low-value tags first (each tag
costs its own tokens PLUS a comma), prefer one canonical tag over
several near-synonyms, and drop rare custom tokens that appear on
only a few images — they cost tokens without teaching anything.

Anything past the trainer's max_token_length is silently truncated
during training: tags at the tail of an over-long caption simply do
not condition the model."""


def build_token_report(
    state: SessionState,
    threshold: int,
    counts: list[tuple[str, str, int]],
    tokenizer_label: str = "SDXL (CLIP)",
    summary_thresholds: tuple[int, ...] = THRESHOLDS,
) -> str:
    """Markdown token-count report (pure, for tests) — same
    spirit as the tag-statistics export: header with every dataset
    root, a chunk summary for the tokenizer's own thresholds, then the
    captions exceeding the selected threshold.

    tokenizer_label / summary_thresholds default to the SDXL set so
    existing callers and tests keep their behaviour."""
    import datetime
    lines: list[str] = []
    lines.append("# TagWalker Caption Token Counts")
    lines.append("")
    lines.append(
        f"Generated: "
        f"{datetime.datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Tokenizer: {tokenizer_label}")
    roots = getattr(state, "roots", None) or [state.root]
    if len(roots) == 1:
        lines.append(f"Dataset root: {roots[0]}")
    else:
        lines.append(f"Dataset roots ({len(roots)}):")
        for r in roots:
            lines.append(f"  - {r}")
    total = len(counts)
    max_row = max(counts, key=lambda c: c[2], default=None)
    exceed = {t: sum(1 for c in counts if c[2] > t)
              for t in summary_thresholds}
    summary_bits = " · ".join(
        f"exceeding {t}: {exceed[t]}" for t in summary_thresholds)
    lines.append(f"Captions counted: {total} · {summary_bits}")
    if max_row:
        lines.append(
            f"Dataset max: {max_row[2]} tokens ({max_row[0]})")
    lines.append("")
    lines.append("## How to read this (for AI reviewers)")
    lines.append("")
    lines.append(
        f"Counts are exact {tokenizer_label} content tokens. Tokens "
        "beyond the trainer's max_token_length are silently truncated "
        "and do not condition the model. (Under CLIP: chunks of 75, and "
        "underscores/commas each cost one token, long_hair = 3.)")
    lines.append("")
    over = sorted((c for c in counts if c[2] > threshold),
                  key=lambda c: (-c[2], c[0]))
    lines.append(
        f"## Captions exceeding {threshold} tokens — "
        f"{len(over)}")
    lines.append("")
    if not over:
        lines.append("(none)")
        lines.append("")
    else:
        lines.append("| image | folder | tokens |")
        lines.append("|---|---|---:|")
        for name, folder, n in over:
            lines.append(f"| {name} | {folder} | {n} |")
        lines.append("")
    return "\n".join(lines)


class TokenCounterDialog(QDialog):
    def __init__(self, state: SessionState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        # The main window, used to re-scan after files are renamed —
        # every path the session holds is stale at that point.
        self._main = parent
        self.setWindowTitle("Token Counter")
        self.setMinimumSize(560, 440)
        self.setModal(False)

        # Start on the tokenizer chosen in Preferences so the tool opens
        # matching how the badge/filter count. Falls back to SDXL.
        self._target_key = mt.default_target_key()
        try:
            self._target_key = self._state.tokenizer_target()
        except Exception:
            pass

        layout = QVBoxLayout(self)
        self.header = QLabel("")
        layout.addWidget(self.header)

        # Tokenizer selector.
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Tokenizer:"))
        self._tokenizer = QComboBox()
        for t in mt.all_targets():
            self._tokenizer.addItem(t.label, t.key)
            # Per-item hover tooltip carries the detail (incl. which other
            # models share this tokenizer) without cluttering the visible
            # label. Shows only on hover over the option.
            self._tokenizer.setItemData(
                self._tokenizer.count() - 1, t.blurb, Qt.ItemDataRole.ToolTipRole)
        ti = self._tokenizer.findData(self._target_key)
        self._tokenizer.setCurrentIndex(ti if ti >= 0 else 0)
        self._tokenizer.currentIndexChanged.connect(self._on_tokenizer_changed)
        trow.addWidget(self._tokenizer)
        trow.addStretch(1)
        layout.addLayout(trow)

        row = QHBoxLayout()
        row.addWidget(QLabel("Threshold:"))
        # Threshold radios are rebuilt when the tokenizer changes (SDXL
        # 75/150/225 vs Flux 256/512). Held in a container row so we can
        # clear and repopulate.
        self._thresh_row = QHBoxLayout()
        self._radios: list[QRadioButton] = []
        row.addLayout(self._thresh_row)
        self._build_threshold_radios()
        row.addStretch(1)
        self.btn_help = QPushButton("?")
        self.btn_help.setFixedWidth(28)
        self.btn_help.setToolTip("How token counting works")
        self.btn_help.clicked.connect(self._show_help)
        row.addWidget(self.btn_help)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._recount)
        row.addWidget(self.btn_refresh)
        layout.addLayout(row)

        row2 = QHBoxLayout()
        self.btn_export = QPushButton("Export to File…")
        self.btn_export.clicked.connect(self._export_file)
        self.btn_copy = QPushButton("Copy to Clipboard")
        self.btn_copy.clicked.connect(self._copy)
        row2.addWidget(self.btn_export)
        row2.addWidget(self.btn_copy)
        self.btn_mark = QToolButton()
        self.btn_mark.setText(
            f"Mark over-limit files ({marker.MARK_PREFIX}\u2026)")
        self.btn_mark.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_mark.setToolTip(
            "Renames every image over the selected threshold, and its "
            f"caption, with a leading \u201c{marker.MARK_PREFIX}\u201d "
            "\u2014 sort the folder by name and they all float to the "
            "top, ready to judge in one pass.\n\nRe-running syncs: a "
            "caption pruned back under the limit loses its mark.")
        mark_menu = QMenu(self.btn_mark)
        act_mark = QAction("Mark files over the limit", mark_menu)
        act_mark.triggered.connect(lambda: self._do_marks(True))
        mark_menu.addAction(act_mark)
        act_clear = QAction("Remove all marks", mark_menu)
        act_clear.triggered.connect(lambda: self._do_marks(False))
        mark_menu.addAction(act_clear)
        self.btn_mark.setMenu(mark_menu)
        self._mark_menu = mark_menu          # test hook
        row2.addWidget(self.btn_mark)
        row2.addStretch(1)
        layout.addLayout(row2)

        self.summary = QLabel("")
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Image", "Folder", "Tokens"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 120)
        layout.addWidget(self.tree, 1)

        self._counts: list[tuple[str, str, int]] = []  # name, folder, n
        # Full paths alongside the display rows. Kept separate so the
        # report builder's tuple shape (and its tests) stay untouched.
        self._paths: list[tuple[Path, Path, int]] = []
        self._error: str | None = None
        self._refresh_header()
        self._recount()

    # ------------------------------------------------------------------
    def _current_target(self):
        return mt.get_target(self._target_key)

    def _refresh_header(self) -> None:
        self.header.setText(
            f"Captions exceeding the selected {self._current_target().label} "
            "token limit\n(exact counts with that tokenizer)."
        )

    def _build_threshold_radios(self) -> None:
        """(Re)create the threshold radios for the current tokenizer."""
        # Clear any existing radios.
        for rb in self._radios:
            rb.setParent(None)
        self._radios = []
        for t in self._current_target().limits:
            rb = QRadioButton(str(t))
            rb.toggled.connect(self._refilter)
            self._radios.append(rb)
            self._thresh_row.addWidget(rb)
        if self._radios:
            self._radios[0].setChecked(True)

    def _on_tokenizer_changed(self, _index: int) -> None:
        key = self._tokenizer.currentData()
        if key == self._target_key:
            return
        self._target_key = key
        self._refresh_header()
        # Rebuild thresholds for the new tokenizer, then recount (counts
        # are tokenizer-specific).
        # Disconnect old radios' signals implicitly by rebuilding.
        self._build_threshold_radios()
        self._recount()

    def threshold(self) -> int:
        limits = self._current_target().limits
        for rb, t in zip(self._radios, limits):
            if rb.isChecked():
                return t
        return limits[0]

    def _recount(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if not mt.ensure_loaded(self._target_key):
                self._error = (mt.get_error(self._target_key)
                               or f"{self._current_target().label} "
                                  "tokenizer unavailable")
                self._counts = []
                self._paths = []
                return
            self._error = None
            counts: list[tuple[str, str, int]] = []
            paths: list[tuple[Path, Path, int]] = []
            for img in self._state.all_images:
                tags = self._state.get_image_tags(img.image_path)
                if not tags:
                    continue
                n = mt.count_tokens(", ".join(tags), self._target_key)
                counts.append(
                    (img.image_path.name, img.image_path.parent.name, n))
                paths.append((img.image_path, img.txt_path, n))
            self._counts = counts
            self._paths = paths
        finally:
            QApplication.restoreOverrideCursor()
        self._refilter()

    def _refilter(self) -> None:
        # Radio toggles fire during threshold-radio (re)construction, which
        # can happen before the tree/summary exist (initial build) — no-op
        # until the dialog is fully built. _recount() calls _refilter again
        # once everything is in place.
        if not hasattr(self, "tree") or not hasattr(self, "summary"):
            return
        self.tree.clear()
        if self._error:
            self.summary.setText(self._error)
            return
        limit = self.threshold()
        over = [c for c in self._counts if c[2] > limit]
        over.sort(key=lambda c: (-c[2], c[0]))
        for name, folder, n in over:
            QTreeWidgetItem(self.tree, [name, folder, str(n)])
        max_n = max((c[2] for c in self._counts), default=0)
        self.summary.setText(
            f"{len(over)} of {len(self._counts)} captions exceed "
            f"{limit} tokens  \u00b7  dataset max: {max_n} tokens")

    def _do_marks(self, over_limit_only: bool) -> None:
        """Rename over-limit files so they sort to the top of the
        folder — or, with over_limit_only False, strip every mark.

        Renaming is the most destructive thing this application does,
        so the plan is built and shown first, and nothing is written
        until the user agrees to a stated number of changes.
        """
        if self._error:
            QMessageBox.warning(self, "Token Counter", self._error)
            return
        if not self._paths:
            QMessageBox.information(
                self, "Mark Over-Limit Files",
                "There are no captions to mark.")
            return

        limit = self.threshold()
        over = ({p for p, _t, n in self._paths if n > limit}
                if over_limit_only else set())
        collisions = {
            name
            for group in getattr(self._state, "caption_collisions", [])
            or []
            for name in group}
        plan = marker.plan_marks(
            [(img, txt) for img, txt, _n in self._paths],
            over, collisions)

        if plan.is_empty():
            note = ""
            if plan.skipped:
                note = (f"\n\n{len(plan.skipped)} file(s) were "
                        "skipped because two images share one caption "
                        "file — renaming either would orphan the "
                        "other.")
            QMessageBox.information(
                self, "Mark Over-Limit Files",
                ("Every file is already marked correctly \u2014 "
                 "nothing to change." if over_limit_only else
                 "There are no marks to remove.") + note)
            return

        sample = "\n".join(
            f"  {p.name}  \u2192  {marker.marked_name(p.name)}"
            for p, _t in plan.to_mark[:5])
        sample += "\n".join(
            f"  {p.name}  \u2192  {marker.unmarked_name(p.name)}"
            for p, _t in plan.to_unmark[:5])
        more = len(plan.to_mark) + len(plan.to_unmark) - 5
        if more > 0:
            sample += f"\n  \u2026and {more} more"

        box = QMessageBox(self)
        box.setWindowTitle("Mark Over-Limit Files")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"Rename files on disk? ({plan.summary()})")
        box.setInformativeText(
            "Each image and its caption are renamed together, so the "
            "pairing your trainer relies on is preserved. This changes "
            "files on disk and is not covered by Undo \u2014 "
            "\u201cRemove all marks\u201d reverses it.\n\n"
            + sample)
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        result = marker.apply_plan(plan)
        self._last_result = result           # test hook
        lines = [f"Marked: {result.marked}",
                 f"Unmarked: {result.unmarked}"]
        if result.skipped:
            lines.append("")
            lines.append(f"Skipped {len(result.skipped)}:")
            for path, reason in result.skipped[:8]:
                lines.append(f"  {path.name} \u2014 {reason}")
            if len(result.skipped) > 8:
                lines.append(f"  \u2026and {len(result.skipped) - 8} more")
        QMessageBox.information(self, "Mark Over-Limit Files",
                                "\n".join(lines))

        # Every path the session holds now points at a file that has
        # moved, so a re-scan is not optional. This window's rows are
        # stale for the same reason, hence closing it.
        self._rescan_after_rename()

    def _rescan_after_rename(self) -> None:
        roots = getattr(self._state, "roots", None)
        if not roots:
            root = getattr(self._state, "root", None)
            roots = [root] if root else []
        main = self._main
        self.close()
        if roots and main is not None and hasattr(main, "_start_scan"):
            main._start_scan(list(roots))

    def _show_help(self) -> None:
        QMessageBox.information(self, "Token Counting Rules", HELP_TEXT)

    def _report(self) -> str | None:
        if self._error:
            QMessageBox.warning(self, "Token Counter", self._error)
            return None
        tgt = self._current_target()
        return build_token_report(
            self._state, self.threshold(), self._counts,
            tokenizer_label=tgt.label, summary_thresholds=tgt.limits)

    def _export_file(self) -> None:
        report = self._report()
        if report is None:
            return
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Token Counts",
            "tagwalker_token_counts.md",
            "Markdown (*.md);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(report)
        except OSError as exc:
            QMessageBox.warning(self, "Export Failed", str(exc))
            return
        QMessageBox.information(
            self, "Exported", f"Token counts written to:\n{path}")

    def _copy(self) -> None:
        report = self._report()
        if report is None:
            return
        QApplication.clipboard().setText(report)
        QMessageBox.information(
            self, "Copied",
            "Token counts copied \u2014 paste them into your AI chat.")
