"""
tests/test_dtext.py

Tests for the DText parser/renderer (core/dtext.py) — the wiki-body
half of the Tag Reference's online layer (DESIGN_TAG_REFERENCE.md).

REALISTIC_BODY mirrors the field screenshot of the high_heels wiki
page: prose with [[links]], an h4 Examples section subdivided by h6
groups of staff-picked `!post #` embeds, See also bullets, external
links, and the italic alias footer. Per the spec's testing approach,
this fixture doubles as the canned wiki body for window-level tests.

Run: python3 tests/test_dtext.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from core import dtext as dt

REALISTIC_BODY = """Footwear which raises the heels of the wearer's [[feet]] significantly higher than the toes. High heels today are mostly associated with female shoe styles.

High heels can be a feature of [[shoes]], [[boots]], [[sandals]], or other footwear. To tag the color, use tags like [[red shoes]], [[red boots]], or [[red sandals]].

The opposite of high heels are [[flats]].

h4. Examples

h6. Types

!post #100 !post #101
!post #102

h6. Designs

!post #200 !post #201 !post #202

h4. See also

* [[flats]]
* [[shoes]]
* [[Tag group:Attire]]

h4. External links

* "Wikipedia: High-heeled shoe":https://en.wikipedia.org/wiki/High-heeled_shoe

