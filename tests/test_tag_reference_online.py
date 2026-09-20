"""
tests/test_tag_reference_online.py

Tests for the Tag Reference's ONLINE half (DESIGN_TAG_REFERENCE.md):
the opt-in preferences, the wiki rendering pipeline, curated example
galleries (hidden-until-hover, reveal-all), the batch/per-id fetch
orchestration, both caches, the custom-tag similarity path, and the
tag inspector's diff + one-click adds through the normal undo path.

All network traffic is a canned FakeFetcher (spec's testing
approach). Also contains the regression test for a field-class
defect found while building this: Settings() ignored
TAGWALKER_CONFIG_DIR, so the ENTIRE suite had been writing into the
real per-user preference store — on a user's machine, running tests
would have overwritten their actual TagWalker prefs.

Run: python3 tests/test_tag_reference_online.py
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PIL import Image
import re

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
)

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import danbooru_api as dapi
from core.scanner import scan
from core.state import SessionState
from tests.test_dtext import REALISTIC_BODY
from ui.tag_reference_window import TagReferenceWindow

IDS = [100, 101, 102, 200, 201, 202]


class FakeFetcher:
    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls: list[str] = []

    def get(self, url, cb):
        self.calls.append(url)
        r = self.routes.get(url)
        if r is None:
            cb(None, 404)
        else:
            cb(r[0], r[1])


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return buf.getvalue()


def _fixture_routes():
    wiki = json.dumps({
        "title": "high_heels", "body": REALISTIC_BODY,
        "other_names": ["\u30cf\u30a4\u30d2\u30fc\u30eb"],
        "updated_at": "2026-05-01T00:00:00"}).encode()
    posts = []
    for pid in IDS:
        d = {"id": pid, "preview_file_url": f"https://x/p{pid}",
             "large_file_url": f"https://x/l{pid}", "rating": "g",
             "tag_string": "high_heels shoes 1girl", "tag_count": 3}
        if pid == 100:
            d.update(is_banned=True, preview_file_url="",
                     large_file_url="")
        posts.append(d)
    routes = {dapi.wiki_url("high_heels"): (wiki, 200),
              dapi.posts_by_ids_url(IDS):
                  (json.dumps(posts).encode(), 200)}
    for pid in IDS:
        routes[f"https://x/p{pid}"] = (_png(), 200)
        routes[f"https://x/l{pid}"] = (_png(), 200)
    return routes, posts


def _fresh_settings() -> Settings:
    os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()
    s = Settings()
    initialize_theme(s.theme)
    return s


def _doc_texts(win) -> list[str]:
    return [l.text() for l in win.doc_host.findChildren(QLabel)]


def test_settings_env_isolation_regression() -> None:
    """Field-class defect: Settings() ignored TAGWALKER_CONFIG_DIR —
    the whole suite silently shared (and WROTE) the real per-user
    store. Now the env var routes to an isolated ini beside which the
    caches also live."""
    env = tempfile.mkdtemp()
    os.environ["TAGWALKER_CONFIG_DIR"] = env
    s = Settings()
    assert env in s._qs.fileName(), s._qs.fileName()
    assert s.danbooru_lookups_enabled is False        # true defaults
    assert env in str(s.cache_dir("danbooru_text"))
    print("OK: TAGWALKER_CONFIG_DIR now truly isolates the settings "
          "store and caches (test runs can no longer touch real "
          "user prefs)")


def test_preferences_group_and_cache_controls() -> None:
    s = _fresh_settings()
    dapi.TextCache(s.cache_dir("danbooru_text")).put("t", {"a": 1})
    dapi.ImageCache(s.cache_dir("danbooru_images")).put("1", b"x" * 2048)
    from ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(s)
    assert dlg._dan_images.isEnabled() is False       # gated by master
    assert dlg._dan_text_size.text() != "empty"
    dlg._clear_dan_images()
    assert dlg._dan_img_size.text() == "empty"
    assert dlg._dan_text_size.text() != "empty"       # independent
    dlg._dan_enabled.setChecked(True)
    assert dlg._dan_images.isEnabled()
    dlg._dan_images.setChecked(True)
    dlg._on_ok()
    assert s.danbooru_lookups_enabled and s.danbooru_show_images
    print("OK: Danbooru preferences group — master gates images, "
          "cache clears are independent, OK persists both toggles")


def test_master_off_means_zero_network() -> None:
    s = _fresh_settings()
    routes, _ = _fixture_routes()
    f = FakeFetcher(routes)
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("high_heels")
    texts = _doc_texts(win)
    assert f.calls == []
    assert any("disabled" in t and "Settings" in t for t in texts)
    print("OK: with the master switch off, not one request is made "
          "and the notice points at Settings")


def test_doc_render_and_text_cache() -> None:
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    routes, _ = _fixture_routes()
    f = FakeFetcher(routes)
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("high_heels")
    texts = _doc_texts(win)
    assert f.calls == [dapi.wiki_url("high_heels")]   # no post calls
    assert any("Other names" in t for t in texts)
    assert any("Examples" in t for t in texts)
    assert sum("Thumbnails are off" in t for t in texts) == 2
    assert any("3 curated example" in t for t in texts)
    assert any('href="post:100"' in t for t in texts)   # chips always
    # External links are kept now, as copy-to-clipboard links.
    assert any("External links" in t for t in texts)
    assert any('href="copy:' in t for t in texts)
    assert not any('href="http' in t for t in texts)   # no browser
    win.navigate("high_heels")
    assert f.calls.count(dapi.wiki_url("high_heels")) == 1
    print("OK: images-off render — wiki fetched once, document in "
          "order, galleries announce their contents, external links "
          "render as copy links, revisits served from cache")


def test_images_hover_reveal_and_caches() -> None:
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    routes, _ = _fixture_routes()
    f = FakeFetcher(routes)
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("high_heels")
    assert dapi.posts_by_ids_url(IDS) in f.calls      # single batch
    g1, g2 = win._galleries
    assert len(g1.slots) == 3 and len(g2.slots) == 3  # editors' groups
    assert "banned" in g1.slots[100].label.text()
    t = g1.slots[101]
    assert "hover" in t.label.text()                  # covered
    t._hover = True
    t._reveal()
    assert t.label.pixmap() and not t.label.pixmap().isNull()
    win._set_reveal_all(True)
    t2 = g2.slots[202]
    assert t2.label.pixmap() and not t2.label.pixmap().isNull()
    assert win._fetch_note.text() == "examples fetched via batch"
    before = sum(1 for c in f.calls if c.startswith("https://x/p"))
    win.navigate("high_heels")
    win._set_reveal_all(True)
    after = sum(1 for c in f.calls if c.startswith("https://x/p"))
    assert after == before                            # image cache
    print("OK: batch fetch, hidden-until-hover, reveal-all, banned "
          "placeholder, and the image cache serving re-reveals")


def test_batch_rejected_falls_back_per_id() -> None:
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    routes, posts = _fixture_routes()
    routes[dapi.posts_by_ids_url(IDS)] = (None, 500)
    for pid in IDS:
        routes[dapi.post_url(pid)] = (
            json.dumps([p for p in posts
                        if p["id"] == pid]).encode(), 200)
    f = FakeFetcher(routes)
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("high_heels")
    assert all(dapi.post_url(pid) in f.calls for pid in IDS)
    # The note now also carries the batch status code, so that a
    # field report says WHY the fallback happened.
    assert "per-id fallback" in win._fetch_note.text()
    assert "500" in win._fetch_note.text()
    assert "hover" in win._galleries[1].slots[201].label.text()
    print("OK: a rejected batch falls back to per-id fetches, fills "
          "the galleries anyway, and the footer names the path (the "
          "open API question resolves itself on first real run)")


def test_custom_tag_similarity_path() -> None:
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    sim = json.dumps([{"name": "red_curtain",
                       "post_count": 10}]).encode()
    f = FakeFetcher({dapi.tag_search_url("red_curtian"): (sim, 200)})
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("red_curtian")
    texts = _doc_texts(win)
    assert any("custom tag" in t for t in texts)
    assert any("red_curtain" in t and "tag:" in t for t in texts)
    win._on_doc_link("tag:red_curtain")
    assert win.search.text() == "red_curtain"
    print("OK: 404 wiki takes the similarity path; the suggestion "
          "chip navigates in-window")


def test_inspector_is_live_and_read_only() -> None:
    """FIELD BUG: the popup's one-click "add" wrote to whichever image
    was current when it OPENED, so after switching images it edited
    the wrong file. The add control is gone (adding belongs in the
    main window, where the target is unambiguous), tags are clickable
    lookups instead, and the comparison is recomputed live from state
    events rather than captured once."""
    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for name, tags in {"img1": "high_heels",
                       "img2": "high_heels, shoes, 1girl"}.items():
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(tags, encoding="utf-8")
    st = SessionState(scan(d))
    st.select_tag("high_heels")
    win.set_state(st)
    post = dapi.parse_posts([{
        "id": 100, "preview_file_url": "", "large_file_url": "",
        "rating": "g", "tag_string": "high_heels shoes 1girl",
        "tag_count": 3}])[0]
    win.open_inspector(post)
    pop = win._inspectors[-1]

    assert not hasattr(pop, "_add_buttons")     # gone for good
    labels = [b.text() for b in pop.findChildren(QPushButton)]
    assert "Export image + tags" in labels
    assert pop.btn_copy.text() == "Copy tags to clipboard"
    assert pop.width() >= 600 and pop.height() >= 740   # roomier

    first = st.current_image.image_path.name
    before = pop.diff_caption.text()
    assert first in before
    n_before = int(re.search(r"<b>(\d+)</b>", before).group(1))

    # The user's exact scenario: click another image in the queue.
    st.jump_to_queue_index(1)
    second = st.current_image.image_path.name
    after = pop.diff_caption.text()
    assert second != first
    assert second in after                      # follows the walk
    n_after = int(re.search(r"<b>(\d+)</b>", after).group(1))
    assert (n_before, n_after) == (2, 0)        # recomputed per image

    # Edits to the current caption refresh it too.
    st.jump_to_queue_index(0)
    st.add_tag_to_image(st.current_image.image_path, "shoes")
    assert int(re.search(r"<b>(\d+)</b>",
                         pop.diff_caption.text()).group(1)) == 1

    # Tags are clickable lookups that drive the reference window.
    assert 'href="tag:shoes"' in pop.tag_label.text()
    assert "#7f7f7f" in pop.tag_label.text()    # shared tags dimmed
    pop._on_tag_clicked("tag:shoes")
    assert win.search.text() == "shoes"

    pop.close()
    st.jump_to_queue_index(1)                   # listener detached
    print("OK: inspector is read-only, its comparison follows the "
          "walk live, and every tag is a one-click lookup")


def test_window_affordances_and_cooccurrence_display() -> None:
    """Field feedback: the redundant Look-up button is gone, the
    follow checkbox explains itself, era counts say what they count,
    always-on-top exists and persists, and the co-occurrence list
    shows a real percentage plus dataset counts under an honest
    heading (the data is Danbooru's, not the user's)."""
    from PySide6.QtCore import Qt
    from core.scanner import scan

    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for name, tags in {"a": "long_hair, smile", "b": "long_hair",
                       "c": "smile"}.items():
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(tags, encoding="utf-8")
    win.set_state(SessionState(scan(d)))

    assert not hasattr(win, "btn_go")            # redundant, removed
    assert win.chk_follow.text() == "Follow the tag"
    assert len(win.chk_follow.toolTip()) > 60    # detail on hover
    assert win.btn_on_top.isCheckable()          # icon, not a label
    assert len(win.btn_on_top.text()) <= 2
    assert "Always on top" in win.btn_on_top.toolTip()
    # Era counts moved into a foldable section, folded by default.
    assert "post count" in win.era_toggle.text().lower()
    assert not win.era_label.isVisible()

    win.navigate("long_hair")
    cap = win.cooc_caption.text()
    assert "Danbooru" in cap and "your data" not in cap
    assert len(cap) < 70                         # concise in the UI
    # The explanation moved into a booklet; the tooltip is a pointer.
    from ui.tag_reference_window import COOC_HELP_PAGES
    assert len(win.cooc_help.toolTip()) < 90
    assert any("not your dataset" in body
               for _title, body in COOC_HELP_PAGES)
    rows = [win.cooc_list.item(i).text()
            for i in range(win.cooc_list.count())]
    assert rows and all("%" in r for r in rows)
    assert all("in your set" in r for r in rows)
    smile = [r for r in rows if r.startswith("smile")]
    assert smile and "2 in your set" in smile[0]   # real dataset count

    hint = Qt.WindowType.WindowStaysOnTopHint

    def _is_tool(w) -> bool:
        """Named for history: the pin is the system-wide hint again
        after Qt.Tool was tried and reverted."""
        return bool(w.windowFlags() & hint)

    assert win.btn_on_top.isChecked()              # pinned by default
    win.btn_on_top.setChecked(True)
    assert _is_tool(win)
    assert s.tag_reference_always_on_top is True
    win.btn_on_top.setChecked(False)
    assert not _is_tool(win)
    assert s.tag_reference_always_on_top is False
    s.tag_reference_always_on_top = True
    win2 = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win2.show()
    assert win2.btn_on_top.isChecked()
    assert _is_tool(win2)                          # restored on open
    print("OK: window affordances — no redundant button, "
          "self-explanatory follow label, labelled era counts, "
          "percentage + dataset counts under an honest heading, "
          "always-on-top that persists")


def test_field_reported_embed_spellings_reach_the_gallery() -> None:
    """End-to-end version of the missing-images bug: a wiki body with
    mixed embed spellings must produce fetched thumbnails, and a pool
    search must not leak raw braces."""
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    body = ("Prose. Collection: {{pool:Juicy_Details}}\n\n"
            "h4. Examples\n\n!Post #100 !post#101\n! post # 102\n")
    ids = [100, 101, 102]
    wiki = json.dumps({"title": "close-up", "body": body,
                       "other_names": [],
                       "updated_at": "2026-05-01"}).encode()
    posts = [{"id": i, "preview_file_url": f"https://x/p{i}",
              "large_file_url": f"https://x/l{i}", "rating": "g",
              "tag_string": "close-up 1girl", "tag_count": 2}
             for i in ids]
    routes = {dapi.wiki_url("close-up"): (wiki, 200),
              dapi.posts_by_ids_url(ids):
                  (json.dumps(posts).encode(), 200)}
    for i in ids:
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)
    f = FakeFetcher(routes)
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("close-up")
    assert dapi.posts_by_ids_url(ids) in f.calls   # examples fetched
    assert len(win._galleries) == 1
    assert len(win._galleries[0].slots) == 3
    texts = _doc_texts(win)
    assert not any("!post" in t.lower() for t in texts)
    assert any("Juicy Details (pool)" in t for t in texts)
    assert not any("{{" in t for t in texts)
    print("OK: mixed-spelling embeds reach the gallery and are "
          "fetched; pool searches render readably")


def test_examples_reachable_when_thumbnail_fetch_fails() -> None:
    """FIELD BUG (images never appeared): example thumbnails depended
    entirely on the post fetch succeeding, so any failure left a
    silent blank. Now the clickable chip row is rendered from the
    parsed ids alone — no network — so an example is always reachable,
    and failures state their status codes instead of saying nothing."""
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    ids = [100, 101]
    body = "h4. Examples\n\n!post #100 !post #101\n"
    wiki = json.dumps({"title": "t", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    post100 = {"id": 100, "preview_file_url": "https://x/p100",
               "large_file_url": "https://x/l100", "rating": "g",
               "tag_string": "a b c", "tag_count": 3}
    # EVERY post request fails; only the image URL would work.
    routes = {dapi.wiki_url("t"): (wiki, 200),
              "https://x/l100": (_png(), 200)}
    f = FakeFetcher(routes)
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("t")
    g = win._galleries[0]
    assert 'href="post:100"' in g.chip_label.text()
    assert 'href="post:101"' in g.chip_label.text()
    assert "batch returned 404" in g.status.text()
    assert "per-id returned 404" in g.status.text()
    assert "Click any example" in g.status.text()

    # The chip still opens the example, image and all.
    f.routes[dapi.post_url(100)] = (json.dumps(post100).encode(), 200)
    win._on_doc_link("post:100")
    pop = win._inspectors[-1]
    assert "3 tags" in pop.findChildren(QLabel)[0].text()
    # The picture now lives on a zoomable surface shared with the
    # main image viewer, fitted to the box rather than a fixed scale.
    assert pop.media._item is not None
    assert not pop.media._item.pixmap().isNull()
    print("OK: examples stay reachable as clickable chips even when "
          "every thumbnail fetch fails, and the failure names its "
          "status codes")


def test_partial_results_and_images_off_paths() -> None:
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    ids = [100, 101]
    body = "h4. Examples\n\n!post #100 !post #101\n"
    wiki = json.dumps({"title": "t", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    posts = [{"id": i, "preview_file_url": f"https://x/p{i}",
              "large_file_url": f"https://x/l{i}", "rating": "g",
              "tag_string": "a b c", "tag_count": 3} for i in ids]
    routes = {dapi.wiki_url("t"): (wiki, 200),
              dapi.posts_by_ids_url(ids):
                  (json.dumps([posts[0]]).encode(), 200)}
    for i in ids:
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)
    win = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win.show()
    win.navigate("t")
    assert "1 example(s) not returned" in win._galleries[0].status.text()

    # Images off: no post traffic at all, chips still the escape hatch.
    s.danbooru_show_images = False
    routes[dapi.posts_by_ids_url(ids)] = (json.dumps(posts).encode(), 200)
    routes[dapi.post_url(100)] = (json.dumps(posts[0]).encode(), 200)
    f2 = FakeFetcher(routes)
    win2 = TagReferenceWindow(s, fetcher=f2)
    win2.show()
    win2.navigate("t")
    g2 = win2._galleries[0]
    assert dapi.posts_by_ids_url(ids) not in f2.calls
    assert 'href="post:100"' in g2.chip_label.text()
    assert "Thumbnails are off" in g2.status.text()
    win2._on_doc_link("post:100")
    assert "turned off" in win2._inspectors[-1].media.notice.text()
    print("OK: partial batches name what is missing; with images off "
          "no post traffic occurs and the inspector honours the "
          "setting")


def test_always_on_top_keeps_the_close_button() -> None:
    """FIELD BUG: the window became unclosable the moment
    always-on-top was introduced — its close button greyed out.
    setWindowFlags() replaces the ENTIRE flag set, and the decoration
    hints a window got implicitly are not all reported back by
    windowFlags(), so feeding that value back dropped the close
    button. Decorations are now stated explicitly every time."""
    from PySide6.QtCore import Qt

    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()
    close_hint = Qt.WindowType.WindowCloseButtonHint
    def _tool(w) -> bool:
        return bool(w.windowFlags()
                    & Qt.WindowType.WindowStaysOnTopHint)

    assert win.windowFlags() & close_hint
    # Pinned by default: the window is parentless so it otherwise
    # falls behind the main window the moment you use it.
    assert s.tag_reference_always_on_top is True
    assert win.btn_on_top.isChecked()

    win.btn_on_top.setChecked(False)
    assert not _tool(win)
    assert win.windowFlags() & close_hint

    win.btn_on_top.setChecked(True)
    assert _tool(win)
    assert win.windowFlags() & close_hint       # the regression
    geom = win.saveGeometry()

    win.btn_on_top.setChecked(False)
    assert not _tool(win)
    assert win.windowFlags() & close_hint
    assert win.saveGeometry() == geom           # no window drift

    # An inspector opened under a pinned window must inherit the pin,
    # or it opens behind and looks stuck — and must stay closable.
    win.btn_on_top.setChecked(True)
    post = dapi.parse_posts([{
        "id": 100, "preview_file_url": "https://x/p100",
        "large_file_url": "https://x/l100", "rating": "g",
        "tag_string": "a b c", "tag_count": 3}])[0]
    win.open_inspector(post)
    pop = win._inspectors[-1]
    assert _tool(pop)
    assert pop.windowFlags() & close_hint
    print("OK: always-on-top keeps the close button and window "
          "position; inspectors inherit the pin and stay closable")


def test_bullet_style_examples_and_source_view() -> None:
    """The field shape that produced no image, no link and no error:
    an Examples section written as a bullet list. Plus the source
    view, which reports how many references the source contains
    versus how many were recognised."""
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    ids = [100, 101]
    body = "h4. Examples\n\n* !post #100\n* !post #101\n"
    wiki = json.dumps({"title": "t", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    posts = [{"id": i, "preview_file_url": f"https://x/p{i}",
              "large_file_url": f"https://x/l{i}", "rating": "g",
              "tag_string": "a b c", "tag_count": 3} for i in ids]
    routes = {dapi.wiki_url("t"): (wiki, 200),
              dapi.posts_by_ids_url(ids):
                  (json.dumps(posts).encode(), 200)}
    for i in ids:
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)
    f = FakeFetcher(routes)
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("t")
    assert len(win._galleries) == 1
    assert dapi.posts_by_ids_url(ids) in f.calls
    assert 'href="post:100"' in win._galleries[0].chip_label.text()

    assert not win.source_view.isVisible()      # off by default
    win.btn_source.setChecked(True)
    src = win.source_view.toPlainText()
    assert "!post #100" in src                  # raw source shown
    assert "2 example id(s) recognised" in src
    assert "2 post reference(s) present" in src
    print("OK: bullet-style example lists produce fetched galleries; "
          "the source view reports references present vs recognised")


def test_post_metadata_and_display_vocabulary() -> None:
    """Examples carry artist / character / copyright / meta splits in
    the same response, so showing them costs nothing extra. Ratings
    are spelled out — a bare "e" tells a reader nothing."""
    rich = {
        "id": 100, "preview_file_url": "https://x/p100",
        "large_file_url": "https://x/l100", "rating": "e",
        "tag_string": "1girl absurdres high_heels hatsune_miku "
                      "vocaloid wada_arco",
        "tag_count": 6, "tag_string_artist": "wada_arco",
        "tag_string_character": "hatsune_miku",
        "tag_string_copyright": "vocaloid",
        "tag_string_meta": "absurdres",
        "tag_string_general": "1girl high_heels"}
    post = dapi.parse_posts([rich])[0]
    assert post.tag_string_artist == "wada_arco"
    assert post.tag_string_meta == "absurdres"
    assert [h for h, _c, _t in post.grouped_tags()] == [
        "Artist", "Character", "Copyright", "Meta", "General"]

    assert dapi.rating_name("e") == "explicit"
    assert dapi.rating_name("q") == "questionable"
    assert dapi.rating_colour("e") == "#e05a5a"    # red
    assert dapi.rating_colour("g") == "#5fb85f"    # green

    # A response without the per-category strings still renders.
    bare = dapi.parse_posts([{"id": 9, "preview_file_url": "p",
                              "tag_string": "x y", "tag_count": 2}])[0]
    assert [h for h, _c, _t in bare.grouped_tags()] == ["Tags"]
    print("OK: per-category tag strings parsed and grouped in reader "
          "order; ratings spelled out and colour-coded, with a safe "
          "fallback when categories are absent")


def test_reference_window_folding_ratings_and_verdict_colour() -> None:
    from PySide6.QtCore import Qt

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    ids = [100, 101]
    body = ("h4. Examples\n\n* !post #100: [[Pumps]]\n"
            "* !post #101: [[Kitten heels]]\n")
    wiki = json.dumps({"title": "t", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    posts = [
        {"id": 100, "preview_file_url": "https://x/p100",
         "large_file_url": "https://x/l100", "rating": "e",
         "tag_string": "a b", "tag_count": 2},
        {"id": 101, "preview_file_url": "https://x/p101",
         "large_file_url": "https://x/l101", "rating": "g",
         "tag_string": "a b", "tag_count": 2}]
    routes = {dapi.wiki_url("t"): (wiki, 200),
              dapi.posts_by_ids_url(ids):
                  (json.dumps(posts).encode(), 200)}
    for i in ids:
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)
    win = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win.show()
    win.navigate("t")

    # Era counts fold away by default for a calmer first view.
    assert not win.era_toggle.isChecked()
    assert not win.era_label.isVisible()
    assert win.era_toggle.arrowType() == Qt.ArrowType.RightArrow
    win.era_toggle.setChecked(True)
    assert win.era_label.isVisible()
    assert "April 2026" in win.era_label.text()
    assert win.era_toggle.arrowType() == Qt.ArrowType.DownArrow
    win.era_toggle.setChecked(False)
    assert not win.era_label.isVisible()

    g = win._galleries[0]
    assert "Rating: explicit" in g.slots[100].badge.text()
    assert "#e05a5a" in g.slots[100].badge.text()
    assert "Rating: general" in g.slots[101].badge.text()
    assert "#5fb85f" in g.slots[101].badge.text()

    win.navigate("long_hair")
    assert "#5fb85f" in win.verdict.text()        # stable = green
    win.navigate("aoi_my_oc")
    assert "#e08a3c" in win.verdict.text()        # custom = amber
    print("OK: era counts fold by default, thumbnail ratings are "
          "spelled out and colour-coded, verdicts carry a quiet "
          "colour cue")


def test_inspector_shows_credits_and_grouped_tags() -> None:
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(d / "a.png")
    (d / "a.txt").write_text("1girl, high_heels", encoding="utf-8")
    st = SessionState(scan(d))
    st.select_tag("high_heels")
    win.set_state(st)
    post = dapi.parse_posts([{
        "id": 100, "preview_file_url": "", "large_file_url": "",
        "rating": "e", "tag_count": 6,
        "tag_string": "1girl absurdres high_heels hatsune_miku "
                      "vocaloid wada_arco",
        "tag_string_artist": "wada_arco",
        "tag_string_character": "hatsune_miku",
        "tag_string_copyright": "vocaloid",
        "tag_string_meta": "absurdres",
        "tag_string_general": "1girl high_heels"}])[0]
    win.open_inspector(post)
    pop = win._inspectors[-1]

    credits = pop.credits.text()
    assert "Artist:" in credits and "wada_arco" in credits
    assert "Character:" in credits and "hatsune_miku" in credits
    assert "Copyright:" in credits and "vocaloid" in credits
    assert dapi.CATEGORY_COLOURS["artist"] in credits
    assert 'href="tag:wada_arco"' in credits     # clickable

    tags = pop.tag_label.text()
    assert "<b>Artist</b> (1)" in tags
    assert "<b>Meta</b> (1)" in tags and "absurdres" in tags
    assert "<b>General</b> (2)" in tags
    assert "#7f7f7f" in tags                     # already-held dimmed

    pop._on_tag_clicked("tag:wada_arco")
    assert win.search.text() == "wada_arco"
    print("OK: inspector credits the artist, character and copyright "
          "as clickable colour-coded chips, and groups the full tag "
          "list by category")


def test_reveal_by_default_preference() -> None:
    from PySide6.QtWidgets import QPushButton

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    ids = [100]
    body = "h4. Examples\n\n* !post #100: [[Pumps]]\n"
    wiki = json.dumps({"title": "t", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    posts = [{"id": 100, "preview_file_url": "https://x/p100",
              "large_file_url": "https://x/l100", "rating": "g",
              "tag_string": "a b", "tag_count": 2}]
    routes = {dapi.wiki_url("t"): (wiki, 200),
              dapi.posts_by_ids_url(ids):
                  (json.dumps(posts).encode(), 200),
              "https://x/p100": (_png(), 200),
              "https://x/l100": (_png(), 200)}

    assert s.danbooru_reveal_by_default is False   # hover stays default
    win = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win.show()
    win.navigate("t")
    assert any(b.text() == "Reveal all examples"
               for b in win.doc_host.findChildren(QPushButton))
    assert "hover" in win._galleries[0].slots[100].label.text()

    s.danbooru_reveal_by_default = True
    win2 = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win2.show()
    win2.navigate("t")
    # The button would have nothing left to do, so it is not shown.
    assert not any(b.text() == "Reveal all examples"
                   for b in win2.doc_host.findChildren(QPushButton))
    pix = win2._galleries[0].slots[100].label.pixmap()
    assert pix and not pix.isNull()               # already visible
    assert win2._fetch_note is not None           # diagnostic kept

    from ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(s)
    assert dlg._dan_reveal.isChecked()
    dlg._dan_images.setChecked(False)
    assert not dlg._dan_reveal.isEnabled()        # gated by images
    print("OK: reveal-by-default shows examples immediately and drops "
          "the now-pointless Reveal-all button; the preference is "
          "gated behind images being on")


def test_inspector_copy_modes_and_export() -> None:
    """Copying offers the three shapes a caption is actually wanted
    in, general first (artist/copyright/meta rarely belong in a
    training caption). Export drops the image and its caption side by
    side so a good reference can live beside the dataset it informed,
    and reports where it landed."""
    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()
    post = dapi.parse_posts([{
        "id": 100, "preview_file_url": "https://x/p100",
        "large_file_url": "https://x/l100.png", "rating": "e",
        "tag_string": "1girl absurdres solo wada_arco", "tag_count": 4,
        "tag_string_artist": "wada_arco",
        "tag_string_meta": "absurdres",
        "tag_string_general": "1girl solo"}])[0]
    win.open_inspector(post)
    pop = win._inspectors[-1]

    assert [a.text() for a in pop._copy_menu.actions()] == [
        "Only general tags", "All", "All except metadata"]
    assert pop.tags_for_mode("general") == ["1girl", "solo"]
    assert len(pop.tags_for_mode("all")) == 4
    assert "absurdres" not in pop.tags_for_mode("no_meta")
    assert "wada_arco" in pop.tags_for_mode("no_meta")
    pop._copy_tags("general")
    assert QGuiApplication.clipboard().text() == "1girl, solo"
    assert "Copied 2" in pop.status.text()

    # Refuses to guess a location.
    pop._export()
    assert "Set an export folder" in pop.status.text()

    out = tempfile.mkdtemp()
    s.danbooru_export_dir = out
    win._img_cache.put("100_lg", _png())      # image already cached
    pop._export()
    # A cache hit now calls back through the event loop rather than
    # inline, so that a page of twenty cached thumbnails cannot decode
    # in one uninterruptible block. Tests have to let it turn.
    _app.processEvents()
    names = sorted(p.name for p in Path(out).iterdir())
    # The stem follows the naming preference now; what must hold is
    # that the image and its caption are written as a pair.
    assert len(names) == 2
    assert len({n.rsplit(".", 1)[0] for n in names}) == 1
    assert {n.rsplit(".", 1)[1] for n in names} == {"png", "txt"}
    caption_file = next(f for f in Path(out).iterdir()
                        if f.suffix == ".txt")
    caption = caption_file.read_text(
        encoding="utf-8")
    assert "absurdres" not in caption          # metadata excluded
    assert "1girl" in caption
    assert "Exported" in pop.status.text()
    assert hasattr(pop, "mouseDoubleClickEvent")   # double-click closes
    print("OK: copy offers general / all / no-metadata; export writes "
          "image + caption, refuses to guess a folder, and confirms "
          "where it landed")


def test_tag_referencer_button_and_link_handling() -> None:
    """The tool earned a permanent menu-bar button (open/close, "
    reopening on the last tag), external links copy rather than
    launch a browser, contents-box anchors jump, and a tag that
    exists without a wiki no longer suggests itself forever."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMenu, QToolButton
    from ui.main_window import MainWindow

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    mw = MainWindow(s)
    view_items = []
    for menu in mw.findChildren(QMenu):
        if menu.title() and "View" in menu.title():
            view_items = [a.text() for a in menu.actions() if a.text()]
    assert not any("Tag Refer" in t for t in view_items)
    # The corner now holds a pair: post browser, then Tag Referencer.
    host = mw.menuBar().cornerWidget(Qt.Corner.TopRightCorner)
    buttons = host.findChildren(QToolButton)
    assert len(buttons) == 2
    assert "Post Browser" in buttons[0].text()
    btn = buttons[1]
    assert isinstance(btn, QToolButton)
    assert "Tag Referencer" in btn.text()   # carries a globe icon
    assert not btn.isChecked()
    btn.click()
    assert mw._tag_ref_win.isVisible() and btn.isChecked()
    mw._tag_ref_win.navigate("long_hair")
    btn.click()
    assert not mw._tag_ref_win.isVisible() and not btn.isChecked()
    btn.click()
    assert "long_hair" in mw._tag_ref_win.search.text()  # last tag

    win = mw._tag_ref_win
    assert win.windowTitle() == "Tag Referencer"

    # External link -> clipboard, never a browser.
    win._offer_link("https://dic.pixiv.net/a/Danbooru")
    labels = [a.text() for a in win._link_menu.actions()]
    assert any("dic.pixiv.net" in t for t in labels)
    assert "Copy link" in labels
    [a for a in win._link_menu.actions()
     if a.text() == "Copy link"][0].trigger()
    assert (QGuiApplication.clipboard().text()
            == "https://dic.pixiv.net/a/Danbooru")
    win._link_menu.hide()

    # A tag that exists but has no wiki must not suggest itself.
    sim = json.dumps([{"name": "solo", "post_count": 9}]).encode()
    win2 = TagReferenceWindow(
        s, fetcher=FakeFetcher({dapi.tag_search_url("solo"): (sim, 200)}))
    win2.show()
    win2.navigate("solo")
    texts = _doc_texts(win2)
    assert not any("Did you mean" in t for t in texts)
    assert any("no wiki page" in t for t in texts)
    print("OK: menu-bar button toggles and reopens on the last tag; "
          "external links copy instead of opening; a wiki-less tag no "
          "longer suggests itself in a loop")


