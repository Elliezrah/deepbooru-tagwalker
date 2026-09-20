"""
tests/test_post_browser.py

Tests for the post browser (ui/post_browser_window.py) and the search
query builder it rides on.

The browser answers a different question from the Tag Referencer —
how a tag is USED across the site, rather than what it means — so the
things pinned here are mostly about honesty: results are labelled
uncurated, the two-term anonymous limit is shown before a query is
sent rather than discovered through an empty result, and the
blacklist applies from the outset (uncurated results are far likelier
to surface blocked content than staff picks).

Run: python3 tests/test_post_browser.py
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from PIL import Image
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
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


def _posts(ids, tag="high_heels"):
    return [{"id": i, "preview_file_url": f"https://x/p{i}",
             "large_file_url": f"https://x/l{i}", "rating": "g",
             "tag_string": f"{tag} 1girl", "tag_count": 2,
             "tag_string_general": f"{tag} 1girl"} for i in ids]


def _fresh_settings() -> Settings:
    os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()
    s = Settings()
    initialize_theme(s.theme)
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    return s


def _routes():
    page1, page2 = list(range(1, 21)), [21, 22]
    routes = {
        dapi.posts_search_url("high_heels", "rated", 1, 20):
            (json.dumps(_posts(page1)).encode(), 200),
        dapi.posts_search_url("high_heels", "rated", 2, 20):
            (json.dumps(_posts(page2)).encode(), 200),
    }
    for i in page1 + page2:
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)
    return routes


def test_query_builder_and_term_cost() -> None:
    """A sort metatag SPENDS one of the two terms an anonymous search
    gets — except "newest", which is Danbooru's default ordering and
    therefore free."""
    assert dapi.build_post_query("high_heels", "score") == (
        "high_heels order:score", 2)
    assert dapi.build_post_query("high_heels 1girl", "newest") == (
        "high_heels 1girl", 2)
    assert dapi.build_post_query("high_heels 1girl", "score")[1] == 3
    assert dapi.ANON_TERM_LIMIT == 2
    url = dapi.posts_search_url("high_heels", "score", 2, 20)
    assert "page=2" in url and "limit=20" in url
    assert "order%3Ascore" in url
    # The default ordering filters instead of sorting.
    assert "score%3A%3E50" in dapi.posts_search_url(
        "high_heels", "rated", 1, 20)
    print("OK: query builder counts terms correctly, and 'newest' "
          "costs nothing because it is the default ordering")


def test_browser_opens_from_the_referencer() -> None:
    s = _fresh_settings()
    f = FakeFetcher(_routes())
    ref = TagReferenceWindow(s, fetcher=f)
    ref.show()
    ref.search.setText("high_heels")
    br = ref._open_browser()
    assert br.isVisible()
    # Its own window, titled with whatever it is showing.
    assert br.windowTitle().startswith("Browse posts")
    assert "high_heels" in br.windowTitle()
    assert br.search.text() == "high_heels"      # prefilled
    # Defaults to the ordering that reliably works: sorting by score
    # times out on any common tag.
    assert br._sort_key() == dapi.DEFAULT_SORT == "rated"
    assert len(br._thumbs) == 20
    # The distinction from curated examples stays visible.
    assert "ncurated" in br.notice.text()
    print("OK: the browse button opens a separate prefilled window, "
          "defaulting to best score and labelled uncurated")


def test_paging() -> None:
    s = _fresh_settings()
    br = TagReferenceWindow(s, fetcher=FakeFetcher(_routes()))
    br.show()
    br.search.setText("high_heels")
    win = br._open_browser()
    assert not win.btn_prev.isEnabled()          # first page
    assert win.btn_next.isEnabled()
    win._step(1)
    assert win._page == 2 and len(win._thumbs) == 2
    # A short page is the only evidence there is no next one.
    assert not win.btn_next.isEnabled()
    assert win.btn_prev.isEnabled()
    win._step(-1)
    assert win._page == 1 and len(win._thumbs) == 20
    print("OK: explicit paging, with the next button disabled once a "
          "short page proves there is nothing after it")


def test_term_limit_is_shown_before_sending() -> None:
    s = _fresh_settings()
    ref = TagReferenceWindow(s, fetcher=FakeFetcher(_routes()))
    ref.show()
    win = ref._open_browser()
    win.search.setText("high_heels 1girl")
    win._update_notice()
    assert "3 terms" in win.notice.text()
    assert "over the limit" in win.notice.text()
    # The full explanation lives in the help booklet now: one
    # explanation, one place, rather than a second long tooltip
    # sitting beside the term count.
    from ui.post_browser_window import HELP_PAGES
    assert not hasattr(win, "notice_help")
    assert any("Newest first" in body for _t, body in HELP_PAGES)
    newest = [i for i in range(win.sort.count())
              if win.sort.itemData(i) == "newest"][0]
    win.sort.setCurrentIndex(newest)
    assert "3 terms" not in win.notice.text()
    assert len(win.notice.text()) < 90            # stays concise
    print("OK: an over-limit query is flagged before it is sent, "
          "concisely, with the reasoning available on hover")


def test_blacklist_applies_to_browse_results() -> None:
    """Uncurated results are far likelier to surface blocked content
    than staff picks, so this is the path that matters most."""
    s = _fresh_settings()
    routes = _routes()
    gore = _posts([50])
    gore[0]["tag_string"] = "guro blood"
    routes[dapi.posts_search_url("guro", "rated", 1, 20)] = (
        json.dumps(gore).encode(), 200)
    routes["https://x/p50"] = (_png(), 200)
    f = FakeFetcher(routes)
    ref = TagReferenceWindow(s, fetcher=f)
    ref.show()
    win = ref._open_browser()
    # A blacklisted tag is refused before any request: every result
    # would be filtered out anyway.
    win.browse("guro")
    assert "blacklist" in win.status.text()
    assert not win._thumbs
    assert not any("guro" in c for c in f.calls)

    # And a blocked post inside ordinary results is dropped, not
    # drawn, and its image is never requested.
    mixed = _posts([1]) + [dict(_posts([50])[0],
                                tag_string="guro blood")]
    f.routes[dapi.posts_search_url("high_heels", "rated", 1, 20)] = (
        json.dumps(mixed).encode(), 200)
    win.browse("high_heels")
    assert sorted(win._thumbs) == [1]
    assert "1 hidden" in win.status.text()
    assert "https://x/p50" not in f.calls
    print("OK: blacklisted search terms are refused before any "
          "request, and blocked results inside ordinary searches are "
          "dropped without ever being fetched")


def test_inspector_bridges_back_to_the_referencer() -> None:
    s = _fresh_settings()
    f = FakeFetcher(_routes())
    ref = TagReferenceWindow(s, fetcher=f)
    ref.show()
    ref.search.setText("high_heels")
    win = ref._open_browser()
    win.open_inspector(dapi.parse_posts(_posts([1]))[0])
    pop = win._inspectors[-1]
    pop._on_tag_clicked("tag:1girl")
    # Reading belongs to the referencer; browsing stays put.
    assert ref.search.text() == "1girl"
    assert win.search.text() == "high_heels"
    print("OK: clicking a tag inside a browsed example sends the Tag "
          "Referencer there, leaving the browse results alone")


def test_failure_and_disabled_paths() -> None:
    s = _fresh_settings()
    routes = _routes()
    routes[dapi.posts_search_url("zzz", "rated", 1, 20)] = (None, 429)
    routes[dapi.posts_search_url("empty", "rated", 1, 20)] = (b"[]", 200)
    ref = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    ref.show()
    win = ref._open_browser()

    win.browse("zzz")
    assert "429" in win.status.text()
    win.browse("empty")
    assert "No posts found" in win.status.text()

    s.danbooru_lookups_enabled = False
    win.browse("high_heels")
    assert "disabled" in win.status.text()
    assert len(win._thumbs) == 0
    print("OK: rate limits, empty results and the master switch each "
          "explain themselves rather than showing a blank grid")


def test_blocked_and_deleted_results_are_dropped() -> None:
    """FIELD REPORT: blocked posts were rendering as placeholder tiles
    and deleted posts as empty boxes, both eating grid space the
    results needed. They are dropped outright now, with a count.

    Paging still keys off what Danbooru RETURNED, not what survived —
    otherwise a page of mostly-blocked results would look like the end
    of the search."""
    s = _fresh_settings()
    posts = _posts([1])
    posts += [dict(_posts([2])[0], tag_string="guro 1girl")]
    posts += [dict(_posts([3])[0], is_deleted=True)]
    posts += [dict(_posts([4])[0], preview_file_url="",
                   large_file_url="")]          # nothing to display
    posts += _posts([5])
    routes = {dapi.posts_search_url("high_heels", "rated", 1, 20):
              (json.dumps(posts).encode(), 200)}
    for i in range(1, 6):
        routes[f"https://x/p{i}"] = (_png(), 200)
    ref = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    ref.show()
    ref.search.setText("high_heels")
    win = ref._open_browser()
    assert sorted(win._thumbs) == [1, 5]        # only displayable ones
    assert "3 hidden" in win.status.text()
    # Video posts are NOT dropped: their still thumbnail displays, and
    # the inspector offers the file as a download.
    video = _posts([7])
    video[0].update(file_ext="mp4",
                    large_file_url="https://x/l7.mp4")
    win._fetcher.routes[
        dapi.posts_search_url("vid", "rated", 1, 20)] = (
            json.dumps(video).encode(), 200)
    win._fetcher.routes["https://x/p7"] = (_png(), 200)
    win.browse("vid")
    assert 7 in win._thumbs
    print("OK: blocked, deleted and undisplayable results are dropped "
          "rather than occupying the grid, and are counted")


def test_term_breakdown_and_expensive_sort_failure() -> None:
    """FIELD REPORT: `1girl` with Best score returned 500 while
    newest/oldest worked, and the notice said 2/2 terms for one typed
    tag.

    Both are now explained rather than mysterious: the breakdown shows
    the sort taking the second term, and a 5xx on an expensive sort
    says what actually went wrong. Score and random make the server
    sort or scan every matching post; on a tag the size of 1girl that
    times out. Newest and oldest ride the id index."""
    s = _fresh_settings()
    ref = TagReferenceWindow(s, fetcher=FakeFetcher(_routes()))
    ref.show()
    win = ref._open_browser()

    win.search.setText("1girl")
    for i in range(win.sort.count()):
        if win.sort.itemData(i) == "score":
            win.sort.setCurrentIndex(i)
    notice = win.notice.text()
    assert "1 tag" in notice
    assert "sort order" in notice and "order:score" in notice
    assert "2/2 terms" in notice
    for i in range(win.sort.count()):
        if win.sort.itemData(i) == "newest":
            win.sort.setCurrentIndex(i)
    assert "1/2 terms" in win.notice.text()     # newest is free

    for i in range(win.sort.count()):
        if win.sort.itemData(i) == "score":
            win.sort.setCurrentIndex(i)
    win._fetcher.routes[
        dapi.posts_search_url("1girl", "score", 1, 20)] = (None, 500)
    win.browse("1girl")
    status = win.status.text()
    assert "could not sort" in status
    assert "times out" in status
    assert "Highly rated" in status             # names the way out
    assert "term count" not in status           # no longer misblamed

    # The fast alternative filters on an indexed column instead.
    assert dapi.build_post_query("1girl", "rated") == (
        "1girl score:>50", 2)
    assert "score" in dapi.EXPENSIVE_SORTS
    assert "rated" not in dapi.EXPENSIVE_SORTS
    assert "newest" not in dapi.EXPENSIVE_SORTS
    print("OK: the term breakdown shows where each term went, and an "
          "expensive-sort timeout is explained with a working "
          "alternative")


def test_main_window_button_and_pin() -> None:
    """The browser earned its own control beside the Tag Referencer,
    rather than being reachable only through it.

    They stay one pair: the browser is still owned by the referencer
    so there is only ever one of it, and so a tag clicked inside a
    browsed post has somewhere to land. Opening it from the toolbar
    creates that window without putting it on screen."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolButton
    from ui.main_window import MainWindow

    s = _fresh_settings()
    mw = MainWindow(s)
    host = mw.menuBar().cornerWidget(Qt.Corner.TopRightCorner)
    browse, reference = host.findChildren(QToolButton)
    assert "Post Browser" in browse.text()      # left of the pair
    assert "Tag Referencer" in reference.text()
    assert not browse.isChecked()

    reference.click()
    mw._tag_ref_win.navigate("long_hair")
    browse.click()
    win = mw._tag_ref_win._browser
    assert win is not None and win.isVisible()
    # Inherits whatever tag the referencer is showing.
    assert win.search.text() == "long_hair"
    assert browse.isChecked()
    assert "long_hair" in win.windowTitle()

    # Pinned by default, and pinning must not cost the close button.
    assert win.btn_on_top.isChecked()
    assert s.post_browser_always_on_top is True
    # System-wide, deliberately: see the note on the equivalent test
    # in test_field_report_fixes.py. Qt.Tool was tried and reverted.
    assert win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert win.windowFlags() & Qt.WindowType.WindowCloseButtonHint
    win.btn_on_top.setChecked(False)
    assert s.post_browser_always_on_top is False
    win.btn_on_top.setChecked(True)

    browse.click()
    assert not win.isVisible() and not browse.isChecked()
    browse.click()
    assert mw._tag_ref_win._browser.isVisible()

    # Dismissed by its own title bar, the button must follow. The
    # notification fires once hidden, not during closeEvent, when Qt
    # still reports the window visible.
    mw._tag_ref_win._browser.close()
    assert not browse.isChecked()
    mw._tag_ref_win.close()
    assert not reference.isChecked()

    # The referencer's own browse control still works, on its tag.
    mw._tag_ref_win.show()
    mw._tag_ref_win.navigate("smile")
    same = mw._tag_ref_win._open_browser()
    assert same is mw._tag_ref_win._browser      # never two of them
    assert same.search.text() == "smile"

    # And the browser opens standalone without forcing the referencer
    # on screen.
    mw2 = MainWindow(_fresh_settings())
    host2 = mw2.menuBar().cornerWidget(Qt.Corner.TopRightCorner)
    host2.findChildren(QToolButton)[0].click()
    assert mw2._tag_ref_win._browser.isVisible()
    assert not mw2._tag_ref_win.isVisible()
    print("OK: the post browser has its own toolbar button beside the "
          "Tag Referencer, shares that window's tag, pins to front, "
          "and both buttons track windows closed by their title bars")


