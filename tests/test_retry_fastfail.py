"""Tests for the adaptive retry fast-fail in _write_tags_with_retry.

The transient-lock retry (0.24s of sleep per failing file) is right for
momentary antivirus/indexer locks but wrong for systemic failure: a
dataset whose files carry Windows' read-only attribute would freeze a
big batch for minutes while changing nothing. After a short streak of
consecutive persistent failures the primitive now skips the sleeps
(single attempt per file). Every file is still attempted and named; any
success — or the start of a new batch — resets the streak so transient
locks keep their retries.

Run: python3 tests/test_retry_fastfail.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from core.state import SessionState
from core.scanner import scan
from core import tag_io

_REAL_WRITE = tag_io.write_tags
_REAL_SLEEP = time.sleep


def _build(n: int) -> Path:
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for i in range(n):
        Image.new("RGB", (8, 8)).save(d / f"im{i:04d}.png")
        (d / f"im{i:04d}.txt").write_text("1girl, oldtag", encoding="utf-8")
    return d


def _restore() -> None:
    tag_io.write_tags = _REAL_WRITE
    time.sleep = _REAL_SLEEP


def test_wholly_unwritable_batch_fast_fails_but_names_everything() -> None:
    d = _build(120)
    st = SessionState(scan(d))
    sleeps = [0]
    tag_io.write_tags = lambda p, t: (_ for _ in ()).throw(OSError("ro"))
    time.sleep = lambda s: sleeps.__setitem__(0, sleeps[0] + 1)
    try:
        t0 = time.monotonic()
        applied = st.rename_tag_globally("oldtag", "newtag")
        elapsed = time.monotonic() - t0
    finally:
        _restore()
    assert applied == 0
    assert len(st.last_write_failures) == 120, len(st.last_write_failures)
    # Streak of 3 slow files x 2 sleeps each, then fast-fail.
    max_sleeps = st._WRITE_RETRY_GIVEUP_STREAK * 2
    assert sleeps[0] == max_sleeps, sleeps[0]
    assert elapsed < 3.0, elapsed
    print("OK: an all-unwritable batch fast-fails after the streak — "
          f"{sleeps[0]} sleeps for 120 files, every file still named")


def test_success_resets_streak_so_transients_still_recover() -> None:
    d = _build(8)
    st = SessionState(scan(d))
    names = sorted(p.name for p in d.glob("*.txt"))
    hard = set(names[:3])                 # max the streak first
    transient = {names[5]: True}          # then one momentary lock

    def spy(p, t):
        n = Path(p).name
        if n in hard:
            raise OSError("locked hard")
        if transient.get(n):
            transient[n] = False
            raise OSError("momentary")
        return _REAL_WRITE(p, t)

    tag_io.write_tags = spy
    time.sleep = lambda s: None
    try:
        applied = st.rename_tag_globally("oldtag", "newtag")
    finally:
        _restore()
    assert applied == 5, applied
    assert names[5] not in st.last_write_failures, st.last_write_failures
    assert sorted(st.last_write_failures) == names[:3]
    print("OK: a success resets the streak; a later transient lock is "
          "still retried and recovers")


def test_new_batch_gets_fresh_retries_after_a_disaster() -> None:
    d = _build(4)
    st = SessionState(scan(d))
    tag_io.write_tags = lambda p, t: (_ for _ in ()).throw(OSError("x"))
    time.sleep = lambda s: None
    try:
        st.rename_tag_globally("oldtag", "t1")   # maxes the streak
    finally:
        _restore()

    attempts: list[str] = []
    once = [True]

    def spy(p, t):
        attempts.append(Path(p).name)
        if once[0]:
            once[0] = False
            raise OSError("transient at batch start")
        return _REAL_WRITE(p, t)

    tag_io.write_tags = spy
    time.sleep = lambda s: None
    try:
        applied = st.rename_tag_globally("oldtag", "t2")
    finally:
        _restore()
    assert applied == 4, applied
    assert st.last_write_failures == [], st.last_write_failures
    # The very first file of the new batch was retried (two attempts).
    assert attempts.count(attempts[0]) == 2, attempts
    print("OK: a new batch resets the streak — its first transient lock "
          "still gets a retry")


def test_single_edit_path_unaffected() -> None:
    d = _build(1)
    st = SessionState(scan(d))
    p = next(iter(st._image_tags))
    assert st.add_tag_to_image(p, "fresh") is True
    assert tag_io.read_tags(d / "im0000.txt") == ["1girl", "oldtag", "fresh"]
    print("OK: normal single edits are unaffected")


def run() -> None:
    test_wholly_unwritable_batch_fast_fails_but_names_everything()
    test_success_resets_streak_so_transients_still_recover()
    test_new_batch_gets_fresh_retries_after_a_disaster()
    test_single_edit_path_unaffected()
    print("\nALL PASS: adaptive retry fast-fail (no minutes-long freeze on "
          "read-only datasets; nothing silent)")


if __name__ == "__main__":
    run()
