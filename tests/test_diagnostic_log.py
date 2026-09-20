"""
tests/test_diagnostic_log.py

Help -> Diagnostic Log.

The log itself is not new: core/crashlog.py has been recording
unhandled exceptions all along. What was missing was any way to reach
it — an error dialog appeared, the user dismissed it, and as far as
they were concerned the diagnostic information was gone, even though
it was sitting on disk the whole time.

So these tests are mostly about the reading end, and about one
guarantee worth stating: this window never writes to the log. The only
destructive action is clearing it, which asks first.

Run: python3 tests/test_diagnostic_log.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import crashlog
from ui.diagnostic_log_dialog import DiagnosticLogDialog

QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes
QMessageBox.warning = lambda *a, **k: None


def test_reads_what_the_program_recorded() -> None:
    dlg = DiagnosticLogDialog()
    # An empty log is the good outcome and should read like one.
    assert "No errors recorded" in dlg.summary.text()
    assert str(crashlog.get_log_path()) in dlg.summary.text()

    # Written by the real logger, not by the test poking a file.
    crashlog.log_error_text(
        "tag walk",
        'Traceback (most recent call last):\n'
        '  File "ui/queue_panel.py", line 42\n'
        'ValueError: something specific went wrong')
    crashlog.log_error_text(
        "danbooru fetch",
        'Traceback (most recent call last):\n'
        '  File "ui/danbooru_fetcher.py", line 7\n'
        'TimeoutError: the reply never arrived')
    dlg.reload()
    shown = dlg.view.toPlainText()
    assert "ValueError" in shown and "TimeoutError" in shown
    print("OK: the viewer shows the errors crashlog recorded, and an "
          "empty log reads as the good news it is")


def test_filter_copy_and_clear() -> None:
    dlg = DiagnosticLogDialog()
    dlg.reload()

    dlg.filter.setText("timeout")
    assert "TimeoutError" in dlg.view.toPlainText()
    assert "ValueError" not in dlg.view.toPlainText()
    dlg.filter.setText("zzz-no-such-thing")
    assert "no lines containing" in dlg.view.toPlainText()
    dlg.filter.setText("")
    assert "ValueError" in dlg.view.toPlainText()

    # Copying the whole log matters more than reading it here: it is
    # how a traceback reaches a bug report.
    dlg._copy()
    clipboard = QApplication.clipboard().text()
    assert "ValueError" in clipboard and "TimeoutError" in clipboard

    dlg._clear()
    assert not crashlog.get_log_path().read_text(
        encoding="utf-8").strip()
    assert "No errors recorded" in dlg.summary.text()
    print("OK: the log filters, copies whole to the clipboard, and "
          "clears only after confirming")


def test_reachable_from_the_help_menu() -> None:
    from ui.main_window import MainWindow

    s = Settings()
    initialize_theme(s.theme)
    mw = MainWindow(s)
    labels: list = []
    for menu in mw.findChildren(QMenu):
        if menu.title() and "Help" in menu.title():
            labels = [a.text() for a in menu.actions() if a.text()]
    assert any("Diagnostic Log" in t for t in labels), labels
    # And a route straight to the folder, for opening the file in a
    # real editor.
    assert any("Log File Location" in t for t in labels), labels

    mw._action_diagnostic_log()
    assert mw._diag_log_dlg.isVisible()
    assert mw._diag_log_dlg.log_path() == crashlog.get_log_path()
    print("OK: Help offers both the viewer and the folder, and the "
          "viewer opens on the real log")


def test_the_viewer_never_writes_to_the_log() -> None:
    """Everything except Clear is read-only, deliberately: a window
    for inspecting a problem must not become part of it."""
    crashlog.log_error_text("x", "RuntimeError: keep me")
    before = crashlog.get_log_path().read_bytes()
    dlg = DiagnosticLogDialog()
    dlg.reload()
    dlg.filter.setText("keep")
    dlg._copy()
    dlg.reload()
    assert crashlog.get_log_path().read_bytes() == before
    print("OK: opening, filtering, copying and refreshing leave the "
          "log byte-identical")


def run() -> None:
    test_reads_what_the_program_recorded()
    test_filter_copy_and_clear()
    test_reachable_from_the_help_menu()
    test_the_viewer_never_writes_to_the_log()
    print("\nALL PASS: diagnostic log")


if __name__ == "__main__":
    run()
