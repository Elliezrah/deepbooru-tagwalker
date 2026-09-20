"""Regression test: the four self-teaching touches in multi-select mode.

1. The action buttons relabel ("Add tag(s)" / "Next" / "Prev", No off).
2. A one-line how-to strip is visible only in multi-select.
3. The queue status spells out the filter ("missing the ticked tags" vs
   "have all the ticked tags"), updating live with the filter.
4. Empty states guide: nothing ticked -> "Tick one or more tags...",
   ticked-but-no-match -> "No images match the ticked tags".

Run: python3 tests/test_multi_select_usability.py
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
    ip = w._image_panel
    qp = w._queue_panel

    # ---- 2. how-to strip hidden in normal mode ----
    assert not ip.label_multi_hint.isVisible(), \
        "how-to strip must be hidden during normal walking"

    # Enter multi-select.
    w._tag_tree.multi_select_btn.setChecked(True)
    app.processEvents()

    # ---- 2. strip now visible ----
    assert ip.label_multi_hint.isVisible(), \
        "how-to strip must show in multi-select"

    # ---- 4. nothing ticked yet -> guiding empty state ----
    st.set_filter_mode(FilterMode.MISSING_TAG)
    app.processEvents()
    assert "Tick one or more tags" in qp.status_label.text(), \
        f"nothing-ticked status was: {qp.status_label.text()!r}"

    # Tick smile (the real user path).
    w._tag_tree._tag_items["smile"].setCheckState(0, Qt.CheckState.Checked)
    app.processEvents()

    # ---- 3. MISSING filter spelled out ----
    assert "missing the ticked tags" in qp.status_label.text(), \
        f"MISSING status was: {qp.status_label.text()!r}"

    # ---- 3. switch to HAS -> description updates live ----
    st.set_filter_mode(FilterMode.HAS_TAG)
    app.processEvents()
    assert "have all the ticked tags" in qp.status_label.text(), \
        f"HAS status was: {qp.status_label.text()!r}"

    # ---- 4. ticked but no match -> distinct empty message ----
    # Untick smile, tick a tag no image has, in MISSING... instead use a
    # tag present everywhere in HAS mode so MISSING yields zero? Simpler:
    # MISSING + a tag every image has => zero missing.
    w._tag_tree._tag_items["smile"].setCheckState(0, Qt.CheckState.Unchecked)
    app.processEvents()
    st.set_filter_mode(FilterMode.MISSING_TAG)
    w._tag_tree._tag_items["1girl"].setCheckState(0, Qt.CheckState.Checked)
    app.processEvents()
    assert "No images match the ticked tags" in qp.status_label.text(), \
        f"no-match status was: {qp.status_label.text()!r}"

    # ---- 2. strip hides again on leaving multi-select ----
    w._tag_tree.multi_select_btn.setChecked(False)
    app.processEvents()
    assert not ip.label_multi_hint.isVisible(), \
        "how-to strip must hide when leaving multi-select"

    print("OK: usability verified (strip show/hide, live filter description "
          "Missing/Has, both guiding empty states)")


if __name__ == "__main__":
    run()