def test_windows_close_independently() -> None:
    """Each surface has its own toolbar button and its own reason to
    be open, so dismissing one must not take the other away. Full
    reset still closes both, because that means "forget everything"."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolButton
    from ui.main_window import MainWindow

    mw = MainWindow(_fresh_settings())
    host = mw.menuBar().cornerWidget(Qt.Corner.TopRightCorner)
    browse, reference = host.findChildren(QToolButton)
    reference.click()
    mw._tag_ref_win.navigate("long_hair")
    browse.click()
    ref, win = mw._tag_ref_win, mw._tag_ref_win._browser
    assert ref.isVisible() and win.isVisible()

    ref.close()
    assert not ref.isVisible()
    assert win.isVisible()                  # survives independently
    assert browse.isChecked()

    win.close()
    ref.show()
    assert ref.isVisible() and not win.isVisible()
    print("OK: the Tag Referencer and the post browser open and close "
          "independently of one another")


def test_browser_completion_folder_button_and_help() -> None:
    from PySide6.QtWidgets import QToolButton
    from ui.post_browser_window import HELP_PAGES

    s = _fresh_settings()
    ref = TagReferenceWindow(s, fetcher=FakeFetcher(_routes()))
    ref.show()
    win = ref._open_browser()

    # Typing a tag from memory is the slow part of using this window.
    assert getattr(win, "_ac", None) is not None

    # The export folder button belongs where exporting happens.
    assert not hasattr(ref, "btn_export_dir")
    assert isinstance(win.btn_export_dir, QToolButton)
    win._open_export_dir()
    assert "No export folder set" in win.status.text()

    # The hand-off control reads as forwarding the tag onward.
    assert ref.btn_browse.text() == "\u27a1"
    assert "Send this tag" in ref.btn_browse.toolTip()

    win._show_help()
    assert win._help.__class__.__name__ == "PagedHelpDialog"
    pages = "\n".join(body for _title, body in HELP_PAGES)
    # Every rating value, spelled out, with the exclusion form.
    for value in ("rating:general", "rating:sensitive",
                  "rating:questionable", "rating:explicit",
                  "rating:g", "-rating:explicit"):
        assert value in pages, value
    # The mistake that silently does nothing.
    assert "NO SPACE" in pages and "rating: general" in pages
    # The term budget, worked through rather than asserted.
    assert "2 terms" in pages and "3 terms" in pages
    assert "order:score" in pages and "score:>50" in pages
    assert "blacklist still applies" in pages
    win._help.close()
    print("OK: the browser completes tags, owns the export-folder "
          "button, and its help documents every rating filter "
          "including the space-after-colon trap")


def test_search_history() -> None:
    """FIELD REPORT: the browser had no way back to a previous search,
    so refining a query meant retyping the one before it.

    A search is the pair (query, sort): returning to one that silently
    reordered itself would not be returning to it."""
    s = _fresh_settings()
    ref = TagReferenceWindow(s, fetcher=FakeFetcher(_routes()))
    ref.show()
    win = ref._open_browser()
    payload = json.dumps([{
        "id": 1, "preview_file_url": "https://x/p",
        "large_file_url": "https://x/l", "rating": "g",
        "tag_string": "a", "tag_count": 1}]).encode()
    routes = {"https://x/p": (_png(), 200), "https://x/l": (_png(), 200)}
    for tag in ("1girl", "solo", "smile"):
        for sort in ("rated", "newest"):
            routes[dapi.posts_search_url(tag, sort, 1, 20)] = (payload, 200)
    win._fetcher = FakeFetcher(routes)

    assert not win.btn_back.isEnabled()
    assert not win.btn_fwd.isEnabled()
    win.browse("1girl")
    win.browse("solo")
    win.browse("smile")
    assert len(win._hist) == 3
    assert win.btn_back.isEnabled() and not win.btn_fwd.isEnabled()

    win.btn_back.click()
    assert win.search.text() == "solo"
    assert win.btn_fwd.isEnabled()
    win.btn_back.click()
    assert win.search.text() == "1girl"
    assert not win.btn_back.isEnabled()      # at the start
    win.btn_fwd.click()
    assert win.search.text() == "solo"

    # Replaying history must not itself be recorded, or going back
    # would append forever.
    size = len(win._hist)
    win.btn_back.click()
    win.btn_fwd.click()
    assert len(win._hist) == size

    # The sort travels with the entry.
    for i in range(win.sort.count()):
        if win.sort.itemData(i) == "newest":
            win.sort.setCurrentIndex(i)
    win.browse("1girl")
    assert not win.btn_fwd.isEnabled()       # forward tail discarded
    assert win._hist[-1] == ("1girl", "newest")
    win.btn_back.click()
    assert win._sort_key() == win._hist[win._hpos][1]
    print("OK: the browser remembers its searches with their sort "
          "order, and replaying one does not grow the history")


def test_unsupported_formats_are_named() -> None:
    """"A format this app cannot display" reads like a fault in the
    program. Naming them makes it a property of the file."""
    from ui.media_view import UNSUPPORTED_NOTICE

    for fmt in ("AVIF", "JPEG XL", "PSD", "Flash"):
        assert fmt in UNSUPPORTED_NOTICE, fmt
    # And what DOES work, so the message is actionable.
    for fmt in ("JPEG", "PNG", "GIF", "WebP"):
        assert fmt in UNSUPPORTED_NOTICE, fmt
    print("OK: the unsupported-format notice names the formats that "
          "fail and the ones that work")


def run() -> None:
    test_query_builder_and_term_cost()
    test_browser_opens_from_the_referencer()
    test_paging()
    test_term_limit_is_shown_before_sending()
    test_blacklist_applies_to_browse_results()
    test_inspector_bridges_back_to_the_referencer()
    test_failure_and_disabled_paths()
    test_blocked_and_deleted_results_are_dropped()
    test_term_breakdown_and_expensive_sort_failure()
    test_main_window_button_and_pin()
    test_windows_close_independently()
    test_browser_completion_folder_button_and_help()
    test_search_history()
    test_unsupported_formats_are_named()
    print("\nALL PASS: post browser")


if __name__ == "__main__":
    run()
