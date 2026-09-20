"""Regression test for the multi-select STATE engine.

Multi-select mode drives the walk queue from a SET of selected tags
(with the filter dropdown applying set logic) instead of a single
walking tag. This test exercises the engine at the state layer — no UI
— which is where all the filtering logic lives. The UI (checkbox tag
selection, disabling Yes/No, hiding the co-occurrence hint, live-shrink)
sits on top of these methods.

Verifies:
  - Entering with the default ALL filter + empty selection shows all
    images; the per-tag walk is suspended (current_tag is None).
  - MISSING_TAG over a tag set = images with NONE of the selected tags
    (the "forgotten image" finder — the headline use case).
  - HAS_TAG over a set = images with ALL selected tags (intersection).
  - ALL ignores the selection entirely.
  - SKIPPED_ONLY = images skipped for ANY selected tag.
  - An empty selection yields an empty queue for the set filters but
    everything for ALL.
  - The filter dropdown re-filters in multi-select mode (the setters
    rebuild even though current_tag is None).
  - Selection is de-duplicated case-insensitively, first-seen order.
  - Yes/No are inert in multi-select (current_tag is None).
  - Leaving multi-select empties the queue; a normal tag click then
    rebuilds it (the single-tag walk is fully restored).

Run: python3 tests/test_multi_select.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from core.state import SessionState, FilterMode, Decision
from core.scanner import scan


def _make_dataset() -> Path:
    d = Path(tempfile.mkdtemp()) / "imgs"
    d.mkdir(parents=True)

    def mk(name, tags):
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")

    mk("img1", ["1girl", "long_hair", "smile"])
    mk("img2", ["1girl", "short_hair"])
    mk("img3", ["1girl", "smile"])                     # forgotten: no hair len
    mk("img4", ["1girl", "medium_hair", "long_hair"])  # legit in-between combo
    mk("img5", ["1girl"])                              # forgotten: no hair len
    return d


def run() -> None:
    st = SessionState(scan(_make_dataset()))

    def names():
        return sorted(e.image_path.stem for e in st.get_current_queue())

    HAIR = ["long_hair", "short_hair", "medium_hair",
            "very_long_hair", "absurdly_long_hair"]
    ALL5 = ["img1", "img2", "img3", "img4", "img5"]

    # Enter multi-select: default ALL + empty selection shows everything,
    # and the single-tag walk is suspended.
    st.set_multi_select_mode(True)
    assert st.multi_select_mode is True
    assert st.current_tag is None, "walk must be suspended in multi-select"
    assert names() == ALL5, names()

    # MISSING_TAG over the hair set = the forgotten-image finder.
    st.set_selected_tags(HAIR)
    st.set_filter_mode(FilterMode.MISSING_TAG)
    assert names() == ["img3", "img5"], names()

    # HAS_TAG = intersection (ALL selected present).
    st.set_selected_tags(["long_hair", "smile"])
    st.set_filter_mode(FilterMode.HAS_TAG)
    assert names() == ["img1"], names()

    # ALL ignores the selection.
    st.set_filter_mode(FilterMode.ALL)
    assert names() == ALL5, names()

    # Empty selection: set filters empty, ALL shows everything.
    st.set_selected_tags([])
    st.set_filter_mode(FilterMode.MISSING_TAG)
    assert names() == [], names()
    st.set_filter_mode(FilterMode.HAS_TAG)
    assert names() == [], names()
    st.set_filter_mode(FilterMode.ALL)
    assert names() == ALL5, names()

    # SKIPPED_ONLY = images skipped for ANY selected tag.
    st.set_selected_tags(["long_hair"])
    img1 = [e.image_path for e in st._images if e.image_path.stem == "img1"][0]
    st._decisions[(img1, "long_hair")] = Decision.SKIPPED
    st.set_filter_mode(FilterMode.SKIPPED_ONLY)
    assert names() == ["img1"], names()

    # De-dup, case-insensitive, first-seen order preserved.
    st.set_filter_mode(FilterMode.ALL)
    st.set_selected_tags(["long_hair", "Long_Hair", "smile", "smile"])
    assert st.get_selected_tags() == ["long_hair", "smile"], st.get_selected_tags()

    # Yes/No inert (no current_tag to decide).
    before = st.current_tag
    st.record_yes()
    st.record_no()
    assert before is None and st.current_tag is None

    # set_selected_tags is a no-op outside multi-select mode.
    st.set_multi_select_mode(False)
    assert st.multi_select_mode is False
    assert names() == [], "leaving multi-select empties the queue"
    st.set_selected_tags(["long_hair"])
    assert st.get_selected_tags() == [], "selection ignored outside multi-select"

    # The single-tag walk is fully restored.
    st.set_filter_mode(FilterMode.ALL)
    st.select_tag("1girl")
    assert st.current_tag == "1girl"
    assert names() == ALL5, names()

    print("OK: multi-select engine verified "
          "(MISSING finder, HAS intersection, ALL, SKIPPED, dedup, "
          "inert yes/no, clean enter/exit)")


if __name__ == "__main__":
    run()
