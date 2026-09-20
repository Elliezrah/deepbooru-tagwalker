"""
tests/test_dataset_marker.py

Tests for core/dataset_marker.py and its Token Counter entry point.

This is the only feature that renames the user's files, so the checks
lean on the failure paths rather than the happy one: an image and its
caption must move together or not at all, two images sharing one
caption must be refused, an existing file must never be clobbered, and
a partial failure must leave a usable pair behind.

Run: python3 tests/test_dataset_marker.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from PIL import Image
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import dataset_marker as marker
from core.scanner import scan
from core.state import SessionState
from ui.token_counter_dialog import TokenCounterDialog

# Modal dialogs block forever without a user, so every one is stubbed.
_notes: list = []
QMessageBox.information = lambda *a, **k: _notes.append(a[-1] if a else "")
QMessageBox.warning = lambda *a, **k: _notes.append(a[-1] if a else "")


def _answer(button) -> None:
    QMessageBox.exec = lambda self: button


class _FakeMain(QWidget):
    """Stands in for MainWindow: the dialog only needs a QWidget
    parent that can start a re-scan."""

    def __init__(self) -> None:
        super().__init__()
        self.scans: list = []

    def _start_scan(self, roots) -> None:
        self.scans.append(roots)


def _pairs(root: Path, names) -> list:
    out = []
    for n in names:
        img = root / f"{n}.png"
        img.write_bytes(b"IMG")
        txt = root / f"{n}.txt"
        txt.write_text("a, b", encoding="utf-8")
        out.append((img, txt))
    return out


def test_prefix_choice() -> None:
    """`*` cannot be used: it is a reserved character in Windows
    filenames. `!` is legal everywhere and sorts above letters and
    digits in Explorer, which is the whole point of the feature."""
    assert marker.MARK_PREFIX == "!"
    assert marker.MARK_PREFIX not in set('<>:"/\\|?*')
    assert marker.marked_name("a.png") == "!a.png"
    assert marker.marked_name("!a.png") == "!a.png"      # idempotent
    assert marker.unmarked_name("!a.png") == "a.png"
    assert marker.unmarked_name("a.png") == "a.png"
    print("OK: the prefix is legal on Windows, sorts to the top, and "
          "marking twice does not double it")


def test_marks_move_image_and_caption_together() -> None:
    d = Path(tempfile.mkdtemp())
    pairs = _pairs(d, ["a", "b", "c"])
    plan = marker.plan_marks(pairs, {pairs[0][0], pairs[2][0]})
    assert [p.name for p, _t in plan.to_mark] == ["a.png", "c.png"]
    assert not plan.to_unmark
    result = marker.apply_plan(plan)
    assert result.marked == 2 and not result.skipped
    names = sorted(p.name for p in d.iterdir())
    assert names == ["!a.png", "!a.txt", "!c.png", "!c.txt",
                     "b.png", "b.txt"]
    # The point of the exercise: they sort first.
    assert names[0].startswith(marker.MARK_PREFIX)
    print("OK: an over-limit image and its caption are renamed "
          "together, leaving stem-based pairing intact")


def test_marking_is_a_sync_not_a_stamp() -> None:
    """Re-running after pruning must leave the marks TRUE: files still
    over the limit keep theirs, files brought under lose theirs."""
    d = Path(tempfile.mkdtemp())
    pairs = _pairs(d, ["a", "c"])
    marker.apply_plan(marker.plan_marks(pairs, {p for p, _t in pairs}))
    marked = [(d / "!a.png", d / "!a.txt"), (d / "!c.png", d / "!c.txt")]

    # Nothing changes while both are still over.
    assert marker.plan_marks(marked, {d / "!a.png", d / "!c.png"}).is_empty()

    # `c` is pruned under the limit, so its mark goes.
    plan = marker.plan_marks(marked, {d / "!a.png"})
    assert [p.name for p, _t in plan.to_unmark] == ["!c.png"]
    marker.apply_plan(plan)
    assert sorted(p.name for p in d.iterdir()) == [
        "!a.png", "!a.txt", "c.png", "c.txt"]

    # Clearing everything is the same operation with nothing over.
    rest = [(d / "!a.png", d / "!a.txt"), (d / "c.png", d / "c.txt")]
    result = marker.apply_plan(marker.plan_marks(rest, set()))
    assert result.unmarked == 1
    assert not any(p.name.startswith("!") for p in d.iterdir())
    print("OK: marking syncs rather than stamps, so marks stay honest "
          "across edits, and clearing them all is the same code path")


def test_refusals_protect_the_dataset() -> None:
    # Two images resolving to one caption: renaming it would orphan
    # the other, so the whole group is refused.
    d = Path(tempfile.mkdtemp())
    (d / "foo.png").write_bytes(b"IMG")
    (d / "foo.jpg").write_bytes(b"IMG")
    (d / "foo.txt").write_text("a", encoding="utf-8")
    plan = marker.plan_marks(
        [(d / "foo.png", d / "foo.txt"), (d / "foo.jpg", d / "foo.txt")],
        {d / "foo.png", d / "foo.jpg"},
        collision_names={"foo.png", "foo.jpg"})
    assert plan.is_empty() and len(plan.skipped) == 2
    assert "share" in plan.skipped[0][1]

    # An existing target is never clobbered.
    d2 = Path(tempfile.mkdtemp())
    pairs = _pairs(d2, ["x"])
    (d2 / "!x.png").write_bytes(b"SOMETHING ELSE")
    result = marker.apply_plan(marker.plan_marks(pairs, {pairs[0][0]}))
    assert result.marked == 0
    assert "already exists" in result.skipped[0][1]
    assert (d2 / "!x.png").read_bytes() == b"SOMETHING ELSE"
    assert (d2 / "x.png").exists()

    # An image with no caption is still markable.
    d3 = Path(tempfile.mkdtemp())
    img = d3 / "solo.png"
    img.write_bytes(b"IMG")
    result = marker.apply_plan(
        marker.plan_marks([(img, d3 / "solo.txt")], {img}))
    assert result.marked == 1 and (d3 / "!solo.png").exists()
    print("OK: shared captions and existing filenames are refused with "
          "a reason; a caption-less image still marks cleanly")


def test_failed_caption_rename_rolls_back() -> None:
    """A renamed image beside an unrenamed caption is a broken pair —
    worse than not marking at all — so the image goes back.

    The failure is injected rather than simulated with permissions,
    which root would ignore."""
    d = Path(tempfile.mkdtemp())
    img, txt = _pairs(d, ["lock"])[0]
    real_rename = marker.os.rename

    def fail_on_caption(src, dst):
        if str(src).endswith(".txt"):
            raise OSError(13, "Permission denied")
        return real_rename(src, dst)

    marker.os.rename = fail_on_caption
    try:
        result = marker.apply_plan(marker.plan_marks([(img, txt)], {img}))
    finally:
        marker.os.rename = real_rename

    assert result.marked == 0
    assert sorted(p.name for p in d.iterdir()) == ["lock.png", "lock.txt"]
    assert "caption" in result.skipped[0][1]
    assert (d / "lock.png").read_bytes() == b"IMG"

    # If even the rollback fails, say so plainly rather than pretending.
    d2 = Path(tempfile.mkdtemp())
    img2, txt2 = _pairs(d2, ["x"])[0]
    calls = {"n": 0}

    def fail_after_image(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_rename(src, dst)
        raise OSError(13, "Permission denied")

    marker.os.rename = fail_after_image
    try:
        result2 = marker.apply_plan(marker.plan_marks([(img2, txt2)],
                                                      {img2}))
    finally:
        marker.os.rename = real_rename
    assert result2.marked == 0 and "by hand" in result2.skipped[0][1]
    print("OK: a caption that cannot be renamed rolls the image back, "
          "and an unrecoverable split pair is reported rather than "
          "hidden")


def test_token_counter_entry_point() -> None:
    s = Settings()
    initialize_theme(s.theme)
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    long_caption = ", ".join(f"tag_number_{i}" for i in range(40))
    for name, caption in {"big1": long_caption, "big2": long_caption,
                          "small": "1girl, solo"}.items():
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(caption, encoding="utf-8")

    main = _FakeMain()
    dlg = TokenCounterDialog(SessionState(scan(d)), main)
    assert len(dlg._paths) == 3
    assert sorted(p.name for p, _t, n in dlg._paths if n > 75) == [
        "big1.png", "big2.png"]
    assert [a.text() for a in dlg._mark_menu.actions()] == [
        "Mark files over the limit", "Remove all marks"]

    # Cancelling the confirmation must write nothing.
    _answer(QMessageBox.StandardButton.Cancel)
    dlg._do_marks(True)
    assert not any(p.name.startswith("!") for p in d.iterdir())

    _answer(QMessageBox.StandardButton.Yes)
    dlg._do_marks(True)
    assert sorted(p.name for p in d.iterdir()) == [
        "!big1.png", "!big1.txt", "!big2.png", "!big2.txt",
        "small.png", "small.txt"]
    assert dlg._last_result.marked == 2
    # Every path the session holds has moved, so a re-scan is required
    # and this window's rows are stale.
    assert len(main.scans) == 1
    assert not dlg.isVisible()

    dlg2 = TokenCounterDialog(SessionState(scan(d)), _FakeMain())
    _answer(QMessageBox.StandardButton.Yes)
    dlg2._do_marks(False)
    assert dlg2._last_result.unmarked == 2
    assert not any(p.name.startswith("!") for p in d.iterdir())

    # The selected threshold is what counts as "over".
    dlg3 = TokenCounterDialog(SessionState(scan(d)), _FakeMain())
    for rb, t in zip(dlg3._radios, [75, 150, 225, 256, 512]):
        rb.setChecked(t == 512)
    assert dlg3.threshold() == 512
    before = len(_notes)
    dlg3._do_marks(True)
    assert len(_notes) == before + 1          # said so, did nothing
    assert not any(p.name.startswith("!") for p in d.iterdir())
    print("OK: the Token Counter marks at the selected threshold, "
          "confirms first, re-scans afterwards, and can clear every "
          "mark again")


def run() -> None:
    test_prefix_choice()
    test_marks_move_image_and_caption_together()
    test_marking_is_a_sync_not_a_stamp()
    test_refusals_protect_the_dataset()
    test_failed_caption_rename_rolls_back()
    test_token_counter_entry_point()
    print("\nALL PASS: dataset marker")


if __name__ == "__main__":
    run()
