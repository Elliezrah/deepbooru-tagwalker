"""Regression test for the tag-tree multi-select UI.

Sits on top of the engine test (test_multi_select.py). Drives the actual
TagTree widget: the Multi-select toggle, the per-row checkboxes, and the
suppression of the single-tag walk while the mode is on.

Verifies:
  - The toggle switches state.multi_select_mode and (un)draws the
    per-row checkboxes. A checkbox is "drawn" iff the row's
    CheckStateRole data is set, so leaving the mode must CLEAR that data
    (not merely uncheck it).
  - Ticking rows pushes the set to state.set_selected_tags; unticking
    updates it; pairing with the queue's MISSING_TAG filter yields the
    forgotten-image finder.
  - A row click does NOT start a walk while in multi-select mode.
  - Leaving the mode clears the selection, removes the checkboxes, and
    fully restores the normal single-tag walk.

Run: python3 tests/test_multi_select_ui.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from core.state import SessionState, FilterMode
from core.scanner import scan
from ui.tag_tree import TagTree


def _make_dataset() -> Path:
    d = Path(tempfile.mkdtemp()) / "imgs"
    d.mkdir(parents=True)

    def mk(name, tags):
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")

    mk("img1", ["1girl", "long_hair", "smile"])
    mk("img2", ["1girl", "short_hair"])
    mk("img3", ["1girl", "smile"])                     # forgotten
    mk("img4", ["1girl", "medium_hair", "long_hair"])
    mk("img5", ["1girl"])                              # forgotten
    return d


def run() -> None:
    app = QApplication.instance() or QApplication([])
    st = SessionState(scan(_make_dataset()))
    tt = TagTree()
    tt.attach(st)

    def item(tag):
        return tt._tag_items[tag]

    def has_checkbox(it):
        # The checkbox is drawn iff CheckStateRole data is present.
        return it.data(0, Qt.ItemDataRole.CheckStateRole) is not None

    def names():
        return sorted(e.image_path.stem for e in st.get_current_queue())

    assert hasattr(tt, "multi_select_btn")
    assert tt.multi_select_btn.isChecked() is False
    # No checkboxes before the mode is on (despite Qt's default
    # user-checkable flag — visibility is driven by CheckStateRole).
    assert not has_checkbox(item("long_hair"))

    # Turn the mode ON.
    tt.multi_select_btn.setChecked(True)
    assert st.multi_select_mode is True
    assert has_checkbox(item("long_hair"))
    assert item("long_hair").checkState(0) == Qt.CheckState.Unchecked

    # Tick the hair-length set (simulating user clicks -> itemChanged).
    for t in ["long_hair", "short_hair", "medium_hair"]:
        item(t).setCheckState(0, Qt.CheckState.Checked)
    assert sorted(st.get_selected_tags()) == \
        ["long_hair", "medium_hair", "short_hair"], st.get_selected_tags()

    # The forgotten-image finder.
    st.set_filter_mode(FilterMode.MISSING_TAG)
    assert names() == ["img3", "img5"], names()

    # A row click must NOT start a walk in this mode.
    tt._on_item_clicked(item("1girl"), 0)
    assert st.current_tag is None

    # Untick one.
    item("short_hair").setCheckState(0, Qt.CheckState.Unchecked)
    assert sorted(st.get_selected_tags()) == ["long_hair", "medium_hair"]

    # Turn the mode OFF: selection cleared, checkboxes removed.
    tt.multi_select_btn.setChecked(False)
    assert st.multi_select_mode is False
    assert st.get_selected_tags() == []
    assert not has_checkbox(item("long_hair")), "checkbox must be removed"

    # Single-tag walk fully restored.
    st.set_filter_mode(FilterMode.ALL)
    tt._on_item_clicked(item("1girl"), 0)
    assert st.current_tag == "1girl"
    assert names() == ["img1", "img2", "img3", "img4", "img5"], names()

    print("OK: tag-tree multi-select UI verified "
          "(toggle, checkbox draw/clear, ticking -> selection, finder, "
          "walk suppressed, clean restore)")


if __name__ == "__main__":
    run()
