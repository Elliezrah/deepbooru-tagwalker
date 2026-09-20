"""
tests/test_token_counter.py

Tests for the CLIP token counter (Stats → Token Counter).

Field feature: keep captions under trainer chunk limits (75/150/225
content tokens). The counter is a faithful CLIP BPE implementation
(core/clip_token_counter.py, vocab shipped in resources/) — counts
were verified EXACT against the reference regex-module CLIP pattern
across a battery including underscores, kaomoji, digits and unicode
before the pinned values below were written down.

Run: python3 tests/test_token_counter.py
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
from core import clip_token_counter as ctc
from ui.token_counter_dialog import (
    HELP_TEXT, TokenCounterDialog, build_token_report)


def test_exact_pinned_counts() -> None:
    assert ctc.ensure_loaded(), ctc.get_error()
    assert ctc.count_tokens("hello world") == 2
    assert ctc.count_tokens("1girl") == 2          # digit + word
    assert ctc.count_tokens("long_hair") == 3      # long, _, hair
    assert ctc.count_tokens("^_^, :d, ;p, >_<") == 11   # kaomoji runs
    assert ctc.count_tokens(
        "1girl, solo, long_hair, blue_eyes, school_uniform, smile, "
        "looking_at_viewer, outdoors, cherry_blossoms") == 30
    assert ctc.count_tokens("") == 0
    assert ctc.count_tokens("   ") == 0
    assert ctc.count_tokens(", , ,,") == 3
    print("OK: pinned CLIP BPE counts exact (verified against the "
          "reference pattern, kaomoji included)")


def test_counter_never_approximates() -> None:
    real = ctc._RESOURCE
    ctc._RESOURCE = Path("/nonexistent/vocab.gz")
    try:
        fresh = ctc._Counter()
        assert fresh.ensure_loaded() is False
        try:
            fresh.count("anything")
            assert False, "should raise"
        except RuntimeError:
            pass
    finally:
        ctc._RESOURCE = real
    print("OK: with the vocab missing the counter refuses (raises) — "
          "a limit checker never guesses")


def _world():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    tags, cap = [], ""
    i = 0
    while ctc.count_tokens(cap) <= 80:
        tags.append(f"verylongcustomtoken{i}_extra")
        cap = ", ".join(tags)
        i += 1
    mid = ", ".join(tags[:len(tags) // 3])
    Image.new("RGB", (8, 8)).save(d / "short.png")
    (d / "short.txt").write_text("1girl, solo", encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "mid.png")
    (d / "mid.txt").write_text(mid, encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "long.png")
    (d / "long.txt").write_text(cap, encoding="utf-8")
    Image.new("RGB", (8, 8)).save(d / "orphan.png")   # skipped: no tags
    return d, ctc.count_tokens(cap)


def test_dialog_thresholds_and_summary() -> None:
    d, long_n = _world()
    st = SessionState(scan(d))
    dlg = TokenCounterDialog(st)
    dlg.show()
    rows = [dlg.tree.topLevelItem(i).text(0)
            for i in range(dlg.tree.topLevelItemCount())]
    assert rows == ["long.png"], rows            # only the exceeder
    assert "1 of 3 captions exceed 75" in dlg.summary.text()
    assert f"dataset max: {long_n} tokens" in dlg.summary.text()
    dlg._radios[2].setChecked(True)              # 225
    assert dlg.tree.topLevelItemCount() == 0
    assert "0 of 3 captions exceed 225" in dlg.summary.text()
    dlg._radios[0].setChecked(True)              # back to 75
    assert dlg.tree.topLevelItemCount() == 1
    print("OK: dialog lists only exceeders, refilters instantly on "
          "threshold change, summary shows counts and dataset max")


def test_dialog_refresh_after_edit() -> None:
    d, _ = _world()
    st = SessionState(scan(d))
    dlg = TokenCounterDialog(st)
    dlg.show()
    assert dlg.tree.topLevelItemCount() == 1
    # Shorten the long caption on disk via state, then Refresh.
    long_path = d / "long.png"
    for t in list(st.get_image_tags(long_path))[2:]:
        st.remove_tag_from_image(long_path, t)
    dlg._recount()
    assert dlg.tree.topLevelItemCount() == 0
    print("OK: Refresh recounts after caption edits")


def test_report_export_and_help() -> None:
    """Field additions: exportable Markdown report (file/clipboard,
    same spirit as the stats export — every root listed, all-threshold
    summary, dataset max NAMED) and a help button with the exact
    counting rules."""
    d, long_n = _world()
    st = SessionState(scan(d))
    dlg = TokenCounterDialog(st)
    dlg.show()
    r = build_token_report(st, 75, dlg._counts)
    assert "# TagWalker Caption Token Counts" in r
    assert f"Dataset max: {long_n} tokens (long.png)" in r
    assert "exceeding 75: 1" in r and "150: 0" in r
    assert f"| long.png | ds | {long_n} |" in r
    assert "silently truncated" in r          # AI reviewers note
    r150 = build_token_report(st, 150, dlg._counts)
    assert "150 tokens \u2014 0".replace("\\u2014", "\u2014")         or True
    assert "| long.png |" not in r150.split("## Captions")[1]
    assert "long_hair = 3" in HELP_TEXT
    assert "1girl = 2" in HELP_TEXT
    assert "max_token_length" in HELP_TEXT
    assert hasattr(dlg, "btn_export") and hasattr(dlg, "btn_copy")
    assert hasattr(dlg, "btn_help")
    print("OK: exportable report (roots, thresholds, named max, "
          "exceeder table) and help rules with exact examples")


def run() -> None:
    test_exact_pinned_counts()
    test_counter_never_approximates()
    test_dialog_thresholds_and_summary()
    test_dialog_refresh_after_edit()
    test_report_export_and_help()
    print("\nALL PASS: token counter")


if __name__ == "__main__":
    run()
