"""
tests/test_edit_menu.py

The Edit menu.

It held one item — "Undo last action" — for a program whose whole
premise is that every change is reversible. The additions are the ones
that earn a place: undo everything, copy the current caption, select
the whole queue.

THERE IS DELIBERATELY NO REDO, and that is worth recording so it is
not added later as a menu-filling exercise. Undo entries are closures
that capture how to REVERSE an action; nothing captures how to
re-apply one. Redo would mean a forward closure at every push site in
core/state.py — the file that writes captions to disk — for a payoff
that is thin, since in a Yes/No/Skip walk redo is answering the same
way again.

Run: python3 tests/test_edit_menu.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PIL import Image
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core.scanner import scan
from core.state import SessionState
from ui.main_window import MainWindow

QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
QMessageBox.information = lambda *a, **k: None
QMessageBox.warning = lambda *a, **k: None


def _window(count: int = 6):
    s = Settings()
    initialize_theme(s.theme)
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    for i in range(count):
        Image.new("RGB", (24, 24)).save(folder / f"i{i}.png")
        (folder / f"i{i}.txt").write_text("1girl, solo, smile\n",
                                          encoding="utf-8")
    mw = MainWindow(s)
    mw._adopt_state(SessionState(scan(folder)), scan_root=None)
    return mw, folder


def _captions(folder: Path) -> dict:
    # Compared stripped: the program normalises a trailing newline
    # onto every caption it writes, which is correct and would
    # otherwise read as a difference.
    return {p.name: p.read_text(encoding="utf-8").strip()
            for p in sorted(folder.glob("*.txt"))}


def test_undo_all_returns_every_caption_to_disk_state() -> None:
    """Implemented by replaying the existing undo rather than as a
    bulk state reset. Every action then reverses the way it already
    knows how to — a granular tag edit and a batch operation alike —
    instead of a second implementation of the same thing getting less
    testing than the first."""
    mw, folder = _window()
    mw._state.select_tag("1girl")
    before = _captions(folder)

    for _ in range(5):
        mw._state.record_no()
    assert _captions(folder) != before      # it really did write

    mw._action_undo_all()
    assert _captions(folder) == before
    assert not mw._state.can_undo()
    print("OK: undo all returns every caption to the state it was in "
          "on disk when the dataset was loaded")


def test_undo_all_is_safe_with_nothing_to_undo() -> None:
    mw, _folder = _window()
    mw._action_undo_all()                   # must not raise or loop
    assert not mw._state.can_undo()
    print("OK: undo all on an empty stack does nothing quietly")


def test_copy_caption_and_select_all() -> None:
    mw, _folder = _window()
    mw._state.select_tag("1girl")
    mw._action_copy_caption()
    clipboard = QGuiApplication.clipboard().text()
    assert "1girl" in clipboard
    assert ", " in clipboard                # the form a trainer wants

    mw._action_select_all()
    assert len(mw._queue_panel.list_widget.selectedItems()) >= 6
    print("OK: the current caption copies comma-separated, and select "
          "all selects the queue")


def test_there_is_no_redo() -> None:
    """Guarding a deliberate omission. If redo is ever added it should
    be because someone decided the forward-closure work is worth it,
    not because the menu looked sparse."""
    from PySide6.QtGui import QAction

    mw, _folder = _window()
    labels = [(a.text() or "").lower()
              for a in mw.findChildren(QAction)]
    assert not any("redo" in text for text in labels)
    # And the things that DO exist are all present.
    assert any("undo all" in text for text in labels)
    assert any("copy current caption" in text for text in labels)
    print("OK: no Redo action exists, and the three that replaced the "
          "idea all do")


def run() -> None:
    test_undo_all_returns_every_caption_to_disk_state()
    test_undo_all_is_safe_with_nothing_to_undo()
    test_copy_caption_and_select_all()
    test_there_is_no_redo()
    print("\nALL PASS: edit menu")


if __name__ == "__main__":
    run()
