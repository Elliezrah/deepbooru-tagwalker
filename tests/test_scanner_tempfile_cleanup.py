"""Tests for the scanner's stale write-temp cleanup.

tag_io's atomic writes stage content in ".tw_tmp_*.txt" files and clean
them up on any failure — but a hard kill (crash, power loss) between
the temp write and os.replace orphans one inside the user's dataset
folder. The scanner now removes those, conservatively: exact pattern
only, and only when over a minute old, so a live writer's in-flight
temp (lifetime: milliseconds) can never be touched.

Run: python3 tests/test_scanner_tempfile_cleanup.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from PIL import Image

from core.scanner import scan


def _dataset() -> Path:
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(d / "a.png")
    (d / "a.txt").write_text("1girl", encoding="utf-8")
    return d


def _backdate(p: Path, seconds: int) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def test_old_temp_removed_fresh_and_lookalikes_kept() -> None:
    d = _dataset()
    stale = d / ".tw_tmp_deadbeef0001.txt"
    stale.write_text("orphan", encoding="utf-8")
    _backdate(stale, 300)

    fresh = d / ".tw_tmp_cafebabe0002.txt"
    fresh.write_text("in-flight", encoding="utf-8")   # just created

    lookalike = d / "tw_tmp_users_own.txt"            # no leading dot
    lookalike.write_text("user file", encoding="utf-8")
    _backdate(lookalike, 300)

    r = scan(d)
    assert r.stale_tempfiles_removed == 1, r.stale_tempfiles_removed
    assert not stale.exists()
    assert fresh.exists(), "a young temp might belong to a live writer"
    assert lookalike.exists(), "only our exact pattern may be touched"
    # The dataset itself is untouched.
    assert r.total_images == 1 and r.total_tags == 1
    print("OK: an old orphaned temp is removed; young temps and "
          "lookalike names are left alone")


def test_clean_dataset_removes_nothing() -> None:
    d = _dataset()
    r = scan(d)
    assert r.stale_tempfiles_removed == 0
    print("OK: a clean dataset reports zero removals")


def test_multiple_stale_temps_all_removed() -> None:
    d = _dataset()
    for i in range(3):
        p = d / f".tw_tmp_{i:012x}.txt"
        p.write_text("x", encoding="utf-8")
        _backdate(p, 120)
    r = scan(d)
    assert r.stale_tempfiles_removed == 3, r.stale_tempfiles_removed
    assert not list(d.glob(".tw_tmp_*.txt"))
    print("OK: several accumulated orphans are all swept in one scan")


def run() -> None:
    test_old_temp_removed_fresh_and_lookalikes_kept()
    test_clean_dataset_removes_nothing()
    test_multiple_stale_temps_all_removed()
    print("\nALL PASS: stale write-temp cleanup on scan")


if __name__ == "__main__":
    run()
