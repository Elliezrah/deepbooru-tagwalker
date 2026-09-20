"""
ui/diagnostic_log_dialog.py

Help -> Diagnostic Log. Shows the crash log the program has been
writing all along.

The log itself is not new — `core/crashlog.py` has been recording
unhandled exceptions since long before this window existed. What was
missing was any way to reach it. An error dialog would appear, the
user would dismiss it, and the diagnostic information was gone as far
as they were concerned even though it was sitting on disk. That is the
gap this closes: everything here reads a file that already exists.

Deliberately crash-only. A general session log — every click, every
request — grows until it is noise, and noise is worse than nothing
when someone is trying to describe what went wrong. If a freeze ever
needs diagnosing, that is a different feature with a different budget.

Nothing here writes to the log. The one destructive action, clearing
it, asks first and says what is being thrown away.
"""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core import crashlog

# A very large log is usually one error repeating. Showing the tail is
# both faster and more useful than the beginning of a week-old file.
TAIL_LIMIT = 400_000


class DiagnosticLogDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Diagnostic Log")
        self.setModal(False)
        self.resize(860, 560)

        layout = QVBoxLayout(self)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setToolTip(
            "Unhandled errors the program has recorded, newest at the "
            "bottom. Written automatically \u2014 this window only "
            "reads it.")
        layout.addWidget(self.summary)

        filter_row = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(
            "filter lines\u2026 (e.g. a file name, or Error)")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._render)
        filter_row.addWidget(self.filter, 1)
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.reload)
        filter_row.addWidget(btn_refresh)
        layout.addLayout(filter_row)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.NoWrap)
        font = self.view.font()
        font.setFamily("Consolas")
        font.setStyleHint(font.StyleHint.Monospace)
        self.view.setFont(font)
        layout.addWidget(self.view, 1)

        row = QHBoxLayout()
        self.btn_copy = QPushButton("Copy All")
        self.btn_copy.setToolTip(
            "Copy the whole log, so it can be pasted into a bug "
            "report without hunting for the file.")
        self.btn_copy.clicked.connect(self._copy)
        row.addWidget(self.btn_copy)
        self.btn_folder = QPushButton("Open Containing Folder")
        self.btn_folder.clicked.connect(self._open_folder)
        row.addWidget(self.btn_folder)
        self.btn_clear = QPushButton("Clear Log\u2026")
        self.btn_clear.setToolTip(
            "Delete the recorded errors. Worth doing once a problem "
            "is resolved, so the next one is easy to spot.")
        self.btn_clear.clicked.connect(self._clear)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        row.addWidget(btn_close)
        layout.addLayout(row)

        self._text = ""
        self.reload()

    # ------------------------------------------------------------------
    def log_path(self):
        return crashlog.get_log_path()

    def reload(self) -> None:
        path = self.log_path()
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        if len(raw) > TAIL_LIMIT:
            raw = ("[earlier entries omitted \u2014 showing the most "
                   "recent portion of a large log]\n\n"
                   + raw[-TAIL_LIMIT:])
        self._text = raw
        if raw.strip():
            entries = raw.count("=" * 20) or raw.count("Traceback")
            self.summary.setText(
                f"<b>{path}</b><br>{len(raw.splitlines()):,} lines"
                + (f" \u00b7 about {entries} recorded error(s)"
                   if entries else ""))
        else:
            self.summary.setText(
                f"<b>{path}</b><br>No errors recorded \u2014 which is "
                "the result you want.")
        self._render()
        # Newest entries are appended, so the useful end is the bottom.
        self.view.verticalScrollBar().setValue(
            self.view.verticalScrollBar().maximum())

    def _render(self) -> None:
        needle = (self.filter.text() or "").strip().lower()
        if not needle:
            self.view.setPlainText(self._text)
            return
        kept = [line for line in self._text.splitlines()
                if needle in line.lower()]
        self.view.setPlainText(
            "\n".join(kept) if kept
            else f"(no lines containing \u201c{needle}\u201d)")

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._text)
        self.summary.setText(self.summary.text()
                             + "<br><i>copied to clipboard</i>")

    def _open_folder(self) -> None:
        folder = self.log_path().parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _clear(self) -> None:
        if not self._text.strip():
            return
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Clear Log")
        confirm.setText("Delete the recorded errors?")
        confirm.setInformativeText(
            "If you have not reported this problem yet, copy the log "
            "first \u2014 it cannot be recovered.")
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes
                                   | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            self.log_path().write_text("", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Clear Log",
                                f"Could not clear the log: {exc}")
        self.reload()
