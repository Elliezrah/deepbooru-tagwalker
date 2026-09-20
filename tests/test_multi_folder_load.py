"""
tests/test_multi_folder_load.py

Tests for multi-folder dataset loading (field feature).

File → Open Multiple Folders loads several dataset directories — each
scanned ROOT-ONLY, exactly like a single folder (this is not the
removed recursive-subfolder loading) — and combines every image into
one list: one tag tree, one walk queue, one review session.

Save-state conditioning (the part the field warned about):
  - Multi-root sessions write format_version 2 with a
    "root_directories" list; image references are encoded as
    "{index}:{rel}" where the index points into the CANONICALLY SORTED
    roots, so the same set of folders matches no matter what order
    they are reopened in.
  - Single-root sessions keep writing format_version 1 with the exact
    classic schema (no new keys) — older builds still open them.
  - The same encoder drives BOTH serialization and apply-side
    reconciliation, so the two sides can never disagree.

Run: python3 tests/test_multi_folder_load.py
"""
import json
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
from core.scanner import scan, scan_many
from core import tag_io, persistence


def _two_roots():
    base = Path(tempfile.mkdtemp())
    r1, r2 = base / "set_a", base / "set_b"
    for r, caps in ((r1, {"x1": "1girl, smile", "x2": "1girl"}),
                    (r2, {"y1": "1girl, smile, grin", "y2": "smile"})):
        r.mkdir(parents=True)
        for n, t in caps.items():
            Image.new("RGB", (8, 8)).save(r / f"{n}.png")
            (r / f"{n}.txt").write_text(t, encoding="utf-8")
    Image.new("RGB", (8, 8)).save(r2 / "orph.png")   # orphan in root 2
    return base, r1, r2


def test_scan_many_merges_and_dedupes() -> None:
    _, r1, r2 = _two_roots()
    res = scan_many([r1, r2])
    assert res.total_images == 5
    assert res.roots == [r1, r2] and res.root == r1
    assert res.tag_counts.get("smile") == 3
    assert res.tag_counts.get("1girl") == 3
    assert res.orphan_count == 1
    assert scan_many([r1, r1]).total_images == 2     # duplicate dropped
    print("OK: scan_many merges images, counts and orphans; duplicate "
          "roots are dropped; root stays the first root")


def test_combined_walk_and_correct_write_targets() -> None:
    _, r1, r2 = _two_roots()
    st = SessionState(scan_many([r1, r2]))
    assert st.roots == [r1, r2]
    st.select_tag("smile")
    st.set_filter_mode(FilterMode.HAS_TAG)
    q = sorted(i.image_path.stem for i in st.get_current_queue())
    assert q == ["x1", "y1", "y2"]                   # spans both roots
    st.set_filter_mode(FilterMode.ALL)
    qa = sorted(i.image_path.stem for i in st.get_current_queue())
    assert qa == ["x1", "x2", "y1", "y2"]
    st.set_filter_mode(FilterMode.HAS_TAG)
    st.record_yes()
    st.add_tag_to_image(r2 / "y2.png", "extra")
    assert "extra" in tag_io.read_tags(r2 / "y2.txt")
    print("OK: one combined queue across roots; edits land in the "
          "correct folder's caption files")


def test_v2_session_roundtrip_reversed_order() -> None:
    base, r1, r2 = _two_roots()
    st = SessionState(scan_many([r1, r2]))
    st.select_tag("smile")
    st.set_filter_mode(FilterMode.HAS_TAG)
    st.record_yes()
    cur_stem = st.current_image.image_path.stem
    st.set_front_locked_tokens(["smile"])
    sess = base / "multi.twsession"
    persistence.save(st, sess)

    data = json.loads(sess.read_text(encoding="utf-8"))
    assert data["format_version"] == 2
    assert "root_directories" in data
    assert any(":" in d[0] for d in data["decisions"])   # idx-encoded

    st2 = SessionState(scan_many([r2, r1]))              # reversed order
    persistence.apply_to_state(persistence.load(sess), st2)
    assert len(list(st2.iter_decisions())) == \
        len(list(st.iter_decisions())) >= 1
    assert st2.current_tag == "smile"
    assert st2.current_image.image_path.stem == cur_stem
    assert st2.front_locked_tokens == ["smile"]
    print("OK: v2 sessions round-trip — decisions, walk position and "
          "locks all survive reopening the folders in a different "
          "order (canonical root encoding)")


def test_single_root_stays_v1() -> None:
    base, r1, _ = _two_roots()
    st = SessionState(scan(r1))
    assert st.roots == [r1]
    sess = base / "single.twsession"
    persistence.save(st, sess)
    data = json.loads(sess.read_text(encoding="utf-8"))
    assert data["format_version"] == 1
    assert "root_directories" not in data
    # And classic rel paths (no index prefix).
    assert all(":" not in rel for rel in data["image_inventory"])
    st2 = SessionState(scan(r1))
    persistence.apply_to_state(persistence.load(sess), st2)
    print("OK: single-folder sessions still write the exact v1 schema "
          "— older builds keep opening them")


def test_front_lock_enforced_across_roots() -> None:
    _, r1, r2 = _two_roots()
    st = SessionState(scan_many([r1, r2]))
    st.set_front_locked_tokens(["smile"])
    st.add_tag_to_image(r2 / "y1.png", "zzz")
    assert tag_io.read_tags(r2 / "y1.txt")[0] == "smile"
    st.add_tag_to_image(r1 / "x1.png", "qqq")
    assert tag_io.read_tags(r1 / "x1.txt")[0] == "smile"
    print("OK: trigger-token front locks apply to writes in every "
          "loaded folder")