def test_export_original_resolution_and_folder_button() -> None:
    """Danbooru serves a shrunk sample for quick viewing and keeps the
    untouched upload separately. An export destined for training wants
    the latter, so it is an explicit option — and the two files are
    cached under different keys so neither overwrites the other."""
    from PySide6.QtWidgets import QToolButton

    raw = {"id": 100, "preview_file_url": "https://x/p100",
           "large_file_url": "https://x/sample100.jpg",
           "file_url": "https://x/orig100.png", "rating": "g",
           "tag_string": "1girl solo", "tag_count": 2,
           "tag_string_general": "1girl solo"}
    post = dapi.parse_posts([raw])[0]
    assert post.original_url == "https://x/orig100.png"
    assert post.large_url == "https://x/sample100.jpg"
    # Posts without a file_url fall back rather than exporting nothing.
    fallback = dapi.parse_posts(
        [{"id": 1, "preview_file_url": "p", "large_file_url": "L"}])[0]
    assert fallback.original_url == "L"

    s = _fresh_settings()
    out = tempfile.mkdtemp()
    s.danbooru_export_dir = out
    small, big = _png(), _png() * 4        # distinguishable sizes
    routes = {"https://x/sample100.jpg": (small, 200),
              "https://x/orig100.png": (big, 200)}

    assert s.danbooru_export_original is False   # fast path default
    win = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win.show()
    win.open_inspector(post)
    win._inspectors[-1]._export()
    assert any(f.suffix == ".jpg" for f in Path(out).iterdir())
    saved = next(f for f in Path(out).iterdir() if f.suffix == ".jpg")
    assert len(saved.read_bytes()) == len(small)

    s.danbooru_export_original = True
    win2 = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win2.show()
    win2.open_inspector(post)
    win2._inspectors[-1]._export()
    assert any(f.suffix == ".png" for f in Path(out).iterdir())
    saved = next(f for f in Path(out).iterdir() if f.suffix == ".png")
    assert len(saved.read_bytes()) == len(big)
    assert any(f.suffix == ".txt" for f in Path(out).iterdir())
    assert win2._img_cache.get("100_orig") is not None   # own key

    # The export-folder button now lives in the post browser, beside
    # where exporting actually happens.
    assert not hasattr(win2, "btn_export_dir")
    browser = win2._open_browser()
    assert isinstance(browser.btn_export_dir, QToolButton)
    assert "export folder" in browser.btn_export_dir.toolTip().lower()
    s.danbooru_export_dir = ""
    browser._open_export_dir()
    assert "No export folder set" in browser.status.text()
    print("OK: exports can take the original upload instead of the "
          "shrunk sample, cached under its own key; the folder button "
          "explains itself when no folder is set")