[i]The following tags are aliased to this tag: [[heels]].[/i]
"""


def test_document_order_and_galleries() -> None:
    blocks = dt.parse_dtext(REALISTIC_BODY)
    kinds = [type(b).__name__ for b in blocks]
    # External links are kept (as copy links), so their section
    # survives along with everything else.
    assert kinds == ["Para", "Para", "Para", "Heading", "Heading",
                     "Gallery", "Heading", "Gallery", "Heading",
                     "Bullets", "Heading", "Bullets", "Para"], kinds
    gals = [b for b in blocks if isinstance(b, dt.Gallery)]
    assert gals[0].post_ids == [100, 101, 102]   # rows merged
    assert gals[1].post_ids == [200, 201, 202]
    assert dt.extract_post_ids(blocks) == [100, 101, 102,
                                           200, 201, 202]
    heads = [(b.level, b.text) for b in blocks
             if isinstance(b, dt.Heading)]
    assert heads == [(4, "Examples"), (6, "Types"), (6, "Designs"),
                     (4, "See also"), (4, "External links")]
    print("OK: the page parses in document order — prose, grouped "
          "example galleries (embed rows merged), bullets, footer")


def test_wiki_links_render_and_extract() -> None:
    blocks = dt.parse_dtext(REALISTIC_BODY)
    assert '<a href="tag:feet"' in blocks[0].html
    assert 'href="tag:red_shoes"' in blocks[1].html    # target folds
    assert ">red shoes</a>" in blocks[1].html          # label keeps space
    assert '>thigh highs</a>' in dt.render_inline(
        "[[thighhighs|thigh highs]]")
    links = dt.extract_wiki_links(REALISTIC_BODY)
    assert links[0] == ("feet", "feet")                # doc order
    assert sum(1 for t, _ in links if t == "flats") == 1   # deduped
    assert ("tag_group:attire", "Tag group:Attire") in links
    print("OK: [[links]] render on the app's tag: scheme, targets "
          "folded, labels preserved; editorial link extraction keeps "
          "order and dedupes")


def test_external_links_are_copy_links() -> None:
    """Revised field decision: external links are KEPT and clickable,
    but clicking copies the address rather than opening a browser.
    Stripping them threw away the only content on pages that are
    nothing but an artist's social links.

    Regression: the danbooru_(site) page rendered "Pixiv:" / "GitHub:"
    with the links gone, because the label was removed but the prose
    around it stayed."""
    site = ('h4. External links\n'
            '* Pixiv: "Danbooru":[https://dic.pixiv.net/a/Danbooru]\n'
            '* GitHub: "danbooru":[https://github.com/danbooru/danbooru]\n')
    html = dt.blocks_to_html(dt.parse_dtext(site))
    assert 'href="copy:https%3A%2F%2Fdic.pixiv.net%2Fa%2FDanbooru"' in html
    assert ">Danbooru</a>" in html and "Pixiv:" in html
    assert "External links" in html
    assert 'href="http' not in html         # never opens a browser

    bare = dt.render_inline("see https://example.com/x now")
    assert 'href="copy:https%3A%2F%2Fexample.com%2Fx"' in bare
    # Site-relative targets resolve against Danbooru.
    rel = dt.render_inline('"(Guide)":[/media_assets/7734352]')
    assert "copy:https%3A%2F%2Fdanbooru.donmai.us%2Fmedia_assets" in rel
    # Escaping still happens around the links.
    assert "&amp;" in dt.render_inline("Tom & Jerry")
    print("OK: external links render as copy-to-clipboard links, "
          "site-relative targets resolved, nothing opens a browser")


def test_table_of_contents_anchors() -> None:
    """Long pages (tag groups especially) open with a collapsible
    contents box whose entries are in-page anchors. The box title
    becomes a heading, and each entry links to the heading it names."""
    body = ('[expand=Table of Contents]\n'
            '* 1. "View Angle":#dtext-angle\n'
            '* 2. "Composition":#dtext-composition\n'
            '[/expand]\n\n'
            'h4#angle. View Angle\n\n'
            '* !post #9330077: [[Dutch angle]]\n\n'
            'h4#composition. Composition\n\n'
            '* [[Afterimage]]\n')
    blocks = dt.parse_dtext(body)
    heads = [(b.text, b.anchor) for b in blocks
             if isinstance(b, dt.Heading)]
    assert ("Table of Contents", "") in heads   # expand title kept
    assert ("View Angle", "angle") in heads     # anchor captured
    assert ("Composition", "composition") in heads
    toc = "".join(i for b in blocks if isinstance(b, dt.Bullets)
                  for i in b.items)
    # `#dtext-angle` in the link, `#angle` on the heading.
    assert 'href="anchor:angle"' in toc
    assert 'href="anchor:composition"' in toc
    assert ">View Angle</a>" in toc
    assert dt.extract_post_ids(blocks) == [9330077]
    print("OK: contents boxes keep their title and their entries jump "
          "to the headings they name")


def test_tolerant_post_embeds() -> None:
    """FIELD BUG: embeds were surviving as literal text because the
    pattern was strict. Every real spelling must produce a gallery."""
    for variant in ["!post #123", "!Post #123", "!POST #123",
                    "!post#123", "! post # 123", "!post  #123"]:
        blocks = dt.parse_dtext(variant)
        assert len(blocks) == 1, (variant, blocks)
        assert isinstance(blocks[0], dt.Gallery), variant
        assert blocks[0].post_ids == [123], variant
    print("OK: post embeds parse regardless of case or spacing "
          "(the reported missing-images bug)")


def test_bare_post_references_are_not_always_examples() -> None:
    """Wiki prose uses bare `post #N` for COUNTER-examples ("what
    doesn't belong includes post #123"), so a bare reference only
    becomes gallery content when its paragraph is nothing but
    references. Explicit !embeds always count."""
    only_refs = dt.parse_dtext("post #1\npost #2")
    assert isinstance(only_refs[0], dt.Gallery)
    assert only_refs[0].post_ids == [1, 2]

    prose = dt.parse_dtext(
        "Examples of what don't belong include post #3901837.")
    assert len(prose) == 1 and isinstance(prose[0], dt.Para)
    assert "post #3901837" in prose[0].html
    assert dt.extract_post_ids(prose) == []      # not an example

    mixed = dt.parse_dtext("Compare with !post #55 closely.")
    assert isinstance(mixed[0], dt.Para)
    assert "Compare with" in mixed[0].html
    assert isinstance(mixed[1], dt.Gallery)
    assert mixed[1].post_ids == [55]
    print("OK: bare post references stay inline in prose (they are "
          "often counter-examples); explicit embeds always become "
          "gallery entries, splitting out of mixed paragraphs")


def test_search_links() -> None:
    """FIELD BUG: `{{pool:Juicy_Details}}` rendered as raw braces on
    breast_focus / close-up. A search link is not a wiki link."""
    pool = dt.render_inline("Collection: {{pool:Juicy_Details}}")
    assert "Juicy Details (pool)" in pool
    assert "{{" not in pool and "<a" not in pool   # not navigable
    tag = dt.render_inline("See {{blue_hair}} for more")
    assert 'href="tag:blue_hair"' in tag
    assert ">blue hair</a>" in tag
    multi = dt.render_inline("{{blue_hair long_hair}}")
    assert "{{" not in multi and "<a" not in multi
    # Searches are not editorial wiki links.
    assert dt.extract_wiki_links("{{pool:x}} [[real_tag]]") == \
        [("real_tag", "real_tag")]
    print("OK: {{search}} links render readably — plain tags "
          "navigable, pool/metatag searches as plain text, never raw "
          "braces")


def test_degradation_paths() -> None:
    assert "secret" in dt.render_inline("[spoiler]secret[/spoiler]")
    assert "aliased to this tag" in dt.parse_dtext(
        REALISTIC_BODY)[-1].html               # [i] stripped
    assert dt.parse_dtext("") == []
    assert dt.parse_dtext("[[[]]]] [b **") is not None   # never raises
    print("OK: containers strip to their text, mixed embeds degrade "
          "to chips, garbage never raises")


def test_preview_html() -> None:
    html = dt.blocks_to_html(dt.parse_dtext(REALISTIC_BODY))
    assert "<h4>" in html and "<ul>" in html
    assert "[3 example(s)]" in html            # gallery placeholder
    print("OK: text-only preview renders headings, lists and gallery "
          "placeholders")


def test_post_references_are_clickable_everywhere() -> None:
    """FIELD BUG: examples never appeared — no image, no link, no
    error. Cause: bullet items bypassed post-reference handling
    entirely, so an Examples section written as `* !post #123` stayed
    literal text. Now every context renders a chip, and a chip needs
    no network to appear."""
    prose = dt.parse_dtext(
        "Counter-examples include post #3901837 and others.")
    assert 'href="post:3901837"' in prose[0].html
    assert "include " in prose[0].html          # spacing intact
    assert dt.extract_post_ids(prose) == []     # still not examples

    bullet = dt.parse_dtext("* See post #55 for framing\n")
    items = [b for b in bullet if isinstance(b, dt.Bullets)][0].items
    assert 'href="post:55"' in items[0]
    assert "for framing" in items[0]

    heading = dt.parse_dtext("h4. See post #9\n\nbody\n")
    assert 'href="post:9"' in heading[0].html

    once = dt.render_inline("post #12 post #12")
    assert once.count('href="post:12"') == 2    # no double-linking
    assert "post:post" not in once
    print("OK: post references render as clickable chips in prose, "
          "bullets and headings alike — reachable with no network")


def test_bullet_style_example_lists_become_galleries() -> None:
    """An Examples section written as a bullet list is an example
    list, not prose: it should get thumbnails, not bullet points."""
    embeds = dt.parse_dtext("h4. Examples\n\n* !post #100\n"
                            "* !post #101\n* !post #102\n")
    assert dt.extract_post_ids(embeds) == [100, 101, 102]
    assert any(isinstance(b, dt.Gallery) for b in embeds)

    bare = dt.parse_dtext("h4. Examples\n\n* post #7\n* post #8\n")
    assert dt.extract_post_ids(bare) == [7, 8]

    # A bullet with real prose around the reference stays a bullet.
    mixed = dt.parse_dtext("* See post #55 for framing\n")
    assert any(isinstance(b, dt.Bullets) for b in mixed)
    assert dt.extract_post_ids(mixed) == []
    print("OK: bullet-style example lists route to galleries, while "
          "prose bullets stay bullets")


def test_real_labelled_example_bullets() -> None:
    """THE field format, from the high_heels source: every example is
    a LABELLED bullet, `* !post #N: [[Label]]`. An earlier build only
    recognised bullets that were nothing but a reference, so all
    fifteen examples fell through to plain text — chips worked, but no
    gallery and no thumbnails were ever built."""
    body = ("h4. Examples\n\nh6. Types\n\n"
            "* !post #4229961: [[Pumps]]\n"
            "* !post #8936228: [[High heel boots]]\n\n"
            "h6. Designs\n\n"
            "* !post #8819495: [[Block heels]]\n"
            "* !post #5267634: [[Christian Louboutin (brand)|]]\n"
            "* !asset #24478216: [[D'orsay heels]]\n"
            "* !post #7326896: [[Kitten heels]]\n")
    blocks = dt.parse_dtext(body)
    gals = [b for b in blocks if isinstance(b, dt.Gallery)]
    assert len(gals) == 2                       # one per heading group
    assert gals[0].post_ids == [4229961, 8936228]
    # An unfetchable !asset entry must not fragment the grid.
    assert gals[1].post_ids == [8819495, 5267634, 7326896]
    assert gals[1].asset_ids == [24478216]
    assert dt.extract_post_ids(blocks) == [
        4229961, 8936228, 8819495, 5267634, 7326896]

    # The labels ARE the taxonomy, and stay navigable.
    assert 'href="tag:pumps"' in gals[0].captions[4229961]
    assert ">Pumps</a>" in gals[0].captions[4229961]
    assert 'href="tag:kitten_heels"' in gals[1].captions[7326896]

    # The !asset entry is fetched as a media asset now, so it joins
    # the same gallery rather than degrading to text.
    assert 24478216 in gals[1].asset_ids
    print("OK: labelled example bullets build captioned galleries, "
          "with !asset entries fetched alongside the posts")


def test_media_asset_embeds() -> None:
    """FIELD BUG: character pages' "Appearance" sections rendered
    empty. They embed MEDIA ASSETS (`!asset #N`), which live at a
    different endpoint from posts — the parser was turning them into
    inert text, so nothing was ever fetched."""
    body = ("h4. Appearance\n\n[b]Default[/b]\n"
            "* !asset #111: [[Default outfit]]\n"
            "* !asset #222: [[Swimsuit]]\n\n"
            "h4. Examples\n\n* !post #333: [[Pose]]\n")
    blocks = dt.parse_dtext(body)
    gals = [b for b in blocks if isinstance(b, dt.Gallery)]
    assert gals[0].asset_ids == [111, 222]
    assert gals[0].post_ids == []
    # Captions are keyed negatively so posts and assets can share one
    # mapping without colliding.
    assert "Default outfit" in gals[0].captions[-111]
    assert gals[1].post_ids == [333]

    bare = dt.parse_dtext("h4. Appearance\n\n!asset #99\n")
    assert any(isinstance(b, dt.Gallery) and b.asset_ids == [99]
               for b in bare)
    # A group may hold both kinds at once.
    mixed = dt.parse_dtext("* !asset #5: [[A]]\n* !post #6: [[B]]\n")
    g = [b for b in mixed if isinstance(b, dt.Gallery)][0]
    assert g.asset_ids == [5] and g.post_ids == [6]
    print("OK: media-asset embeds are parsed into galleries so "
          "Appearance sections render instead of coming up empty")


def run() -> None:
    test_document_order_and_galleries()
    test_wiki_links_render_and_extract()
    test_external_links_are_copy_links()
    test_table_of_contents_anchors()
    test_tolerant_post_embeds()
    test_bare_post_references_are_not_always_examples()
    test_search_links()
    test_post_references_are_clickable_everywhere()
    test_bullet_style_example_lists_become_galleries()
    test_real_labelled_example_bullets()
    test_media_asset_embeds()
    test_degradation_paths()
    test_preview_html()
    print("\nALL PASS: dtext parser/renderer")


if __name__ == "__main__":
    run()
