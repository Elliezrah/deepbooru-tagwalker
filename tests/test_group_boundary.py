"""Regression test for the group-boundary auto-advance stop (report item O).

Standalone (no pytest):  python tests/test_group_boundary.py

Locks in the behavior:
  * EXPANDED group  -> HARD stop. Deciding the last member never auto-
    crosses into the next group; the walk stays parked there no matter how
    many times you decide. (You cross deliberately: click, or batch.)
  * Manual jump across a boundary is NEVER blocked (no trap).
  * COLLAPSED group -> gentle stop-once-then-pass (one stop, then a further
    decision is allowed through).
  * The boundary fires only on DECISION-driven auto-advance, not manual
    navigation (which is why the manual jump above crosses freely).
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())


def _make_dataset():
    from PIL import Image
    d = tempfile.mkdtemp()

    def mk(name, tags):
        p = os.path.join(d, name)
        Image.new("RGB", (8, 8)).save(p + ".png")
        with open(p + ".txt", "w", encoding="utf-8") as f:
            f.write(", ".join(tags))

    for i in range(2):
        mk(f"char_({i})", ["1girl", "smile"])
    for i in range(2):
        mk(f"dog_({i})", ["1girl", "smile"])
    mk("zlone", ["1girl", "smile"])
    return Path(d)


def _setup(expanded):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    from config import theme as T
    from config.settings import Settings
    s = Settings()
    T.initialize_theme(s.theme)
    T.apply_theme(QApplication.instance())

    from core.scanner import scan
    from core.state import SessionState
    from ui.queue_panel import QueuePanel

    class Spy:
        def __getattr__(self, n):
            return lambda *a, **k: None

    st = SessionState(scan(_make_dataset()))
    qp = QueuePanel()
    qp.attach(st)
    qp.set_image_panel(Spy())
    qp.set_file_state_panel(Spy())
    qp._on_group_toggled(True)
    st.select_tag("smile")
    qp._on_group_toggled(True)
    if expanded:
        for k in list(qp._groups_by_key):
            if k not in qp._expanded_bases:
                qp._toggle_group(k)
    return st, qp


def _name(st):
    ci = st.current_image
    return ci.image_path.name if ci else None


def _jump_to(st, sub):
    for i, e in enumerate(st.get_current_queue()):
        if sub in e.image_path.name:
            st.jump_to_queue_index(i)
            return


def main() -> int:
    # EXPANDED -> hard stop, and manual jump still crosses.
    st, qp = _setup(expanded=True)
    _jump_to(st, "char_(0)")
    seq = []
    for _ in range(4):
        st.record_yes()
        seq.append(_name(st))
    assert seq[0] == "char_(1).png", f"should advance within group, got {seq}"
    assert all(x == "char_(1).png" for x in seq[1:]), (
        f"EXPANDED hard stop expected on char_(1), got {seq}"
    )
    _jump_to(st, "dog_(0)")
    assert _name(st) == "dog_(0).png", "manual jump must cross the boundary"

    # COLLAPSED -> stop once, then pass.
    st, qp = _setup(expanded=False)
    _jump_to(st, "char_(0)")
    seq = []
    for _ in range(4):
        st.record_yes()
        seq.append(_name(st))
    assert "dog_(0).png" in seq, f"collapsed should pass after one stop, got {seq}"
    i1 = seq.index("char_(1).png")
    assert seq[i1 + 1] == "char_(1).png", f"should stop once on char_(1), got {seq}"

    # The reverse index (image -> group key) must agree with the
    # authoritative groups dict for every member. This is the O(1)
    # lookup that replaced a full scan run twice per navigation step;
    # if it drifts from _groups_by_key, boundary detection misfires.
    st, qp = _setup(expanded=True)
    assert hasattr(qp, "_group_key_by_image")
    for key, members in qp._groups_by_key.items():
        for e in members:
            assert qp._group_key_of_image(e.image_path) == key, (
                f"reverse index disagrees for {e.image_path.name}")
    from pathlib import Path as _P
    assert qp._group_key_of_image(_P("/nonexistent/none.png")) is None

    print("OK: group-boundary stop verified (expanded hard-stop, "
          "no-trap, collapsed stop-once); group reverse index "
          "consistent")
    return 0


if __name__ == "__main__":
    code = main()
    os._exit(code)
