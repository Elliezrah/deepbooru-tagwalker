"""Regression test for the co-occurrence-hints master show/hide toggle.

The "Show co-occurrence hints" preference (default on) hides the
"Often appears with…" panel entirely when off, independent of the
source/threshold. This test drives the file-state panel: with hints
that genuinely exist (dataset source, threshold relaxed so nothing is
suppressed), toggling the preference shows/hides the panel.

Run: python3 tests/test_cooccur_toggle.py
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

from core.state import SessionState
from core.scanner import scan
from ui.file_state_panel import FileStatePanel
from config.settings import Settings


def run() -> None:
    app = QApplication.instance() or QApplication([])

    d = Path(tempfile.mkdtemp()) / "i"
    d.mkdir(parents=True)
    for n, tags in [("img1", ["1girl", "smile", "long_hair"]),
                    ("img2", ["1girl", "smile", "long_hair"]),
                    ("img3", ["1girl", "smile"])]:
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(", ".join(tags), encoding="utf-8")

    st = SessionState(scan(d))
    st.set_cooccur_source("dataset")
    st.set_cooccur_too_common_pct(100)   # relax so hints aren't suppressed
    fp = FileStatePanel()
    fp.attach(st)
    st.select_tag("1girl")

    # Default on.
    assert st.show_cooccur_hints is True
    assert st.get_related_tags_for_tag("1girl", limit=8), "fixture must yield hints"

    fp._refresh()
    assert not fp.label_hints.isHidden(), "hints should show when ON"

    st.set_show_cooccur_hints(False)
    fp._refresh()
    assert fp.label_hints.isHidden(), "hints must hide when OFF"

    st.set_show_cooccur_hints(True)
    fp._refresh()
    assert not fp.label_hints.isHidden(), "hints should show again when ON"

    # Settings round-trip.
    s = Settings()
    assert s.default_show_cooccur_hints is True
    s.default_show_cooccur_hints = False
    assert s.default_show_cooccur_hints is False
    s.default_show_cooccur_hints = True

    print("OK: co-occurrence show/hide toggle verified "
          "(panel shows when on, hides when off, settings round-trip)")


if __name__ == "__main__":
    run()
