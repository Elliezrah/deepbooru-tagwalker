"""
tests/test_trigger_front_lock.py

Tests for the trigger-token front lock (Tools feature).

Field request: LoRA trigger-word conditioning wants chosen tokens at
the FRONT of every caption. The feature has three parts, all pinned:

1. apply_front_locks — pure stable reorder; locked tokens PRESENT in a
   caption move to the front (fold-matched), everything else keeps its
   order. Locks REORDER, they never INJECT — adding a token to
   captions that lack it is only ever the explicit batch option.
2. apply_trigger_token_front — the dataset batch: moves where present,
   optionally adds where missing (orphans included, creating their
   .txt), one undo entry reverts every file (deleting created ones).
3. Enforcement — with tokens locked, every FORWARD caption write keeps
   them in front; locks persist in the session file (additive v1 key;
   old session files load unchanged).

Run: python3 tests/test_trigger_front_lock.py
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

from core.state import SessionState, FilterMode, apply_front_locks
from core.scanner import scan
from core import tag_io, persistence


def _world():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    caps = {"front_ok": "trig, a", "mid": "a, trig, b", "missing": "a, b"}
    for n, t in caps.items():
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(t, encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "orphan.png")   # no caption file
    return d, SessionState(scan(d))


def test_pure_reorder_fn() -> None:
    assert apply_front_locks(["a", "trig", "b"], ["trig"]) == \
        ["trig", "a", "b"]
    assert apply_front_locks(["a", "Trig Ger"], ["trig_ger"]) == \
        ["Trig Ger", "a"]                                  # fold-matched
    assert apply_front_locks(["x", "t2", "y", "t1"], ["t1", "t2"]) == \
        ["t1", "t2", "x", "y"]                             # lock order
    same = ["a", "b"]
    assert apply_front_locks(same, ["absent"]) is same     # never injects
    assert apply_front_locks(["trig", "a"], ["trig"]) is not None
    print("OK: pure reorder — front, stable, fold-matched, lock order, "
          "reorder-not-inject")


def test_batch_moves_adds_and_orphans() -> None:
    d, st = _world()
    r = st.apply_trigger_token_front("trig", add_missing=True, lock=True)
    assert r == {"moved": 1, "added": 2, "unchanged": 1, "failed": 0}, r
    assert tag_io.read_tags(d / "mid.txt") == ["trig", "a", "b"]
    assert tag_io.read_tags(d / "missing.txt")[0] == "trig"
    assert (d / "orphan.txt").exists()
    assert tag_io.read_tags(d / "orphan.txt") == ["trig"]
    assert st.front_locked_tokens == ["trig"]
    assert st._tag_counts.get("trig") == 4
    print("OK: batch moves where present, adds where missing "
          "(orphan caption created), locks, counts updated")


def test_single_undo_reverts_everything() -> None:
    d, st = _world()
    st.apply_trigger_token_front("trig", add_missing=True, lock=False)
    assert st.undo() is True
    assert tag_io.read_tags(d / "mid.txt") == ["a", "trig", "b"]
    assert tag_io.read_tags(d / "missing.txt") == ["a", "b"]
    assert not (d / "orphan.txt").exists()     # created file removed
    assert st._tag_counts.get("trig") == 2     # back to the originals
    print("OK: one undo restores every caption, deletes created files, "
          "reverts counts")


def test_enforcement_on_forward_writes() -> None:
    d, st = _world()
    st.apply_trigger_token_front("trig", add_missing=True, lock=True)
    st.add_tag_to_image(d / "mid.png", "newtag")
    assert tag_io.read_tags(d / "mid.txt")[0] == "trig"
    # Yes-walk in WITHOUT mode adds the current tag — lock still wins.
    ap = d / "front_ok.png"
    st.add_tag_to_image(ap, "walkme")          # bootstrap the tag
    st.select_tag("walkme")
    st.set_filter_mode(FilterMode.MISSING_TAG)
    first = st.current_image.image_path
    st.record_yes()
    assert tag_io.read_tags(first.with_suffix(".txt"))[0] == "trig"
    print("OK: locks enforced on add-box and walk writes alike")


def test_locks_reorder_but_never_inject() -> None:
    d, st = _world()
    st.set_front_locked_tokens(["trig"])
    st.add_tag_to_image(d / "missing.png", "q")   # caption lacks trig
    assert tag_io.read_tags(d / "missing.txt") == ["a", "b", "q"]
    print("OK: a lock never injects the token into captions that lack "
          "it — adding is only the explicit batch option")


def test_persistence_roundtrip_and_legacy() -> None:
    d, st = _world()
    st.set_front_locked_tokens(["trig", "style_x"])
    sess = Path(tempfile.mkdtemp()) / "s.twsession"
    persistence.save(st, sess)
    st2 = SessionState(scan(d))
    persistence.apply_to_state(persistence.load(sess), st2)
    assert st2.front_locked_tokens == ["trig", "style_x"]
    st2.add_tag_to_image(d / "mid.png", "q")
    assert tag_io.read_tags(d / "mid.txt")[0] == "trig"
    # Legacy: a session file WITHOUT the key loads with empty locks.
    import json
    data = json.loads(sess.read_text(encoding="utf-8"))
    data.pop("front_locked_tokens", None)
    legacy = Path(tempfile.mkdtemp()) / "legacy.twsession"
    legacy.write_text(json.dumps(data), encoding="utf-8")
    st3 = SessionState(scan(d))
    persistence.apply_to_state(persistence.load(legacy), st3)
    assert st3.front_locked_tokens == []
    print("OK: locks persist in the session; legacy files (no key) "
          "load cleanly with no locks")


def test_multi_token_comma_list() -> None:
    """Field feature: multi-trigger datasets. A comma-separated input
    sends every listed token to the front IN THE GIVEN ORDER, one
    batch, one undo. With add_missing OFF this is safe on mixed
    multi-artist datasets: each image reorders only the tokens it
    already carries; images carrying none are untouched."""
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    caps = {"both": "x, t2, y, t1", "only1": "a, t1",
            "front_ok2": "t2, b", "neither": "plain, tags"}
    for n, c in caps.items():
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(c, encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "orph.png")
    st = SessionState(scan(d))

    r = st.apply_trigger_token_front("t1, t2",
                                     add_missing=False, lock=False)
    assert tag_io.read_tags(d / "both.txt") == ["t1", "t2", "x", "y"]
    assert tag_io.read_tags(d / "only1.txt") == ["t1", "a"]
    assert tag_io.read_tags(d / "front_ok2.txt") == ["t2", "b"]
    assert tag_io.read_tags(d / "neither.txt") == ["plain", "tags"]
    assert not (d / "orph.txt").exists()
    # front_ok2 was already correct; neither + orphan carry none.
    assert r == {"moved": 2, "added": 0, "unchanged": 3,
                 "failed": 0}, r
    assert st.undo() is True
    assert tag_io.read_tags(d / "both.txt") == ["x", "t2", "y", "t1"]

    r2 = st.apply_trigger_token_front("t1, t2",
                                      add_missing=True, lock=True)
    assert tag_io.read_tags(d / "neither.txt") == \
        ["t1", "t2", "plain", "tags"]
    assert tag_io.read_tags(d / "orph.txt") == ["t1", "t2"]
    assert st._tag_counts.get("t1") == 5
    assert st._tag_counts.get("t2") == 5
    assert st.front_locked_tokens == ["t1", "t2"]
    assert st.undo() is True
    assert st._tag_counts.get("t1") == 2
    assert not (d / "orph.txt").exists()
    print("OK: comma-list sends tokens front in given order; "
          "non-carriers untouched with add off; multi-add + counts + "
          "locks all covered by one undo")


def test_dialog_field_defaults_and_multi_ui() -> None:
    """Field-revised defaults: BOTH checkboxes start OFF (a stray
    add-to-all could be catastrophic; locking is opt-in). The input
    dedupes comma lists and previews per token; the keep_tokens
    trainer tip is visible."""
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(d / "a.png")
    (d / "a.txt").write_text("t1, x", encoding="utf-8")
    st = SessionState(scan(d))
    from ui.trigger_token_dialog import TriggerTokenDialog
    from PySide6.QtWidgets import QLabel
    dlg = TriggerTokenDialog(st)
    dlg.show()
    assert dlg.chk_add.isChecked() is False
    assert dlg.chk_lock.isChecked() is False
    dlg.token_input.setText("t1, t2, T1")
    assert dlg._tokens() == ["t1", "t2"]        # fold-deduped
    assert dlg.preview.text().count("present on") == 2
    assert any("keep_tokens" in lbl.text()
               for lbl in dlg.findChildren(QLabel))
    print("OK: dialog defaults OFF, comma input dedupes with "
          "per-token preview, keep_tokens tip shown")


def test_dialog_apply_end_to_end() -> None:
    """Regression (field crash): _apply called last_write_failures as
    a method, but it is a @property — TypeError the moment Apply was
    pressed. None of the earlier tests exercised the Apply path, so
    the bug shipped invisibly. This drives the REAL button flow:
    confirm patched to Yes, summary captured — happy path AND the
    write-failure branch that reads the property."""
    from PySide6.QtWidgets import QMessageBox
    from ui.trigger_token_dialog import TriggerTokenDialog

    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(d / "a.png")
    (d / "a.txt").write_text("x, trig", encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "b.png")
    (d / "b.txt").write_text("trig, y", encoding="utf-8")
    st = SessionState(scan(d))
    dlg = TriggerTokenDialog(st)
    dlg.show()
    dlg.token_input.setText("trig")

    infos: list[str] = []
    real_q, real_i = QMessageBox.question, QMessageBox.information
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    QMessageBox.information = staticmethod(
        lambda *a, **k: infos.append(a[2]))
    try:
        dlg._apply()                      # crashed here before the fix
    finally:
        QMessageBox.question = real_q
        QMessageBox.information = real_i
    assert tag_io.read_tags(d / "a.txt") == ["trig", "x"]
    assert infos and "Moved to front: 1" in infos[-1]
    assert "Could not write" not in infos[-1]

    # Failure branch: one file refuses to write — the summary must
    # name it (this is the line that read the property).
    st2 = SessionState(scan(d))
    (d / "a.txt").write_text("x, trig", encoding="utf-8")
    st2.reload_image_tags(d / "a.png") if hasattr(
        st2, "reload_image_tags") else None
    st2 = SessionState(scan(d))
    dlg2 = TriggerTokenDialog(st2)
    dlg2.show()
    dlg2.token_input.setText("trig")
    real_write = st2._write_tags_with_retry
    st2._write_tags_with_retry = (
        lambda p, tags: False if p.name == "a.txt"
        else real_write(p, tags))
    infos2: list[str] = []
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    QMessageBox.information = staticmethod(
        lambda *a, **k: infos2.append(a[2]))
    try:
        dlg2._apply()
    finally:
        QMessageBox.question = real_q
        QMessageBox.information = real_i
        st2._write_tags_with_retry = real_write
    assert infos2 and "Could not write" in infos2[-1]
    assert "a.txt" in infos2[-1]
    print("OK: Apply runs end-to-end — happy path and the "
          "write-failure summary that reads last_write_failures as a "
          "property")


def run() -> None:
    test_pure_reorder_fn()
    test_batch_moves_adds_and_orphans()
    test_single_undo_reverts_everything()
    test_enforcement_on_forward_writes()
    test_locks_reorder_but_never_inject()
    test_persistence_roundtrip_and_legacy()
    test_multi_token_comma_list()
    test_dialog_field_defaults_and_multi_ui()
    test_dialog_apply_end_to_end()
    print("\nALL PASS: trigger-token front lock")


if __name__ == "__main__":
    run()
