"""
tests/test_blacklist.py

Tests for core/blacklist.py — content blocking for images fetched from
Danbooru.

Danbooru ships every new account with `guro scat furry -rating:g`
blacklisted, because the site carries material most people never want
to meet by accident. TagWalker fetches Danbooru images, so it needs
the same protection.

Two properties matter most here and are pinned below:
- the shipped list is visible and editable, and nothing is
  enforced that it does not name;
- the rules apply ONLY to fetched Danbooru posts. The user's own
  dataset is never filtered, which is why nothing in this module
  touches SessionState.

Run: python3 tests/test_blacklist.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from core import blacklist as bl


def test_shipped_defaults() -> None:
    """The list ships pre-filled the way Danbooru pre-fills a new
    account's, and it is fully editable — one visible list beats
    hidden rules plus switches."""
    rules = bl.build_rules(bl.DEFAULT_BLACKLIST)
    assert bl.blocked_reason("1girl guro blood", "q", rules) == "guro"
    assert bl.blocked_reason("1girl scat", "e", rules) == "scat"
    assert bl.blocked_reason("mutilation", "g", rules) == "mutilation"
    # Danbooru's own furry rule: blocked unless rated general.
    assert bl.blocked_reason("furry", "e", rules) == "furry -rating:g"
    assert bl.blocked_reason("furry", "g", rules) is None
    assert bl.blocked_reason("1girl long_hair smile", "g", rules) is None
    print("OK: the shipped blacklist blocks gore, scat and "
          "non-general furry out of the box")


def test_the_list_is_the_whole_truth() -> None:
    """Nothing is enforced behind the user's back: an empty list
    blocks nothing at all. That is the point of showing the rules
    instead of hiding them."""
    assert bl.build_rules("") == []
    assert bl.blocked_reason("guro", "e", bl.build_rules("")) is None
    only_zombie = bl.build_rules("zombie")
    assert bl.blocked_reason("guro", "e", only_zombie) is None
    assert bl.blocked_reason("zombie", "e", only_zombie) == "zombie"
    print("OK: the blacklist is the whole truth — nothing is filtered "
          "that the list does not name")


def test_rule_syntax_is_and_not_or() -> None:
    rules = bl.build_rules("loli rating:e")
    assert bl.blocked_reason("loli 1girl", "e", rules) == "loli rating:e"
    assert bl.blocked_reason("loli 1girl", "g", rules) is None  # AND
    assert bl.blocked_reason("1girl", "e", rules) is None

    negated = bl.build_rules("furry -solo")
    assert bl.blocked_reason("furry", "e", negated) == "furry -solo"
    assert bl.blocked_reason("furry solo", "e", negated) is None

    furry = bl.build_rules("furry -rating:g")
    assert bl.blocked_reason("furry", "e", furry) == "furry -rating:g"
    assert bl.blocked_reason("furry", "g", furry) is None
    print("OK: a rule line is an AND of its terms, `-` inverts, and "
          "rating tests work in both directions")


def test_parsing_details() -> None:
    rules = bl.build_rules("zombie\n# comment\n\n  \nx rating:explicit")
    sources = [r.source for r in rules]
    assert "zombie" in sources
    assert "# comment" not in sources and "" not in sources
    # Full rating names and single letters are interchangeable.
    assert bl.blocked_reason("x", "e", rules) == "x rating:explicit"
    assert bl.blocked_reason("x", "explicit", rules) == "x rating:explicit"
    # Case and spacing folded on both sides.
    cased = bl.build_rules("LONG_HAIR")
    assert bl.blocked_reason("Long_Hair 1girl", "g", cased)
    # A metatag we cannot evaluate is dropped, but the rest of the
    # line still applies — dropping the whole line would silently
    # under-block.
    partial = bl.build_rules("zombie score:>100")
    assert bl.blocked_reason("zombie", "g", partial) == "zombie score:>100"
    print("OK: comments and blank lines ignored, ratings and case "
          "normalised, unevaluatable metatags dropped without "
          "discarding their line")


def test_empty_and_missing_inputs() -> None:
    assert bl.blocked_reason("guro", "e", []) is None    # no rules
    assert bl.build_rules("") == bl.build_rules("  \n\n")
    rules = bl.build_rules(bl.DEFAULT_BLACKLIST)
    assert bl.blocked_reason("", "", rules) is None
    assert bl.blocked_reason(None, None, rules) is None
    print("OK: empty and missing inputs are handled without raising")


def test_blocked_tags_are_unreachable_not_just_unshown() -> None:
    """A line naming a single tag means "not this subject at all", so
    the tag's wiki page should not open either — hiding the pictures
    while still describing the subject is not what the user asked for.

    A CONDITIONAL line is different: `furry -rating:g` says which
    posts to hide and nothing about whether furry may be read about,
    so it leaves the page reachable."""
    rules = bl.build_rules(bl.DEFAULT_BLACKLIST)
    assert bl.blocked_tag_reason("scat", rules) == "scat"
    assert bl.blocked_tag_reason("guro", rules) == "guro"
    assert bl.blocked_tag_reason("furry", rules) is None    # conditional
    assert bl.blocked_tag_reason("long_hair", rules) is None
    # Folding applies here too.
    assert bl.blocked_tag_reason("Scat", rules) == "scat"
    # A multi-tag rule is conditional and blocks neither tag's page.
    pair = bl.build_rules("zombie blood")
    assert bl.blocked_tag_reason("zombie", pair) is None
    print("OK: a bare tag on a line makes that tag unreachable; "
          "conditional rules only hide posts")


def test_animated_is_no_longer_blocked() -> None:
    """Superseded: `animated` and `animated_gif` used to ship blocked
    because the app cannot play video. Video posts are now offered as
    a download instead, which is more useful than hiding them, so the
    tags are ordinary again."""
    rules = bl.build_rules(bl.DEFAULT_BLACKLIST)
    assert bl.blocked_reason("1girl animated", "g", rules) is None
    assert bl.blocked_reason("1girl animated_gif", "g", rules) is None
    assert bl.blocked_tag_reason("animated", rules) is None
    # The content rules are untouched by that change.
    assert bl.blocked_reason("guro", "e", rules) == "guro"
    print("OK: animated formats are reachable again, handled by the "
          "video download path rather than by blocking")


def test_colon_tags_are_blockable() -> None:
    """AUDIT FIND: any colon meant "metatag", so `:d` and `:o` — real,
    common tags — parsed as a metatag with an empty name, were
    dropped, and left an empty rule that was discarded. A blacklist
    line naming one silently did nothing.

    That is exactly the failure this design set out to rule out: the
    list is meant to be the whole truth, so a line must either work or
    be visibly wrong. A term is a metatag only when the part before
    its colon is a known metatag name."""
    for tag in (":d", ":o", ":3", ";)",
                "re:zero_kara_hajimeru_isekai_seikatsu"):
        rules = bl.build_rules(tag)
        assert len(rules) == 1, tag
        assert bl.blocked_reason(tag, "g", rules) == tag, tag
        assert bl.blocked_tag_reason(tag, rules) == tag, tag

    # Real metatags are untouched.
    furry = bl.build_rules("furry -rating:g")
    assert bl.blocked_reason("furry", "e", furry) == "furry -rating:g"
    assert bl.blocked_reason("furry", "g", furry) is None
    # One we cannot evaluate is still dropped, and its line still
    # applies rather than being silently discarded whole.
    partial = bl.build_rules("zombie score:>100")
    assert bl.blocked_reason("zombie", "g", partial) == "zombie score:>100"
    assert bl.build_rules("score:>100") == []
    print("OK: tags containing a colon are blockable; only known "
          "metatag names are treated as metatags")


def test_aliases_are_not_a_way_around_the_list() -> None:
    """AUDIT FIND, reached through a flaky test. The window checked
    the blacklist against the name that was TYPED, then resolved
    aliases afterwards — so every blocked tag stayed reachable under
    any of its other spellings, with its counts, verdict, wiki and
    examples all rendered. Danbooru has thousands of aliases.

    Driven at the window rather than here in the core, because the
    hole was in the order of operations, not in the matching."""
    import os
    import tempfile

    os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication.instance() or QApplication([])
    from config.settings import Settings
    from config.theme import initialize_theme
    from ui.tag_reference_window import TagReferenceWindow

    class _NoNetwork:
        def get(self, url, cb):
            cb(None, 404)

    def document(win) -> str:
        app.processEvents()
        # deleteLater posts a DeferredDelete event that processEvents
        # does not flush outside a running loop, so a stale label
        # would otherwise still be found here.
        QCoreApplication.sendPostedEvents(
            None, QEvent.Type.DeferredDelete)
        return "".join(l.text() for l in win.doc_host.findChildren(QLabel))

    s = Settings()
    initialize_theme(s.theme)
    s.danbooru_lookups_enabled = True
    s.danbooru_block_custom = "1girl"          # block the canonical
    win = TagReferenceWindow(s, fetcher=_NoNetwork())
    win.show()

    # sole_female is a Danbooru alias of 1girl.
    win.navigate("sole_female")
    text = document(win)
    assert "blacklist rule" in text
    assert "1girl" in text                     # names what it refused
    assert not win.era_label.text()            # nothing leaks
    assert win.cooc_list.count() == 0

    win.navigate("long_hair")
    assert "blacklist rule" not in document(win)
    win.navigate("sole_female")
    assert "blacklist rule" in document(win)
    print("OK: a blacklisted tag cannot be reached through one of its "
          "aliases")


def run() -> None:
    test_shipped_defaults()
    test_the_list_is_the_whole_truth()
    test_rule_syntax_is_and_not_or()
    test_parsing_details()
    test_empty_and_missing_inputs()
    test_blocked_tags_are_unreachable_not_just_unshown()
    test_animated_is_no_longer_blocked()
    test_colon_tags_are_blockable()
    test_aliases_are_not_a_way_around_the_list()
    print("\nALL PASS: blacklist")


if __name__ == "__main__":
    run()
