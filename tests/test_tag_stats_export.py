"""
tests/test_tag_stats_export.py

Tests for the tag-statistics export (Tools feature).

Field request: an AI-readable Markdown summary of the dataset's tag
distribution, centered on the token-count / image-count RATIO
(learning-likelihood signal), with vocabulary-bucket filtering that
reuses tag_database.bucket_for — the same classifier behind the tree
filter and the audit, so all three always agree.

NSFW suppression is intentionally absent (design under discussion).

Run: python3 tests/test_tag_stats_export.py
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

from core.state import SessionState
from core.scanner import scan, scan_many
from core import tag_database as tdb
from ui.tag_stats_dialog import build_stats_report


def _state():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    caps = {
        "a": "long_hair, aoi_my_oc",
        "b": "long_hair, crimson_scarf",
        "c": "long_hair, aoi_my_oc, autotag_v3",
    }
    for n, t in caps.items():
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(t, encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "orph.png")   # orphan
    st = SessionState(scan(d))
    assert tdb.get_database().ensure_loaded()
    return st


def test_shared_bucket_fn() -> None:
    # bucket_for auto-loads the DB on first use (and raises rather
    # than silently misclassifying if it can't load).
    assert tdb.bucket_for("long_hair") == "known"
    assert tdb.bucket_for("long hair") == "known"        # folded
    assert tdb.bucket_for("1girls") == "known"           # alias
    assert tdb.bucket_for("crimson_scarf") == "color_combo"
    assert tdb.bucket_for("aoi_my_oc") == "custom"
    print("OK: bucket_for is the shared single source (folded, "
          "alias-aware) for filter, audit and export")


def test_full_report_numbers() -> None:
    st = _state()
    r = build_stats_report(st, {"known", "color_combo", "custom"}, True)
    assert "Images: 4" in r and "with tags: 3" in r
    assert "no caption file: 1 (orph.png)" in r      # named, findable
    assert "known 1" in r and "color-combo 1" in r and "custom 2" in r
    assert "| long_hair | 3 | 0.750 |" in r      # the headline ratio
    assert "| aoi_my_oc | 2 | 0.500 |" in r
    assert "| autotag_v3 | 1 | 0.250 |" in r
    assert "for AI reviewers" in r               # ratio definition given
    print("OK: totals, per-tag ratios (count/total-images) and the AI "
          "reading guide are all present and correct")


def test_bucket_filtering() -> None:
    st = _state()
    custom_only = build_stats_report(st, {"custom"}, True)
    assert "| aoi_my_oc | 2" in custom_only
    assert "| long_hair |" not in custom_only
    assert "| crimson_scarf |" not in custom_only
    assert "Included below: Custom tags" in custom_only
    combo_only = build_stats_report(st, {"color_combo"}, True)
    assert "| crimson_scarf | 1" in combo_only
    assert "| aoi_my_oc |" not in combo_only
    print("OK: bucket checkboxes translate into filtered exports "
          "(unique-conditioning views)")


def test_sorting_within_bucket() -> None:
    st = _state()
    r = build_stats_report(st, {"custom"}, True)
    assert r.find("| aoi_my_oc |") < r.find("| autotag_v3 |")
    print("OK: rows sorted by image count descending, then name")


def test_db_unavailable_degrades_unclassified() -> None:
    st = _state()
    r = build_stats_report(st, set(), False)
    assert "unclassified" in r and "NOTE:" in r
    assert "| long_hair | 3 | 0.750 |" in r      # data still exports
    print("OK: without the vocabulary DB the export still works, "
          "clearly marked unclassified — never silently empty")


def test_breakdown_and_multi_root_header() -> None:
    """Field reports, both fixed here: (1) an EMPTY caption file was
    mislabeled "no caption file" — the header now distinguishes and
    NAMES both kinds; (2) multi-folder exports named only the first
    root — the header now lists every loaded folder."""
    base = Path(tempfile.mkdtemp())
    r1, r2 = base / "set_a", base / "set_b"
    r1.mkdir(parents=True)
    r2.mkdir(parents=True)
    for n in ("a", "b", "c"):
        Image.new("RGB", (8, 8)).save(r1 / f"{n}.png")
        (r1 / f"{n}.txt").write_text("long_hair", encoding="utf-8")
    Image.new("RGB", (8, 8)).save(r1 / "emptycap.png")
    (r1 / "emptycap.txt").write_text("   ", encoding="utf-8")
    Image.new("RGB", (8, 8)).save(r1 / "noorph.png")   # no txt at all
    Image.new("RGB", (8, 8)).save(r2 / "d.png")
    (r2 / "d.txt").write_text("long_hair, smile", encoding="utf-8")
    st = SessionState(scan_many([r1, r2]))
    assert tdb.get_database().ensure_loaded()
    r = build_stats_report(st, {"known", "color_combo", "custom"}, True)
    assert "Images: 6" in r and "with tags: 4" in r
    assert "empty caption file: 1 (emptycap.png)" in r
    assert "no caption file: 1 (noorph.png)" in r
    assert "Dataset roots (2):" in r
    assert str(r1) in r and str(r2) in r
    assert "| long_hair | 4 | 0.667 |" in r     # denominator = 6
    print("OK: header distinguishes and names empty vs missing "
          "caption files, and lists every loaded root")


def run() -> None:
    test_shared_bucket_fn()
    test_full_report_numbers()
    test_bucket_filtering()
    test_sorting_within_bucket()
    test_db_unavailable_degrades_unclassified()
    test_breakdown_and_multi_root_header()
    print("\nALL PASS: tag statistics export")


if __name__ == "__main__":
    run()
