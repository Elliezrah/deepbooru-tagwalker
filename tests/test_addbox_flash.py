"""Tests for the add-box rejected-flash mechanism.

The flash used to disconnect/reconnect textEdited on every rejection,
which newer PySide6 builds report with a "Failed to disconnect"
RuntimeWarning on every flash (seen in a field log). The clearer is now
connected once at construction and is idempotent, so the flash cycle
involves no signal bookkeeping at all.

Run: python3 tests/test_addbox_flash.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from ui.file_state_panel import FileStatePanel


def test_flash_then_typing_clears_repeatedly() -> None:
    panel = FileStatePanel()
    for cycle in range(3):
        panel._flash_add_box_error("Invalid tag (commas aren't allowed)")
        assert panel._add_box_error_active is True
        assert "border" in panel.add_box.styleSheet(), cycle
        assert panel.add_box.toolTip() != ""
        # The user types — the permanently-connected clearer fires.
        panel.add_box.textEdited.emit("s")
        assert panel._add_box_error_active is False
        assert panel.add_box.styleSheet() == ""
        assert panel.add_box.toolTip() == ""
    print("OK: flash \u2192 type \u2192 clear works across repeated cycles "
          "with no signal churn")


def test_typing_without_error_is_a_cheap_noop() -> None:
    panel = FileStatePanel()
    panel.add_box.setToolTip("unrelated tooltip")
    for _ in range(50):
        panel.add_box.textEdited.emit("x")   # must not raise or reset UI
    assert panel.add_box.toolTip() == "unrelated tooltip"
    print("OK: keystrokes with no active error leave the box untouched")


def run() -> None:
    test_flash_then_typing_clears_repeatedly()
    test_typing_without_error_is_a_cheap_noop()
    print("\nALL PASS: add-box flash/clear with permanent connection")


if __name__ == "__main__":
    run()
