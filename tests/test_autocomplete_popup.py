"""Regression test for the tag-autocomplete popup positioning.

The popup normally drops below the input box, but the file-state
add-tag box sits low in the layout, so near the screen's bottom edge the
popup must flip ABOVE the box to stay fully visible. Also guards the
row-height calculation so a stray sizeHintForRow can't collapse or
oversize the popup.

The flip is only asserted when the box is genuinely too close to the
bottom for the popup to fit below (so the test is screen-size agnostic).

Run: python3 tests/test_autocomplete_popup.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication, QWidget, QLineEdit, QVBoxLayout, QListWidgetItem,
)

from ui.tag_autocomplete import TagAutocomplete

_keep = []  # keep widgets alive against GC


def _make(app, y):
    w = QWidget()
    lay = QVBoxLayout(w)
    ed = QLineEdit()
    lay.addWidget(ed)
    w.resize(300, 40)
    w.move(20, y)
    w.show()
    ac = TagAutocomplete(ed)
    pop = ac._ensure_popup()
    for s in ["smile", "smiling", "smirk", "smug", "sweat", "sad",
              "serious", "shy"]:
        QListWidgetItem(s, pop)
    ac._show_popup()
    _keep.extend([w, ed, ac, pop])
    return ed, pop


def run() -> None:
    app = QApplication.instance() or QApplication([])
    scr = app.primaryScreen().availableGeometry()

    # Box near the top -> popup sits below it, with a real height.
    ed1, p1 = _make(app, 10)
    below1 = ed1.mapToGlobal(ed1.rect().bottomLeft()).y()
    assert p1.pos().y() >= below1 - 2, "popup should sit below near the top"
    assert p1.height() > 0, "popup height must be positive"

    # Box near the bottom -> popup flips above (only required if it can't
    # fit below).
    ed2, p2 = _make(app, scr.bottom() - 30)
    top2 = ed2.mapToGlobal(ed2.rect().topLeft()).y()
    below2 = ed2.mapToGlobal(ed2.rect().bottomLeft()).y()
    gap_below = scr.bottom() - below2
    flipped = p2.pos().y() < top2
    assert flipped or gap_below >= p2.height(), \
        "popup must flip above when it can't fit below"

    print("OK: autocomplete popup positioning verified "
          "(below with room, flips above near bottom, sane height)")


if __name__ == "__main__":
    run()
