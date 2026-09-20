"""Tests for completion credit following tag STATUS.

Field report: a finished project (every tag green in the sidebar) sat
below 100% in the stats. Root cause: get_tag_completion counted YES/NO
decisions only — a manually-marked tag showed as partial in the
per-tag swirl/progress — and a green restored from an older session
file (status only, no manually_completed marker) earned no credit in
get_project_completion either. Both now treat status COMPLETED as
fully adjudicated: a green tag always reads as done, everywhere.

Run: python3 tests/test_completion_credit.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from core.state import SessionState, TagStatus, FilterMode
from core.scanner import scan
from core import persistence as P


def _build():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for n, t in {"a": "1girl, smile", "b": "1girl, hat",
                 "c": "1girl", "d": "solo"}.items():
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(t, encoding="utf-8")
    return d
    # universe: 4 tags x 4 non-orphan images = 16 pairs


def test_marked_tag_is_fully_credited_per_tag() -> None:
    st = SessionState(scan(_build()))
    assert st.get_tag_completion("smile") == (0, 4)
    st.mark_tag_complete("smile")
    # THE reported bug: this returned (0, 4) — partial swirl/progress
    # for a tag the sidebar showed green.
    assert st.get_tag_completion("smile") == (4, 4)
    assert st.get_project_completion() == (4, 16)
    print("OK: 'Mark tag complete' fully credits the tag in the per-tag "
          "view (was stuck at its decision count)")


def test_all_marked_reaches_exactly_100() -> None:
    st = SessionState(scan(_build()))
    for t in list(st.all_tags):
        st.mark_tag_complete(t)
    assert st.get_project_completion() == (16, 16)
    print("OK: marking every tag complete reaches exactly 100%")


def test_legacy_green_status_without_marker_is_credited() -> None:
    # An older session file restores COMPLETED status without the
    # manually_completed marker. Green in the sidebar must still count.
    st = SessionState(scan(_build()))
    for t in list(st.all_tags):
        st.restore_tag_status(t, TagStatus.COMPLETED)
    assert not st._manually_completed
    assert st.get_project_completion() == (16, 16)
    assert st.get_tag_completion("hat") == (4, 4)
    print("OK: a green restored from an old session (status only) is "
          "fully credited — heals legacy session files")


def test_mixed_walked_and_marked() -> None:
    st = SessionState(scan(_build()))
    st.select_tag("solo")
    for _ in range(len(st.get_current_queue())):
        st.record_yes()                      # strict walk -> green
    st.mark_tag_complete("1girl")
    st.mark_tag_complete("smile")
    st.mark_tag_complete("hat")
    assert st.get_project_completion() == (16, 16)
    print("OK: strictly-walked greens and marked greens combine to 100% "
          "with no double counting")


def test_undo_of_mark_drops_credit() -> None:
    st = SessionState(scan(_build()))
    st.mark_tag_complete("smile")
    assert st.get_project_completion() == (4, 16)
    assert st.undo() is True
    assert st.get_project_completion() == (0, 16)
    assert st.get_tag_status("smile") == TagStatus.PENDING
    assert st.get_tag_completion("smile") == (0, 4)
    print("OK: undoing a mark removes its credit everywhere")


def test_skips_and_filtered_walks_stay_partial() -> None:
    st = SessionState(scan(_build()))
    st.select_tag("1girl")
    st.record_yes(); st.record_skip_image()
    st.record_yes(); st.record_skip_image()
    assert st.get_tag_status("1girl") == TagStatus.PENDING
    assert st.get_tag_completion("1girl") == (2, 4)

    st2 = SessionState(scan(_build()))
    st2.set_filter_mode(FilterMode.HAS_TAG)
    st2.select_tag("smile")
    for _ in range(len(st2.get_current_queue())):
        st2.record_yes()
    assert st2.get_tag_status("smile") == TagStatus.PENDING
    assert st2.get_tag_completion("smile") == (1, 4)
    print("OK: skip-heavy and filtered walks earn no false credit "
          "(only green counts as done)")


def test_marked_completion_survives_save_and_reload() -> None:
    d = _build()
    st = SessionState(scan(d))
    for t in list(st.all_tags):
        st.mark_tag_complete(t)
    jf = d.parent / "s.json"
    P.save(st, jf)
    st2 = SessionState(scan(d))
    P.apply_to_state(P.load(jf), st2)
    assert st2.get_project_completion() == (16, 16)
    assert st2.get_tag_completion("solo") == (4, 4)
    print("OK: 100% survives a save/reload round trip")


def run() -> None:
    test_marked_tag_is_fully_credited_per_tag()
    test_all_marked_reaches_exactly_100()
    test_legacy_green_status_without_marker_is_credited()
    test_mixed_walked_and_marked()
    test_undo_of_mark_drops_credit()
    test_skips_and_filtered_walks_stay_partial()
    test_marked_completion_survives_save_and_reload()
    print("\nALL PASS: completion credit follows tag status (green == done)")


if __name__ == "__main__":
    run()
