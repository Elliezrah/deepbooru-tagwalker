"""Regression test for the skip / complete tag behavior (report item L).

Standalone (no pytest needed):  python tests/test_skip_behavior.py

This locks in the behavior verified during investigation of the "skip is
broken" report: the SessionState logic for skipping a tag, resuming it,
deciding its images, and marking it complete is correct. If a future change
breaks any of these, this test fails loudly.

What it asserts:
  1. record_skip_tag       -> status SKIPPED
  2. deciding any image    -> SKIPPED is cleared (PENDING while work remains)
  3. deciding all images   -> status COMPLETED
  4. skip then mark_tag_complete -> status COMPLETED (works from SKIPPED)
  5. resume a skipped tag (re-select) -> queue rebuilds and is walkable
"""

import os
import sys
import tempfile
from pathlib import Path

# Allow running from the repo root or the tests/ dir.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def _build_state():
    from PIL import Image
    from core.scanner import scan
    from core.state import SessionState

    d = tempfile.mkdtemp()

    def mk(name, tags):
        p = os.path.join(d, name)
        Image.new("RGB", (8, 8)).save(p + ".png")
        with open(p + ".txt", "w", encoding="utf-8") as f:
            f.write(", ".join(tags))

    mk("a", ["1girl", "smile"])
    mk("b", ["1girl", "smile"])
    mk("c", ["1girl"])
    return SessionState(scan(Path(d)))


def main() -> int:
    from core.state import TagStatus

    st = _build_state()

    # 1) skip
    st.select_tag("smile")
    st.record_skip_tag("smile")
    assert st.get_tag_status("smile") == TagStatus.SKIPPED, "skip should set SKIPPED"

    # 2) resume + one decision clears the skip
    st.select_tag("smile")
    assert len(st._current_queue) > 0, "resumed tag should have a walkable queue"
    st.record_yes()
    assert st.get_tag_status("smile") != TagStatus.SKIPPED, (
        "a decision must clear SKIPPED"
    )

    # 3) deciding the rest completes the tag
    guard = 0
    while st.current_image is not None and guard < 50:
        st.record_yes()
        guard += 1
    assert st.get_tag_status("smile") == TagStatus.COMPLETED, (
        "deciding all images should complete the tag"
    )

    # 4) mark_tag_complete works straight from SKIPPED
    st.select_tag("1girl")
    st.record_skip_tag("1girl")
    assert st.get_tag_status("1girl") == TagStatus.SKIPPED
    st.mark_tag_complete("1girl")
    assert st.get_tag_status("1girl") == TagStatus.COMPLETED, (
        "mark_tag_complete must work from SKIPPED"
    )

    print("OK: skip/complete behavior verified (all 4 cases pass)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
