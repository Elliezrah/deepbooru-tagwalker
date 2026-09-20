"""Regression test: the Yes/Skip/Back walk in multi-select mode.

In multi-select there's no single tag, so the buttons take on new roles:
- Yes  -> add the ticked tag(s) to the current image (then it leaves the
          MISSING finder and the cursor advances), and the button reads
          "Add tag(s)".
- Skip -> next matching image (reads "Next").
- Back -> previous matching image (reads "Prev"); navigation, not undo.
- No   -> disabled (nothing to reject).

Critically, this ticks the real tree checkbox (the user path) so the tree
and the engine selection stay in sync — a programmatic style refresh
during the add must not wipe the selection.

Run: python3 tests/test_multi_select_walk.py
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

    # Enter multi-select, MISSING filter, then TICK the real checkbox.
    w._tag_tree.multi_select_btn.setChecked(True)
    app.processEvents()
    st.set_filter_mode(FilterMode.MISSING_TAG)
    app.processEvents()
    w._tag_tree._tag_items["smile"].setCheckState(0, Qt.CheckState.Checked)
    app.processEvents()

    assert len(st.get_current_queue()) == 5, \
        f"5 images missing smile, got {len(st.get_current_queue())}"

    # Labels + enabled states.
    assert "Add tag(s)" in ip.btn_yes.text(), ip.btn_yes.text()
    assert "Remove tag(s)" in ip.btn_no.text(), ip.btn_no.text()
    # Skip/Next is removed in multi-select (button hidden); Back is now Undo.
    assert not ip.btn_skip.isVisible(), "Skip/Next must be hidden in multi-select"
    assert "Undo" in ip.btn_back.text(), ip.btn_back.text()
    assert ip.btn_yes.isEnabled()
    # No is ENABLED in multi-select: it removes the ticked tag(s).
    assert ip.btn_no.isEnabled(), "No should be enabled (remove) in multi-select"

    # Add: stamps smile on the current image, which then leaves the finder.
    first = st.current_image.image_path.name
    ip.trigger_yes()
    app.processEvents()
    assert len(st.get_current_queue()) == 4, \
        f"queue should shrink 5->4, got {len(st.get_current_queue())}"
    assert st.get_selected_tags() == ["smile"], \
        f"selection must survive the add, got {st.get_selected_tags()}"
    fixed = [p for p in st._image_tags if p.name == first][0]
    assert "smile" in st._image_tags[fixed], "the image should now have smile"

    # Back is now UNDO (not navigation): it reverses the add, bringing the
    # image back into the finder. Skip/Space is a no-op (button gone).
    assert ip.btn_back.isEnabled(), "Undo should be enabled after an add"
    i0 = st._walk_index
    ip.trigger_skip_image()  # no-op in multi-select now
    app.processEvents()
    assert st._walk_index == i0, "Skip/Space should do nothing in multi-select"
    ip.trigger_back()  # Undo
    app.processEvents()
    assert "smile" not in st._image_tags[fixed], "Back/Undo should reverse the add"
    assert len(st.get_current_queue()) == 5, "undone image returns to the finder"

    # Leaving multi-select restores normal labels and the Skip button.
    w._tag_tree.multi_select_btn.setChecked(False)
    app.processEvents()
    assert ip.btn_skip.isVisible(), "Skip returns in normal mode"
    assert "Yes" in ip.btn_yes.text() and "Skip" in ip.btn_skip.text() \
        and "Back" in ip.btn_back.text()

    print("OK: multi-select buttons verified (Add stamps+shrinks+keeps "
          "selection; Skip hidden; Back=Undo reverses; labels swap & restore)")


if __name__ == "__main__":
    run()