def test_new_tag_is_not_mislabelled_custom() -> None:
    """Offline, a tag in none of the four snapshots reads as "custom"
    — the user's own invented token. A tag CREATED after the newest
    snapshot is indistinguishable from that offline, and calling it
    custom is simply wrong (`presenting_own_body` is a real Danbooru
    tag that postdates the April 2026 data). A wiki page proves it is
    real, so the verdict is corrected once the page arrives."""
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    new_tag = "presenting_own_body"
    wiki = json.dumps({"title": new_tag, "body": "A new tag.",
                       "other_names": [],
                       "updated_at": "2026-06-01"}).encode()
    win = TagReferenceWindow(
        s, fetcher=FakeFetcher({dapi.wiki_url(new_tag): (wiki, 200)}))
    win.show()
    win.navigate(new_tag)
    verdict = win.verdict.text()
    assert "custom" not in verdict.lower()
    assert "newer than your data" in verdict
    assert "April 2026" in verdict          # names the newest snapshot

    # A tag with no wiki is still custom, and known tags are untouched.
    win2 = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win2.show()
    win2.navigate("aoi_my_oc")
    assert "custom" in win2.verdict.text().lower()
    win2.navigate("long_hair")
    assert "stable" in win2.verdict.text()
    print("OK: a tag that postdates the shipped snapshot is named as "
          "such rather than mislabelled custom; genuine custom tags "
          "and known tags are unaffected")


