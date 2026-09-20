"""Regression test for multi-select LIVE-SHRINK (#4).

In multi-select mode the queue is a snapshot, but when a tag edit makes
the edited image stop matching the set filter it is dropped from the
queue immediately — so the "forgotten image" finder shrinks as you fix
images, and the view advances to the next one.

Because the edits write to the .txt files on disk, every scenario uses a
FRESH dataset (re-scanning a polluted folder would see prior edits).

Verifies:
  - MISSING_TAG finder: giving a forgotten image one of the ticked tags
    removes it and advances to the image that takes its slot.
  - An unrelated tag edit does NOT shrink (still missing the ticked set).
  - HAS_TAG: removing a required tag drops the image.
  - ALL mode never shrinks (selection is ignored there).

Run: python3 tests/test_multi_select_shrink.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from core.state import SessionState, FilterMode
from core.scanner import scan

HAIR = ["long_hair", "short_hair", "medium_hair"]


def _fresh() -> SessionState:
    d = Path(tempfile.mkdtemp()) / "imgs"
    d.mkdir(parents=True)

    def mk(name, tags):
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")

    mk("img1", ["1girl", "long_hair", "smile"])
    mk("img2", ["1girl", "short_hair"])
    mk("img3", ["1girl", "smile"])                     # forgotten
    mk("img4", ["1girl", "medium_hair", "long_hair"])
    mk("img5", ["1girl"])                              # forgotten
    return SessionState(scan(d))


def _names(st):
    return [e.image_path.stem for e in st.get_current_queue()]


def _cur(st):
    return st.current_image.image_path.stem if st.current_image else None


def _path(st, stem):
    return [e.image_path for e in st._images if e.image_path.stem == stem][0]


def run() -> None:
    # MISSING_TAG finder shrinks + advances.
    st = _fresh()
    st.set_multi_select_mode(True)
    st.set_selected_tags(HAIR)
    st.set_filter_mode(FilterMode.MISSING_TAG)
    assert _names(st) == ["img3", "img5"] and _cur(st) == "img3", \
        (_names(st), _cur(st))

    st.add_tag_to_image(_path(st, "img3"), "long_hair")
    assert _names(st) == ["img5"] and _cur(st) == "img5", \
        "fixing img3 should drop it and advance to img5"

    # Unrelated tag does NOT shrink.
    st.add_tag_to_image(_path(st, "img5"), "outdoors")
    assert _names(st) == ["img5"], "unrelated tag must not shrink"

    # Fix img5 too -> queue empties.
    st.add_tag_to_image(_path(st, "img5"), "short_hair")
    assert _names(st) == [] and _cur(st) is None

    # ALL mode never shrinks.
    st = _fresh()
    st.set_multi_select_mode(True)
    st.set_selected_tags(["long_hair"])
    st.set_filter_mode(FilterMode.ALL)
    before = len(_names(st))
    st.add_tag_to_image(_path(st, "img2"), "long_hair")
    assert len(_names(st)) == before, "ALL mode must ignore edits"

    # HAS_TAG: removing a required tag drops the image.
    st = _fresh()
    st.set_multi_select_mode(True)
    st.set_selected_tags(["long_hair"])
    st.set_filter_mode(FilterMode.HAS_TAG)
    assert _names(st) == ["img1", "img4"], _names(st)
    st.remove_tag_from_image(_path(st, "img1"), "long_hair")
    assert _names(st) == ["img4"], "removing long_hair from img1 should drop it"

    print("OK: multi-select live-shrink verified "
          "(MISSING shrink+advance, unrelated no-op, HAS drop, ALL never)")


if __name__ == "__main__":
    run()