def test_window_start_scan_accepts_folder_list() -> None:
    """Regression (field crash): _start_scan(list) died at the status
    line ('list' object has no attribute 'name') because the body
    still assumed a single Path. Drive the REAL window entry point —
    the path File → Open Multiple Folders uses — through the threaded
    scan to completion."""
    _, r1, r2 = _two_roots()
    from config.settings import Settings
    from config.theme import initialize_theme
    s = Settings()
    initialize_theme(s.theme)
    from ui.main_window import MainWindow

    w = MainWindow(s)
    w.show()
    w._start_scan([r1, r2])          # crashed here before the fix
    import time
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        _app.processEvents()
        if w._state is not None and len(w._state.all_images) == 5:
            break
        time.sleep(0.02)
    assert w._state is not None, "scan never finished"
    assert len(w._state.all_images) == 5
    assert w._state.roots == [r1, r2]
    # The path-bar label was removed; the loaded-dataset identity now
    # lives in the window title, which reflects multi-folder loads with a
    # folder count (see _refresh_window_title). The primary (first) folder
    # anchors the name.
    title = w.windowTitle()
    assert "(2 folders)" in title, title
    assert r1.name in title, title
    print("OK: window _start_scan accepts a folder list, loads the "
          "combined dataset, and the window title shows the primary "
          "folder with a '(2 folders)' count")


def test_load_list_manager_playlist_model() -> None:
    """Field revision two: MULTIPLE named load lists ("playlists").
    The dialog is creator and picker: New/Rename/Delete lists on top,
    the selected list's folders below, edits AUTO-SAVED to user
    config. Load Selected List applies it; the last-used list is
    preselected on reopen (two-click restart flow). The legacy single
    remembered set migrates into a list named "Remembered folders".
    The missing-folder policy is unchanged, per list: marked, load
    confirms available-only, the list keeps the missing entry."""
    from PySide6.QtWidgets import QMessageBox
    from PySide6.QtCore import Qt
    from config.settings import Settings
    from ui.multi_folder_dialog import MultiFolderDialog

    base, r1, r2 = _two_roots()
    ghost = base / "unplugged"
    s = Settings()
    s.multi_load_lists = {}
    s.multi_load_folders = [str(r1)]        # legacy set to migrate
    s.last_multi_list = ""

    d0 = MultiFolderDialog(s)
    assert s.multi_load_lists == {"Remembered folders": [str(r1)]}
    assert s.multi_load_folders == []       # legacy cleared
    assert d0._current_name() == "Remembered folders"

    name = d0._new_list("mix A")
    assert name == "mix A"
    d0._add_path(r1)
    d0._add_path(r2)
    assert d0._add_path(r1) is False        # dedupe within a list
    assert s.multi_load_lists["mix A"] == [str(r1), str(r2)]
    assert d0._new_list("mix A") == "mix A 2"   # uniquified

    # Switching selection shows THAT list's folders.
    for i in range(d0.name_list.count()):
        if d0.name_list.item(i).data(
                Qt.ItemDataRole.UserRole) == "mix A":
            d0.name_list.setCurrentRow(i)
            break
    shown = [d0.folder_list.item(i).data(Qt.ItemDataRole.UserRole)
             for i in range(d0.folder_list.count())]
    assert shown == [str(r1), str(r2)]

    d0.accept()
    assert d0.result() == 1
    assert d0.selected_roots() == [r1, r2]
    assert s.last_multi_list == "mix A"

    d1 = MultiFolderDialog(s)               # restart flow
    assert d1._current_name() == "mix A"    # preselected

    # Missing policy, per list.
    s.multi_load_lists = {**s.multi_load_lists,
                          "with ghost": [str(r2), str(ghost)]}
    d2 = MultiFolderDialog(s)
    for i in range(d2.name_list.count()):
        if d2.name_list.item(i).data(
                Qt.ItemDataRole.UserRole) == "with ghost":
            d2.name_list.setCurrentRow(i)
            break
    assert "1 missing" in d2.name_list.currentItem().text()
    assert any("(missing)" in d2.folder_list.item(i).text()
               for i in range(d2.folder_list.count()))
    d2._confirm_missing = lambda m: True
    d2.accept()
    assert d2.selected_roots() == [r2]
    assert s.multi_load_lists["with ghost"] == [str(r2), str(ghost)]

    # Delete (confirm patched) and the empty-list guard.
    d3 = MultiFolderDialog(s)
    for i in range(d3.name_list.count()):
        if d3.name_list.item(i).data(
                Qt.ItemDataRole.UserRole) == "with ghost":
            d3.name_list.setCurrentRow(i)
            break
    real_q = QMessageBox.question
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    d3._delete_list()
    QMessageBox.question = real_q
    assert "with ghost" not in s.multi_load_lists
    d3._new_list("empty one")
    infos: list[str] = []
    real_i = QMessageBox.information
    QMessageBox.information = staticmethod(
        lambda *a, **k: infos.append(a[1]))
    d3.accept()
    QMessageBox.information = real_i
    assert d3.result() == 0 and infos and "Empty" in infos[0]
    print("OK: playlist model — named lists auto-save, last-used "
          "preselected, legacy set migrated, per-list missing policy "
          "kept, delete + empty-list guards work")



def run() -> None:
    test_scan_many_merges_and_dedupes()
    test_combined_walk_and_correct_write_targets()
    test_v2_session_roundtrip_reversed_order()
    test_single_root_stays_v1()
    test_front_lock_enforced_across_roots()
    test_window_start_scan_accepts_folder_list()
    test_load_list_manager_playlist_model()
    print("\nALL PASS: multi-folder load")


if __name__ == "__main__":
    run()
