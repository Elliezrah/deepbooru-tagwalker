"""
ui/health_check_dialog.py

Tools → Dataset Health Check (opens in its own window, non-modal).

Field feature: find dataset errors before they poison a training run.
Three checks, each toggleable:

  - Missing caption file: the image has no .txt pair at all.
  - Empty caption file: the .txt exists but contains no tags
    (whitespace/commas only) — the case a field report showed being
    easy to miss by eye in a 1000+ image dataset.
  - Unusual resolution: min side under 512 px (won't bucket at
    training size without upscaling), aspect ratio beyond 3:1
    (extreme buckets), or the image file cannot be read at all.
  - Not perfectly square (OFF by default): flags every image whose
    width differs from its height — for non-bucketed training where
    all images must be square. Independent of the unusual-resolution
    check; an image can be flagged by both.
  - Stray caption files: .txt files in a loaded folder that pair with
    no image — usually a typo in a hand-named file (the field case:
    "name_(2)].txt" beside "name_(2).png") or the classic
    "image.png.txt" double-extension mistake. Near-miss strays name
    the similar image so the typo is obvious at a glance.

Results list one row per finding (an image can appear more than once)
with the concrete reason, e.g. "small: 100x100 (min side under 512)".
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from core.state import SessionState

MIN_SIDE = 512
MAX_ASPECT = 3.0

CHECK_MISSING = "missing_txt"
CHECK_EMPTY = "empty_txt"
CHECK_RESOLUTION = "resolution"
CHECK_STRAY = "stray_txt"
CHECK_SQUARE = "square"


def find_issues(state: SessionState, checks: set[str]) -> list[tuple[str, str, str]]:
    """Scan the dataset for the enabled checks. Pure logic (no UI) so
    tests can pin it. Returns (image_name, folder_name, problem) rows,
    sorted by problem kind then name."""
    rows: list[tuple[str, str, str]] = []
    for img in state.all_images:
        path = img.image_path
        name = path.name
        folder = path.parent.name
        has_txt = state._has_txt.get(path, False)
        if CHECK_MISSING in checks and not has_txt:
            rows.append((name, folder, "missing caption file"))
        if (CHECK_EMPTY in checks and has_txt
                and not state.get_image_tags(path)):
            rows.append((name, folder, "empty caption file (no tags)"))
        if CHECK_RESOLUTION in checks or CHECK_SQUARE in checks:
            # One header read serves both dimension-based checks; an
            # unreadable file is reported once whichever is enabled.
            try:
                from PIL import Image as _PILImage
            except ImportError:
                # Distinguished from an unreadable FILE. Reported once
                # and the checks are dropped, rather than declaring
                # every image in the dataset corrupt — which is what a
                # single catch-all did, and it looked entirely
                # plausible.
                rows.append((
                    "\u2014", "",
                    "resolution checks unavailable: the Pillow imaging "
                    "library is missing from this build"))
                checks = checks - {CHECK_RESOLUTION, CHECK_SQUARE}
                continue
            try:
                with _PILImage.open(path) as im:
                    w, h = im.size
            except Exception:
                rows.append((name, folder, "unreadable image file"))
                continue
            if CHECK_RESOLUTION in checks:
                if min(w, h) < MIN_SIDE:
                    rows.append((
                        name, folder,
                        f"small: {w}x{h} (min side under {MIN_SIDE})"))
                aspect = max(w, h) / max(1, min(w, h))
                if aspect > MAX_ASPECT:
                    rows.append((
                        name, folder,
                        f"extreme aspect: {w}x{h} "
                        f"(ratio {aspect:.1f})"))
            if CHECK_SQUARE in checks and w != h:
                rows.append((
                    name, folder, f"not square: {w}x{h}"))
    if CHECK_STRAY in checks:
        import difflib
        roots = getattr(state, "roots", None) or [state.root]
        expected: dict[Path, set[str]] = {}
        stems_by_dir: dict[Path, list[str]] = {}
        img_names_by_dir: dict[Path, set[str]] = {}
        for img in state.all_images:
            parent = img.image_path.parent
            expected.setdefault(parent, set()).add(
                img.txt_path.name.lower())
            stems_by_dir.setdefault(parent, []).append(
                img.image_path.stem)
            img_names_by_dir.setdefault(parent, set()).add(
                img.image_path.name.lower())
        for root in roots:
            root = Path(root)
            try:
                entries = sorted(root.iterdir())
            except OSError:
                continue
            exp = expected.get(root, set())
            for p in entries:
                if p.suffix.lower() != ".txt" or not p.is_file():
                    continue
                if p.name.startswith(".tw_tmp_"):
                    continue
                if p.name.lower() in exp:
                    continue
                stem = p.stem
                if stem.lower() in img_names_by_dir.get(root, set()):
                    proper = Path(stem).with_suffix(".txt").name
                    rows.append((
                        p.name, root.name,
                        "stray caption file (named after the full "
                        f"image filename — rename to {proper})"))
                    continue
                close = difflib.get_close_matches(
                    stem, stems_by_dir.get(root, []), n=1, cutoff=0.8)
                if close:
                    rows.append((
                        p.name, root.name,
                        "stray caption file (no matching image; "
                        f"similar: {close[0]})"))
                else:
                    rows.append((
                        p.name, root.name,
                        "stray caption file (no matching image)"))
    rows.sort(key=lambda r: (r[2], r[0]))
    return rows


class HealthCheckDialog(QDialog):
    def __init__(self, state: SessionState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("Dataset Health Check")
        self.setMinimumSize(520, 440)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Scan for dataset errors before training."))

        self.chk_missing = QCheckBox("Missing caption file (.txt)")
        self.chk_empty = QCheckBox("Empty caption file (no tags)")
        self.chk_res = QCheckBox(
            f"Unusual resolution (side < {MIN_SIDE}px, aspect > "
            f"{MAX_ASPECT:.0f}:1, or unreadable)")
        self.chk_stray = QCheckBox(
            "Stray caption files (.txt with no matching image)")
        for c in (self.chk_missing, self.chk_empty, self.chk_res,
                  self.chk_stray):
            c.setChecked(True)
            layout.addWidget(c)
        self.chk_square = QCheckBox(
            "Not perfectly square (for non-bucketed training)")
        self.chk_square.setChecked(False)   # field spec: OFF by default
        layout.addWidget(self.chk_square)

        row = QHBoxLayout()
        self.btn_run = QPushButton("Run Check")
        self.btn_run.clicked.connect(self._run)
        row.addWidget(self.btn_run)
        row.addStretch(1)
        layout.addLayout(row)

        self.summary = QLabel("")
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Folder", "Problem"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 180)
        self.tree.setColumnWidth(1, 110)
        layout.addWidget(self.tree, 1)

        self._run()

    def _checks(self) -> set[str]:
        checks: set[str] = set()
        if self.chk_missing.isChecked():
            checks.add(CHECK_MISSING)
        if self.chk_empty.isChecked():
            checks.add(CHECK_EMPTY)
        if self.chk_res.isChecked():
            checks.add(CHECK_RESOLUTION)
        if self.chk_stray.isChecked():
            checks.add(CHECK_STRAY)
        if self.chk_square.isChecked():
            checks.add(CHECK_SQUARE)
        return checks

    def _run(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rows = find_issues(self._state, self._checks())
        finally:
            QApplication.restoreOverrideCursor()
        self.tree.clear()
        for name, folder, problem in rows:
            QTreeWidgetItem(self.tree, [name, folder, problem])
        affected = len({r[0] for r in rows})
        total = len(self._state.all_images)
        if rows:
            self.summary.setText(
                f"{len(rows)} issue(s) found across {affected} "
                f"file(s)  ·  {total} images checked.")
        else:
            self.summary.setText(
                f"No issues found  ·  {total} images checked.")
