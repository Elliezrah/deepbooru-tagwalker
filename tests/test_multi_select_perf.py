"""Performance guard for the multi-select checkbox toggle.

Toggling multi-select adds/removes a checkbox on every tag row. The tag
tree's column 0 is ResizeToContents, which rescans every row on each item
change — so a naive bulk update is O(n^2) and froze for ~14s on a
~1,800-tag dataset. _set_checkboxes_now pins the column width and freezes
repaints during the bulk pass to keep it O(n).

This asserts the toggle stays fast on a large tag tree. The bound is
deliberately loose (the fix runs in well under a second; the O(n^2)
regression took many seconds) so it flags a real regression without
being flaky on slow machines.

Run: python3 tests/test_multi_select_perf.py
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

from PySide6.QtWidgets import QApplication

from core.state import SessionState
from core.scanner import scan

BUDGET_S = 4.0  # fixed path ~0.1s; O(n^2) regression was 10s+


def run() -> None:
    app = QApplication.instance() or QApplication([])

    root = Path(tempfile.mkdtemp()) / "ds"
    root.mkdir(parents=True)
    vocab = [f"character_name_{i}" for i in range(1400)]
    import random
    for n in range(700):
        tags = ["1girl"] + random.sample(vocab, 8)
        Image.new("RGB", (8, 8)).save(root / f"img_{n:04d}.png")
        (root / f"img_{n:04d}.txt").write_text(
            ", ".join(sorted(set(tags))), encoding="utf-8"
        )

    from config.settings import Settings
    from config.theme import initialize_theme
    s = Settings()
    initialize_theme(s.theme)
    from ui.tag_tree import TagTree

    st = SessionState(scan(root))
    tt = TagTree()
    tt.attach(st)
    tt.show()
    app.processEvents()
    n_tags = len(st.tree_order)
    assert n_tags > 1000, f"expected a large tag tree, got {n_tags}"

    t = time.time()
    tt._set_checkboxes_now(True)
    app.processEvents()
    on_s = time.time() - t

    t = time.time()
    tt._set_checkboxes_now(False)
    app.processEvents()
    off_s = time.time() - t

    assert on_s < BUDGET_S, f"enter too slow: {on_s:.2f}s for {n_tags} tags"
    assert off_s < BUDGET_S, f"exit too slow: {off_s:.2f}s for {n_tags} tags"

    print(f"OK: multi-select toggle fast on {n_tags} tags "
          f"(enter {on_s:.2f}s, exit {off_s:.2f}s, budget {BUDGET_S}s)")


if __name__ == "__main__":
    run()