def test_fresh_on_restart_and_full_reset() -> None:
    """Two related expectations: a restart starts clean (nothing about
    the last lookup is persisted), and Full reset means "as if the
    program had just started" — the folder is CLOSED rather than
    re-scanned, and the Tag Referencer forgets too."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    from ui.main_window import MainWindow

    s = _fresh_settings()
    assert not hasattr(s, "tag_reference_last_tag")   # not persisted
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()
    assert win.search.text() == "" and win._hpos == -1

    mw = MainWindow(s)
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(d / "a.png")
    (d / "a.txt").write_text("long_hair", encoding="utf-8")
    mw._adopt_state(SessionState(scan(d)), scan_root=d)
    mw._action_tag_reference()
    mw._tag_ref_win.navigate("long_hair")
    assert mw._state is not None and mw._tag_ref_win.isVisible()

    original = QMessageBox.exec
    QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes
    try:
        mw._action_full_reset()
    finally:
        QMessageBox.exec = original

    assert mw._state is None                     # closed, not re-scanned
    # The path label was removed; with no dataset the window title is just
    # the app name, and Full Reset (now a File-menu action) is disabled.
    assert mw.windowTitle() == "TagWalker"
    assert not mw._act_full_reset.isEnabled()
    assert mw._tag_tree._state is None
    assert mw._queue_panel._state is None
    ref = mw._tag_ref_win
    assert not ref.isVisible()
    assert ref.search.text() == "" and ref._hpos == -1
    assert ref._state is None
    from PySide6.QtWidgets import QToolButton as _TB
    btn = mw.menuBar().cornerWidget(
        Qt.Corner.TopRightCorner).findChildren(_TB)[1]
    assert not btn.isChecked()

    # And the app is usable again afterwards.
    mw._adopt_state(SessionState(scan(d)), scan_root=d)
    mw._action_tag_reference()
    assert mw._state is not None
    assert mw._tag_ref_win._state is mw._state
    print("OK: nothing survives a restart; Full reset closes the "
          "folder and clears the Tag Referencer, and the app reloads "
          "cleanly afterwards")


def test_blacklist_blocks_before_the_network() -> None:
    """The property that matters: a blocked post never reaches the
    network, let alone the screen. Tags arrive with the post metadata,
    which is fetched before any thumbnail, so blocking happens first.

    Every route to an image is checked — gallery reveal, reveal-all,
    the chip path, and open_inspector directly — because one unguarded
    path defeats the whole feature."""
    s = _fresh_settings()
    assert "guro" in s.danbooru_block_custom      # shipped default
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True

    ids = [100, 101, 102, 103]
    body = ("h4. Examples\n\n* !post #100: [[Clean]]\n"
            "* !post #101: [[Gore]]\n* !post #102: [[Scat]]\n"
            "* !post #103: [[Furry]]\n")
    wiki = json.dumps({"title": "t", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    tagsets = {100: "1girl long_hair", 101: "1girl guro blood",
               102: "1girl scat", 103: "furry 1girl"}
    posts = [{"id": i, "preview_file_url": f"https://x/p{i}",
              "large_file_url": f"https://x/l{i}", "rating": "e",
              "tag_string": tagsets[i], "tag_count": 2} for i in ids]
    routes = {dapi.wiki_url("t"): (wiki, 200),
              dapi.posts_by_ids_url(ids):
                  (json.dumps(posts).encode(), 200)}
    for i in ids:
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)
    f = FakeFetcher(routes)
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("t")
    g = win._galleries[0]

    assert "blocked" in g.slots[101].label.text()
    assert "guro" in g.slots[101].label.text()      # says why
    assert "blocked" in g.slots[102].label.text()
    # furry is rated e here, so the shipped rule blocks it
    assert "blocked" in g.slots[103].label.text()
    assert "hover" in g.slots[100].label.text()

    # Hovering a blocked thumbnail must not fetch anything.
    g.slots[101]._hover = True
    g.slots[101]._reveal()
    assert "https://x/p101" not in f.calls
    assert "https://x/l101" not in f.calls
    g.slots[100]._hover = True
    g.slots[100]._reveal()
    assert "https://x/p100" in f.calls               # allowed one does

    win._set_reveal_all(True)
    assert "https://x/p101" not in f.calls           # nor reveal-all

    # Chip path and the direct call are both guarded.
    win._on_doc_link("post:101")
    assert not win._inspectors
    assert "blocked" in g.status.text()
    win._on_doc_link("post:100")
    assert len(win._inspectors) == 1
    before = len(win._inspectors)
    win.open_inspector(dapi.parse_posts([posts[1]])[0])
    assert len(win._inspectors) == before

    # Editing the list takes effect on the next lookup, no restart.
    s.danbooru_block_custom = "long_hair"
    win.navigate("t")
    assert "blocked" in win._galleries[0].slots[100].label.text()
    assert "hover" in win._galleries[0].slots[101].label.text()
    # ...and it is the WHOLE truth: gore is only blocked because the
    # list says so.
    print("OK: blocked posts are stopped before any image request, on "
          "every path (hover, reveal-all, chip, direct), and "
          "preference changes apply without a restart")


def test_blacklist_preferences_and_reset_to_defaults() -> None:
    """One visible, editable list — no hidden rules, no switches. And
    the property the publisher asked to be sure of: "Reset to
    defaults" restores the shipped blacklist text exactly, including
    after the user has erased it."""
    from PySide6.QtWidgets import QMessageBox
    from core import blacklist as bl
    from ui.settings_dialog import SettingsDialog

    s = _fresh_settings()
    assert "guro" in s.danbooru_block_custom      # ships pre-filled
    assert "furry -rating:g" in s.danbooru_block_custom
    assert not hasattr(s, "danbooru_block_scat")  # switches gone
    assert not hasattr(s, "danbooru_block_furry")

    dlg = SettingsDialog(s)
    assert not hasattr(dlg, "_dan_block_hard")
    assert "guro" in dlg._dan_block_custom.toPlainText()
    assert (dlg._dan_export_original.text()
            == "Always export images with original resolution")

    # The user erases it. That really does unblock — their call.
    dlg._dan_block_custom.setPlainText("zombie\n")
    dlg._on_ok()
    assert s.danbooru_block_custom.strip() == "zombie"
    erased = bl.build_rules(s.danbooru_block_custom)
    assert bl.blocked_reason("guro", "e", erased) is None

    dlg2 = SettingsDialog(s)
    original = QMessageBox.exec
    QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes
    try:
        dlg2._on_reset()
    finally:
        QMessageBox.exec = original
    assert "guro" in dlg2._dan_block_custom.toPlainText()
    assert "zombie" not in s.danbooru_block_custom
    assert s.danbooru_block_custom == bl.DEFAULT_BLACKLIST
    # Multi-line values survive the INI round trip.
    assert Settings().danbooru_block_custom == bl.DEFAULT_BLACKLIST
    print("OK: one editable pre-filled blacklist; Reset to defaults "
          "restores the shipped text byte-for-byte and it survives a "
          "restart")


def test_appearance_assets_render() -> None:
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    body = ("h4. Appearance\n\n* !asset #111: [[Default outfit]]\n"
            "* !asset #222: [[Swimsuit]]\n")
    wiki = json.dumps({"title": "c", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    routes = {
        dapi.wiki_url("c"): (wiki, 200),
        dapi.media_asset_url(111): (json.dumps({
            "id": 111, "variants": [
                {"type": "180x180", "url": "https://x/small"},
                {"type": "720x720", "url": "https://x/big"}]}).encode(), 200),
        dapi.media_asset_url(222): (json.dumps({
            "id": 222, "variants": [
                {"type": "360x360", "url": "https://x/a222"}]}).encode(), 200),
        "https://x/big": (_png(), 200),
        "https://x/a222": (_png(), 200)}
    f = FakeFetcher(routes)
    win = TagReferenceWindow(s, fetcher=f)
    win.show()
    win.navigate("c")

    gal = [g for g in win._galleries if g.asset_ids][0]
    assert gal.asset_ids == [111, 222]
    assert dapi.media_asset_url(111) in f.calls
    assert "https://x/big" in f.calls          # best variant chosen
    slot = gal.asset_slots[111]
    pix = slot.label.pixmap()
    assert pix and not pix.isNull()            # shown, not hover-gated
    assert "reference image" in gal.chip_label.text()

    # Unfamiliar response shapes degrade to no image, never a crash.
    assert dapi.parse_media_asset({"file_url": "https://x/z"}) == "https://x/z"
    assert dapi.parse_media_asset({}) == ""
    assert dapi.parse_media_asset(None) == ""
    print("OK: Appearance sections fetch and show their media assets, "
          "preferring a mid-size variant, and tolerate unexpected "
          "response shapes")


def test_video_posts_offer_download() -> None:
    """A video post's still thumbnail displays fine but the video
    itself cannot, and "bad image data" was a dead end. The one thing
    that DOES work is saving the file, so that is what is offered."""
    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    post = dapi.parse_posts([{
        "id": 444, "preview_file_url": "https://x/p444",
        "large_file_url": "https://x/l444.mp4",
        "file_url": "https://x/o444.mp4", "file_ext": "mp4",
        "rating": "g", "tag_string": "animated 1girl",
        "tag_count": 2}])[0]
    assert dapi.is_video(post)
    assert post.file_ext == "mp4"

    out = tempfile.mkdtemp()
    s.danbooru_export_dir = out
    win = TagReferenceWindow(
        s, fetcher=FakeFetcher({"https://x/o444.mp4": (b"FAKEVIDEO", 200),
                                "https://x/p444": (_png(), 200)}))
    win.show()
    win.open_inspector(post)
    pop = win._inspectors[-1]
    # FIELD BUG: the message used to REPLACE the still, so opening a
    # video post lost the thumbnail that had displayed perfectly well
    # in the grid. The still stays; the message sits over it.
    assert "MP4" in pop.media.notice.text()
    assert "still frame" in pop.media.notice.text()
    assert pop.btn_export.text() == "Download video + tags"

    pop._export()
    names = sorted(p.name for p in Path(out).iterdir())
    assert len(names) == 2
    assert len({n.rsplit(".", 1)[0] for n in names}) == 1
    # The real file, not the still sample.
    saved = next(f for f in Path(out).iterdir() if f.suffix == ".mp4")
    assert saved.read_bytes() == b"FAKEVIDEO"
    assert "Saved" in pop.status.text()
    print("OK: video posts offer a download that saves the real file "
          "with its caption, instead of a bad-image dead end")


def test_media_view_sizing_and_animation() -> None:
    """Posts arrive at every size from a few hundred pixels to several
    thousand, so the picture area fits by default and never upscales:
    the window stays put whatever opens in it, and a small image is
    shown small rather than blurrily inflated.

    Zooming is the main image viewer's own code, shared verbatim, so
    the two cannot drift apart. A GIF loads paused on its first frame,
    because nothing should start moving unasked."""
    from PySide6.QtGui import QPixmap
    from ui.media_view import MediaView, is_animated

    def _gif() -> bytes:
        buf = io.BytesIO()
        frames = [Image.new("RGB", (64, 64), c)
                  for c in ("red", "green", "blue")]
        frames[0].save(buf, "GIF", save_all=True,
                       append_images=frames[1:], duration=80, loop=0)
        return buf.getvalue()

    def _big(w: int, h: int) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "red").save(buf, "PNG")
        return buf.getvalue()

    # Decided from the bytes, not the extension: a .webp may be either.
    assert is_animated(_gif())
    assert not is_animated(_png())

    view = MediaView()
    view.resize(400, 300)
    view.show()

    huge = QPixmap()
    huge.loadFromData(_big(2000, 1500))
    view.show_pixmap(huge)
    assert view.is_fitted()
    assert view._view.transform().m11() < 1.0        # scaled down
    # Wheel and drag are the only gestures. A click-to-toggle was
    # tried and removed: the wheel already zooms, so clicking bought
    # nothing and competed with drag-to-pan.
    assert not hasattr(view, "toggle_zoom")
    assert hasattr(view._view, "wheelEvent")

    small = QPixmap()
    small.loadFromData(_big(80, 60))
    view.show_pixmap(small)
    assert abs(view._view.transform().m11() - 1.0) < 0.001   # no upscale

    assert view.show_animation(_gif())
    assert not view.is_playing()                     # paused on frame 1
    assert view.btn_play.isVisible()
    assert view._item is not None
    assert not view._item.pixmap().isNull()          # frame 1 visible
    view.toggle_play()
    assert view.is_playing()
    view.toggle_play()
    assert not view.is_playing()
    view.show_pixmap(small)
    assert not view.btn_play.isVisible()             # stills have none
    print("OK: pictures fit the box without upscaling, zoom uses the "
          "shared wheel/drag surface, and animations wait to be "
          "started")


def test_inspector_shows_animation_and_keeps_video_stills() -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True

    def _gif() -> bytes:
        buf = io.BytesIO()
        frames = [Image.new("RGB", (48, 48), c)
                  for c in ("red", "green")]
        frames[0].save(buf, "GIF", save_all=True,
                       append_images=frames[1:], duration=80, loop=0)
        return buf.getvalue()

    video = dapi.parse_posts([{
        "id": 7, "preview_file_url": "https://x/p7.jpg",
        "large_file_url": "https://x/l7.mp4",
        "file_url": "https://x/o7.mp4", "file_ext": "mp4",
        "rating": "g", "tag_string": "animated 1girl",
        "tag_count": 2}])[0]
    animated = dapi.parse_posts([{
        "id": 8, "preview_file_url": "https://x/p8.gif",
        "large_file_url": "https://x/l8.gif", "file_ext": "gif",
        "rating": "g", "tag_string": "animated_gif 1girl",
        "tag_count": 2}])[0]
    routes = {"https://x/p7.jpg": (_png(), 200),
              "https://x/l8.gif": (_gif(), 200)}
    win = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win.show()

    win.open_inspector(video)
    pop = win._inspectors[-1]
    # The still displayed fine in the grid; opening the post used to
    # throw it away for a message. Now the message sits over it.
    assert pop.media._item is not None
    assert not pop.media._item.pixmap().isNull()
    assert "MP4" in pop.media.notice.text()
    assert pop.btn_export.text() == "Download video + tags"

    win.open_inspector(animated)
    gif_pop = win._inspectors[-1]
    assert gif_pop.media._movie is not None
    assert gif_pop.media.btn_play.isVisible()
    assert not gif_pop.media.is_playing()
    gif_pop.media.toggle_play()
    assert gif_pop.media.is_playing()

    # Double-click no longer dismisses: a click on the picture means
    # zoom, so an eager second click would have closed the window.
    # Double-click closes again: safe now that a single click on the
    # picture does nothing.
    assert "mouseDoubleClickEvent" in type(gif_pop).__dict__
    gif_pop.keyPressEvent(QKeyEvent(
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier))
    assert not gif_pop.isVisible()
    assert gif_pop.media._movie is None          # nothing left running
    print("OK: video posts keep their still with the notice over it, "
          "GIFs play on request, and double-click closes as before")


def test_fit_is_deferred_until_the_widget_has_a_size() -> None:
    """FIELD BUG: posts often opened absurdly small, and it got worse
    the more you browsed.

    A cached image calls back SYNCHRONOUSLY while the popup is still
    being built, so the viewport was its unlaid-out default of about
    98x28 and a 1200px picture was fitted to that — a scale of 0.03.
    The first view of a post came from the network, after layout, and
    looked right; every later view came from the cache and did not.

    A viewport too small to be real now defers the fit instead of
    obeying it."""
    from PySide6.QtGui import QPixmap
    from ui.media_view import MediaView

    def _pixels(w: int, h: int) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "red").save(buf, "PNG")
        return buf.getvalue()

    pix = QPixmap()
    pix.loadFromData(_pixels(1200, 900))
    ideal = min(1.0, 560 / 1200, 460 / 900)

    # The cache path: picture first, layout afterwards.
    cached = MediaView()
    cached.show_pixmap(pix)
    assert cached._pending_fit          # refused to fit on 98x28
    cached.resize(560, 460)
    cached.show()
    _app.processEvents()
    from_cache = cached._view.transform().m11()

    # The network path: layout first, picture afterwards.
    fetched = MediaView()
    fetched.resize(560, 460)
    fetched.show()
    _app.processEvents()
    fetched.show_pixmap(pix)
    from_net = fetched._view.transform().m11()

    assert abs(from_cache - ideal) < 0.005
    assert abs(from_net - ideal) < 0.005
    assert abs(from_cache - from_net) < 0.001   # order cannot matter

    # Small pictures are still never blown up.
    small = QPixmap()
    small.loadFromData(_pixels(80, 60))
    fetched.show_pixmap(small)
    assert abs(fetched._view.transform().m11() - 1.0) < 0.001
    print("OK: a picture arriving before the window has a size waits "
          "to be fitted, so cached and freshly fetched posts open at "
          "exactly the same scale")


def test_pane_extraction_holds_together() -> None:
    """Stage one of compare mode: the window's per-tag half is now a
    _ReferencePane, so a second one can be built later rather than
    teaching one window to write every value twice.

    A pure refactor, so the whole suite passes unchanged. What that
    does NOT prove is pinned here instead — the window forwards
    attribute READS to its pane, and an assignment would silently
    create a shadowing window attribute that every reader sees while
    the pane keeps its old value. reset_session did exactly that, and
    the suite was happy."""
    from ui.tag_reference_window import _ReferencePane

    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()
    assert isinstance(win._pane, _ReferencePane)
    # The window's own view of a pane widget IS the pane's.
    assert win.header is win._pane.header
    assert win.verdict is win._pane.verdict
    assert win._hist is win._pane._hist

    win.navigate("long_hair")
    win.navigate("smile")
    win.navigate("1girl")
    assert len(win._pane._hist) == 3
    win._go_back()
    assert win.search.text() == "smile"

    win.reset_session()
    # The pane itself must be empty, not merely a shadow on the window.
    assert win._pane._hist == [] and win._pane._hpos == -1
    for shadowed in ("_hist", "_hpos", "_posts_by_id", "_last_body"):
        assert shadowed not in win.__dict__, shadowed
    win.navigate("blue_eyes")
    assert win.search.text() == "blue_eyes"
    assert len(win._pane._hist) == 1
    print("OK: the pane owns its own history and widgets, the window "
          "reads through to it, and resetting clears the pane rather "
          "than shadowing it")


def test_compare_mode() -> None:
    """Stage two: a second pane beside the first.

    The left pane follows what you look up; the right one stays put.
    Ctrl+click sends a tag across, swap exchanges the sides, and a
    thin strip carries the two things worth comparing directly — how
    common each tag is, and how much company they keep."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from ui.tag_reference_window import COMPARE_WIDTH

    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()
    win.resize(560, 700)
    narrow = win.width()

    assert len(win._panes) == 1 and not win._compare
    assert not win.btn_swap.isVisible()
    win.navigate("long_hair")

    win.btn_compare.setChecked(True)
    assert len(win._panes) == 2 and win._compare
    # Two columns of wiki text need room.
    assert win.width() >= COMPARE_WIDTH
    assert win.btn_swap.isVisible() and win.compare_strip.isVisible()
    # A blank half-window helps nobody, so the pinned pane starts on
    # whatever the live one is showing.
    assert win._panes[1].tag == "long_hair"
    assert "following" in win._panes[0].role.text()
    assert "pinned" in win._panes[1].role.text()

    # The navigation row drives the live pane only.
    win.navigate("smile")
    assert win._panes[0].tag == "smile"
    assert win._panes[1].tag == "long_hair"
    assert win.header is win._panes[0].header       # proxy follows

    # Ordinary clicks stay put; Ctrl sends the tag across.
    win._panes[0]._on_doc_link("tag:blue_eyes")
    assert win._panes[0].tag == "blue_eyes"
    assert win._panes[1].tag == "long_hair"
    real_mods = QGuiApplication.keyboardModifiers
    QGuiApplication.keyboardModifiers = staticmethod(
        lambda: Qt.KeyboardModifier.ControlModifier)
    try:
        win._panes[0]._on_doc_link("tag:short_hair")
    finally:
        QGuiApplication.keyboardModifiers = real_mods
    assert win._panes[0].tag == "blue_eyes"
    assert win._panes[1].tag == "short_hair"

    strip = win.compare_strip.text()
    assert "blue_eyes" in strip and "short_hair" in strip
    assert "posts in" in strip
    assert "usual company is shared" in strip       # overlap reported

    # Navigating must refresh the strip: it went stale otherwise.
    win.navigate("1girl")
    assert "1girl" in win.compare_strip.text()

    # Swap promotes the pinned tag to the followed one.
    left, right = win._panes[0].tag, win._panes[1].tag
    win.btn_swap.click()
    assert win._panes[0].tag == right and win._panes[1].tag == left
    assert "following" in win._panes[0].role.text()
    assert win.search.text() == right
    # And the window still addresses the live pane after the swap.
    assert win._pane is win._panes[0]

    # One inspector: two panes offering examples would bury the window.
    post = dapi.parse_posts([{
        "id": 1, "preview_file_url": "p", "large_file_url": "l",
        "rating": "g", "tag_string": "a b", "tag_count": 2}])[0]
    for _ in range(3):
        win.open_inspector(post)
    assert len(win._inspectors) == 1

    win.btn_compare.setChecked(False)
    assert len(win._panes) == 1 and not win._compare
    assert win.width() == narrow                    # width restored
    assert not win.btn_swap.isVisible()
    assert not win.compare_strip.isVisible()
    assert not win._panes[0].role.isVisible()
    win.navigate("smile")
    assert win.search.text() == "smile"
    print("OK: two panes compare side by side, the left one follows, "
          "Ctrl+click and swap move tags between them, and the strip "
          "tracks both")


