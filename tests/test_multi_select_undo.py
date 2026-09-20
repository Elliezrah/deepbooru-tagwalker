"""Regression test: undoing an Add in the multi-select finder restores
the image to the finder list AND lands the cursor on it.

Background: adding a ticked tag in the MISSING finder makes the image
stop matching, so it correctly drops out (live-shrink). Undo removes the
tag again, but live-shrink has no mirror that re-adds the now-matching
image — so the finder would silently under-count. undo() rebuilds the
multi queue and repositions the cursor on the restored image.

Exercises the real Edit -> Undo menu handler (_action_undo) and the real
tree checkbox, end to end through the window.

Run: python3 tests/test_multi_select_undo.py
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

    for i in range(3):
        mk(f"has{i}", ["1girl", "smile"])
    for i in range(5):
        mk(f"no{i}", ["1girl"])

    from config.settings import Settings
    from config.theme import initialize_theme
    s = Settings()
    initialize_theme(s.theme)
    from ui.main_window import MainWindow

    w = MainWindow(s)
    st = SessionState(scan(d))
    w._adopt_state(st)
    w.show()
    st.select_tag("1girl")
    app.processEvents()
    qp = w._queue_panel

    # Enter multi-select, MISSING, tick the real checkbox.
    w._tag_tree.multi_select_btn.setChecked(True)
    app.processEvents()
    st.set_filter_mode(FilterMode.MISSING_TAG)
    app.processEvents()
    w._tag_tree._tag_items["smile"].setCheckState(0, Qt.CheckState.Checked)
    app.processEvents()

    rows_before = qp.list_widget.count()
    assert rows_before == 5, f"5 rows in the finder, got {rows_before}"
    first = st.current_image.image_path.name

    # Add via the button: the image leaves the finder.
    w._image_panel.trigger_yes()
    app.processEvents()
    assert qp.list_widget.count() == 4, \
        f"list should shrink to 4, got {qp.list_widget.count()}"

    # Undo via the real Edit -> Undo menu handler.
    w._action_undo()
    app.processEvents()

    # File reverted.
    fixed = [p for p in st._image_tags if p.name == first][0]
    assert "smile" not in st._image_tags[fixed], "undo should remove the tag"
    # Finder list grew back (the UI re-rendered, not just the engine).
    assert qp.list_widget.count() == 5, \
        f"finder list should return to 5, got {qp.list_widget.count()}"
    # Cursor landed on the restored image so the user can re-check it.
    assert st.current_image is not None and \
        st.current_image.image_path.name == first, \
        f"cursor should be on the restored image, on {st.current_image}"
    # Selection survived the whole round-trip.
    assert st.get_selected_tags() == ["smile"], st.get_selected_tags()

    print("OK: multi-select undo restores the image to the finder, "
          "re-renders the list, lands the cursor on it, keeps selection")


if __name__ == "__main__":
    run()
