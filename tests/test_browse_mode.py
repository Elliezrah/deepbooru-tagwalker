"""
tests/test_browse_mode.py

Tests for browse mode: inspecting a folder's images with no tag selected,
so fresh uncaptioned images can be selected and captioned from zero via
the file-state panel. Entered by clicking a folder header. Yes/No are
inert (no active tag); the "show images without .txt" toggle gates the
uncaptioned images exactly as in a tag walk.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from core.state import SessionState
from core.scanner import scan


def _dataset():
    """Root folder: 2 captioned images, 2 uncaptioned (no .txt)."""
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(d / "cap1.png")
    (d / "cap1.txt").write_text("1girl, smile", encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "cap2.png")
    (d / "cap2.txt").write_text("1girl, hat", encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "raw1.png")   # no caption
    Image.new("RGB", (8, 8)).save(d / "raw2.png")   # no caption
    return d


def test_no_selection_still_empty():
    """Unchanged baseline: with no tag selected and not browsing, the
    queue is empty (browse mode is opt-in via a folder click)."""
    st = SessionState(scan(_dataset()))
    assert st.get_current_queue() == []
    assert not st.browse_mode
    print("OK: nothing selected -> empty queue (unchanged)")


def test_browse_shows_all_images_with_orphans_on():
    """Browsing the root with the show-without-txt toggle ON lists every
    image — captioned and uncaptioned — and they are selectable."""
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    st.enter_browse_mode("")
    names = sorted(e.image_path.name for e in st.get_current_queue())
    assert names == ["cap1.png", "cap2.png", "raw1.png", "raw2.png"], names
    assert st.browse_mode
    assert st.current_tag is None, "browse mode has no active tag"
    assert st.current_image is not None, "images must be selectable"
    print("OK: browse lists all images; selectable; no active tag")


def test_toggle_hides_uncaptioned():
    """The show-without-txt toggle gates uncaptioned images in browse mode
    exactly as in a tag walk: OFF hides them."""
    st = SessionState(scan(_dataset()))
    st.enter_browse_mode("")
    st.set_show_orphans(False)
    names = sorted(e.image_path.name for e in st.get_current_queue())
    assert names == ["cap1.png", "cap2.png"], names
    st.set_show_orphans(True)
    names = sorted(e.image_path.name for e in st.get_current_queue())
    assert names == ["cap1.png", "cap2.png", "raw1.png", "raw2.png"], names
    print("OK: 'show without txt' toggle hides/shows uncaptioned in browse")


def test_selecting_tag_exits_browse():
    """Clicking a real tag leaves browse mode (mutually exclusive views)."""
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    st.enter_browse_mode("")
    assert st.browse_mode
    assert st.select_tag("smile"), "should select a known tag"
    assert not st.browse_mode, "selecting a tag must exit browse mode"
    assert st.current_tag == "smile"
    print("OK: selecting a tag exits browse mode")


def test_multi_select_exits_browse():
    """Entering multi-select mode leaves browse mode."""
    st = SessionState(scan(_dataset()))
    st.enter_browse_mode("")
    assert st.browse_mode
    st.set_multi_select_mode(True)
    assert not st.browse_mode, "multi-select must end browse mode"
    assert st.multi_select_mode
    print("OK: entering multi-select exits browse mode")


def test_exit_browse_empties_queue():
    """Leaving browse mode with no tag selected empties the queue."""
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    st.enter_browse_mode("")
    assert st.get_current_queue()
    st.exit_browse_mode()
    assert st.get_current_queue() == []
    assert not st.browse_mode
    print("OK: exiting browse mode empties the queue")


def test_search_and_sort_apply_in_browse():
    """Queue search and sort still work in browse mode (shared finalize)."""
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    st.enter_browse_mode("")
    st.set_queue_search("cap1")
    names = [e.image_path.name for e in st.get_current_queue()]
    assert names == ["cap1.png"], names
    st.set_queue_search("")
    assert len(st.get_current_queue()) == 4
    print("OK: search (and sort) apply in browse mode")


def test_queue_panel_renders_browse_queue():
    """Regression: the queue PANEL must render the browse-mode queue.
    It was bailing to an empty list whenever current_tag was None (the
    end-of-walk case), which also swallowed browse mode — the queue was
    built but never shown. Browse is now an exception, like multi-select.
    """
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    from ui.queue_panel import QueuePanel
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    qp = QueuePanel()
    qp.attach(st)
    assert qp.list_widget.count() == 0, "nothing shown before browse"
    st.enter_browse_mode("")
    assert qp.list_widget.count() == 4, (
        f"browse queue should render 4 rows, got {qp.list_widget.count()}")
    st.set_show_orphans(False)
    assert qp.list_widget.count() == 2, (
        "toggling show-without-txt off should update the panel to 2 rows")
    print("OK: queue panel renders and updates the browse-mode queue")


def test_over_token_limit_filter_applies_in_browse():
    """The over-token-limit filter IS tag-independent, so it applies in
    browse mode (unlike Has/Missing/Skipped, which key on a tag and are
    ignored). Only captions strictly over the limit survive; uncaptioned
    images (0 tokens) drop even with the orphan toggle on."""
    from core.state import FilterMode
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(d / "short.png")
    (d / "short.txt").write_text("1girl, solo", encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "long.png")
    (d / "long.txt").write_text(
        ", ".join(f"tag{i}" for i in range(40)), encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "raw.png")   # uncaptioned
    st = SessionState(scan(d))
    st.set_token_limit(10)
    st.set_show_orphans(True)
    st.enter_browse_mode("")

    st.set_filter_mode(FilterMode.ALL)
    assert len(st.get_current_queue()) == 3

    st.set_filter_mode(FilterMode.OVER_TOKEN_LIMIT)
    names = sorted(e.image_path.name for e in st.get_current_queue())
    assert names == ["long.png"], names
    print("OK: over-token-limit filter narrows the browse queue")

    # Tag-based filters are ignored (no active tag) -> queue unchanged.
    for fm in (FilterMode.HAS_TAG, FilterMode.MISSING_TAG,
               FilterMode.SKIPPED_ONLY):
        st.set_filter_mode(fm)
        assert len(st.get_current_queue()) == 3, fm
    print("OK: tag-based filters are ignored in browse mode")


def test_caption_edit_is_undoable_and_logged_in_browse():
    """A file-state caption edit in browse mode pushes an undo entry and
    emits an action-log line, exactly as in a tag walk — so Backspace can
    revert it and the action log records it."""
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(d / "raw1.png")   # uncaptioned
    st = SessionState(scan(d))
    st.set_show_orphans(True)
    st.enter_browse_mode("")
    img = st.current_image
    assert img is not None
    path = img.image_path

    logs = []
    st.add_listener(
        lambda ch: logs.append(ch.extra) if ch.kind == "action_logged"
        else None)

    assert not st.can_undo(), "clean start"
    assert st.add_tag_to_image(path, "1girl")
    assert (d / "raw1.txt").exists(), "caption file auto-created"
    assert any("add" in (e or "").lower() for e in logs), \
        "the edit should be logged"
    assert st.can_undo(), "edit should be undoable"
    st.undo()
    assert "1girl" not in st.get_image_tags(path), "undo should revert it"
    print("OK: browse-mode caption edit is undoable and logged")


def test_browse_ui_highlight_clears_and_back_enables():
    """UI: entering browse clears the blue tag highlight (no tag is
    active), and the Back button enables once a caption edit gives it
    something to undo, while Yes/No stay disabled."""
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    from ui.image_panel import ImagePanel
    from ui.tag_tree import TagTree, CURRENT_ROLE
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    panel = ImagePanel()
    panel.attach(st)
    tree = TagTree()
    tree.attach(st)

    st.select_tag("smile")
    smile = tree._tag_items.get("smile")
    assert smile is not None and smile.data(0, CURRENT_ROLE) is True
    st.enter_browse_mode("")
    assert not any(it.data(0, CURRENT_ROLE)
                   for it in tree._tag_items.values()), \
        "no tag row should stay highlighted in browse mode"

    panel._refresh_enabled()
    assert not panel.btn_back.isEnabled(), "nothing to undo yet"
    img = st.current_image
    assert img is not None
    st.add_tag_to_image(img.image_path, "newtag")
    panel._refresh_enabled()
    assert panel.btn_back.isEnabled(), "Back enables after a caption edit"
    assert panel.btn_yes.isHidden() and panel.btn_no.isHidden(), \
        "Yes/No are hidden in browse mode"
    print("OK: browse clears tag highlight; Back enables on edit; Yes/No hidden")


def test_browse_clears_tree_selection_indicator():
    """Entering browse mode must clear BOTH blue indicators on the tag
    tree: our accent bar (CURRENT_ROLE) and Qt's own :selected row
    background. The Qt selection is separate from our styling, so it
    stayed stuck on the last-clicked tag until explicitly cleared."""
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    from ui.tag_tree import TagTree
    from ui.accent_bar import CURRENT_ROLE
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    tree = TagTree()
    tree.attach(st)

    st.select_tag("smile")
    smile = tree._tag_items.get("smile")
    assert smile is not None
    tree.tree.setCurrentItem(smile)
    smile.setSelected(True)
    assert tree.tree.currentItem() is smile
    assert len(tree.tree.selectedItems()) >= 1
    assert smile.data(0, CURRENT_ROLE) is True

    st.enter_browse_mode("")
    assert tree.tree.currentItem() is None, "Qt current item must clear"
    assert len(tree.tree.selectedItems()) == 0, \
        "Qt :selected blue must clear in browse mode"
    assert not smile.data(0, CURRENT_ROLE), "accent bar must clear"
    print("OK: browse clears both the accent bar and the Qt selection blue")


def test_browse_hides_decision_buttons():
    """Yes/No/Skip are HIDDEN in browse mode (not just disabled); Back
    stays visible for undo. Leaving browse restores them, and multi-select
    button visibility is unchanged."""
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    from ui.image_panel import ImagePanel
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    panel = ImagePanel()
    panel.attach(st)

    def hidden(b):
        return b.isHidden()

    st.select_tag("smile")
    panel._refresh_enabled()
    assert not any(hidden(b) for b in (panel.btn_yes, panel.btn_no,
                                       panel.btn_skip, panel.btn_back))

    st.enter_browse_mode("")
    panel._refresh_enabled()
    assert hidden(panel.btn_yes) and hidden(panel.btn_no) \
        and hidden(panel.btn_skip), "Yes/No/Skip hidden in browse"
    assert not hidden(panel.btn_back), "Back stays visible in browse"

    st.select_tag("smile")
    panel._refresh_enabled()
    assert not any(hidden(b) for b in (panel.btn_yes, panel.btn_no,
                                       panel.btn_skip, panel.btn_back)), \
        "buttons restored after leaving browse"

    st.set_multi_select_mode(True)
    panel._refresh_enabled()
    assert not hidden(panel.btn_yes) and not hidden(panel.btn_no) \
        and hidden(panel.btn_skip), "multi-select visibility unchanged"
    print("OK: browse hides Yes/No/Skip, keeps Back; restores on exit")


def test_browse_hides_tag_based_filters():
    """The filter bar hides the tag-based options (Only WITH / WITHOUT /
    skipped) in browse mode, keeping All and Over-token-limit, and snaps a
    tag-based selection to All. Leaving browse restores them."""
    from PySide6.QtWidgets import QApplication
    from core.state import FilterMode
    _app = QApplication.instance() or QApplication(sys.argv)
    from ui.filter_bar import FilterBar
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    fb = FilterBar()
    fb.attach(st)

    def row_of(mode):
        for i in range(fb.filter_combo.count()):
            if fb.filter_combo.itemData(i) == mode:
                return i
        return -1

    def hidden(mode):
        return fb.filter_combo.view().isRowHidden(row_of(mode))

    st.select_tag("smile")
    for m in (FilterMode.ALL, FilterMode.HAS_TAG, FilterMode.MISSING_TAG,
              FilterMode.SKIPPED_ONLY, FilterMode.OVER_TOKEN_LIMIT):
        assert not hidden(m), f"{m.name} visible in walk mode"

    st.set_filter_mode(FilterMode.HAS_TAG)
    st.enter_browse_mode("")
    assert hidden(FilterMode.HAS_TAG) and hidden(FilterMode.MISSING_TAG) \
        and hidden(FilterMode.SKIPPED_ONLY), "tag-based filters hidden"
    assert not hidden(FilterMode.ALL) and not hidden(FilterMode.OVER_TOKEN_LIMIT)
    assert st.filter_mode == FilterMode.ALL, "tag-based filter snapped to ALL"

    st.select_tag("smile")
    for m in (FilterMode.HAS_TAG, FilterMode.MISSING_TAG,
              FilterMode.SKIPPED_ONLY):
        assert not hidden(m), f"{m.name} restored after leaving browse"
    print("OK: browse hides tag-based filters, keeps all/token, restores")


def test_browse_entry_is_action_logged():
    """Entering browse mode emits an action-log line naming the folder,
    and the action-log widget renders it."""
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    from ui.action_log import ActionLog
    st = SessionState(scan(_dataset()))
    st.set_show_orphans(True)
    al = ActionLog()
    al.attach(st)

    before = al._list.count()
    st.enter_browse_mode("")
    after = al._list.count()
    assert after > before, "browse entry should add a log row"
    assert "browsing" in al._list.item(0).text().lower(), \
        "log row should describe browsing"
    print("OK: browse mode entry is written to the action log")


if __name__ == "__main__":
    test_no_selection_still_empty()
    test_browse_shows_all_images_with_orphans_on()
    test_toggle_hides_uncaptioned()
    test_selecting_tag_exits_browse()
    test_multi_select_exits_browse()
    test_exit_browse_empties_queue()
    test_search_and_sort_apply_in_browse()
    test_queue_panel_renders_browse_queue()
    test_over_token_limit_filter_applies_in_browse()
    test_caption_edit_is_undoable_and_logged_in_browse()
    test_browse_ui_highlight_clears_and_back_enables()
    test_browse_clears_tree_selection_indicator()
    test_browse_hides_decision_buttons()
    test_browse_hides_tag_based_filters()
    test_browse_entry_is_action_logged()
    print("\nALL PASS: browse mode")
