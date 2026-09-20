"""Tests for the tag tree's vocabulary filter (field feature request:
"show non-canonical tags so I can inspect my uniquely made tokens",
amended: color+canonical combinations must be separated).

Two new entries in the tree's existing "Show:" dropdown:
  - Custom tags (not in Danbooru): tokens absent from the 201k DB —
    the user's inventions, and typos (a feature: both deserve eyes).
  - Color + known combos: <color/shade>_<registered tag> compounds not
    themselves registered (light_blue_thighhighs), via the AUDIT's own
    grammar so the two features can never disagree.
Known = canonical OR alias, judged on the FOLDED form — "long hair"
counts as long_hair per the field spec. Some color+noun combos are
themselves canonical (red_coat, aqua_thighhighs — verified) and thus
"known", which keeps both special views clean.

All fixtures below were verified against the shipped database before
being written down.

Run: python3 tests/test_tag_class_filter.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image
from PySide6.QtWidgets import QApplication, QMessageBox

_app = QApplication.instance() or QApplication([])

from core.state import SessionState
from core.scanner import scan
from core import tag_database as tdb
from ui.tag_tree import TagTree


def _tree():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    caps = {
        "a": "long_hair, red_coat, autotag_v3",
        "b": "long hair, aqua_thighhighs, light_blue_thighhighs",
        "c": "1girls, crimson_scarf, aoi_my_oc",
    }
    for n, t in caps.items():
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(t, encoding="utf-8")
    st = SessionState(scan(d))
    tree = TagTree()
    tree.attach(st)
    tree.resize(280, 420)
    tree.show()
    return tree, st


def _visible(tree):
    return sorted(t for t, item in tree._tag_items.items()
                  if not item.isHidden())


def _pick(tree, key):
    for i in range(tree.category_filter.count()):
        if tree.category_filter.itemData(i) == key:
            tree.category_filter.setCurrentIndex(i)
            break


def test_bucket_classifier() -> None:
    tree, _ = _tree()
    b = tree._tag_bucket
    # Known: canonical, folded spelling variant, canonical color+noun,
    # and Danbooru aliases.
    for t in ("long_hair", "long hair", "red_coat", "aqua_thighhighs",
              "1girls"):
        assert b(t) == "known", (t, b(t))
    # Unregistered grammar compounds.
    for t in ("light_blue_thighhighs", "crimson_scarf"):
        assert b(t) == "color_combo", (t, b(t))
    # Inventions and typos.
    for t in ("autotag_v3", "aoi_my_oc", "red_curtian"):
        assert b(t) == "custom", (t, b(t))
    # Cached: second call hits the dict.
    assert "autotag_v3" in tree._tag_class_cache
    print("OK: buckets — folded known (incl. aliases and canonical "
          "color+noun tags), grammar compounds, custom inventions/typos")


def test_custom_view_shows_only_unique_tokens() -> None:
    tree, st = _tree()
    st.select_tag("long_hair")
    _pick(tree, "custom_tags")
    assert _visible(tree) == ["aoi_my_oc", "autotag_v3"], _visible(tree)
    assert st.current_tag == "long_hair"      # filtering never moves walk
    print("OK: Custom view isolates uniquely made tokens; the walk is "
          "untouched")


def test_color_combo_view() -> None:
    tree, _ = _tree()
    _pick(tree, "color_combos")
    assert _visible(tree) == \
        ["crimson_scarf", "light_blue_thighhighs"], _visible(tree)
    print("OK: Color-combo view shows unregistered <color>_<known> "
          "compounds only — canonical ones stay under known")


def test_search_composes_and_all_restores() -> None:
    tree, _ = _tree()
    _pick(tree, "custom_tags")
    tree.search_box.setText("auto")
    assert _visible(tree) == ["autotag_v3"]
    tree.search_box.setText("")
    _pick(tree, "all")
    assert len(_visible(tree)) == 9
    print("OK: text search ANDs with the vocabulary view; All restores")


def test_db_failure_warns_once_and_reverts() -> None:
    tree, _ = _tree()
    warned = []
    real_warn = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))
    db = tdb.get_database()
    real_el = db.ensure_loaded
    db.ensure_loaded = lambda: False
    tree._tag_db_ready = False
    tree._tag_db_failed = False
    tree._tag_class_cache.clear()
    try:
        _pick(tree, "custom_tags")
        _app.processEvents()      # the deferred combo revert
        assert len(warned) == 1, warned
        assert tree.category_filter.currentData() == "all"
    finally:
        db.ensure_loaded = real_el
        QMessageBox.warning = real_warn
    print("OK: a missing tag database warns once and reverts to All — "
          "never a silent empty view")


def run() -> None:
    test_bucket_classifier()
    test_custom_view_shows_only_unique_tokens()
    test_color_combo_view()
    test_search_composes_and_all_restores()
    test_db_failure_warns_once_and_reverts()
    print("\nALL PASS: vocabulary filter (custom / color-combo views)")


if __name__ == "__main__":
    run()
