"""
tests/test_undo_button_first_edit.py

Regression test for the "first edit can't be undone" bug.

Symptom (reported): while walking a single tag, the FIRST granular tag
removal of the session could not be undone — the Back (Undo) button did
nothing. Doing any OTHER action afterwards made Undo start working again.

Cause: a granular add/remove emits an "image_changed" state event. The
image panel's handler for that event refreshed the presence label, the
border, and the progress label — but NOT the enabled state of the Back
(Undo) button. The button's enabled state tracks state.can_undo(), and at
the start of a session it begins DISABLED (nothing to undo). Because the
first edit's image_changed didn't refresh it, the button stayed disabled
even though there was now something to undo, so pressing it did nothing.
A later action that routes through _refresh_display() (walk advance, tag
select, tree rebuild, …) calls _refresh_enabled() and re-enables it —
which is why "any other action makes undo work again".

Subtlety that hid the bug: removing a tag that NO other image has also
fires "tree_rebuilt" (the tag leaves the tree), and that event DOES
refresh the button — masking the problem. The bug only shows when the
removed tag survives on other images, so ONLY image_changed fires. This
test uses exactly that condition.

Fix: the image_changed branch now also calls _refresh_enabled().
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
import PySide6.QtWidgets as W

from config.theme import initialize_theme
from core.scanner import scan
from core.state import SessionState

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv if sys.argv else ["test"])
    return app


def test_back_button_enables_after_first_granular_edit():
    _app()
    initialize_theme("dark")

    tmp = Path(tempfile.mkdtemp())
    # BOTH images carry 'smile', so removing it from one leaves it on the
    # other -> the tag stays in the tree -> NO tree_rebuilt, ONLY
    # image_changed fires. This is the condition that exposes the bug.
    for nm in ("a", "b"):
        (tmp / f"{nm}.png").write_bytes(_PNG)
        (tmp / f"{nm}.txt").write_text("1girl, solo, smile", encoding="utf-8")

    st = SessionState(scan(tmp))

    from ui.image_panel import ImagePanel
    panel = ImagePanel()
    panel.attach(st)

    # Walk a single tag (navigation; not an undoable action).
    st.select_tag("1girl")
    assert not st.can_undo()
    assert panel.btn_back.isEnabled() is False, (
        "Back should start disabled when there's nothing to undo"
    )

    # THE FIRST undoable action: remove a tag that the other image keeps.
    cur = st.current_image.image_path
    assert st.remove_tag_from_image(cur, "smile") is True
    assert st.can_undo() is True

    # The fix: Back (Undo) must be enabled now, on the very first edit.
    assert panel.btn_back.isEnabled() is True, (
        "Back/Undo must be enabled after the first granular edit "
        "(image_changed must refresh the button's enabled state)"
    )
    print("OK: Back/Undo enables after the very first granular edit")


def test_undo_actually_reverts_that_first_edit():
    # Belt and suspenders: the state-level undo of that first edit works
    # (the button being enabled is what lets the user reach it).
    _app()
    initialize_theme("dark")

    tmp = Path(tempfile.mkdtemp())
    for nm in ("a", "b"):
        (tmp / f"{nm}.png").write_bytes(_PNG)
        (tmp / f"{nm}.txt").write_text("1girl, solo, smile", encoding="utf-8")

    st = SessionState(scan(tmp))
    from ui.image_panel import ImagePanel
    panel = ImagePanel()
    panel.attach(st)
    st.select_tag("1girl")
    cur = st.current_image.image_path

    st.remove_tag_from_image(cur, "smile")
    assert "smile" not in st.get_image_tags(cur)
    assert st.undo() is True
    assert "smile" in st.get_image_tags(cur), "first edit should undo"
    # And the file on disk reflects the restoration.
    assert "smile" in (cur.with_suffix(".txt")).read_text()
    print("OK: the first edit reverts cleanly on undo")


if __name__ == "__main__":
    test_back_button_enables_after_first_granular_edit()
    test_undo_actually_reverts_that_first_edit()
    print("\nALL PASS: undo button first-edit enable")
