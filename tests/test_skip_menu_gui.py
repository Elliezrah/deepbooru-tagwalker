"""GUI-level regression test for skip -> mark-complete (report item L).

Standalone:  QT_QPA_PLATFORM=offscreen python tests/test_skip_menu_gui.py

The older test_skip_behavior.py verifies the SessionState logic only. This
one drives the *actual* tag-tree right-click menu through the exact sequence
a user performs:

    right-click a tag -> "Skip ... (review later)"   (tag becomes SKIPPED)
    right-click it again -> "Mark ... complete"      (tag becomes COMPLETED)

and asserts at every step that the menu offers the expected actions and that
the visible row (icon/text) updates. It exists because the reported "can't
mark complete after skip" bug, if it ever recurs, would live in this GUI path
-- not in the state layer the other test already covers.

Implementation note: QMenu.exec() runs a *modal* nested event loop, so the
menu can't be stubbed by reassigning QMenu.exec (PySide6 ignores that). We
instead post a QTimer callback that fires *inside* the modal loop, inspects
the live menu via QApplication.activePopupWidget(), optionally triggers an
action, and closes the menu to unblock exec().
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

TAG = "skirt"


def _build():
    from PIL import Image
    from core.scanner import scan
    from core.state import SessionState

    d = Path(tempfile.mkdtemp()) / "imgs"
    d.mkdir(parents=True)

    def mk(name, tags):
        Image.new("RGB", (8, 8), (120, 120, 120)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")

    mk("a", ["1girl", TAG, "solo"])
    mk("b", ["1girl", TAG, "smile"])
    mk("c", ["1girl", TAG])
    return SessionState(scan(d))


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMenu
    from PySide6.QtCore import QPoint, QTimer
    from core.state import TagStatus
    from ui.tag_tree import TagTree

    app = QApplication.instance() or QApplication([])
    state = _build()
    panel = TagTree()
    panel.attach(state)

    # Resolve the right-click position to our tag's row regardless of the
    # offscreen geometry (hit-testing by pixel is unreliable headless).
    panel.tree.itemAt = lambda pos: panel._tag_items.get(TAG)

    def row_text():
        it = panel._tag_items.get(TAG)
        return None if it is None else it.text(0)

    def open_menu(trigger_prefix=None):
        """Open the real context menu, record its enabled action labels,
        optionally trigger the first action whose label starts with
        `trigger_prefix`, then close the menu to return from exec()."""
        captured = {"labels": []}

        def handler():
            menu = QApplication.activePopupWidget()
            if menu is None or not isinstance(menu, QMenu):
                QTimer.singleShot(10, handler)
                return
            captured["labels"] = [
                a.text() for a in menu.actions() if a.text() and a.isEnabled()
            ]
            if trigger_prefix is not None:
                for a in menu.actions():
                    if a.text().startswith(trigger_prefix):
                        a.trigger()
                        break
            menu.close()

        QTimer.singleShot(0, handler)
        panel._on_context_menu(QPoint(5, 5))  # blocks until handler closes it
        return captured["labels"]

    # Sanity: tag exists and starts pending.
    assert panel._tag_items.get(TAG) is not None, "tag row should exist"
    assert state.get_tag_status(TAG) == TagStatus.PENDING

    # 1) Right-click a PENDING tag: both Mark-complete and Skip are offered.
    labels = open_menu()
    assert any(l.startswith("Mark") for l in labels), (
        "pending tag's menu must offer Mark complete"
    )
    assert any(l.startswith("Skip") for l in labels), (
        "pending tag's menu must offer Skip"
    )

    # 2) Trigger Skip -> status SKIPPED, row shows the skip icon.
    open_menu(trigger_prefix="Skip")
    assert state.get_tag_status(TAG) == TagStatus.SKIPPED, "Skip must set SKIPPED"
    assert row_text() is not None and not row_text().lstrip().startswith("\u2713"), (
        "skipped row must not show the completed check"
    )

    # 3) Right-click the SKIPPED tag: Mark complete is STILL offered (this is
    #    the crux of the report -- a skipped tag must remain completable), and
    #    Skip is no longer offered (already skipped).
    labels = open_menu()
    assert any(l.startswith("Mark") for l in labels), (
        "REGRESSION: a SKIPPED tag's menu must still offer Mark complete"
    )
    assert not any(l.startswith("Skip") for l in labels), (
        "a SKIPPED tag should not re-offer Skip"
    )

    # 4) Trigger Mark complete -> status COMPLETED, row shows the check.
    open_menu(trigger_prefix="Mark")
    assert state.get_tag_status(TAG) == TagStatus.COMPLETED, (
        "REGRESSION: Mark complete on a SKIPPED tag must set COMPLETED"
    )
    assert row_text().lstrip().startswith("\u2713"), (
        "completed row must show the check icon"
    )

    print("OK: GUI skip -> mark-complete verified (menu + status + row icon)")
    return 0


if __name__ == "__main__":
    rc = main()
    os._exit(rc)