def test_compare_mode_survives_the_rest_of_the_program() -> None:
    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()

    # Entering before anything has been looked up.
    win.btn_compare.setChecked(True)
    assert win._panes[0].tag == "" and win._panes[1].tag == ""

    # A blacklisted tag in the pinned pane refuses there, not in the
    # live one.
    s.danbooru_block_custom = "guro\n"
    win._panes[1].navigate("guro")
    assert win._panes[1].tag == "guro"
    s.danbooru_block_custom = ""

    # Toggling and swapping repeatedly must not leak panes.
    for _ in range(5):
        win.btn_swap.click()
    for _ in range(4):
        win.btn_compare.setChecked(False)
        win.btn_compare.setChecked(True)
    assert len(win._panes) == 2

    # A reset returns the window to what a fresh one looks like:
    # single pane, both cleared.
    win.navigate("long_hair")
    win.reset_session()
    assert len(win._panes) == 1 and not win._compare
    assert not win.btn_compare.isChecked()
    assert win._panes[0].tag == "" and win._panes[0]._hist == []
    win.navigate("1girl")
    assert win.search.text() == "1girl"

    # With one pane there is nowhere to send a tag, so Ctrl+click
    # behaves like an ordinary click rather than doing nothing.
    win.send_to_other_pane(win._panes[0], "smile")
    assert win._panes[0].tag == "smile"
    print("OK: compare mode coexists with the blacklist, resets, "
          "repeated toggling and single-pane operation")


