"""Comprehensive save/load round-trip and robustness tests.

Standalone (no pytest):  python tests/test_persistence_roundtrip.py

The session file is where a user's accumulated review work lives, so
the cost of a persistence bug is lost work — the least forgivable kind
of failure. This suite exercises the full save -> load -> apply cycle
and the reconciliation that copes with a dataset that changed under the
file's feet, because those paths are hard to reach with the small
datasets available for manual testing.

Coverage:
  * every saved field round-trips (decisions, tag-status overrides,
    walk position, front locks, and ALL of ui_preferences)
  * two fields that were silently NOT persisted — tag_sort_mode (which
    sets the order auto-advance follows) and search_locked — now do
  * additive-schema back-compat: an old file lacking the new keys still
    loads, leaving the state's own values untouched, and garbage values
    are ignored rather than crashing
  * reconciliation: deleted images and disappeared tags drop cleanly
    and are counted; unicode survives; empty state round-trips
  * malformed decision rows of every shape are filtered by load()
  * the multi-root (v2) format round-trips and resolves each decision
    to the correct root
  * the atomic save leaves the previous file intact when a write fails,
    and leaks no temp files
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from PIL import Image  # noqa: E402

from core import persistence  # noqa: E402
from core.persistence import InvalidSessionFileError  # noqa: E402
from core.scanner import scan, scan_many  # noqa: E402
from core.state import (  # noqa: E402
    FilterMode, SessionState, SortMode, TagSortMode,
)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def _dataset(spec: dict) -> Path:
    """A folder of images+captions from {stem: [tags]} or {stem: 'a, b'}."""
    d = Path(tempfile.mkdtemp())
    for name, tags in spec.items():
        Image.new("RGB", (16, 16)).save(d / f"{name}.png")
        text = tags if isinstance(tags, str) else ", ".join(tags)
        (d / f"{name}.txt").write_text(text, encoding="utf-8")
    return d


def _sess() -> Path:
    return Path(tempfile.mkdtemp()) / "session.tw.json"


def _snapshot(state) -> dict:
    """Everything that is meant to survive a round-trip."""
    return {
        "decisions": sorted(
            (str(p), t, dec.value) for p, t, dec in state.iter_decisions()),
        "overrides": dict(state.iter_tag_status_overrides()),
        "filter_mode": state.filter_mode.value,
        "sort_mode": state.sort_mode.value,
        "show_orphans": state.show_orphans,
        "tag_sort_mode": state._tag_sort_mode.value,
        "search_locked": state.search_locked,
        "front_locks": sorted(state.front_locked_tokens),
    }


# --------------------------------------------------------------------
# 1. Full round-trip of every saved field
# --------------------------------------------------------------------
def test_every_saved_field_round_trips() -> None:
    d = _dataset({
        "a": ["1girl", "smile", "blue_eyes"],
        "b": ["1girl", "smile", "long_hair"],
        "c": ["1girl", "red_eyes"],
        "e": ["2girls", "smile"],
    })
    st = SessionState(scan(d))
    st.select_tag("smile")
    st.record_yes()
    st.record_no()
    st.record_skip_tag("red_eyes")
    st.set_filter_mode(FilterMode.ALL)
    st.set_sort_mode(SortMode.ALPHA_DESC)
    st.set_show_orphans(True)
    st.set_tag_sort_mode(TagSortMode.COUNT_DESC)
    st.set_search_locked(True)
    st.set_front_locked_tokens(["1girl"])

    before = _snapshot(st)
    sess = _sess()
    persistence.save(st, sess)
    st2 = SessionState(scan(d))
    persistence.apply_to_state(persistence.load(sess), st2)
    after = _snapshot(st2)

    for key in before:
        assert before[key] == after[key], (
            f"{key} did not round-trip: {before[key]!r} -> {after[key]!r}")
    print("OK: decisions, overrides, front locks and every ui_preference "
          "(including tag_sort_mode and search_locked) round-trip")


def test_tag_sort_mode_and_search_lock_specifically() -> None:
    """These two were silently dropped before — tag_sort_mode governs
    auto-advance order, so losing it changed navigation after a load.
    Pinned on their own so a regression is unambiguous."""
    d = _dataset({"a": ["1girl", "smile"], "b": ["1girl", "red_eyes"]})
    st = SessionState(scan(d))
    st.set_tag_sort_mode(TagSortMode.COUNT_DESC)
    st.set_search_locked(True)

    sess = _sess()
    persistence.save(st, sess)
    st2 = SessionState(scan(d))
    persistence.apply_to_state(persistence.load(sess), st2)

    assert st2._tag_sort_mode == TagSortMode.COUNT_DESC, (
        "tag_sort_mode was lost on load")
    assert st2.search_locked is True, "search_locked was lost on load"
    print("OK: tag_sort_mode and search_locked survive save/load")


# --------------------------------------------------------------------
# 2. Additive-schema back-compat
# --------------------------------------------------------------------
def test_old_file_without_new_keys_loads() -> None:
    """A file written before the new keys existed must load fine, and
    the absent keys must leave the state's own values untouched (the
    additive contract), not force them to a default."""
    d = _dataset({"a": ["1girl", "smile"], "b": ["1girl"]})
    st = SessionState(scan(d))
    st.set_tag_sort_mode(TagSortMode.COUNT_DESC)
    st.set_search_locked(True)
    sess = _sess()
    persistence.save(st, sess)

    data = json.loads(sess.read_text(encoding="utf-8"))
    del data["ui_preferences"]["tag_sort_mode"]
    del data["ui_preferences"]["search_locked"]
    sess.write_text(json.dumps(data), encoding="utf-8")

    st2 = SessionState(scan(d))
    st2.set_tag_sort_mode(TagSortMode.ALPHA_DESC)   # a non-default value
    st2.set_search_locked(True)
    persistence.apply_to_state(persistence.load(sess), st2)
    assert st2._tag_sort_mode == TagSortMode.ALPHA_DESC, (
        "a missing key overwrote the state's existing value")
    assert st2.search_locked is True
    print("OK: an old file lacking the new keys loads and leaves the "
          "state's own values untouched")


def test_garbage_pref_values_are_ignored() -> None:
    """Invalid values for the new keys must fall back, never crash."""
    d = _dataset({"a": ["1girl", "smile"]})
    st = SessionState(scan(d))
    sess = _sess()
    persistence.save(st, sess)
    data = json.loads(sess.read_text(encoding="utf-8"))
    data["ui_preferences"]["tag_sort_mode"] = "not_a_real_mode"
    data["ui_preferences"]["search_locked"] = "yes_please"   # wrong type
    sess.write_text(json.dumps(data), encoding="utf-8")

    st2 = SessionState(scan(d))
    persistence.apply_to_state(persistence.load(sess), st2)
    assert st2._tag_sort_mode == TagSortMode.ALPHA_ASC        # default
    assert st2.search_locked is False                        # default
    print("OK: garbage values for the new prefs fall back to defaults "
          "without crashing")


# --------------------------------------------------------------------
# 3. Reconciliation: the dataset changed under the file
# --------------------------------------------------------------------
def test_deleted_image_is_reconciled() -> None:
    d = _dataset({"a": ["1girl", "smile"], "b": ["1girl"], "c": ["2girls"]})
    st = SessionState(scan(d))
    st.select_tag("1girl")
    st.record_yes()
    st.record_no()
    sess = _sess()
    persistence.save(st, sess)

    (d / "a.png").unlink()
    (d / "a.txt").unlink()
    st2 = SessionState(scan(d))
    report = persistence.apply_to_state(persistence.load(sess), st2)

    names = {Path(p).name for p, _, _ in st2.iter_decisions()}
    assert "a.png" not in names, "decision for a deleted image survived"
    assert report.decisions_discarded_missing_image >= 1
    print("OK: a decision for a deleted image is dropped and counted")


def test_disappeared_tag_is_reconciled() -> None:
    d = _dataset({"a": ["1girl", "rare_tag"], "b": ["1girl"]})
    st = SessionState(scan(d))
    st.record_skip_tag("rare_tag")
    sess = _sess()
    persistence.save(st, sess)

    (d / "a.txt").write_text("1girl", encoding="utf-8")   # rare_tag gone
    st2 = SessionState(scan(d))
    persistence.apply_to_state(persistence.load(sess), st2)
    assert "rare_tag" not in st2.all_tags
    assert "rare_tag" not in dict(st2.iter_tag_status_overrides())
    print("OK: a skip override for a tag that no longer exists is "
          "dropped silently")


def test_unicode_survives_round_trip() -> None:
    d = _dataset({
        "a": ["1girl", "\u9752\u3044\u76ee", "laughing"],   # 青い目
        "cafe": ["1girl", "smile"],
        "c": ["emoji_\U0001F600_tag", "normal"],            # 😀
    })
    st = SessionState(scan(d))
    st.select_tag("1girl")
    st.record_yes()
    sess = _sess()
    persistence.save(st, sess)

    raw = sess.read_text(encoding="utf-8")
    assert "\u9752\u3044\u76ee" in raw and "\U0001F600" in raw, (
        "unicode was escaped or lost in the saved file")
    st2 = SessionState(scan(d))
    persistence.apply_to_state(persistence.load(sess), st2)
    assert set(st.all_tags) == set(st2.all_tags)
    print("OK: unicode tag names survive the round-trip as real "
          "characters in the file")


def test_empty_state_round_trips() -> None:
    d = Path(tempfile.mkdtemp())
    Image.new("RGB", (16, 16)).save(d / "orphan.png")   # no caption
    st = SessionState(scan(d))
    sess = _sess()
    persistence.save(st, sess)
    st2 = SessionState(scan(d))
    persistence.apply_to_state(persistence.load(sess), st2)   # must not raise
    print("OK: a near-empty state (one orphan, no decisions) round-trips")


# --------------------------------------------------------------------
# 4. Corruption and malformed files
# --------------------------------------------------------------------
def test_corrupt_files_raise_clean_errors() -> None:
    cases = {
        "truncated": '{"format_version": 1, "decisions": [',
        "not_an_object": "[1, 2, 3]",
        "future_version": '{"format_version": 999, "decisions": []}',
    }
    for label, text in cases.items():
        p = Path(tempfile.mkdtemp()) / f"{label}.json"
        p.write_text(text, encoding="utf-8")
        try:
            persistence.load(p)
            raise AssertionError(f"{label} should have been rejected")
        except InvalidSessionFileError:
            pass          # the one acceptable outcome
    print("OK: truncated, wrong-shaped, and future-version files raise "
          "InvalidSessionFileError rather than crashing")


def test_malformed_decision_rows_are_filtered() -> None:
    d = _dataset({"a": ["1girl", "smile"]})
    st = SessionState(scan(d))
    st.select_tag("1girl")
    st.record_yes()
    sess = _sess()
    persistence.save(st, sess)

    data = json.loads(sess.read_text(encoding="utf-8"))
    valid = len(data["decisions"])
    data["decisions"] += [
        ["a.png"],                          # too short
        ["a.png", "smile"],                 # 2 elements
        ["a.png", "smile", "yes", "x"],     # 4 elements
        ["a.png", 123, "yes"],              # non-string
        "not_a_list",
        None,
        [],
        {"a": "b"},
    ]
    sess.write_text(json.dumps(data), encoding="utf-8")

    loaded = persistence.load(sess)
    assert len(loaded.decisions) == valid, (
        "load() failed to filter malformed decision rows")
    st2 = SessionState(scan(d))
    report = persistence.apply_to_state(loaded, st2)
    assert report.decisions_restored == valid
    print("OK: malformed decision rows of every shape are filtered by "
          "load(), leaving the valid ones intact")


# --------------------------------------------------------------------
# 5. Multi-root (v2) format
# --------------------------------------------------------------------
def test_multi_root_v2_round_trips() -> None:
    base = Path(tempfile.mkdtemp())
    r1, r2 = base / "set_a", base / "set_b"
    for r, caps in ((r1, {"x1": "1girl, smile", "x2": "1girl"}),
                    (r2, {"y1": "1girl, smile, grin", "y2": "smile"})):
        r.mkdir(parents=True)
        for n, t in caps.items():
            Image.new("RGB", (8, 8)).save(r / f"{n}.png")
            (r / f"{n}.txt").write_text(t, encoding="utf-8")

    st = SessionState(scan_many([r1, r2]))
    st.select_tag("smile")
    st.record_yes()
    st.record_no()
    st.set_tag_sort_mode(TagSortMode.COUNT_DESC)
    st.set_search_locked(True)

    sess = _sess()
    persistence.save(st, sess)
    data = json.loads(sess.read_text(encoding="utf-8"))
    assert data["format_version"] == 2, "multi-root must save as v2"
    assert len(data.get("root_directories", [])) == 2

    st2 = SessionState(scan_many([r1, r2]))
    persistence.apply_to_state(persistence.load(sess), st2)
    assert (sorted(str(p) for p, _, _ in st.iter_decisions())
            == sorted(str(p) for p, _, _ in st2.iter_decisions()))
    assert st2._tag_sort_mode == TagSortMode.COUNT_DESC
    assert st2.search_locked is True

    # Each decision must resolve under the correct root.
    by_name = {Path(p).name: str(p) for p, _, _ in st2.iter_decisions()}
    if "x1.png" in by_name:
        assert "set_a" in by_name["x1.png"]
    if "y1.png" in by_name:
        assert "set_b" in by_name["y1.png"]
    print("OK: the multi-root v2 format round-trips and resolves each "
          "decision to its correct root")


# --------------------------------------------------------------------
# 6. Atomic save corruption safety
# --------------------------------------------------------------------
def test_failed_save_preserves_the_previous_file() -> None:
    d = _dataset({"a": ["1girl", "smile"]})
    st = SessionState(scan(d))
    st.select_tag("1girl")
    st.record_yes()
    sess = _sess()
    persistence.save(st, sess)
    original = sess.read_text(encoding="utf-8")

    st.record_no()      # change state so a new save would differ
    real_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("simulated failure during replace")

    os.replace = failing_replace
    try:
        raised = False
        try:
            persistence.save(st, sess)
        except OSError:
            raised = True
        assert raised, "a failed replace should propagate"
    finally:
        os.replace = real_replace

    assert sess.read_text(encoding="utf-8") == original, (
        "a failed save corrupted the existing session file")
    persistence.load(sess)      # still valid
    leftover = list(sess.parent.glob(".tw_session_tmp_*"))
    assert not leftover, f"failed save leaked {len(leftover)} temp files"
    print("OK: a save that fails mid-write leaves the previous file "
          "intact and loadable, and leaks no temp files")


def run() -> None:
    test_every_saved_field_round_trips()
    test_tag_sort_mode_and_search_lock_specifically()
    test_old_file_without_new_keys_loads()
    test_garbage_pref_values_are_ignored()
    test_deleted_image_is_reconciled()
    test_disappeared_tag_is_reconciled()
    test_unicode_survives_round_trip()
    test_empty_state_round_trips()
    test_corrupt_files_raise_clean_errors()
    test_malformed_decision_rows_are_filtered()
    test_multi_root_v2_round_trips()
    test_failed_save_preserves_the_previous_file()
    print("ALL PASS: persistence round-trip")


if __name__ == "__main__":
    run()
