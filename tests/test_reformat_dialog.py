"""Integration test for the Reformat Tags dialog.

Verifies the dialog splits protected emoticons out (unticked) from normal
conversions (ticked), flags merges, applies only what's ticked, lets you
opt a protected tag back in, and that Apply is one undoable step.

Run: python3 tests/test_reformat_dialog.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.state import SessionState
from core.scanner import scan
from ui.reformat_tags_dialog import ReformatTagsDialog, _PAIR_ROLE


def _dataset():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    imgs = [
        ("a", ["black_elbow_gloves", "blue_eyes"]),
        ("b", ["blue eyes", "^_^"]),        # existing spaced tag (merge target) + face
        ("c", ["o_o", "long_hair"]),         # lettered emoticon + normal
    ]
    for name, tags in imgs:
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")
    return d


def _items(lst):
    return {
        lst.item(i).data(_PAIR_ROLE): lst.item(i)
        for i in range(lst.count())
    }


def run() -> None:
    app = QApplication.instance() or QApplication([])
    d = _dataset()
    st = SessionState(scan(d))
    dlg = ReformatTagsDialog(st)

    # Default direction: underscores -> spaces.
    main = _items(dlg.list_main)
    prot = _items(dlg.list_protected)
    main_olds = {old for old, _ in main}
    prot_olds = {old for old, _ in prot}

    # Normal word-joins are in the main list, ticked.
    assert "black_elbow_gloves" in main_olds and "blue_eyes" in main_olds and "long_hair" in main_olds, main_olds
    for it in main.values():
        assert it.checkState() == Qt.CheckState.Checked
    # Emoticons are in the protected list, UNticked.
    assert "^_^" in prot_olds and "o_o" in prot_olds, prot_olds
    for it in prot.values():
        assert it.checkState() == Qt.CheckState.Unchecked
    # The blue_eyes -> blue eyes row is flagged as a merge (b already has it).
    assert "merges" in main[("blue_eyes", "blue eyes")].text(), main[("blue_eyes", "blue eyes")].text()
    print("OK: dialog splits protected emoticons out (unticked) and flags merges")

    # Apply with defaults: normal tags convert, emoticons untouched.
    rmap = dlg._collect_checked()
    assert ("^_^", "^ ^") not in rmap.items() and "o_o" not in rmap, rmap
    res = st.apply_tag_rename_map(rmap)
    assert res["tags"] == 3, res  # black_elbow_gloves, blue_eyes, long_hair

    bp = {p.name: p for p in st._image_tags}
    assert st._image_tags[bp["a.png"]] == ["black elbow gloves", "blue eyes"]
    # ^_^ and o_o survive untouched.
    assert "^_^" in st._image_tags[bp["b.png"]], st._image_tags[bp["b.png"]]
    assert "o_o" in st._image_tags[bp["c.png"]], st._image_tags[bp["c.png"]]
    # blue_eyes merged into existing "blue eyes".
    assert st.get_tag_count_global("blue_eyes") == 0
    assert st.get_tag_count_global("blue eyes") == 2
    print("OK: applying defaults converts normal tags, leaves emoticons intact")

    # One undo reverses the whole thing.
    assert len(st._undo_stack) == 1
    assert st.undo() is True
    assert st._image_tags[bp["a.png"]] == ["black_elbow_gloves", "blue_eyes"]

    # Opt-in: reopen, tick the o_o protected row, apply -> it converts.
    dlg2 = ReformatTagsDialog(st)
    prot2 = _items(dlg2.list_protected)
    prot2[("o_o", "o o")].setCheckState(Qt.CheckState.Checked)
    rmap2 = dlg2._collect_checked()
    assert rmap2.get("o_o") == "o o", rmap2
    st.apply_tag_rename_map(rmap2)
    assert "o_o" not in st._image_tags[bp["c.png"]]
    assert "o o" in st._image_tags[bp["c.png"]], st._image_tags[bp["c.png"]]
    print("OK: a protected tag can be opted in and then converts")

    # Direction toggle rebuilds the lists (spaces -> underscores now).
    st.undo()  # revert the o_o opt-in
    dlg3 = ReformatTagsDialog(st)
    dlg3.rb_to_unders.setChecked(True)
    app.processEvents()
    main3 = {old for old, _ in _items(dlg3.list_main)}
    # "long hair" doesn't exist; but "blue eyes" (spaced) should now appear as a source.
    assert any(" " in old for old in main3), main3
    print("OK: switching direction rebuilds the preview")

    print("\nALL PASS: reformat dialog")


if __name__ == "__main__":
    run()