def test_pane_refactor_regressions() -> None:
    """FIELD REPORT, both caused by the pane split.

    Thumbnails stopped opening: the galleries are built with their
    PANE as "window", and the pane had no open_inspector, so a click
    raised AttributeError inside a Qt handler — which Qt swallows, so
    the thumbnail simply did nothing.

    Back/Forward drove the wrong pane after a swap: connecting a
    signal straight to self._go_back resolves through __getattr__
    ONCE, at connect time, and stores a bound method of whichever pane
    existed then."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from ui.tag_reference_window import SPLITTER_GAP

    def click(widget):
        widget.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(4, 4),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    body = "h4. Examples\n\n* !post #100: [[Pumps]]\n"
    wiki = json.dumps({"title": "t", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    posts = [{"id": 100, "preview_file_url": "https://x/p",
              "large_file_url": "https://x/l", "rating": "g",
              "tag_string": "a b", "tag_count": 2}]
    routes = {dapi.wiki_url("t"): (wiki, 200),
              dapi.posts_by_ids_url([100]):
                  (json.dumps(posts).encode(), 200),
              "https://x/p": (_png(), 200),
              "https://x/l": (_png(), 200)}

    win = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win.show()
    win.navigate("t")
    click(win._galleries[0].slots[100])
    assert len(win._inspectors) == 1
    assert win._inspectors[0]._post.id == 100

    # Back and Forward must follow the swap.
    win2 = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win2.show()
    win2.navigate("long_hair")
    win2.btn_compare.setChecked(True)
    win2._panes[1].navigate("smile")
    win2._panes[1].navigate("1girl")
    win2.navigate("blue_eyes")
    win2.btn_swap.click()
    left, right = [p.tag for p in win2._panes]
    win2.btn_back.click()
    moved_left, moved_right = [p.tag for p in win2._panes]
    assert moved_left != left          # the followed pane moved
    assert moved_right == right        # the pinned one did not
    win2.btn_fwd.click()
    assert win2._panes[0].tag == left

    # Same trap, same fix: the source toggle.
    win2.btn_source.setChecked(True)
    assert win2._panes[0].source_view.isVisible()
    assert not win2._panes[1].source_view.isVisible()

    # And thumbnails work in the pinned pane, not only the live one.
    win2._panes[1].navigate("t")
    if win2._panes[1]._galleries:
        click(win2._panes[1]._galleries[0].slots[100])
        assert len(win2._inspectors) == 1

    assert win2._splitter.handleWidth() == SPLITTER_GAP
    print("OK: thumbnails open their post from either pane, and the "
          "navigation buttons drive whichever pane is live after a "
          "swap")


def test_every_thumbnail_click_does_something() -> None:
    """FIELD REPORT: on a character page only the first two examples
    opened; the rest ignored clicks.

    Character wiki pages illustrate outfits with `!asset` embeds
    rather than posts. An asset has no post behind it — no rating, no
    tags — which was taken as a reason to make those tiles
    unclickable. Wrong: the tile shows a picture, and a picture that
    ignores clicks is indistinguishable from a broken one.

    The rule now is that a click always does something or says why
    not. Silence is never the answer."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    def click(widget):
        widget.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(4, 4),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    body = ("h4. Examples\n\n* !post #101: [[Default]]\n"
            "* !post #102: [[Swimsuit]]\n\n"
            "h4. Appearance\n\n* !asset #9001: [[Gym outfit]]\n"
            "* !asset #9002: [[Casual]]\n")
    wiki = json.dumps({"title": "nessa", "body": body,
                       "other_names": [], "updated_at": "2026"}).encode()
    posts = [{"id": i, "preview_file_url": f"https://x/p{i}",
              "large_file_url": f"https://x/l{i}", "rating": "g",
              "tag_string": "1girl", "tag_count": 1}
             for i in (101, 102)]
    routes = {dapi.wiki_url("nessa"): (wiki, 200),
              dapi.posts_by_ids_url([101, 102]):
                  (json.dumps(posts).encode(), 200)}
    for i in (101, 102):
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)
    for a in (9001, 9002):
        routes[dapi.media_asset_url(a)] = (json.dumps({
            "id": a, "variants": [
                {"type": "720x720", "url": f"https://x/a{a}"}]}).encode(), 200)
        routes[f"https://x/a{a}"] = (_png(), 200)

    win = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win.show()
    win.navigate("nessa")
    post_gallery = [g for g in win._galleries if g.slots][0]
    asset_gallery = [g for g in win._galleries if g.asset_slots][0]

    click(post_gallery.slots[101])
    assert type(win._inspectors[-1]).__name__ == "TagInspectorPopup"

    for asset_id in (9001, 9002):
        click(asset_gallery.asset_slots[asset_id])
        _app.processEvents()          # cached callbacks are deferred
        viewer = win._inspectors[-1]
        assert type(viewer).__name__ == "AssetViewerDialog", asset_id
        assert str(asset_id) in viewer.windowTitle()
        assert viewer.media._item is not None
        assert not viewer.media._item.pixmap().isNull()
    assert len(win._inspectors) == 1        # one viewer at a time

    # A tile whose post never arrived must say so rather than ignore
    # the click.
    broken = dict(routes)
    broken[dapi.posts_by_ids_url([101, 102])] = (None, 500)
    for i in (101, 102):
        broken[dapi.post_url(i)] = (None, 500)
    win2 = TagReferenceWindow(s, fetcher=FakeFetcher(broken))
    win2.show()
    win2.navigate("nessa")
    gallery2 = [g for g in win2._galleries if g.slots][0]
    click(gallery2.slots[101])
    assert "never loaded" in gallery2.status.text()

    # ...and so must a blocked one.
    s.danbooru_block_custom = "1girl\n"
    win3 = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win3.show()
    win3.navigate("nessa")
    gallery3 = [g for g in win3._galleries if g.slots][0]
    click(gallery3.slots[101])
    assert "cannot be opened" in gallery3.status.text()
    s.danbooru_block_custom = ""
    print("OK: post tiles open the inspector, reference images open a "
          "viewer, and a tile that cannot open explains why instead "
          "of ignoring the click")


