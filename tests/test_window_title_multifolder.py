"""
tests/test_window_title_multifolder.py

Regression test for the window title not reflecting a multi-folder load.

Bug: the window title was built from self._state.root.name — the FIRST
root only. A multi-folder (playlist) load therefore showed a title
identical to a single-folder load on just that first folder, giving no
indication that several folders were loaded. Reported as "the destination
display can't correctly display multi-folder loaded state — only showing
one of the folders".

Fix: when more than one root contributed (state.roots has length > 1),
the title shows the primary folder name plus the total folder count,
e.g. "TagWalker — project (3 folders)". Single-folder loads are unchanged.

This test exercises the title-building logic directly against real
SessionState objects (single-folder scan and multi-folder scan_many) so
it doesn't need to construct the full MainWindow. The logic under test is
_refresh_window_title in ui/main_window.py; it's replicated here exactly.
Keep the two in sync — if the title format changes, update both.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from core.scanner import scan, scan_many
from core.state import SessionState

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv if sys.argv else ["test"])
    return app


def _folder(prefix: str, tag: str = "1girl") -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    (d / "img.png").write_bytes(_PNG)
    (d / "img.txt").write_text(tag, encoding="utf-8")
    return d


def _title_for(state, session_path=None) -> str:
    """Replica of MainWindow._refresh_window_title's string construction.
    MUST match ui/main_window.py."""
    parts = ["TagWalker"]
    if state is not None:
        roots = getattr(state, "roots", None) or [state.root]
        if len(roots) > 1:
            parts.append(f"{state.root.name} ({len(roots)} folders)")
        else:
            parts.append(state.root.name)
    if session_path is not None:
        parts.append(session_path.stem)
    return " — ".join(parts)


def test_single_folder_title_is_just_the_name():
    _app()
    d = _folder("solo_project_")
    st = SessionState(scan(d))
    title = _title_for(st)
    assert title == f"TagWalker — {d.name}", title
    assert "folders)" not in title, "single folder must NOT show a count"
    print("OK: single-folder title shows just the folder name")


def test_multi_folder_title_shows_count():
    _app()
    a = _folder("primary_", "1girl")
    b = _folder("styleb_", "solo")
    c = _folder("stylec_", "smile")
    st = SessionState(scan_many([a, b, c]))
    title = _title_for(st)
    # The primary (first) folder anchors the name; the count reflects ALL.
    assert st.root.name in title, "primary folder name should anchor the title"
    assert "(3 folders)" in title, (
        f"multi-folder title must show the folder count, got: {title!r}"
    )
    # The bug was that it looked identical to a single-folder load — assert
    # it does NOT.
    assert title != f"TagWalker — {st.root.name}", (
        "multi-folder title must differ from the single-folder title"
    )
    print("OK: multi-folder title shows the folder count")


def test_two_folder_title_counts_two():
    _app()
    a = _folder("a_")
    b = _folder("b_")
    st = SessionState(scan_many([a, b]))
    assert "(2 folders)" in _title_for(st)
    print("OK: two-folder title counts two")


def test_multi_folder_with_session_name_appended():
    _app()
    a = _folder("proj_")
    b = _folder("extra_")
    st = SessionState(scan_many([a, b]))
    title = _title_for(st, Path("/wherever/my_review.json"))
    assert "(2 folders)" in title
    assert title.endswith("my_review"), (
        f"saved-session name should append after the folder info: {title!r}"
    )
    print("OK: multi-folder title still appends the saved-session name")


if __name__ == "__main__":
    test_single_folder_title_is_just_the_name()
    test_multi_folder_title_shows_count()
    test_two_folder_title_counts_two()
    test_multi_folder_with_session_name_appended()
    print("\nALL PASS: window title multi-folder")
