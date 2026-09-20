"""Regression test: the autocomplete popup is selectable with the mouse.

The popup is a separate top-level window. Clicking it makes the line edit
lose focus, and the focus-out handler used to hide the popup
unconditionally — so the click dismissed the popup on mouse-down before
mouse-up could land on an item, and itemClicked never fired. Keyboard
selection still worked (it never changes focus), which is exactly the bug
the user hit in the conflict-rules dialog.

The fix keeps the popup up when the cursor is over it, so the click lands
and commits. This test drives the real eventFilter with focus events.

Run: python3 tests/test_autocomplete_click.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication, QWidget, QLineEdit, QVBoxLayout, QListWidgetItem,
)
from PySide6.QtGui import QFocusEvent
from PySide6.QtCore import QEvent

from ui.tag_autocomplete import TagAutocomplete

_keep = []


def _setup(app):
    w = QWidget()
    lay = QVBoxLayout(w)
    ed = QLineEdit()
    lay.addWidget(ed)
    w.resize(300, 40)
    w.move(40, 40)
    w.show()
    ac = TagAutocomplete(ed)
    pop = ac._ensure_popup()
    for s in ["water", "waterfall", "watermelon"]:
        QListWidgetItem(s, pop)
    ac._show_popup()
    _keep.extend([w, ed, ac, pop])
    return ed, ac, pop


def _focus_out(app, ed):
    app.sendEvent(ed, QFocusEvent(QEvent.Type.FocusOut))


def run() -> None:
    app = QApplication.instance() or QApplication([])

    # 0) The popup must be a transient child of its line edit's window,
    #    not an independent top-level. Independent top-levels are blocked
    #    from receiving mouse input by an application-modal dialog (the
    #    conflict-rules / audit-exceptions dialogs use exec()), which is
    #    why suggestions could only be picked with the keyboard there.
    from PySide6.QtWidgets import QDialog
    dlg = QDialog()
    dlg.setModal(True)
    dlay = QVBoxLayout(dlg)
    dbox = QLineEdit()
    dlay.addWidget(dbox)
    dac = TagAutocomplete(dbox)
    dlg.resize(300, 80)
    dlg.show()
    app.processEvents()
    dpop = dac._ensure_popup()
    _keep.extend([dlg, dbox, dac, dpop])
    assert dpop.parent() is dbox, "popup must be parented to its line edit"
    assert dpop.isWindow(), "popup must still be a top-level window"
    assert dpop.parent().window() is dlg, \
        "popup's owner window must be the modal dialog (else clicks blocked)"

    # A) Cursor over the popup: focus-out must NOT hide it (the click is
    #    landing).
    ed, ac, pop = _setup(app)
    ac._cursor_over_popup = lambda: True
    _focus_out(app, ed)
    assert pop.isVisible(), "popup must stay up while the cursor is on it"

    # B) Cursor elsewhere: focus-out hides it (normal dismissal).
    ac._cursor_over_popup = lambda: False
    _focus_out(app, ed)
    assert not pop.isVisible(), "popup must hide when focus leaves elsewhere"

    # C) itemClicked (what a real mouse click emits) commits the choice and
    #    hides the popup.
    ed2, ac2, pop2 = _setup(app)
    item = pop2.item(1)  # "waterfall"
    assert item is not None
    pop2.itemClicked.emit(item)
    assert ed2.text() == "waterfall", "click should fill in the tag"
    assert not pop2.isVisible(), "popup should close after a pick"

    print("OK: autocomplete popup is mouse-selectable "
          "(stays up under cursor, hides on real focus-out, click commits)")


if __name__ == "__main__":
    run()
