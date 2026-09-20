"""
tests/test_discovery.py

Tests for core/discovery.py and the Discover button.

The feature is one press with everything else in Preferences, so the
tests are mostly about the filters actually biting — a Discover button
that quietly ignores its settings would look like it worked.

One of those silent failures is pinned below as a regression: the
skip-owned filter read `state.all_tags` as a method when it is a
property, and a broad `except` turned the resulting TypeError into
"no tags matched". The filter did nothing and said nothing.

Run: python3 tests/test_discovery.py
"""
import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from PIL import Image
from PySide6.QtWidgets import QApplication, QToolButton

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import discovery as dsc
from core.scanner import scan
from core.state import SessionState
from ui.tag_reference_window import COOC_HELP_PAGES, TagReferenceWindow

ERA = "mid2024"


class _NoNetwork:
    def get(self, url, cb):
        cb(None, 404)


def _fresh_settings() -> Settings:
    os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()
    s = Settings()
    initialize_theme(s.theme)
    return s


def _doc_text(win) -> str:
    from PySide6.QtWidgets import QLabel
    return "".join(l.text() for l in win.doc_host.findChildren(QLabel))


def test_pool_filters() -> None:
    general = dsc.pool(ERA, "general", 500)
    everything = dsc.pool(ERA, "all", 500)
    assert len(general) > 100
    assert len(general) < len(everything)      # general is a subset

    # The minimum count is the setting that decides whether this is
    # useful or noise: a random tag from the whole database is almost
    # always a one-off name.
    strict = dsc.pool(ERA, "general", 100_000)
    assert 0 < len(strict) < len(general)

    # Built once and kept: rebuilding would rescan the whole snapshot
    # for one answer.
    assert dsc.pool(ERA, "general", 500) is general

    # Nothing here may raise on bad input.
    assert dsc.pool("no_such_era") == []
    assert dsc.pick("no_such_era") is None
    assert dsc.pick(ERA, "general", 99_999_999) is None
    print("OK: pools respect scope and minimum count, are cached, and "
          "degrade quietly on impossible filters")


def test_no_repeat_and_skip_owned() -> None:
    rng = random.Random(7)
    seen: set[str] = set()
    picked = []
    for _ in range(25):
        tag = dsc.pick(ERA, "general", 50_000, seen=seen, rng=rng)
        assert tag is not None
        seen.add(tag.lower())
        picked.append(tag)
    assert len(set(picked)) == 25

    owned = set(dsc.pool(ERA, "general", 100_000))
    assert dsc.pick(ERA, "general", 100_000, owned=owned,
                    skip_owned=True, rng=rng) is None
    # ...and the same call without the filter still answers.
    assert dsc.pick(ERA, "general", 100_000, owned=owned,
                    skip_owned=False, rng=rng) is not None
    print("OK: no-repeat yields distinct tags and skip-owned excludes "
          "what the dataset already has")


def test_button_is_one_press_with_controls_in_preferences() -> None:
    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=_NoNetwork())
    win.show()

    assert isinstance(win.btn_discover, QToolButton)
    # Everything configurable lives in Preferences; the window keeps
    # one button.
    for attr in ("disc_scope", "disc_min", "chk_no_repeat",
                 "spin_min_count"):
        assert not hasattr(win, attr)
    tip = win.btn_discover.toolTip()
    assert "tags match" in tip and "Settings" in tip

    win.discover()
    first = win.search.text()
    assert first
    assert first.lower() in win._discovered

    got = set()
    for _ in range(30):
        win.discover()
        got.add(win.search.text())
    assert len(got) == 30                     # no repeats in a session

    # Preferences drive it.
    s.discovery_scope = "all"
    s.discovery_min_count = 100_000
    win._discovered.clear()
    win.discover()
    assert win.search.text() in set(dsc.pool(ERA, "all", 100_000))
    print("OK: one press picks a tag and looks it up, honouring the "
          "scope and threshold set in Preferences")