def test_deleted_posts_are_shown_not_refused() -> None:
    """FIELD REPORT: wiki examples appeared as 'deleted' and would not
    open — yet clicking the post-id chip beside them opened the very
    same picture.

    Deletion on Danbooru is a moderation state, not removal: the files
    stay served. Treating it as 'skip' meant the two routes to one
    post disagreed, which is incoherent from the user's side. The
    thumbnail is shown, labelled, and opens. Banned posts really do
    have their files pulled, so those are still skipped."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    def click(widget):
        widget.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(4, 4),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))

    deleted = {"id": 501, "preview_file_url": "https://x/p501",
               "large_file_url": "https://x/l501", "rating": "g",
               "tag_string": "1girl", "tag_count": 1,
               "is_deleted": True}
    banned = {"id": 502, "preview_file_url": "", "large_file_url": "",
              "rating": "g", "tag_string": "1girl", "tag_count": 1,
              "is_banned": True}
    normal = {"id": 503, "preview_file_url": "https://x/p503",
              "large_file_url": "https://x/l503", "rating": "g",
              "tag_string": "1girl", "tag_count": 1}

    d = dapi.parse_posts([deleted])[0]
    assert d.deleted and not d.skip
    assert dapi.parse_posts([banned])[0].skip_reason == "banned"
    assert not dapi.parse_posts([normal])[0].deleted

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    body = "h4. Examples\n\n* !post #501: [[A]]\n* !post #503: [[B]]\n"
    wiki = json.dumps({"title": "t", "body": body, "other_names": [],
                       "updated_at": "2026"}).encode()
    routes = {dapi.wiki_url("t"): (wiki, 200),
              dapi.posts_by_ids_url([501, 503]):
                  (json.dumps([deleted, normal]).encode(), 200)}
    for i in (501, 503):
        routes[f"https://x/p{i}"] = (_png(), 200)
        routes[f"https://x/l{i}"] = (_png(), 200)
    win = TagReferenceWindow(s, fetcher=FakeFetcher(routes))
    win.show()
    win.navigate("t")
    tile = win._galleries[0].slots[501]
    assert "deleted" in tile.badge.text()      # labelled, not hidden
    click(tile)
    assert len(win._inspectors) == 1
    assert win._inspectors[0]._post.id == 501

    # The browser is uncurated, so a deleted result there is noise.
    search = {dapi.posts_search_url("1girl", "rated", 1, 20):
              (json.dumps([deleted, normal]).encode(), 200),
              "https://x/p501": (_png(), 200),
              "https://x/p503": (_png(), 200)}
    browser = win._open_browser()
    browser._fetcher = FakeFetcher(search)
    browser.browse("1girl")
    assert 503 in browser._thumbs and 501 not in browser._thumbs
    print("OK: deleted wiki examples display, are labelled, and open "
          "— matching what their post-id chip always did")


def test_alias_lookup_shows_a_redirect_banner() -> None:
    """FIELD REPORT: looking up an alias silently bounced to the
    canonical tag's page. The redirect was noted only in muted header
    metadata, so users thought the tool had opened the wrong page. It
    now shows a distinct accent banner naming both the alias typed and
    the canonical landed on, only on an alias lookup.

    Driven by stubbing tag_reference.lookup so the test does not depend
    on the tag database being present; visibility is checked with
    isVisibleTo (reliable regardless of whether the window is mapped).
    """
    import core.tag_reference as tr
    from core.tag_reference import EraCount, ReferenceInfo

    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=FakeFetcher({}))
    win.show()
    pane = win._pane

    def info(query, canonical, redirected_from):
        i = ReferenceInfo(
            query=query, canonical=canonical,
            redirected_from=redirected_from,
            category_id=None, category_name="general")
        i.era_counts = [EraCount(
            key=pane._host._selected_era_key(), label="Era", count=100)]
        i.verdict = "stable"
        i.aliases = []
        i.is_custom = False
        return i

    real = tr.lookup
    try:
        # An alias lookup: banner visible, names both directions.
        tr.lookup = lambda tag, era=None: info("heels", "high_heels",
                                               "heels")
        pane._load("heels")
        assert pane.redirect_banner.isVisibleTo(pane), (
            "alias lookup did not show the redirect banner")
        text = pane.redirect_banner.text()
        assert "heels" in text and "high_heels" in text, text
        assert "alias" in text
        # And the muted "redirected from" is no longer duplicated in the
        # header (one clear notice, not two).
        assert "redirected from" not in pane.header.text()

        # A direct (non-alias) lookup: banner hidden.
        tr.lookup = lambda tag, era=None: info("high_heels",
                                               "high_heels", None)
        pane._load("high_heels")
        assert not pane.redirect_banner.isVisibleTo(pane), (
            "a direct lookup should not show the banner")

        # Banner must not linger: alias, then a direct lookup clears it.
        tr.lookup = lambda tag, era=None: info("heels", "high_heels",
                                               "heels")
        pane._load("heels")
        assert pane.redirect_banner.isVisibleTo(pane)
        tr.lookup = lambda tag, era=None: info("cat", "cat", None)
        pane._load("cat")
        assert not pane.redirect_banner.isVisibleTo(pane), (
            "the banner lingered after a later direct lookup")
    finally:
        tr.lookup = real
    print("OK: an alias lookup shows a distinct redirect banner naming "
          "both tags, a direct lookup shows none, and it never lingers")


def run() -> None:
    test_settings_env_isolation_regression()
    test_preferences_group_and_cache_controls()
    test_master_off_means_zero_network()
    test_doc_render_and_text_cache()
    test_images_hover_reveal_and_caches()
    test_batch_rejected_falls_back_per_id()
    test_custom_tag_similarity_path()
    test_inspector_is_live_and_read_only()
    test_window_affordances_and_cooccurrence_display()
    test_field_reported_embed_spellings_reach_the_gallery()
    test_examples_reachable_when_thumbnail_fetch_fails()
    test_partial_results_and_images_off_paths()
    test_always_on_top_keeps_the_close_button()
    test_bullet_style_examples_and_source_view()
    test_post_metadata_and_display_vocabulary()
    test_reference_window_folding_ratings_and_verdict_colour()
    test_inspector_shows_credits_and_grouped_tags()
    test_reveal_by_default_preference()
    test_inspector_copy_modes_and_export()
    test_tag_referencer_button_and_link_handling()
    test_export_original_resolution_and_folder_button()
    test_new_tag_is_not_mislabelled_custom()
    test_fresh_on_restart_and_full_reset()
    test_pane_extraction_holds_together()
    test_compare_mode()
    test_compare_mode_survives_the_rest_of_the_program()
    test_pane_refactor_regressions()
    test_every_thumbnail_click_does_something()
    test_deleted_posts_are_shown_not_refused()
    test_blacklist_blocks_before_the_network()
    test_blacklist_preferences_and_reset_to_defaults()
    test_appearance_assets_render()
    test_video_posts_offer_download()
    test_media_view_sizing_and_animation()
    test_fit_is_deferred_until_the_widget_has_a_size()
    test_inspector_shows_animation_and_keeps_video_stills()
    test_alias_lookup_shows_a_redirect_banner()
    print("\nALL PASS: tag reference (online wave)")


if __name__ == "__main__":
    run()
