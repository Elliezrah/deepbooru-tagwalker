"""Regression test: the QUEUE PANEL actually renders matching images in
multi-select mode.

The engine (test_multi_select.py) verifies get_current_queue() is right.
This verifies the panel *displays* it: multi-select sets current_tag to
None by design, and the queue panel used to clear its list whenever
current_tag was None — so nothing showed no matter what was ticked. This
asserts the panel's list_widget.count() matches the filtered queue under
each filter mode.

Run: python3 tests/test_multi_select_queue_render.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.state import SessionState, FilterMode
from core.scanner import scan


def run() -> None:
    app = QApplication.instance() or QApplication([])

    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)

    def mk(name, tags):
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")

    for i in range(6):
        mk(f"a{i}", ["1girl", "apple"])
    for i in range(4):
        mk(f"b{i}", ["1girl", "banana"])

    from config.settings import Settings
    from config.theme import initialize_theme
    s = Settings()
    initialize_theme(s.theme)
    from ui.main_window import MainWindow

    w = MainWindow(s)
    st = SessionState(scan(d))
    w._adopt_state(st)
    w.show()
    app.processEvents()
    qlist = w._queue_panel.list_widget

    def tick(tag, on=True):
        it = w._tag_tree._tag_items[tag]
        it.setCheckState(
            0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        )
        app.processEvents()

    # Enter multi-select: empty selection + default ALL filter -> all rows.
    w._tag_tree.multi_select_btn.setChecked(True)
    app.processEvents()
    assert qlist.count() == 10, f"entry should show all 10, got {qlist.count()}"

    # Tick apple, then check each filter mode renders the right rows.
    tick("apple", True)

    st.set_filter_mode(FilterMode.HAS_TAG)
    app.processEvents()
    assert qlist.count() == 6, f"HAS_TAG+apple should show 6, got {qlist.count()}"

    st.set_filter_mode(FilterMode.MISSING_TAG)
    app.processEvents()
    assert qlist.count() == 4, \
        f"MISSING_TAG+apple should show 4, got {qlist.count()}"

    st.set_filter_mode(FilterMode.ALL)
    app.processEvents()
    assert qlist.count() == 10, f"ALL should show 10, got {qlist.count()}"

    # Untick -> empty selection. HAS_TAG with nothing ticked -> 0 rows.
    tick("apple", False)
    st.set_filter_mode(FilterMode.HAS_TAG)
    app.processEvents()
    assert qlist.count() == 0, \
        f"HAS_TAG with no ticks should show 0, got {qlist.count()}"

    # Leaving multi-select restores normal single-tag walking.
    w._tag_tree.multi_select_btn.setChecked(False)
    app.processEvents()
    assert not st.multi_select_mode

    print("OK: queue panel renders multi-select matches under every filter "
          "(entry=10, HAS=6, MISSING=4, ALL=10, empty-HAS=0, clean exit)")


if __name__ == "__main__":
    run()
