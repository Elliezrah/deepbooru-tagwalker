"""Tests for queue/tree navigation UX (field reports).

- Modifier + mouse wheel over the queue steps the selection (and the
  walk) image by image; the modifier is configurable in Settings
  (default Shift — Ctrl+wheel is image zoom) and read live.
- Qt's type-to-jump on the QUEUE is disabled (global shortcuts own
  most letters; leftovers caused surprise jumps). Real type-ahead now
  lives on the TAG TREE, matching tag NAMES in visual order.
- A full tree rebuild (new tag created) keeps the selected tag
  selected and in view instead of dumping the scroll position — even
  when the tree's currentItem is STALE from an earlier selection (the
  capture prefers the walk's current tag; that ordering bug was caught
  live and is pinned here).

Run: python3 tests/test_queue_nav_tree_ux.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QWheelEvent

_app = QApplication.instance() or QApplication([])

from core.state import SessionState, FilterMode
from core.scanner import scan
from config.settings import Settings
from ui.queue_panel import QueuePanel
from ui.tag_tree import TagTree


def _world(n=30):
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for i in range(n):
        Image.new("RGB", (8, 8)).save(d / f"im{i:02d}.png")
        (d / f"im{i:02d}.txt").write_text(
            f"1girl, tag_{i:02d}", encoding="utf-8")
    return SessionState(scan(d))


def _wheel(widget, mods, up=False):
    ev = QWheelEvent(
        QPointF(50, 50), QPointF(50, 50), QPoint(0, 0),
        QPoint(0, 120 if up else -120),
        Qt.MouseButton.NoButton, mods,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    widget.wheelEvent(ev)


def test_wheel_navigation() -> None:
    st = _world()
    st.select_tag("1girl")
    st.set_filter_mode(FilterMode.ALL)
    panel = QueuePanel()
    panel.attach(st)
    panel.resize(240, 400)
    panel.show()
    lw = panel.list_widget
    panel._app_settings.wheel_nav_modifier = "Shift"

    r0, img0 = lw.currentRow(), st.current_image.image_path.stem
    _wheel(lw, Qt.KeyboardModifier.ShiftModifier)
    assert lw.currentRow() == r0 + 1
    assert st.current_image.image_path.stem != img0     # the walk moved
    _wheel(lw, Qt.KeyboardModifier.ShiftModifier, up=True)
    assert lw.currentRow() == r0
    assert st.current_image.image_path.stem == img0

    r1 = lw.currentRow()
    _wheel(lw, Qt.KeyboardModifier.NoModifier)          # plain wheel
    assert lw.currentRow() == r1
    for _ in range(5):                                  # clamp at top
        _wheel(lw, Qt.KeyboardModifier.ShiftModifier, up=True)
    assert lw.currentRow() == 0

    panel._app_settings.wheel_nav_modifier = "Ctrl"     # live re-read
    r2 = lw.currentRow()
    _wheel(lw, Qt.KeyboardModifier.ShiftModifier)
    assert lw.currentRow() == r2                        # shift now inert
    _wheel(lw, Qt.KeyboardModifier.ControlModifier)
    assert lw.currentRow() == r2 + 1
    panel._app_settings.wheel_nav_modifier = "Disabled"
    r3 = lw.currentRow()
    _wheel(lw, Qt.KeyboardModifier.ControlModifier)
    assert lw.currentRow() == r3                        # fully off
    panel._app_settings.wheel_nav_modifier = "Shift"    # restore default
    print("OK: modifier+wheel steps the walk; plain wheel doesn't; "
          "clamped at the ends; setting is live and can disable")


def test_queue_typeahead_disabled() -> None:
    st = _world()
    st.select_tag("1girl")
    panel = QueuePanel()
    panel.attach(st)
    lw = panel.list_widget
    r = lw.currentRow()
    lw.keyboardSearch("i")      # every filename starts with 'i'…
    lw.keyboardSearch("z")
    assert lw.currentRow() == r
    print("OK: queue type-to-jump is a no-op (no surprise jumps on "
          "mispressed letters)")


def test_tree_typeahead_matches_tag_names() -> None:
    st = _world()
    tree = TagTree()
    tree.attach(st)
    tree.resize(260, 320)
    tree.show()
    tree.tree.keyboardSearch("tag_1")
    cur = tree.tree.currentItem()
    sel = next((t for t, i in tree._tag_items.items() if i is cur), None)
    assert sel is not None and sel.startswith("tag_1"), sel
    assert st.current_tag == sel        # jumping selects = starts walk
    tree.tree.keyboardSearch("zzz_nope")
    cur2 = tree.tree.currentItem()
    sel2 = next((t for t, i in tree._tag_items.items() if i is cur2), None)
    assert sel2 == sel                  # unknown prefix: stay put
    print("OK: tree type-ahead matches TAG NAMES (visual order) and "
          "selecting starts that walk; unknown prefixes do nothing")


def test_tree_rebuild_preserves_selection_and_view() -> None:
    """Both orderings: a fresh selection, AND the stale-currentItem case
    (an earlier '1girl' selection leaves currentItem pointing at it
    while the walk moves to tag_20) — the capture must prefer the
    walk's current tag."""
    for stale_first in (False, True):
        st = _world(40)
        if stale_first:
            st.select_tag("1girl")      # leaves a stale currentItem
        tree = TagTree()
        tree.attach(st)
        tree.resize(260, 300)
        tree.show()
        st.select_tag("tag_20")
        tree.tree.scrollToItem(tree._tag_items["tag_20"])
        walk_before = st.current_tag
        ap = next(i.image_path for i in st.all_images)
        st.add_tag_to_image(ap, "aaa_brand_new")   # forces full rebuild
        cur = tree.tree.currentItem()
        sel = next(
            (t for t, i in tree._tag_items.items() if i is cur), None)
        assert sel == "tag_20", (stale_first, sel)
        rect = tree.tree.visualItemRect(tree._tag_items["tag_20"])
        assert rect.intersects(tree.tree.viewport().rect()), stale_first
        assert st.current_tag == walk_before       # no walk restart
    print("OK: creating a new tag keeps the selected tag selected and "
          "in view — fresh and stale-currentItem orderings alike")


