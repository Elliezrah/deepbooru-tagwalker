"""
tests/test_bookmarks.py

Tests for core/bookmarks.py and the star button the three surfaces
share.

The format matters as much as the behaviour here. Bookmarks live in
plain JSON beside the settings, not in a cache or a database, for one
reason: if the program stops opening on someone's machine, the saved
posts, tags and searches are still a text file they can read and copy
out. Everything below that looks like fussiness about files is
protecting that.

Run: python3 tests/test_bookmarks.py
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from PIL import Image
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import bookmarks as bmk
from core import danbooru_api as dapi
from ui.tag_reference_window import TagReferenceWindow


class FakeFetcher:
    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls: list[str] = []

    def get(self, url, cb):
        self.calls.append(url)
        r = self.routes.get(url)
        cb(None, 404) if r is None else cb(r[0], r[1])


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return buf.getvalue()


def _fresh_settings() -> Settings:
    os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()
    bmk._STORES.clear()          # a new config dir means a new file
    s = Settings()
    initialize_theme(s.theme)
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    return s


def test_store_round_trip() -> None:
    path = Path(tempfile.mkdtemp()) / "bookmarks.json"
    bmk._STORES.clear()
    store = bmk.get_store(path)
    assert store.all() == []                 # missing file is empty

    store.add("tag", "high_heels", "high_heels")
    store.add("post", "4229961", "post #4229961 - pumps")
    store.add("query", "1girl rating:general",
              "1girl rating:general", sort="rated")
    assert len(store.all()) == 3
    assert [b.value for b in store.all("tag")] == ["high_heels"]
    # Newest first: the thing just saved is the likeliest wanted again.
    assert store.all()[0].value == "1girl rating:general"
    assert store.all("query")[0].sort == "rated"

    assert not store.add("tag", "high_heels")      # deduped
    assert store.toggle("tag", "high_heels") is False
    assert not store.contains("tag", "high_heels")
    assert store.toggle("tag", "high_heels") is True

    # Three windows sharing one file must share one store, or each
    # would save its own stale list over the others.
    assert bmk.get_store(path) is store
    print("OK: bookmarks of all three kinds round-trip, dedupe, "
          "toggle, and share one store per file")


def test_file_is_readable_and_survives_damage() -> None:
    path = Path(tempfile.mkdtemp()) / "bookmarks.json"
    bmk._STORES.clear()
    store = bmk.get_store(path)
    store.add("tag", "long_hair", "long_hair")

    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["version"] == bmk.FORMAT_VERSION
    assert data["bookmarks"][0]["value"] == "long_hair"
    assert "will not open" in data["note"]     # says why it is plain
    assert raw.count("\n") > 4                 # indented, not one line

    # Hand-edited into nonsense: load empty rather than refuse to
    # start, and leave the damaged file alone so it can be salvaged.
    path.write_text("{ not json at all", encoding="utf-8")
    bmk._STORES.clear()
    broken = bmk.get_store(path)
    assert broken.all() == []
    assert path.read_text(encoding="utf-8").startswith("{ not")
    broken.add("tag", "x")
    assert json.loads(path.read_text(encoding="utf-8"))

    # Atomic writes leave no litter behind.
    assert not any(f.name.startswith(".bookmarks-")
                   for f in path.parent.iterdir())
    print("OK: the file is indented JSON with a recovery note, a "
          "corrupt one loads empty without raising, and writes leave "
          "no temporary files")


def test_the_three_surfaces() -> None:
    """Each kind opens differently — a tag opens its wiki, a search
    re-runs, a post reopens — but they share one store and one
    control."""
    s = _fresh_settings()
    posts = [{"id": i, "preview_file_url": f"https://x/p{i}",
              "large_file_url": f"https://x/l{i}", "rating": "g",
              "tag_string": "high_heels 1girl solo",
              "tag_count": 3} for i in (1, 2)]
    routes = {
        dapi.posts_search_url("high_heels", "rated", 1, 20):
            (json.dumps(posts).encode(), 200),
        dapi.posts_search_url("shoes", "rated", 1, 20):
            (json.dumps(posts).encode(), 200),
        dapi.post_url(1): (json.dumps(posts[0]).encode(), 200)}
    for i in (1, 2):
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)
    fetcher = FakeFetcher(routes)
    ref = TagReferenceWindow(s, fetcher=fetcher)
    ref.show()
    browser = ref._open_browser()

    # --- tag: opens the wiki ---
    ref.navigate("long_hair")
    assert ref.btn_bookmark.text() == bmk_star_off()
    ref.btn_bookmark._toggle()
    assert ref.btn_bookmark.text() == bmk_star_on()
    ref.navigate("smile")
    assert ref.btn_bookmark.text() == bmk_star_off()
    ref.btn_bookmark._rebuild()
    entry = [a for a in ref.btn_bookmark._menu.actions()
             if a.text() == "long_hair"][0]
    entry.trigger()
    assert ref.search.text() == "long_hair"

    # --- query: re-runs the search, ordering included ---
    browser.browse("high_heels")
    browser.btn_bookmark._toggle()
    browser.browse("shoes")
    for i in range(browser.sort.count()):
        if browser.sort.itemData(i) == "newest":
            browser.sort.setCurrentIndex(i)
    browser.btn_bookmark._rebuild()
    saved = [a for a in browser.btn_bookmark._menu.actions()
             if a.text() == "high_heels"][0]
    saved.trigger()
    assert browser.search.text() == "high_heels"
    assert browser._sort_key() == "rated"        # ordering restored

    # A non-default ordering is written into the label, so the list
    # does not show two identical-looking entries.
    for i in range(browser.sort.count()):
        if browser.sort.itemData(i) == "oldest":
            browser.sort.setCurrentIndex(i)
    browser.search.setText("boots")
    value, label, sort = browser._current_bookmark()
    assert sort == "oldest" and "Oldest first" in label

    # --- post: reopens the post ---
    browser.open_inspector(dapi.parse_posts([posts[0]])[0])
    pop = browser._inspectors[-1]
    value, label = pop._current_bookmark()
    assert value == "1"
    assert "post #1" in label and "high_heels" in label   # not a bare id
    pop.btn_bookmark._toggle()
    pop.close()

    browser.open_inspector(dapi.parse_posts([posts[1]])[0])
    pop2 = browser._inspectors[-1]
    pop2.btn_bookmark._rebuild()
    item = [a for a in pop2.btn_bookmark._menu.actions()
            if a.text().startswith("post #1 ")][0]
    item.trigger()
    assert dapi.post_url(1) in fetcher.calls

    # One file, one store, all three kinds.
    assert ref._bookmarks is browser._bookmarks is pop2._bookmarks
    data = json.loads(Path(s.bookmarks_path).read_text(encoding="utf-8"))
    assert sorted({b["kind"] for b in data["bookmarks"]}) == [
        "post", "query", "tag"]
    print("OK: tags reopen their wiki, searches re-run with their "
          "ordering, posts reopen by id, and all three share one file")


def bmk_star_on() -> str:
    from ui.bookmark_button import STAR_ON
    return STAR_ON


def bmk_star_off() -> str:
    from ui.bookmark_button import STAR_OFF
    return STAR_OFF


def test_sort_default_and_ordering() -> None:
    """FIELD REPORT: most single-tag searches failed with a 500 under
    Best score, because sorting every matching post times out on any
    common tag. The ordering that reliably works now leads the list,
    and Best score sits last with the caveat in its own label."""
    assert dapi.DEFAULT_SORT == "rated"
    assert dapi.SORT_LABELS[0][0] == "rated"
    assert dapi.SORT_LABELS[-1][0] == "score"
    assert "times out" in dapi.SORT_LABELS[-1][1]

    s = _fresh_settings()
    ref = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    ref.show()
    browser = ref._open_browser()
    assert browser._sort_key() == "rated"
    assert browser.sort.itemData(0) == "rated"
    assert browser.sort.itemData(browser.sort.count() - 1) == "score"
    # The term-count help folded into the booklet: one explanation,
    # one place.
    assert not hasattr(browser, "notice_help")
    assert hasattr(browser, "btn_help")
    print("OK: the browser defaults to the ordering that works, Best "
          "score is last and labelled, and the term-count tooltip has "
          "merged into the help booklet")


def test_long_lists_stay_usable() -> None:
    """A menu grows with its contents: sixty saved tags already reach
    the bottom of a 1080p screen, and past that Qt falls back to
    scroll arrows. So the dropdown keeps the recent handful and the
    rest lives in a real list.

    That window also closes a gap in the original design — the star
    only ever toggles what is on screen, so removing a bookmark meant
    navigating to it first, which is impossible if the tag no longer
    exists."""
    from PySide6.QtWidgets import QMessageBox
    from ui.bookmark_button import MENU_LIMIT

    QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes
    s = _fresh_settings()
    ref = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    ref.show()
    for i in range(60):
        ref._bookmarks.add("tag", f"tag_number_{i:03d}")
    assert len(ref._bookmarks.all("tag")) == 60

    ref.btn_bookmark._rebuild()
    menu = ref.btn_bookmark._menu
    labels = [a.text() for a in menu.actions() if a.text()]
    assert len(labels) <= MENU_LIMIT + 4
    assert menu.sizeHint().height() < 400        # fits any screen
    assert any("most recent" in t for t in labels)
    assert any("(60)" in t for t in labels)      # route to the rest
    assert "tag_number_059" in labels            # newest first

    [a for a in menu.actions()
     if "(60)" in (a.text() or "")][0].trigger()
    manager = ref.btn_bookmark._manager
    assert manager.list.count() == 60
    assert "60 saved" in manager.count_label.text()

    manager.filter.setText("_01")
    assert 0 < manager.list.count() < 60
    assert "of 60 shown" in manager.count_label.text()
    manager.filter.setText("")
    assert manager.list.count() == 60

    # Remove something that is NOT on screen.
    manager.list.setCurrentRow(5)
    victim = manager._selected().value
    ref.navigate("something_else")
    manager._remove()
    assert not ref._bookmarks.contains("tag", victim)
    assert manager.list.count() == 59

    manager.list.setCurrentRow(0)
    target = manager._selected().value
    manager._open()
    assert ref.search.text() == target
    assert not manager.isVisible()

    # And at the store's own ceiling nothing degrades.
    for i in range(600):
        ref._bookmarks.add("tag", f"bulk_{i:04d}")
    assert len(ref._bookmarks.all("tag")) <= bmk.MAX_PER_KIND
    ref.btn_bookmark._rebuild()
    assert len([a for a in ref.btn_bookmark._menu.actions()
                if a.text()]) <= MENU_LIMIT + 4
    print("OK: the dropdown stays a glance however many are saved, "
          "and the manager filters, opens and removes entries that "
          "are not currently on screen")


def test_menu_headings_are_not_mistakable_for_entries() -> None:
    """FIELD REPORT: "Saved tags" looked like one more bookmark you
    could click.

    A disabled QAction is greyed but STILL highlights on hover. A
    separator carrying a title is a real section heading: Qt draws it
    differently and never highlights it."""
    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()

    win.btn_bookmark._rebuild()
    empty = [a for a in win.btn_bookmark._menu.actions()
             if a.text().startswith("No ")]
    assert empty and all(a.isSeparator() for a in empty)

    win.navigate("long_hair")
    win.btn_bookmark._toggle()
    win.navigate("smile")
    win.btn_bookmark._toggle()
    win.btn_bookmark._rebuild()
    actions = win.btn_bookmark._menu.actions()

    headings = [a for a in actions if a.isSeparator() and a.text()]
    assert any(a.text() == "Saved tags" for a in headings)
    # The bookmarks themselves stay ordinary, clickable entries.
    entries = [a for a in actions
               if a.text() in ("smile", "long_hair")]
    assert len(entries) == 2
    assert all(not a.isSeparator() and a.isEnabled() for a in entries)
    print("OK: section headings are separators and cannot be "
          "mistaken for bookmarks")


def run() -> None:
    test_store_round_trip()
    test_file_is_readable_and_survives_damage()
    test_the_three_surfaces()
    test_sort_default_and_ordering()
    test_long_lists_stay_usable()
    test_menu_headings_are_not_mistakable_for_entries()
    print("\nALL PASS: bookmarks")


if __name__ == "__main__":
    run()
