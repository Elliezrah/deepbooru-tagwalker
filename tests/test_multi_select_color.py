"""Regression test: presence-dot colors in the queue panel under
multi-select.

Before the fix, the dot was derived from _displayed_tag, which is None in
multi-select, so EVERY row's dot came out red — even in Has mode, where
the listed images all carry the ticked tags. Now the dot reflects whether
the image has ALL the ticked tags:
  * HAS_TAG     -> all rows green
  * MISSING_TAG -> all rows red
  * ALL         -> mixed

We capture the colors by patching _dot_icon to record the hex it's asked
for, then read the recorded colors per row.

Run: python3 tests/test_multi_select_color.py
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
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

import ui.queue_panel as qpmod
from core.state import SessionState, FilterMode
from core.scanner import scan
from config.theme import Colors


def run() -> None:
    app = QApplication.instance() or QApplication([])

    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)

    def mk(name, tags):
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")

    # 3 images have both smile+blush; 3 have neither.
    for i in range(3):
        mk(f"has{i}", ["1girl", "smile", "blush"])
    for i in range(3):
        mk(f"no{i}", ["1girl"])

    # Capture every color _dot_icon is asked to draw.
    captured: list[str] = []
    orig = qpmod._dot_icon

    def spy(hex_color: str) -> QIcon:
        captured.append(hex_color)
        return orig(hex_color)

    qpmod._dot_icon = spy
    try:
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

        w._tag_tree.multi_select_btn.setChecked(True)
        app.processEvents()
        w._tag_tree._tag_items["smile"].setCheckState(0, Qt.CheckState.Checked)
        w._tag_tree._tag_items["blush"].setCheckState(0, Qt.CheckState.Checked)
        app.processEvents()

        # --- HAS mode: the 3 images that carry BOTH tags, all green ---
        st.set_filter_mode(FilterMode.HAS_TAG)
        captured.clear()
        app.processEvents()
        # Force a restyle pass over the listed rows.
        w._queue_panel._rebuild_list()
        app.processEvents()
        assert captured, "no dots drawn in HAS mode"
        assert all(c == Colors.SUCCESS_GREEN for c in captured), \
            f"HAS mode should be all green, got {set(captured)}"
        print(f"OK: HAS mode -> {len(captured)} green dots, no red")

        # --- MISSING mode: the 3 images lacking the tags, all red ---
        st.set_filter_mode(FilterMode.MISSING_TAG)
        captured.clear()
        app.processEvents()
        w._queue_panel._rebuild_list()
        app.processEvents()
        assert captured, "no dots drawn in MISSING mode"
        assert all(c == Colors.DANGER_RED for c in captured), \
            f"MISSING mode should be all red, got {set(captured)}"
        print(f"OK: MISSING mode -> {len(captured)} red dots, no green")
    finally:
        qpmod._dot_icon = orig

    print("\nALL PASS: multi-select presence-dot colors")


if __name__ == "__main__":
    run()
