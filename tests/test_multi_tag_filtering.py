"""Tests for the multi-tag filtering family (field debugging session).

Pins the full grammar and its composition:
- Search: comma = AND (unchanged), '-' = NOT, and NEW '~' = ANY-OF
  (booru-style OR) — the union view neither plain lists nor
  multi-select could express. All folded (case/space/zero-width).
- Multi-select set filters: HAS = ALL ticked, MISSING = NONE ticked;
  search applies on top.
- Filter labels now say WHICH tags they mean, and swap live between
  single-tag and multi-select mode.
- The add-walk recipe (field workflow: tag `medium_hair` onto every
  image lacking long/very_long): current tag + WITHOUT mode + '-a, -b'
  search, then Yes writes the tag and advances.

Run: python3 tests/test_multi_tag_filtering.py
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

_app = QApplication.instance() or QApplication([])

from core.state import SessionState, FilterMode
from core.scanner import scan
from core import tag_io
from ui.filter_bar import FilterBar


def _state():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for n, t in {
        "s1": "1girl, smile", "s2": "1girl, smile, outdoors",
        "ls1": "1girl, light_smile", "ls2": "1girl, light_smile, indoors",
        "both": "1girl, smile, light_smile", "grin": "1girl, grin",
        "lh": "1girl, long_hair", "vlh": "1girl, very_long_hair",
        "none1": "1girl, outdoors", "none2": "1girl, indoors",
    }.items():
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(t, encoding="utf-8")
    st = SessionState(scan(d))
    st.select_tag("1girl")
    st.set_filter_mode(FilterMode.ALL)
    return st, d


def _run(st, q, exact=False):
    st.set_queue_search_exact(exact)
    st.set_queue_search(q)
    r = sorted(i.image_path.stem for i in st.get_current_queue())
    st.set_queue_search("")
    st.set_queue_search_exact(False)
    return r


def test_or_groups() -> None:
    st, _ = _state()
    assert _run(st, "~light_smile, ~smile") == \
        sorted(["s1", "s2", "ls1", "ls2", "both"])
    assert _run(st, "~light_smile, ~smile, -outdoors") == \
        sorted(["s1", "ls1", "ls2", "both"])
    assert _run(st, "~grin") == ["grin"]                 # lone ~ = positive
    assert _run(st, "~light smile, ~smile") == \
        sorted(["s1", "s2", "ls1", "ls2", "both"])        # ~ folds
    assert _run(st, "~smile", exact=True) == \
        sorted(["s1", "s2", "both"])                      # ~ + exact toggle
    print("OK: ~ ANY-OF groups work — mixed with -, folded, exact")


def test_and_and_none_of_unchanged() -> None:
    st, _ = _state()
    assert _run(st, "smile, outdoors") == ["s2"]          # comma stays AND
    assert _run(st, "-light_smile, -smile") == \
        sorted(["grin", "lh", "vlh", "none1", "none2"])   # none-of
    print("OK: comma AND and '-' none-of are unchanged")


def test_multi_select_semantics_and_search_on_top() -> None:
    st, _ = _state()
    st.set_multi_select_mode(True)
    st.set_selected_tags(["long_hair", "very_long_hair"])
    st.set_filter_mode(FilterMode.HAS_TAG)
    assert [i.image_path.stem for i in st.get_current_queue()] == []
    st.set_filter_mode(FilterMode.MISSING_TAG)
    assert sorted(i.image_path.stem for i in st.get_current_queue()) == \
        sorted(["s1", "s2", "ls1", "ls2", "both", "grin", "none1", "none2"])
    assert _run(st, "-grin") == \
        sorted(["s1", "s2", "ls1", "ls2", "both", "none1", "none2"])
    st.set_multi_select_mode(False)
    print("OK: multi-select HAS=ALL / MISSING=NONE; search composes on top")


def test_filter_labels_swap_with_mode() -> None:
    st, _ = _state()
    bar = FilterBar()
    bar.attach(st)

    def labels():
        return [bar.filter_combo.itemText(i)
                for i in range(bar.filter_combo.count())]

    assert "Only images WITH current tag" in labels()
    st.set_multi_select_mode(True)
    assert "With ALL ticked tags" in labels()
    assert "With NONE of the ticked tags" in labels()
    st.set_multi_select_mode(False)
    assert "Only images WITHOUT current tag" in labels()
    print("OK: filter labels state WHICH tags they mean and swap live")


def test_add_walk_recipe_end_to_end() -> None:
    """The field workflow: add medium_hair to every image that has
    neither long_hair nor very_long_hair. Bootstrap: a brand-new tag
    must exist on one image before it can be walked."""
    st, d = _state()
    ap = next(i.image_path for i in st.all_images
              if i.image_path.stem == "none1")
    assert st.add_tag_to_image(ap, "medium_hair") is True   # bootstrap
    st.select_tag("medium_hair")
    st.set_filter_mode(FilterMode.MISSING_TAG)
    st.set_queue_search("-long_hair, -very_long_hair")
    q = sorted(i.image_path.stem for i in st.get_current_queue())
    assert "lh" not in q and "vlh" not in q and "none1" not in q
    first = st.current_image.image_path.stem
    st.record_yes()
    assert "medium_hair" in tag_io.read_tags(d / f"{first}.txt")
    assert st.current_image.image_path.stem != first        # advanced
    st.set_queue_search("")
    print("OK: the add-walk recipe works end to end — Yes writes the "
          "tag and advances through the negated set")


def run() -> None:
    test_or_groups()
    test_and_and_none_of_unchanged()
    test_multi_select_semantics_and_search_on_top()
    test_filter_labels_swap_with_mode()
    test_add_walk_recipe_end_to_end()
    print("\nALL PASS: multi-tag filtering (OR grammar, multi-select, "
          "labels, add-walk recipe)")


if __name__ == "__main__":
    run()
