"""
tests/test_walk_performance.py

The cost of one Yes/No, and that it does not grow with the dataset.

MEASURED, before these fixes:

    100 images     5.2 ms per keypress
    400 images    11.5 ms
    1,200 images  27.3 ms
    2,400 images  55.0 ms

Clean O(n), from two places doing whole-queue work on every decision:

1. The queue restyled EVERY row when two had changed — 1,201 calls to
   _apply_style per keypress on a 1,200-image queue.
2. Three separate progress readouts each counted the whole queue's
   decisions independently, producing the same number three times.

These tests assert the SHAPE rather than a millisecond figure, because
a timing threshold on shared hardware is a flaky test. Constant work
per decision is the property that matters; if it ever becomes
proportional to the queue again, this fails.

Run: python3 tests/test_walk_performance.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core.scanner import scan
from core.state import Decision, SessionState
from ui.main_window import MainWindow


def _dataset(count: int) -> Path:
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    for i in range(count):
        Image.new("RGB", (16, 16)).save(folder / f"i{i:05d}.png")
        (folder / f"i{i:05d}.txt").write_text("1girl, smile",
                                              encoding="utf-8")
    return folder


def _window(count: int):
    s = Settings()
    initialize_theme(s.theme)
    mw = MainWindow(s)
    mw._adopt_state(SessionState(scan(_dataset(count))), scan_root=None)
    mw._state.select_tag("1girl")
    return mw


def _restyles_per_decision(mw, decisions: int = 10) -> float:
    panel = mw._queue_panel
    original = panel._apply_style
    calls = [0]

    def counting(item, entry):
        calls[0] += 1
        return original(item, entry)

    panel._apply_style = counting
    try:
        for _ in range(decisions):
            mw._state.record_yes()
    finally:
        panel._apply_style = original
    return calls[0] / decisions


def test_restyling_is_constant_per_decision() -> None:
    """A decision changes at most three rows: the one just decided,
    the one that was current, and the one that becomes current."""
    small = _restyles_per_decision(_window(60))
    large = _restyles_per_decision(_window(600))
    assert small <= 6, small
    assert large <= 6, large
    # Ten times the images must not mean ten times the work.
    assert large <= small + 1, (small, large)
    print(f"OK: a decision restyles ~{large:.0f} rows whatever the "
          "queue size, instead of every row in it")


def test_the_decided_count_is_computed_once() -> None:
    """Three readouts wanted the same number and each counted the
    whole queue for it."""
    mw = _window(400)
    state = mw._state
    queue = state.get_current_queue()
    calls = [0]
    original = state.get_decision

    def counting(path, tag):
        calls[0] += 1
        return original(path, tag)

    state.get_decision = counting
    try:
        for _ in range(10):
            state.record_yes()
    finally:
        state.get_decision = original
    per_decision = calls[0] / 10
    # Was ~7,200 on a 1,200-image queue; must not scale with it.
    assert per_decision < len(queue), (per_decision, len(queue))
    print(f"OK: a decision makes ~{per_decision:.0f} decision lookups "
          f"on a {len(queue)}-image queue, not one per image")


def test_the_shared_count_stays_correct() -> None:
    """Speed is worthless if the number is wrong. Checked against a
    brute-force count through every path that can change it."""
    mw = _window(20)
    state = mw._state

    def brute() -> int:
        tag = state.current_tag
        return sum(1 for img in state.get_current_queue()
                   if state.get_decision(img.image_path, tag)
                   in (Decision.YES, Decision.NO))

    assert state.current_queue_decided() == brute() == 0
    state.record_yes()
    state.record_no()
    state.record_yes()
    assert state.current_queue_decided() == brute() == 3
    # SKIPPED is not decided — all three original callers agreed, and
    # changing that would silently alter what the progress bar means.
    state.record_skip_image()
    assert state.current_queue_decided() == brute() == 3
    for _ in range(3):
        state.undo()
        assert state.current_queue_decided() == brute()
    state.select_tag("smile")
    assert state.current_queue_decided() == brute()
    state.record_yes()
    assert state.current_queue_decided() == brute() == 1
    state.select_tag("1girl")
    assert state.current_queue_decided() == brute()

    # The cache keys on a version the decisions map maintains itself,
    # so even a direct write cannot leave it stale — bumping a counter
    # by hand at fourteen call sites would be one missed line from a
    # silently wrong number.
    before = state._decisions.version
    first = state.get_current_queue()[0].image_path
    state._decisions[(first, "1girl")] = Decision.YES
    assert state._decisions.version > before
    assert state.current_queue_decided() == brute()
    print("OK: the shared count matches a brute-force count through "
          "decisions, skips, undo and tag changes")


def test_derived_values_are_not_recomputed() -> None:
    """Filenames never change, so neither do their sort keys or
    stems — but a queue rebuild recomputed both for every entry, and
    natural_key twice each (subfolder and filename).

    Measured on 2,400 names: 4.2 ms per sort uncached, 0.4 ms
    memoised."""
    from core.state import natural_key
    from ui.queue_panel import _stem_of

    # Correctness first: memoising must not change the ordering.
    names = ["a (10).png", "a (2).png", "a (1).png", "B (3).png"]
    assert sorted(names, key=natural_key) == [
        "a (1).png", "a (2).png", "a (10).png", "B (3).png"]
    # Numbers compare as numbers, text case-insensitively.
    assert natural_key("x_2") < natural_key("x_10")
    assert natural_key("Apple") < natural_key("banana")

    # And they are genuinely cached.
    natural_key.cache_clear()
    natural_key("cache_me")
    natural_key("cache_me")
    assert natural_key.cache_info().hits >= 1

    _stem_of.cache_clear()
    path = Path("/tmp/ds/image_00001.png")
    assert _stem_of(path) == "image_00001"
    _stem_of(path)
    assert _stem_of.cache_info().hits >= 1
    print("OK: sort keys and stems are memoised without changing the "
          "order they produce")


def test_subfolder_is_computed_without_pathlib() -> None:
    """Path.relative_to was about 40% of the time to open a folder —
    2,400 calls building 24,000 intermediate Path objects. The answer
    is a prefix strip, so string work does the same job.

    Rewriting real logic for speed means the edge cases matter more,
    not less."""
    from core.scanner import _subfolder_of

    root = Path(tempfile.mkdtemp())
    cases = [
        (root / "a.png", ""),
        (root / "train" / "a.png", "train"),
        (root / "train" / "subset_a" / "a.png", "train/subset_a"),
        # Japanese folder names: the publisher's own case.
        (root / "日本語" / "フォルダ" / "a.png", "日本語/フォルダ"),
        (root / "with space" / "a.png", "with space"),
        # Outside the root — a symlink could do this. The old code
        # caught ValueError and returned ""; this must match.
        (Path("/somewhere/else/a.png"), ""),
    ]
    for path, expected in cases:
        assert _subfolder_of(path, root) == expected, path
    # A folder whose name merely starts with the root's name must not
    # be mistaken for being inside it.
    sibling = Path(str(root) + "_other") / "a.png"
    assert _subfolder_of(sibling, root) == ""
    print("OK: subfolders resolve without pathlib, including nested, "
          "Japanese, spaced and outside-the-root paths")


def run() -> None:
    test_restyling_is_constant_per_decision()
    test_the_decided_count_is_computed_once()
    test_the_shared_count_stays_correct()
    test_derived_values_are_not_recomputed()
    test_subfolder_is_computed_without_pathlib()
    print("\nALL PASS: walk performance")


if __name__ == "__main__":
    run()
