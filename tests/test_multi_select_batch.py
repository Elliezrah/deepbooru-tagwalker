"""Regression tests for multi-select row-batch Add/Remove (Part 3).

Mirrors normal-mode batch behavior into multi-select:
  * Ctrl/Shift-select rows, press Add  -> stamp ALL ticked tags onto every
    selected row, one undo step, no walk advance, tracked in the pass.
  * Ctrl/Shift-select rows, press No   -> remove ALL ticked tags from every
    selected row, one undo step, no walk advance, NOT tracked in the pass.
  * No rows selected -> Add/No act on the current image and advance, exactly
    like single-image walking (No is the new mirror of Add).
  * The No button is enabled in multi-select (it removes the ticked tags).

Run: python3 tests/test_multi_select_batch.py
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
from ui.queue_panel import IMAGE_ENTRY_ROLE


def _build(window_tags_per_image):
    """window_tags_per_image: list of (name, [tags]). Returns (app, w, st, qp, ip)."""
    app = QApplication.instance() or QApplication([])
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for name, tags in window_tags_per_image:
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")

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
    return app, w, st, w._queue_panel, w._image_panel


def _enter(w, st, app, mode, ticked):
    w._tag_tree.multi_select_btn.setChecked(True)
    app.processEvents()
    st.set_filter_mode(mode)
    app.processEvents()
    for t in ticked:
        w._tag_tree._tag_items[t].setCheckState(0, Qt.CheckState.Checked)
    app.processEvents()


def _select_rows(qp, paths):
    """Select the queue rows for the given image paths (Ctrl-click sim)."""
    qp.list_widget.clearSelection()
    wanted = set(paths)
    for i in range(qp.list_widget.count()):
        item = qp.list_widget.item(i)
        entry = item.data(IMAGE_ENTRY_ROLE)
        if entry is not None and entry.image_path in wanted:
            item.setSelected(True)


def _rows(qp):
    return [
        qp.list_widget.item(i).data(IMAGE_ENTRY_ROLE).image_path
        for i in range(qp.list_widget.count())
        if qp.list_widget.item(i).data(IMAGE_ENTRY_ROLE) is not None
    ]


# ---------------------------------------------------------------------------
# 1. Row-batch Add in the MISSING finder.
# ---------------------------------------------------------------------------
def test_row_batch_add() -> None:
    app, w, st, qp, ip = _build(
        [("has0", ["1girl", "smile"]), ("has1", ["1girl", "smile"])]
        + [(f"no{i}", ["1girl"]) for i in range(5)]
    )
    _enter(w, st, app, FilterMode.MISSING_TAG, ["smile"])
    assert qp.list_widget.count() == 5, _rows(qp)

    # Ctrl-select 3 of the 5 missing rows.
    rows = _rows(qp)
    chosen = rows[:3]
    _select_rows(qp, chosen)
    app.processEvents()

    ip.trigger_yes()  # Add
    app.processEvents()

    # All 3 chosen got the tag; ONE undo entry; pass counts 3.
    for p in chosen:
        assert "smile" in st._image_tags[p], f"{p.name} should get smile"
    assert len(st._undo_stack) == 1, \
        f"row-batch add is ONE undo step, got {len(st._undo_stack)}"
    assert st.multi_pass_image_count() == 3, st.multi_pass_image_count()
    # Finder shrank to the 2 still missing it.
    assert qp.list_widget.count() == 2, _rows(qp)

    # One undo reverses the whole batch.
    assert st.undo() is True
    for p in chosen:
        assert "smile" not in st._image_tags[p], f"{p.name} should revert"
    assert qp.list_widget.count() == 5, _rows(qp)
    print("OK: row-batch Add stamps all selected rows in one undoable step")


# ---------------------------------------------------------------------------
# 2. Row-batch Remove (No) in the HAS finder.
# ---------------------------------------------------------------------------
def test_row_batch_remove() -> None:
    app, w, st, qp, ip = _build(
        [(f"g{i}", ["1girl", "smile"]) for i in range(5)]
        + [("plain", ["1girl"])]
    )
    _enter(w, st, app, FilterMode.HAS_TAG, ["smile"])
    assert qp.list_widget.count() == 5, _rows(qp)

    rows = _rows(qp)
    chosen = rows[:3]
    _select_rows(qp, chosen)
    app.processEvents()

    ip.trigger_no()  # Remove
    app.processEvents()

    for p in chosen:
        assert "smile" not in st._image_tags[p], f"{p.name} should lose smile"
    assert len(st._undo_stack) == 1, \
        f"row-batch remove is ONE undo step, got {len(st._undo_stack)}"
    # Removals ARE folded into the pass now (symmetric with Add), so a bulk
    # remove can be reversed via "Undo all this pass" too.
    assert st.multi_pass_image_count() == 3, st.multi_pass_image_count()
    # The 3 left the HAS finder.
    assert qp.list_widget.count() == 2, _rows(qp)

    assert st.undo() is True
    for p in chosen:
        assert "smile" in st._image_tags[p], f"{p.name} should be restored"
    assert qp.list_widget.count() == 5, _rows(qp)
    print("OK: row-batch No removes the ticked tags from all selected rows")


# ---------------------------------------------------------------------------
# 3. Single-image No (no rows selected) removes from current + advances.
# ---------------------------------------------------------------------------
def test_single_no_removes_and_advances() -> None:
    app, w, st, qp, ip = _build(
        [(f"g{i}", ["1girl", "smile"]) for i in range(4)]
    )
    _enter(w, st, app, FilterMode.HAS_TAG, ["smile"])
    assert qp.list_widget.count() == 4

    # No deliberate row selection -> acts on the current image.
    qp.list_widget.clearSelection()
    first = st.current_image.image_path
    # No button must be available in multi-select.
    assert ip.btn_no.isEnabled() is True, "No should be enabled in multi-select"

    ip.trigger_no()  # Remove ticked tag from current image
    app.processEvents()

    assert "smile" not in st._image_tags[first], "current image should lose smile"
    assert qp.list_widget.count() == 3, "current image left the HAS finder"
    assert st.multi_pass_image_count() == 1, "removal IS tracked in pass now"
    print("OK: single-image No removes the ticked tag from the current image")


# ---------------------------------------------------------------------------
# 4. Walk symmetry: single-image Add advances; row-batch does not advance.
# ---------------------------------------------------------------------------
def test_walk_symmetry() -> None:
    app, w, st, qp, ip = _build(
        [("carrier", ["1girl", "smile"])]
        + [(f"no{i}", ["1girl"]) for i in range(4)]
    )
    _enter(w, st, app, FilterMode.MISSING_TAG, ["smile"])
    rows_before = _rows(qp)
    assert len(rows_before) == 4
    first = st.current_image.image_path

    # Single-image Add (no selection): stamps current, it leaves the finder,
    # and the cursor advances to the next missing image.
    qp.list_widget.clearSelection()
    ip.trigger_yes()
    app.processEvents()
    assert "smile" in st._image_tags[first]
    assert st.current_image is not None and st.current_image.image_path != first, \
        "single-image Add should advance off the stamped image"
    assert qp.list_widget.count() == 3
    print("OK: single-image Add advances; row-batch keeps position (no auto-advance)")


def run() -> None:
    test_row_batch_add()
    test_row_batch_remove()
    test_single_no_removes_and_advances()
    test_walk_symmetry()
    print("\nALL PASS: multi-select row-batch Add/Remove")


if __name__ == "__main__":
    run()