def test_wheel_setting_roundtrip() -> None:
    s = Settings()
    original = s.wheel_nav_modifier
    s.wheel_nav_modifier = "Alt"
    assert s.wheel_nav_modifier == "Alt"
    s.wheel_nav_modifier = "bogus_value"
    assert s.wheel_nav_modifier == "Shift"       # validated fallback
    s.wheel_nav_modifier = original
    print("OK: wheel-nav setting round-trips and rejects junk values")


def test_wheel_nav_with_physical_modifier_held() -> None:
    """The field condition the synthesized-event tests missed: the
    handler's multi-selection guard reads the GLOBAL keyboard state,
    and the user is physically holding Shift because it's the wheel
    gesture — so every tick moved the highlight but never the walk
    (image window frozen). Wheel transitions now self-identify and are
    exempt; Shift + non-wheel row changes stay guarded."""
    st = _world()
    st.select_tag("1girl")
    st.set_filter_mode(FilterMode.ALL)
    panel = QueuePanel()
    panel.attach(st)
    panel.resize(240, 400)
    panel.show()
    lw = panel.list_widget
    panel._app_settings.wheel_nav_modifier = "Shift"

    real_km = QApplication.keyboardModifiers
    QApplication.keyboardModifiers = staticmethod(
        lambda: Qt.KeyboardModifier.ShiftModifier)
    try:
        r0, img0 = lw.currentRow(), st.current_image.image_path.stem
        _wheel(lw, Qt.KeyboardModifier.ShiftModifier)
        assert lw.currentRow() == r0 + 1
        assert st.current_image.image_path.stem != img0   # image followed

        stems = []
        for _ in range(5):                                # rapid stream
            _wheel(lw, Qt.KeyboardModifier.ShiftModifier)
            stems.append(st.current_image.image_path.stem)
        assert stems == sorted(set(stems)), stems         # every tick

        # The guard's real purpose survives: Shift + a NON-wheel row
        # change (arrow/click-style selection extension) must not move
        # the walk.
        img_before = st.current_image.image_path.stem
        lw.setCurrentRow(lw.currentRow() + 3)
        assert st.current_image.image_path.stem == img_before
    finally:
        QApplication.keyboardModifiers = real_km
    print("OK: with the physical modifier held, the image follows every "
          "wheel tick; non-wheel modifier row changes stay guarded")


def run() -> None:
    test_wheel_navigation()
    test_queue_typeahead_disabled()
    test_tree_typeahead_matches_tag_names()
    test_tree_rebuild_preserves_selection_and_view()
    test_wheel_setting_roundtrip()
    test_wheel_nav_with_physical_modifier_held()
    print("\nALL PASS: queue/tree navigation UX")


if __name__ == "__main__":
    run()