def test_skip_owned_actually_bites() -> None:
    """REGRESSION: `state.all_tags` is a property, and reading it as a
    method raised inside a broad except, so this filter silently did
    nothing while appearing to work."""
    s = _fresh_settings()
    pool = dsc.pool(ERA, "all", 100_000)
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(folder / "a.png")
    (folder / "a.txt").write_text(", ".join(sorted(pool)),
                                  encoding="utf-8")

    win = TagReferenceWindow(s, fetcher=_NoNetwork())
    win.show()
    win.set_state(SessionState(scan(folder)))
    s.discovery_scope = "all"
    s.discovery_min_count = 100_000
    s.discovery_skip_owned = True

    win.discover()
    text = _doc_text(win)
    assert "Discover found nothing" in text
    # And it names the settings that would loosen the filters, rather
    # than leaving the user to guess.
    assert "minimum post count" in text
    assert "skip tags already" in text

    # With only half the pool owned, the other half is still offered.
    half = sorted(pool)[:200]
    (folder / "a.txt").write_text(", ".join(half), encoding="utf-8")
    win.set_state(SessionState(scan(folder)))
    win._discovered.clear()
    win.discover()
    assert win.search.text() in set(pool) - set(half)
    print("OK: skip-owned genuinely excludes owned tags, offers the "
          "remainder, and explains itself when nothing is left")


def test_cooccurrence_help_is_a_booklet() -> None:
    s = _fresh_settings()
    win = TagReferenceWindow(s, fetcher=_NoNetwork())
    win.show()
    win._show_cooc_help()
    assert win._cooc_help_win.__class__.__name__ == "PagedHelpDialog"
    pages = "\n".join(body for _t, body in COOC_HELP_PAGES)
    assert "not your dataset" in pages
    assert "in your set" in pages
    assert "1girl" in pages and "STRENGTH" in pages
    # A short tooltip stays as a pointer to the booklet.
    assert len(win.cooc_help.toolTip()) < 90
    win._cooc_help_win.close()
    print("OK: the co-occurrence explanation is a booklet, with only a "
          "pointer left in the tooltip")


def test_discover_never_offers_a_blocked_tag() -> None:
    """AUDIT FIND: Discover did not consult the blacklist, so it
    handed out tags at exactly the blacklist's coverage rate that the
    window then blanked with "blocked by your blacklist" — a wasted
    press rather than a discovery. Measured at 21 dead ends in 200
    with a tenth of the pool blocked."""
    from core import blacklist as bl

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    pool = dsc.pool(ERA, "general", 500)
    s.danbooru_block_custom = "\n".join(pool[:len(pool) // 10])
    rules = bl.build_rules(s.danbooru_block_custom)

    win = TagReferenceWindow(s, fetcher=_NoNetwork())
    win.show()
    dead_ends = 0
    for _ in range(200):
        win.discover()
        if bl.blocked_tag_reason(win.search.text(), rules):
            dead_ends += 1
    assert dead_ends == 0

    # The predicate is part of the picker, so it is testable without
    # a window too.
    blocked = set(pool[:50])
    for _ in range(50):
        got = dsc.pick(ERA, "general", 500,
                       blocked=lambda t: t in blocked)
        assert got not in blocked
    print("OK: Discover consults the blacklist, so a press can never "
          "land on a tag the window would refuse to show")


def test_full_reset_clears_the_discovery_history() -> None:
    """AUDIT FIND: a reset means "as if freshly started", but the
    no-repeat history survived it, so Discover kept refusing tags it
    had shown in a session the user had already thrown away."""
    from PySide6.QtWidgets import QMessageBox
    from core.scanner import scan as _scan
    from ui.main_window import MainWindow

    QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes
    QMessageBox.information = lambda *a, **k: None

    s = _fresh_settings()
    s.danbooru_lookups_enabled = True
    mw = MainWindow(s)
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(folder / "a.png")
    (folder / "a.txt").write_text("1girl", encoding="utf-8")
    mw._adopt_state(SessionState(_scan(folder)), scan_root=None)
    mw._action_tag_reference()
    mw._tag_ref_win._fetcher = _NoNetwork()
    for _ in range(5):
        mw._tag_ref_win.discover()
    assert len(mw._tag_ref_win._discovered) == 5

    mw._tag_ref_win._bookmarks.add("tag", "keep_me")
    mw._action_full_reset()
    assert mw._tag_ref_win._discovered == set()
    # Bookmarks are user data, not session state, and must survive.
    assert mw._tag_ref_win._bookmarks.contains("tag", "keep_me")
    print("OK: Full reset forgets what Discover has offered, while "
          "leaving bookmarks alone")


def run() -> None:
    test_pool_filters()
    test_no_repeat_and_skip_owned()
    test_button_is_one_press_with_controls_in_preferences()
    test_skip_owned_actually_bites()
    test_discover_never_offers_a_blocked_tag()
    test_full_reset_clears_the_discovery_history()
    test_cooccurrence_help_is_a_booklet()
    print("\nALL PASS: discovery")


if __name__ == "__main__":
    run()
