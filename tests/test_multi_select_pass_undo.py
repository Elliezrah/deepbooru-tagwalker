"""Regression tests for the multi-select "tagging pass" safety net.

Two protections for bulk tagging in multi-select:

  1. Atomic Add  - one Add click is a SINGLE undo step, even when several
     tags are ticked (so one Ctrl+Z reverses the whole stamp, not one
     tag at a time).

  2. Undo-all-this-pass - a running tally of every image stamped since
     entering multi-select, and a one-click reverse that pulls all the
     added tags back off and refreshes the finder. Resets on leaving
     multi-select. Robust past the bounded undo stack (tracks entries by
     reference, not by a positional index the deque could invalidate).

Run: python3 tests/test_multi_select_pass_undo.py
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


def _make_dataset(missing_count: int, missing_tags=("smile",)) -> Path:
    """Create `missing_count` images that all LACK `missing_tags` (plus a
    couple that already have them, as noise)."""
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)

    def mk(name, tags):
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")

    for i in range(2):
        mk(f"has{i}", ["1girl"] + list(missing_tags))
    for i in range(missing_count):
        mk(f"no{i}", ["1girl"])
    return d


def _enter_finder(st: SessionState, selected: list[str]) -> None:
    st.set_multi_select_mode(True)
    st.set_filter_mode(FilterMode.MISSING_TAG)
    st.set_selected_tags(selected)


# ---------------------------------------------------------------------------
# A. Atomic Add: multiple ticked tags -> ONE undo entry, reversed together.
# ---------------------------------------------------------------------------
def test_atomic_add() -> None:
    QApplication.instance() or QApplication([])
    d = _make_dataset(missing_count=1, missing_tags=("smile", "blush"))
    st = SessionState(scan(d))
    _enter_finder(st, ["smile", "blush"])

    assert len(st._undo_stack) == 0, "no actions yet"
    target = st.current_image.image_path
    assert target is not None

    added = st.multi_add_selected_to_current()
    assert added == 2, f"both ticked tags added, got {added}"
    tags_now = {t.lower() for t in st._image_tags[target]}
    assert {"smile", "blush"} <= tags_now, tags_now

    # The whole stamp is ONE undo entry, not two.
    assert len(st._undo_stack) == 1, \
        f"one Add click = one undo entry, got {len(st._undo_stack)}"
    assert st.multi_pass_image_count() == 1, "one image stamped this pass"

    # A single undo reverses BOTH tags.
    assert st.undo() is True
    tags_after = {t.lower() for t in st._image_tags.get(target, [])}
    assert "smile" not in tags_after and "blush" not in tags_after, \
        f"one undo should remove both tags, left {tags_after}"
    # Reverting that entry individually empties the pass tally too.
    assert st.multi_pass_image_count() == 0, \
        "individual undo should drop the entry from the pass"
    print("OK: atomic Add - multiple ticked tags reverse in one undo")


# ---------------------------------------------------------------------------
# B. Undo-all-this-pass through the real panel + UI tally.
# ---------------------------------------------------------------------------
def test_undo_all_pass_via_panel() -> None:
    app = QApplication.instance() or QApplication([])
    d = _make_dataset(missing_count=5)

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
    qp = w._queue_panel
    ip = w._image_panel

    # Enter the MISSING finder for 'smile' via the real checkbox.
    w._tag_tree.multi_select_btn.setChecked(True)
    app.processEvents()
    st.set_filter_mode(FilterMode.MISSING_TAG)
    app.processEvents()
    w._tag_tree._tag_items["smile"].setCheckState(0, Qt.CheckState.Checked)
    app.processEvents()

    assert qp.list_widget.count() == 5, \
        f"finder should list 5 images, got {qp.list_widget.count()}"
    # The Undo-all row is visible for the whole pass (so the layout doesn't
    # jump when the first edit lands); the button is disabled and the tally
    # reads "no changes" until something is stamped.
    assert ip._multi_undo_row.isVisible() is True, "row visible from pass start"
    assert ip.btn_undo_pass.isEnabled() is False, "undo-all disabled before any edit"
    assert "No changes" in ip.label_pass_count.text(), ip.label_pass_count.text()

    stamped = []
    for _ in range(5):
        assert st.current_image is not None, "should have an image each step"
        stamped.append(st.current_image.image_path)
        ip.trigger_yes()
        app.processEvents()

    assert qp.list_widget.count() == 0, \
        f"finder emptied after stamping all 5, got {qp.list_widget.count()}"
    assert st.multi_pass_image_count() == 5, st.multi_pass_image_count()
    # UI tally is visible and correct.
    assert ip._multi_undo_row.isVisible() is True, "row should show after adds"
    assert "5 images" in ip.label_pass_count.text(), ip.label_pass_count.text()
    # All five files actually got the tag.
    for p in stamped:
        assert "smile" in st._image_tags[p], f"{p.name} should have smile"

    # One click reverses the entire pass — confirm the dialog.
    from PySide6.QtWidgets import QMessageBox
    import ui.image_panel as _ip_mod
    _orig_q = _ip_mod.QMessageBox.question
    _ip_mod.QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    try:
        ip._on_undo_pass()
        app.processEvents()
    finally:
        _ip_mod.QMessageBox.question = _orig_q

    for p in stamped:
        assert "smile" not in st._image_tags[p], \
            f"{p.name} should have smile removed after Undo all"
    assert qp.list_widget.count() == 5, \
        f"finder should refill to 5, got {qp.list_widget.count()}"
    assert st.multi_pass_image_count() == 0, "pass cleared after Undo all"
    # Still in multi-select, so the row stays put; only the button disables.
    assert ip._multi_undo_row.isVisible() is True, "row stays visible in multi-select"
    assert ip.btn_undo_pass.isEnabled() is False, "undo-all disabled once pass empty"
    print("OK: Undo all (confirmed) reverses the whole pass, refreshes finder, "
          "row stays put with button disabled")


# ---------------------------------------------------------------------------
# C. A single Ctrl+Z decrements the pass tally (doesn't nuke the whole pass).
# ---------------------------------------------------------------------------
def test_individual_undo_decrements() -> None:
    app = QApplication.instance() or QApplication([])
    d = _make_dataset(missing_count=3)

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
    w._tag_tree.multi_select_btn.setChecked(True)
    app.processEvents()
    st.set_filter_mode(FilterMode.MISSING_TAG)
    app.processEvents()
    w._tag_tree._tag_items["smile"].setCheckState(0, Qt.CheckState.Checked)
    app.processEvents()

    for _ in range(3):
        w._image_panel.trigger_yes()
        app.processEvents()
    assert st.multi_pass_image_count() == 3, st.multi_pass_image_count()

    # One Edit -> Undo brings the pass tally down to 2, not 0.
    w._action_undo()
    app.processEvents()
    assert st.multi_pass_image_count() == 2, \
        f"single undo should leave 2 in the pass, got {st.multi_pass_image_count()}"
    print("OK: individual undo decrements the pass tally by one")


# ---------------------------------------------------------------------------
# D. Robust past the bounded undo stack (>UNDO_LIMIT additions).
#    Tracking by reference (not a positional floor) is what makes this work:
#    the deque discards its oldest entries once full, but the pass still
#    holds every revert closure and can run them all.
# ---------------------------------------------------------------------------
def test_pass_survives_undo_limit() -> None:
    QApplication.instance() or QApplication([])
    n = SessionState.UNDO_LIMIT + 20  # comfortably past the deque cap
    d = _make_dataset(missing_count=n)
    st = SessionState(scan(d))
    _enter_finder(st, ["smile"])

    stamped = []
    for _ in range(n):
        assert st.current_image is not None
        stamped.append(st.current_image.image_path)
        st.multi_add_selected_to_current()
    assert len(stamped) == n
    # The deque is capped, but the pass tracked every image.
    assert len(st._undo_stack) <= SessionState.UNDO_LIMIT, "deque is bounded"
    assert st.multi_pass_image_count() == n, \
        f"pass should track all {n}, got {st.multi_pass_image_count()}"

    reverted = st.undo_all_multi_pass()
    assert reverted == n, f"Undo all should report {n}, got {reverted}"
    for p in stamped:
        assert "smile" not in st._image_tags[p], \
            f"{p.name} should be reverted even past the undo cap"
    assert st.multi_pass_image_count() == 0
    print(f"OK: Undo all reverses all {n} additions past the {SessionState.UNDO_LIMIT}-entry undo cap")


def test_remove_tracked_in_pass() -> None:
    """Symmetry: removes fold into the pass the same as adds, so a bulk
    remove (single-image AND row-batch) is reversible via Undo-all."""
    app = QApplication.instance() or QApplication([])
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    # Four images that all HAVE smile, so Remove actually does something.
    for i in range(4):
        Image.new("RGB", (8, 8)).save(d / f"im{i}.png")
        (d / f"im{i}.txt").write_text("1girl, smile", encoding="utf-8")

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

    # Multi-select, HAS filter, tick smile via the real checkbox.
    w._tag_tree.multi_select_btn.setChecked(True)
    app.processEvents()
    st.set_filter_mode(FilterMode.HAS_TAG)
    app.processEvents()
    w._tag_tree._tag_items["smile"].setCheckState(0, Qt.CheckState.Checked)
    app.processEvents()

    assert st.multi_pass_image_count() == 0, "fresh pass"
    # Single-image Remove (No) — now tracked in the pass.
    ip.trigger_no()
    app.processEvents()
    assert st.multi_pass_image_count() == 1, \
        f"single remove should be tracked, got {st.multi_pass_image_count()}"

    # Row-batch Remove on the images that still have smile — also tracked.
    rest = [p for p in st._image_tags if "smile" in st._image_tags[p]]
    assert len(rest) == 3, rest
    st.multi_batch_remove_selected(rest)
    app.processEvents()
    assert st.multi_pass_image_count() == 4, st.multi_pass_image_count()

    # Undo-all (confirmed) brings every removed tag back.
    from PySide6.QtWidgets import QMessageBox
    import ui.image_panel as _ip_mod
    _orig_q = _ip_mod.QMessageBox.question
    _ip_mod.QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    try:
        ip._on_undo_pass()
        app.processEvents()
    finally:
        _ip_mod.QMessageBox.question = _orig_q

    assert all("smile" in st._image_tags[p] for p in st._image_tags), \
        "Undo all should restore every removed tag"
    assert st.multi_pass_image_count() == 0
    print("OK: removes (single + row-batch) are tracked in the pass and "
          "reversed by Undo all (symmetric with adds)")


def run() -> None:
    test_atomic_add()
    test_undo_all_pass_via_panel()
    test_individual_undo_decrements()
    test_pass_survives_undo_limit()
    test_remove_tracked_in_pass()
    print("\nALL PASS: multi-select pass undo")


if __name__ == "__main__":
    run()
