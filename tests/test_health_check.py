"""
tests/test_health_check.py

Tests for Tools → Dataset Health Check (field feature: detect dataset
errors before training).

Three toggleable checks: missing caption file, empty caption file
(exists but no tags — the case that hid in a 1187-image field
dataset), and unusual resolution (min side under 512 px, aspect ratio
beyond 3:1, or an unreadable image file). One row per finding, an
image may appear more than once, healthy images never appear.

Fixture notes (learned in probing): a 1600x400 image trips BOTH
resolution rules (min side 400 < 512), so the extreme-aspect fixture
uses 1600x512; the corrupt image gets a caption file so it is flagged
for unreadability only.

Run: python3 tests/test_health_check.py
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
from core.scanner import scan
from ui.health_check_dialog import (
    CHECK_EMPTY,
    CHECK_MISSING,
    CHECK_RESOLUTION,
    CHECK_SQUARE,
    CHECK_STRAY,
    HealthCheckDialog,
    find_issues,
)


def _world():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (800, 800)).save(d / "healthy.png")
    (d / "healthy.txt").write_text("1girl", encoding="utf-8")
    Image.new("RGB", (100, 100)).save(d / "tiny.png")
    (d / "tiny.txt").write_text("1girl", encoding="utf-8")
    Image.new("RGB", (1600, 512)).save(d / "stretched.png")
    (d / "stretched.txt").write_text("1girl", encoding="utf-8")
    Image.new("RGB", (800, 800)).save(d / "no_txt.png")
    Image.new("RGB", (800, 800)).save(d / "empty_txt.png")
    (d / "empty_txt.txt").write_text(" , ", encoding="utf-8")
    (d / "corrupt.png").write_bytes(b"not an image at all")
    (d / "corrupt.txt").write_text("x", encoding="utf-8")
    return d, SessionState(scan(d))


def _kinds(rows):
    return {(r[0], r[2].split(":")[0].split(" (")[0]) for r in rows}


def test_all_checks_find_exactly_the_planted_errors() -> None:
    _, st = _world()
    rows = find_issues(st, {CHECK_MISSING, CHECK_EMPTY, CHECK_RESOLUTION})
    assert _kinds(rows) == {
        ("tiny.png", "small"),
        ("stretched.png", "extreme aspect"),
        ("no_txt.png", "missing caption file"),
        ("empty_txt.png", "empty caption file"),
        ("corrupt.png", "unreadable image file"),
    }, _kinds(rows)
    assert not any(r[0] == "healthy.png" for r in rows)
    print("OK: all five planted errors found, each with a concrete "
          "reason; the healthy image is never flagged")


def test_reasons_carry_details() -> None:
    _, st = _world()
    rows = find_issues(st, {CHECK_RESOLUTION})
    by_name = {r[0]: r[2] for r in rows}
    assert by_name["tiny.png"] == "small: 100x100 (min side under 512)"
    assert by_name["stretched.png"] == \
        "extreme aspect: 1600x512 (ratio 3.1)"
    print("OK: resolution findings state the actual dimensions and "
          "the rule that fired")


def test_per_check_filtering() -> None:
    _, st = _world()
    res_only = find_issues(st, {CHECK_RESOLUTION})
    assert len(res_only) == 3
    assert all("caption" not in r[2] for r in res_only)
    cap_only = find_issues(st, {CHECK_MISSING, CHECK_EMPTY})
    assert _kinds(cap_only) == {
        ("no_txt.png", "missing caption file"),
        ("empty_txt.png", "empty caption file"),
    }
    print("OK: each check can be run independently")


def test_dialog_rows_summary_and_toggles() -> None:
    _, st = _world()
    dlg = HealthCheckDialog(st)
    dlg.show()
    assert dlg.tree.topLevelItemCount() == 5
    assert "5 issue(s) found across 5 file(s)" in dlg.summary.text()
    assert "6 images checked" in dlg.summary.text()
    dlg.chk_res.setChecked(False)
    dlg._run()
    assert dlg.tree.topLevelItemCount() == 2
    dlg.chk_missing.setChecked(False)
    dlg.chk_empty.setChecked(False)
    dlg._run()
    assert dlg.tree.topLevelItemCount() == 0
    assert "No issues found" in dlg.summary.text()
    print("OK: dialog lists findings, summary counts, and check "
          "toggles rerun correctly (clean bill when all off)")


def test_stray_caption_files() -> None:
    """Field case: a hand-named caption with a typo
    ("name_(2)].txt" beside "name_(2).png") paired with nothing, so
    the image looked caption-less. The stray check finds such files
    and, for near-misses, NAMES the similar image so the typo is
    obvious. The classic image.png.txt double-extension mistake gets
    explicit rename advice."""
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (800, 800)).save(d / "tsuki-chan_full_high_(2).png")
    (d / "tsuki-chan_full_high_(2).txt").write_text(
        "1girl, smile", encoding="utf-8")
    (d / "tsuki-chan_full_high_(2)].txt").write_text(
        "1girl, smile, moon", encoding="utf-8")
    Image.new("RGB", (800, 800)).save(d / "healthy.png")
    (d / "healthy.txt").write_text("1girl", encoding="utf-8")
    (d / "healthy.png.txt").write_text("1girl", encoding="utf-8")
    (d / "random_notes.txt").write_text("todo", encoding="utf-8")
    st = SessionState(scan(d))
    rows = find_issues(st, {CHECK_STRAY})
    byname = {r[0]: r[2] for r in rows}
    assert len(rows) == 3, rows
    assert "similar: tsuki-chan_full_high_(2)" in         byname["tsuki-chan_full_high_(2)].txt"]
    assert "rename to healthy.txt" in byname["healthy.png.txt"]
    assert byname["random_notes.txt"] ==         "stray caption file (no matching image)"
    assert "tsuki-chan_full_high_(2).txt" not in byname
    assert "healthy.txt" not in byname
    # No false findings from the other checks on this clean fixture.
    assert len(find_issues(st, {CHECK_MISSING, CHECK_EMPTY,
                                CHECK_RESOLUTION, CHECK_STRAY})) == 3
    dlg = HealthCheckDialog(st)
    dlg.show()
    assert dlg.tree.topLevelItemCount() == 3
    dlg.chk_stray.setChecked(False)
    dlg._run()
    assert dlg.tree.topLevelItemCount() == 0
    print("OK: stray caption files detected — the field typo gets a "
          "similar-image hint, double extensions get rename advice, "
          "proper pairs are never flagged")


def test_square_check_off_by_default_and_independent() -> None:
    """Field feature: non-bucketed training wants perfect squares.
    OFF by default; independent of the unusual-resolution check (an
    image can be flagged by both); unreadable files are reported once
    whichever dimension check is enabled."""
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (800, 800)).save(d / "square_ok.png")
    (d / "square_ok.txt").write_text("1girl", encoding="utf-8")
    Image.new("RGB", (1600, 512)).save(d / "wide.png")
    (d / "wide.txt").write_text("1girl", encoding="utf-8")
    Image.new("RGB", (700, 900)).save(d / "tallish.png")
    (d / "tallish.txt").write_text("1girl", encoding="utf-8")
    (d / "corrupt.png").write_bytes(b"junk")
    (d / "corrupt.txt").write_text("x", encoding="utf-8")
    st = SessionState(scan(d))
    sq = find_issues(st, {CHECK_SQUARE})
    byname = {r[0]: r[2] for r in sq}
    assert byname["wide.png"] == "not square: 1600x512"
    assert byname["tallish.png"] == "not square: 700x900"
    assert byname["corrupt.png"] == "unreadable image file"
    assert "square_ok.png" not in byname and len(sq) == 3
    both = find_issues(st, {CHECK_SQUARE, CHECK_RESOLUTION})
    wide_rows = sorted(r[2] for r in both if r[0] == "wide.png")
    assert wide_rows == [
        "extreme aspect: 1600x512 (ratio 3.1)",
        "not square: 1600x512"]
    assert sum(1 for r in both if r[0] == "corrupt.png") == 1
    dlg = HealthCheckDialog(st)
    dlg.show()
    assert dlg.chk_square.isChecked() is False
    assert not any(
        "not square" in dlg.tree.topLevelItem(i).text(2)
        for i in range(dlg.tree.topLevelItemCount()))
    dlg.chk_square.setChecked(True)
    dlg._run()
    assert sum(
        1 for i in range(dlg.tree.topLevelItemCount())
        if "not square" in dlg.tree.topLevelItem(i).text(2)) == 2
    print("OK: square check off by default, flags every non-square "
          "when enabled, coexists with the resolution check, and "
          "unreadable files are reported exactly once")


def test_stats_menu_merged_into_view() -> None:
    """Field request: Stats menu merged into View. Verified via
    findChildren (holding a menu reference across statements is
    unreliable under the offscreen platform)."""
    from PySide6.QtWidgets import QMenu
    from config.settings import Settings
    from config.theme import initialize_theme
    s = Settings()
    initialize_theme(s.theme)
    from ui.main_window import MainWindow
    w = MainWindow(s)
    titles = []
    view_has_stats = view_has_counter = False
    for m in w.findChildren(QMenu):
        if not m.title():
            continue
        titles.append(m.title())
        if "View" in m.title():
            acts = m.actions()
            view_has_stats = any(a is w._act_tag_stats for a in acts)
            view_has_counter = any(
                a is w._act_token_counter for a in acts)
    assert not any("Stats" in t for t in titles), titles
    assert view_has_stats and view_has_counter
    print("OK: no Stats menu; View carries Export Tag Statistics and "
          "Token Counter")


def run() -> None:
    test_all_checks_find_exactly_the_planted_errors()
    test_reasons_carry_details()
    test_per_check_filtering()
    test_dialog_rows_summary_and_toggles()
    test_stray_caption_files()
    test_square_check_off_by_default_and_independent()
    test_stats_menu_merged_into_view()
    print("\nALL PASS: dataset health check")


if __name__ == "__main__":
    run()
