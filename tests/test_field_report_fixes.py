"""
tests/test_field_report_fixes.py

Fixes from a full pass of real use on the packaged build.

These are grouped together because they share a cause worth naming:
every one of them was invisible to a headless suite. A wheel event
that quietly changes a setting, a window that floats over other
applications, a completion popup that stops after the first term —
none of these raise, none fail a test that was not written for them,
and all of them are obvious within a minute of using the program.

Run: python3 tests/test_field_report_fixes.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLineEdit,
    QSpinBox,
)

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core.scanner import scan
from core.state import SessionState
from ui.settings_dialog import SettingsDialog
from ui.tag_autocomplete import TagAutocomplete


def _fresh_settings() -> Settings:
    os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()
    s = Settings()
    initialize_theme(s.theme)
    return s


def _wheel() -> QWheelEvent:
    return QWheelEvent(
        QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False)


def test_the_wheel_scrolls_preferences_it_does_not_change_them() -> None:
    """FIELD REPORT: scrolling Preferences silently altered whatever
    control the pointer crossed. A settings change you did not intend
    and did not notice is the worst kind."""
    dialog = SettingsDialog(_fresh_settings())
    combos = dialog.findChildren(QComboBox)
    spins = dialog.findChildren(QSpinBox)
    assert combos and spins

    for widget in combos:
        before = widget.currentIndex()
        _app.sendEvent(widget, _wheel())
        assert widget.currentIndex() == before, widget.objectName()
    for widget in spins:
        before = widget.value()
        _app.sendEvent(widget, _wheel())
        assert widget.value() == before, widget.objectName()
    print("OK: the wheel over an unfocused combo or spin box changes "
          "nothing, so scrolling Preferences is safe")


def test_autocomplete_completes_the_term_under_the_cursor() -> None:
    """FIELD REPORT: only the first tag got suggestions. The whole box
    was matched as one string, so "1girl sol" matched nothing and the
    popup went quiet for every term after the first."""
    edit = QLineEdit()
    complete = TagAutocomplete(edit)

    for text, cursor, expected in [
            ("1girl", 5, "1girl"),
            ("1girl sol", 9, "sol"),
            ("1girl solo rating:g", 19, "rating:g"),
            ("1girl solo", 5, "1girl"),        # cursor in the first
    ]:
        edit.setText(text)
        edit.setCursorPosition(cursor)
        assert complete.current_term()[0] == expected, text

    # Applying a choice must not destroy the terms around it.
    edit.setText("1girl sol")
    edit.setCursorPosition(9)
    complete._apply_choice("solo")
    assert edit.text() == "1girl solo"
    edit.setText("1girl solo rating:g")
    edit.setCursorPosition(5)
    complete._apply_choice("2girls")
    assert edit.text() == "2girls solo rating:g"
    print("OK: every term in a search box gets suggestions, and "
          "accepting one leaves the rest of the query intact")


def test_pin_uses_the_system_wide_hint() -> None:
    """The pin is deliberately system-wide, after trying not to be.

    An attempt to scope it to this application with Qt.Tool made
    things worse on Windows: these windows are created PARENTLESS, so
    a Tool window has no owner to float above and the OS treats it as
    a palette for the whole app — it vanished whenever the main window
    was clicked, and two of them had no defined order between each
    other.

    Scoping it properly needs both windows parented to the main window
    at construction, which is a larger change than the problem
    justifies. Floating over other applications is the known cost of
    the version that works."""
    from ui.tag_reference_window import TagReferenceWindow

    class _NoNetwork:
        def get(self, url, cb):
            cb(None, 404)

    hint = Qt.WindowType.WindowStaysOnTopHint
    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=_NoNetwork())
    win.show()

    win.btn_on_top.setChecked(True)
    assert win.windowFlags() & hint
    # An ordinary Window, never a Tool.
    assert ((win.windowFlags() & Qt.WindowType.WindowType_Mask)
            == Qt.WindowType.Window)
    # Pinning must never cost the close button — an earlier bug.
    assert win.windowFlags() & Qt.WindowType.WindowCloseButtonHint

    win.btn_on_top.setChecked(False)
    assert not (win.windowFlags() & hint)
    assert win.windowFlags() & Qt.WindowType.WindowCloseButtonHint

    # The two windows pin independently of one another.
    browser = win._open_browser()
    win.btn_on_top.setChecked(True)
    browser.btn_on_top.setChecked(False)
    assert win.windowFlags() & hint
    assert not (browser.windowFlags() & hint)
    assert s.tag_reference_always_on_top is True
    assert s.post_browser_always_on_top is False
    print("OK: pinning uses the system-wide hint, keeps the close "
          "button, and each window pins independently")


def test_browser_columns_are_configurable() -> None:
    """20 results per page either way; this only changes how they are
    arranged. Five fills the default window."""
    s = _fresh_settings()
    assert s.post_browser_columns == 5          # new default
    for value in (4, 5, 6, 8, 10):
        s.post_browser_columns = value
        assert Settings().post_browser_columns == value
    s.post_browser_columns = 7                  # not offered
    assert Settings().post_browser_columns == 5  # falls back safely

    dialog = SettingsDialog(_fresh_settings())
    assert dialog._browser_cols.count() == 5
    print("OK: thumbnails per row is a preference with a safe "
          "fallback, and the page size is unaffected")


def test_undo_is_credited_to_the_tag_it_reverted() -> None:
    """FIELD REPORT: walk 1girl, switch to 1boy, press undo — the
    right decision was reverted but the action log credited 1boy,
    because it read the CURRENT selection instead of asking the entry
    what it belonged to."""
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    for i in range(3):
        Image.new("RGB", (8, 8)).save(folder / f"i{i}.png")
        (folder / f"i{i}.txt").write_text("1girl, 1boy, smile",
                                          encoding="utf-8")
    state = SessionState(scan(folder))
    state.select_tag("1girl")
    state.record_yes()
    state.select_tag("1boy")          # walk away before undoing
    assert state.current_tag == "1boy"

    # Read the context BEFORE undo restores the walk: the log builds
    # its entry from this, and at the moment of the undo the selection
    # is still the tag the user had moved to.
    state.undo()
    context = state.last_undo_context or {}
    assert context.get("tag") == "1girl"
    assert context.get("decision") == "Yes"
    # And it came from the entry, not from the selection — proven by
    # doing it again without any restore in between.
    state.select_tag("smile")
    state.record_yes()
    state.select_tag("1boy")
    state.undo()
    assert (state.last_undo_context or {}).get("tag") == "smile"
    print("OK: an undo reports the tag it actually reverted, not "
          "whichever tag happens to be selected")


def test_prewarm_never_leaves_the_main_thread() -> None:
    """The reference data used to load on first use, freezing the
    window for ~2.5s. It was briefly moved to a worker thread, which
    segfaulted Qt; it is now chunked across timer ticks instead."""
    import inspect

    from core import prewarm

    source = inspect.getsource(prewarm)
    assert "threading" not in source          # no worker thread
    assert "QTimer" in source
    steps = prewarm._steps()
    assert len(steps) >= 4
    prewarm.reset_for_tests()
    prewarm.run_all()                         # every step must work
    from core import tag_reference
    assert tag_reference._TABLES
    print("OK: bundled data loads in timed chunks on the main thread, "
          "with no worker thread to race Qt")


def test_no_window_offers_a_stray_context_menu() -> None:
    """FIELD REPORT, twice. Right-clicking a post offered Copy / Copy
    Link Location / Select All: Qt's default menu for interactive
    text, not a feature anyone designed. "Copy" needed a selection
    first and "Copy Link Location" copied an internal tag: address.

    The first fix named two labels and missed the caption, the status
    line, the picture — and every equivalent widget in three other
    windows. This checks the whole tree of every window, and after
    the rebuilds that create new widgets, because content here
    arrives asynchronously and a one-time sweep is outrun by it."""
    import json

    from PySide6.QtWidgets import QWidget
    from core import danbooru_api as dapi
    from ui.asset_viewer_dialog import AssetViewerDialog
    from ui.tag_reference_window import TagReferenceWindow

    class _Fake:
        def __init__(self, routes):
            self.routes = routes

        def get(self, url, cb):
            r = self.routes.get(url)
            cb(None, 404) if r is None else cb(r[0], r[1])

    def _png() -> bytes:
        import io
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buf, "PNG")
        return buf.getvalue()

    def offenders(win):
        default = Qt.ContextMenuPolicy.DefaultContextMenu
        return [c for c in [win] + win.findChildren(QWidget)
                if c.contextMenuPolicy() == default]

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    body = ("h4. Examples\n\n* !post #101: [[A]]\n"
            "* !post #102: [[B]]\n")
    wiki = json.dumps({"title": "t", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    posts = [{"id": i, "preview_file_url": f"https://x/p{i}",
              "large_file_url": f"https://x/l{i}", "rating": "g",
              "tag_string": "a b", "tag_count": 2} for i in (101, 102)]
    routes = {dapi.wiki_url("t"): (wiki, 200),
              dapi.posts_by_ids_url([101, 102]):
                  (json.dumps(posts).encode(), 200),
              dapi.posts_search_url("1girl", "rated", 1, 20):
                  (json.dumps(posts).encode(), 200)}
    for i in (101, 102):
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)

    win = TagReferenceWindow(s, fetcher=_Fake(routes))
    win.show()
    assert not offenders(win)
    win.navigate("t")                      # builds a whole document
    assert not offenders(win)
    win.btn_compare.setChecked(True)       # a second pane appears
    assert not offenders(win)
    win._panes[1].navigate("t")
    assert not offenders(win)

    browser = win._open_browser()
    browser._fetcher = _Fake(routes)
    browser.browse("1girl")                # builds thumbnails
    assert not offenders(browser)

    win.open_inspector(dapi.parse_posts(posts)[0])
    popup = win._inspectors[-1]
    assert not offenders(popup)            # the reported window
    popup._render_tags()                   # rebuilds the tag list
    assert not offenders(popup)

    assert not offenders(AssetViewerDialog(1, "caption"))

    # Suppressing the menu must not disable the links themselves.
    assert (popup.tag_label.textInteractionFlags()
            & Qt.TextInteractionFlag.LinksAccessibleByMouse)
    print("OK: no window offers Qt's default right-click menu, "
          "including after the rebuilds that create new widgets")


def test_export_naming_modes() -> None:
    """FIELD REPORT: downloads were named `danbooru_<id>`, which is
    unambiguous and useless for finding what you saved ten minutes
    ago — a folder of ids sorts by Danbooru upload order, not yours.

    Every mode keeps the post id, because it is the only part that
    identifies the source without doubt."""
    import datetime

    from core import danbooru_api as dapi
    from core import export_naming as naming

    when = datetime.datetime(2026, 8, 2, 14, 30, 5)
    post = dapi.parse_posts([{
        "id": 7431892, "preview_file_url": "p", "large_file_url": "l",
        "rating": "g",
        "tag_string": "1girl solo long_hair blue_eyes",
        "tag_string_general": "1girl solo long_hair blue_eyes",
        "tag_string_artist": "wada_arco", "tag_count": 4}])[0]

    assert naming.build_stem(post, naming.MODE_ID, when) == \
        "danbooru_7431892"
    assert naming.build_stem(post, naming.MODE_TIME_ID, when) \
        .startswith("20260802-143005")      # sorts correctly as text
    assert "1girl" in naming.build_stem(
        post, naming.MODE_TIME_TAGS, when)
    assert naming.build_stem(post, naming.MODE_ARTIST_ID, when) \
        .startswith("wada_arco")
    for mode, _label, _example in naming.MODES:
        assert "7431892" in naming.build_stem(post, mode, when), mode

    # Tags come from users and contain anything at all.
    nasty = dapi.parse_posts([{
        "id": 5, "preview_file_url": "p", "large_file_url": "l",
        "rating": "g", "tag_string": 'a<b>c:d/e\\f|g?h*i',
        "tag_string_general": 'a<b>c:d/e\\f|g?h*i',
        "tag_count": 1}])[0]
    stem = naming.build_stem(nasty, naming.MODE_TIME_TAGS, when)
    assert not any(ch in stem for ch in '<>:"/\\|?*')
    assert stem == stem.strip(" .")        # Windows strips these

    long_post = dapi.parse_posts([{
        "id": 9, "preview_file_url": "p", "large_file_url": "l",
        "rating": "g",
        "tag_string": " ".join(["averyverylongtagname"] * 10),
        "tag_string_general": " ".join(["averyverylongtagname"] * 10),
        "tag_count": 10}])[0]
    trimmed = naming.build_stem(long_post, naming.MODE_TIME_TAGS, when)
    assert len(trimmed) <= naming.MAX_STEM
    assert "danbooru_9" in trimmed         # the id survives the trim

    # An unrecognised setting must not produce a nameless file.
    assert naming.build_stem(post, "nonsense", when) == \
        naming.build_stem(post, naming.DEFAULT_MODE, when)
    print("OK: downloads can be named by id, timestamp, tags or "
          "artist; every mode keeps the post id and survives hostile "
          "tag text")


def test_downloading_does_not_open_the_folder() -> None:
    """FIELD REPORT: saving a post opened its folder. That was
    deliberate for videos — the app cannot play them — and it was
    still wrong: a window stealing focus mid-session is worse than a
    click, and the Post Browser has a folder button for this."""
    import io as _io
    import json as _json

    from PySide6.QtGui import QDesktopServices
    from core import danbooru_api as dapi
    from ui.tag_reference_window import TagReferenceWindow

    class _Fake:
        def __init__(self, routes):
            self.routes = routes

        def get(self, url, cb):
            r = self.routes.get(url)
            cb(None, 404) if r is None else cb(r[0], r[1])

    def _png() -> bytes:
        buf = _io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buf, "PNG")
        return buf.getvalue()

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    out = Path(tempfile.mkdtemp()) / "exports"
    out.mkdir()
    s.danbooru_export_dir = str(out)

    opened: list = []
    real = QDesktopServices.openUrl
    QDesktopServices.openUrl = lambda url: opened.append(url)
    try:
        video = dapi.parse_posts([{
            "id": 8, "preview_file_url": "https://x/p8",
            "large_file_url": "https://x/l8.mp4",
            "file_url": "https://x/o8.mp4", "file_ext": "mp4",
            "rating": "g", "tag_string": "animated",
            "tag_count": 1}])[0]
        win = TagReferenceWindow(s, fetcher=_Fake({
            "https://x/o8.mp4": (b"VIDEO", 200),
            "https://x/p8": (_png(), 200)}))
        win.show()
        win.open_inspector(video)
        popup = win._inspectors[-1]
        popup._export()
    finally:
        QDesktopServices.openUrl = real

    assert not opened                       # nothing was launched
    assert list(out.iterdir())              # but the file was saved
    assert "Post Browser" in popup.status.text()
    print("OK: downloading saves the file and says where it went, "
          "without opening a window")


def test_preferences_groups_are_signposted() -> None:
    """FIELD REPORT: the Preferences page ran as flat lists, so a
    hover-images checkbox was followed straight by a caching dropdown
    with nothing to mark the change of subject.

    Long groups need internal headings, or every setting reads as a
    continuation of the one above it. Two settings were also filed in
    the wrong place: the export options were split across ninety lines
    of unrelated controls, and the tag database sat under
    "Appearance"."""
    from PySide6.QtWidgets import QGroupBox, QLabel

    dialog = SettingsDialog(_fresh_settings())

    def headings(group) -> list:
        return [c.text()[3:-4] for c in group.findChildren(QLabel)
                if c.text().startswith("<b>")
                and c.text().endswith("</b>")]

    groups = {g.title(): g for g in dialog.findChildren(QGroupBox)}

    danbooru = next(g for name, g in groups.items()
                    if "Danbooru" in name)
    found = headings(danbooru)
    for expected in ("Lookups", "Cached images", "Saving posts to disk",
                     "Post browser", "Discover", "Blocked content"):
        assert expected in found, f"{expected} missing from {found}"

    # The export settings must sit together, not be split by caching
    # and browser options.
    order = [c.text() for c in danbooru.findChildren(QLabel)
             if c.text().strip()]
    saving = order.index("<b>Saving posts to disk</b>")
    browser = order.index("<b>Post browser</b>")
    folder = next(i for i, t in enumerate(order)
                  if "Export folder" in t)
    assert saving < folder < browser

    # The tag database is not an appearance setting.
    appearance = next(g for name, g in groups.items()
                      if "tag data" in name.lower())
    assert "Tag database" in headings(appearance)
    assert "Appearance" in headings(appearance)

    defaults = next(g for name, g in groups.items()
                    if "Default UI" in name)
    assert "Co-occurrence hints" in headings(defaults)
    print("OK: every long settings group carries internal headings, "
          "and the export and tag-database options are filed where "
          "they belong")


def test_group_mode_starts_the_group_walk() -> None:
    """FIELD REPORT: switching to group mode left the walk running on
    single images until a group header was clicked, and changing tag
    while grouped did the same.

    One cause for both: a group walk needs a selected group, and
    nothing selected one. advance_to_next_group_header() could not
    serve — it starts from the row AFTER the current one, which is
    right for advancing and wrong for arriving."""
    from core.scanner import scan
    from core.state import SessionState
    from ui.main_window import MainWindow

    s = _fresh_settings()
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    # The grouping convention is "name (1)", "name (2)" — the shape
    # Windows produces when copying files.
    for base in ("seriesA", "seriesB"):
        for i in range(1, 4):
            Image.new("RGB", (8, 8)).save(folder / f"{base} ({i}).png")
            (folder / f"{base} ({i}).txt").write_text(
                "1girl, 1boy, smile", encoding="utf-8")

    mw = MainWindow(s)
    mw._adopt_state(SessionState(scan(folder)), scan_root=None)
    queue = mw._queue_panel
    mw._state.select_tag("1girl")
    assert queue._selected_header_key is None

    queue.btn_group.setChecked(True)
    assert len(queue._groups_by_key) == 2
    # Entering group mode must START the group walk.
    assert queue._selected_header_key is not None
    assert queue.selected_group_preview() is not None

    # Changing tag while grouped must keep one selected.
    mw._state.select_tag("1boy")
    assert queue._selected_header_key is not None

    # A fully decided group is passed over for one with work left.
    mw._state.select_tag("smile")
    first = queue._selected_header_key
    for _ in range(12):
        image = mw._state.current_image
        if image is None:
            break
        if queue._group_key_of_image(image.image_path) != first:
            break
        mw._state.record_yes()
    queue.btn_group.setChecked(False)
    queue.btn_group.setChecked(True)
    assert queue._selected_header_key is not None
    assert queue._selected_header_key != first

    queue.btn_group.setChecked(False)
    assert queue._selected_header_key is None
    print("OK: entering group mode and changing tag both select a "
          "group straight away, preferring one with undecided images")


def run() -> None:
    test_the_wheel_scrolls_preferences_it_does_not_change_them()
    test_autocomplete_completes_the_term_under_the_cursor()
    test_pin_uses_the_system_wide_hint()
    test_browser_columns_are_configurable()
    test_preferences_groups_are_signposted()
    test_group_mode_starts_the_group_walk()
    test_no_window_offers_a_stray_context_menu()
    test_export_naming_modes()
    test_downloading_does_not_open_the_folder()
    test_undo_is_credited_to_the_tag_it_reverted()
    test_prewarm_never_leaves_the_main_thread()
    print("\nALL PASS: field report fixes")


if __name__ == "__main__":
    run()
