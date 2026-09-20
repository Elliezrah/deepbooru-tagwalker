"""Tests for queue-search negation and space/underscore normalization.

Field report: after finishing a large_breasts walk, the user selected
medium_breasts and typed the other tag into the queue search box
expecting the Has/Missing switch to key on it. The modes key on the
CURRENT tag only, so the intersection showed large-tagged images under
"Without a Tag" (they lacked medium) — mechanically consistent,
humanly baffling — and "images without tag Y" was impossible to
express at all. Also, a space where the tag has an underscore
("large breasts") silently matched nothing.

Now: '-tag' negates (both substring and exact/comma modes), and spaces
match underscores in tag comparisons. This file pins the replay, the
fix, and the edges.

Run: python3 tests/test_queue_search_negation.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from core.state import SessionState, FilterMode
from core.scanner import scan


def _state():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for n, t in {
        "img_large_a": "1girl, large_breasts",
        "img_large_b": "1girl, large_breasts",
        "img_both":    "1girl, large_breasts, medium_breasts",
        "img_medium":  "1girl, medium_breasts",
        "img_neither": "1girl, flat_chest",
    }.items():
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(t, encoding="utf-8")
    st = SessionState(scan(d))
    st.select_tag("medium_breasts")
    return st


def _q(st):
    return sorted(i.image_path.stem for i in st.get_current_queue())


def test_field_replay_positive_search_is_an_intersection() -> None:
    """The original sightings, pinned as the (documented) AND semantics:
    positive search 'large_breasts' + Missing(medium) = has-large-and-
    lacks-medium; + Has(medium) = the double-tagged image."""
    st = _state()
    st.set_queue_search("large_breasts")
    st.set_filter_mode(FilterMode.MISSING_TAG)
    assert _q(st) == ["img_large_a", "img_large_b"], _q(st)
    st.set_filter_mode(FilterMode.HAS_TAG)
    assert _q(st) == ["img_both"], _q(st)
    print("OK: the field sightings are the documented intersection of "
          "two filters (mode keys on the walk tag)")


def test_negation_expresses_images_without_another_tag() -> None:
    st = _state()
    st.set_filter_mode(FilterMode.ALL)
    st.set_queue_search("-large_breasts")
    assert _q(st) == ["img_medium", "img_neither"], _q(st)
    # And it composes with the mode: lacking medium AND lacking large.
    st.set_filter_mode(FilterMode.MISSING_TAG)
    assert _q(st) == ["img_neither"], _q(st)
    print("OK: '-tag' shows images WITHOUT that tag, and composes with "
          "the Has/Missing mode")


def test_spaces_match_underscores() -> None:
    st = _state()
    st.set_filter_mode(FilterMode.ALL)
    st.set_queue_search("large breasts")           # the silent-empty trap
    assert _q(st) == ["img_both", "img_large_a", "img_large_b"], _q(st)
    st.set_queue_search("-large breasts")
    assert _q(st) == ["img_medium", "img_neither"], _q(st)
    print("OK: a space where the tag has an underscore now matches "
          "(positive and negated)")


def test_exact_comma_mode_mixed_negation() -> None:
    st = _state()
    st.set_filter_mode(FilterMode.ALL)
    st.set_queue_search("1girl, -large_breasts")
    assert _q(st) == ["img_medium", "img_neither"], _q(st)
    st.set_queue_search("medium_breasts, -large_breasts")
    assert _q(st) == ["img_medium"], _q(st)
    print("OK: comma lists mix positive and negated exact tags")


def test_edges_and_no_regression() -> None:
    st = _state()
    st.set_filter_mode(FilterMode.ALL)
    st.set_queue_search("-")                        # bare minus: no filter
    assert len(_q(st)) == 5
    st.set_queue_search("-no_such_tag")             # excludes nothing
    assert len(_q(st)) == 5
    st.set_queue_search("large_breasts")            # positives unchanged
    assert _q(st) == ["img_both", "img_large_a", "img_large_b"], _q(st)
    st.set_queue_search("img_medium")               # filename match intact
    assert _q(st) == ["img_medium"], _q(st)
    st.set_queue_search("")                         # clearing restores all
    assert len(_q(st)) == 5
    print("OK: edges are safe and existing search behavior is unchanged")


def run() -> None:
    test_field_replay_positive_search_is_an_intersection()
    test_negation_expresses_images_without_another_tag()
    test_spaces_match_underscores()
    test_exact_comma_mode_mixed_negation()
    test_edges_and_no_regression()
    print("\nALL PASS: queue search negation + space/underscore "
          "normalization")


if __name__ == "__main__":
    run()
