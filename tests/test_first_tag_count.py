"""
tests/test_first_tag_count.py

Regression test for the first-tag double-count bug.

Symptom (reported): on a fresh/empty dataset, adding the VERY FIRST tag to
an image's caption showed a count of 2 in the tag category pane when it
should be 1. Subsequent tags counted correctly.

Cause: adding a first tag *creates* the .txt file (it didn't exist before).
The file watcher sees the new file appear and fires apply_external_create
for it — but that is an echo of our OWN write, not a genuine external
create. apply_external_change already had a self-write echo guard
(prev_tags == new_tags -> return); apply_external_create did NOT, so it
re-ran _adjust_counts and counted the first tag a second time (1 from our
write, +1 from the echo = 2).

Fix: give apply_external_create the same echo guard — if the in-memory
tags already match the disk tags AND _has_txt is already set, it's the
echo of our own first-tag write, so skip the count adjustment.

These tests exercise the state layer directly (the watcher's echo is
simulated by calling apply_external_create with the post-write disk
content, exactly as the real watcher does after our write settles).
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.scanner import scan
from core.state import SessionState

# Minimal valid 1x1 PNG.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)


def _fresh_orphan():
    """A fresh dataset: one image, NO .txt (orphan) — the empty-dataset
    first-tag scenario."""
    tmp = Path(tempfile.mkdtemp(prefix="tw_firsttag_"))
    img = tmp / "pic1.png"
    img.write_bytes(_PNG)
    return tmp, img, SessionState(scan(tmp))


def test_first_tag_not_double_counted_by_watcher_echo():
    _, img, st = _fresh_orphan()
    assert st.get_tag_count_global("solo") == 0

    # Our write: creates the .txt, count -> 1.
    assert st.add_tag_to_image(img, "solo") is True
    assert st.get_tag_count_global("solo") == 1, "our own write should count once"

    # The watcher sees the newly-created .txt and fires create with the
    # disk content — an echo of our write. Must NOT count again.
    st.apply_external_create(img, ["solo"])
    assert st.get_tag_count_global("solo") == 1, (
        "watcher echo of our first-tag write double-counted the tag"
    )
    print("OK: first tag counts once despite the watcher create-echo")


def test_genuine_external_create_still_counts():
    # The guard must not swallow a REAL external create (an outside tool
    # writes a .txt for an orphan image while memory is still empty).
    _, img, st = _fresh_orphan()
    st.apply_external_create(img, ["1girl", "outdoors"])
    assert st.get_tag_count_global("1girl") == 1
    assert st.get_tag_count_global("outdoors") == 1
    print("OK: a genuine external create still counts correctly")


def test_subsequent_tags_count_once():
    # Second/third tags go through the modify path; their echoes are
    # apply_external_change (already guarded). Confirm end-to-end.
    _, img, st = _fresh_orphan()
    st.add_tag_to_image(img, "solo")
    st.apply_external_create(img, ["solo"])                 # create echo
    st.add_tag_to_image(img, "1girl")
    st.apply_external_change(img, ["solo", "1girl"])         # modify echo
    st.add_tag_to_image(img, "smile")
    st.apply_external_change(img, ["solo", "1girl", "smile"])  # modify echo
    for tag in ("solo", "1girl", "smile"):
        assert st.get_tag_count_global(tag) == 1, f"{tag} miscounted"
    print("OK: subsequent tags each count once")


def test_real_external_modify_after_our_create_reconciles():
    # After our create + its echo, a genuine external modify must still
    # reconcile (the guard only catches the exact echo).
    _, img, st = _fresh_orphan()
    st.add_tag_to_image(img, "solo")
    st.apply_external_create(img, ["solo"])                 # echo
    st.apply_external_change(img, ["solo", "night"])         # real external add
    assert st.get_tag_count_global("night") == 1
    assert st.get_tag_count_global("solo") == 1
    print("OK: a real external modify after our create still reconciles")


if __name__ == "__main__":
    test_first_tag_not_double_counted_by_watcher_echo()
    test_genuine_external_create_still_counts()
    test_subsequent_tags_count_once()
    test_real_external_modify_after_our_create_reconciles()
    print("\nALL PASS: first-tag count (watcher create-echo no longer double-counts)")
