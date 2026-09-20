"""
tests/test_tag_reference.py

Tests for the Tag Reference feature (DESIGN_TAG_REFERENCE.md).

Wave A covers the offline core (core/tag_reference.py): loading every
shipped snapshot, era counts, drift verdicts, alias resolution both
directions, category names, prefix search — all against the REAL
shipped CSVs, since they are part of the repo and exactly what ships.

Run: python3 tests/test_tag_reference.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from core import tag_reference as tr


def test_all_shipped_eras_load() -> None:
    assert tr.ensure_loaded()
    keys = [k for k, _ in tr.loaded_eras()]
    assert keys == ["current", "mid2024", "pony", "ancient"], keys
    print("OK: all four shipped snapshots load, newest to oldest")


def test_era_counts_and_stable_verdict() -> None:
    info = tr.lookup("long_hair", "mid2024")
    counts = {e.key: e.count for e in info.era_counts}
    assert all(v is not None for v in counts.values())
    assert (counts["ancient"] < counts["pony"]
            < counts["mid2024"] < counts["current"])
    assert info.verdict.startswith("stable")
    assert info.category_name == "general"
    assert not info.is_custom
    print("OK: long_hair carries ascending counts across all four "
          "eras, stable verdict, category resolved")


def test_alias_redirect_and_reverse_aliases() -> None:
    ali = tr.lookup("1girls", "mid2024")
    assert ali.canonical == "1girl"
    assert ali.redirected_from == "1girls"
    assert "renamed" in ali.verdict and "1girl" in ali.verdict
    direct = tr.lookup("1girl", "mid2024")
    assert direct.redirected_from is None
    assert "renamed" not in direct.verdict     # canonical is not a rename
    assert "1girls" in direct.aliases
    print("OK: alias lookups redirect with a renamed verdict; direct "
          "canonical lookups do not, and list their reverse aliases")


def test_custom_tag_verdict() -> None:
    info = tr.lookup("aoi_my_oc", "mid2024")
    assert info.is_custom
    assert info.verdict.startswith("custom")
    assert all(e.count is None for e in info.era_counts)
    print("OK: an invented tag is custom in every era")


def test_postdating_tag_detected() -> None:
    """Deterministically pick a real tag that exists in the current
    snapshot but not in the Nov-2024 one, and confirm the verdict says
    a model of that era never learned it."""
    assert tr.ensure_loaded()
    cur, mid = tr._TABLES["current"], tr._TABLES["mid2024"]
    fold = next(f for f, c in sorted(cur.counts.items(),
                                     key=lambda kv: -kv[1])
                if f not in mid.counts and f not in mid.alias_to
                and c > 500)
    info = tr.lookup(cur.display[fold], "mid2024")
    assert "postdates" in info.verdict
    assert "Illustrious" in info.verdict       # names the selected era
    print(f"OK: postdating tag detected ({cur.display[fold]}) with the "
          "selected era named in the verdict")


def test_pure_verdicts() -> None:
    E = tr.EraCount
    retired = tr.drift_verdict(
        [E("current", "Latest", None), E("mid2024", "Nov24", 100)],
        "mid2024", None)
    assert retired.startswith("retired")
    renamed = tr.drift_verdict(
        [E("current", "Latest", 5)], "current", "new_name")
    assert "alias of new_name" in renamed
    custom = tr.drift_verdict(
        [E("current", "Latest", None)], "current", None)
    assert custom.startswith("custom")
    print("OK: pure verdict function covers retired / renamed / custom")


def test_prefix_search() -> None:
    hits = tr.search("long_h", 10)
    assert "long_hair" in hits and len(hits) <= 10
    assert tr.search("zzzznotag") == []
    assert tr.search("") == []
    print("OK: prefix search over current-era canonical names")


def test_window_render_navigation_and_paths() -> None:
    """Wave B: the window renders offline data, the co-occurrence list
    doubles as navigation with back/forward history, and the alias /
    custom paths render their special cases."""
    from PySide6.QtCore import Qt
    from config.settings import Settings
    from config.theme import initialize_theme
    from ui.tag_reference_window import TagReferenceWindow

    s = Settings()
    initialize_theme(s.theme)
    win = TagReferenceWindow(s)
    win.show()
    win.navigate("long_hair")
    assert "long_hair" in win.header.text()
    assert "general" in win.header.text()
    assert win.era_label.text().count("<br>") == 3      # four eras
    assert "your era" in win.era_label.text()
    # The verdict now carries a colour cue, so match on content.
    assert "Verdict:" in win.verdict.text()
    assert "stable" in win.verdict.text()
    assert win.cooc_list.count() >= 5

    partner = win.cooc_list.item(0).data(Qt.ItemDataRole.UserRole)
    win._on_cooc_clicked(win.cooc_list.item(0))
    assert partner in win.header.text()
    assert win.search.text() == partner
    win._go_back()
    assert "long_hair" in win.header.text()
    win._go_forward()
    assert partner in win.header.text()

    win.navigate("1girls")
    assert "redirected from" in win.header.text()
    assert win.search.text() == "1girl"
    win.navigate("aoi_my_oc")
    assert "custom" in win.verdict.text()
    assert win.cooc_list.count() == 1
    assert "no co-occurrence" in win.cooc_list.item(0).text()
    print("OK: window renders eras/verdict/aliases, co-occurrence "
          "click navigates with working back/forward, alias and "
          "custom paths handled")


def test_follow_current_tag_debounced() -> None:
    """The pin tracks the walk through the state's tag_selected
    events with a debounce; unpinning stops it."""
    from pathlib import Path
    from PIL import Image
    from config.settings import Settings
    from config.theme import initialize_theme
    from core.state import SessionState
    from core.scanner import scan
    from ui.tag_reference_window import TagReferenceWindow

    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for n, tags in {"a": "long_hair, smile", "b": "smile"}.items():
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(tags, encoding="utf-8")
    st = SessionState(scan(d))
    s = Settings()
    initialize_theme(s.theme)
    win = TagReferenceWindow(s)
    win.show()
    win.set_state(st)
    st.select_tag("smile")
    assert win._debounce.isActive()
    assert win._pending_follow == "smile"
    win._debounce.stop()
    win._on_follow_debounce()
    assert "smile" in win.header.text()
    win.chk_follow.setChecked(False)
    st.select_tag("long_hair")
    assert not win._debounce.isActive()
    assert "smile" in win.header.text()      # did not follow
    print("OK: follow-current-tag is debounced and respects the pin")


def test_main_window_entry_points() -> None:
    """View menu carries the action; both panels expose
    reference_requested and firing it opens the window at the tag —
    driving the REAL wiring (the suite's UI-handler blind spot rule)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMenu, QToolButton
    from config.settings import Settings
    from config.theme import initialize_theme
    from ui.main_window import MainWindow

    s = Settings()
    initialize_theme(s.theme)
    mw = MainWindow(s)
    # Promoted out of the View menu into a permanent control at the
    # right-hand end of the menu bar.
    view_items = []
    for menu in mw.findChildren(QMenu):
        if menu.title() and "View" in menu.title():
            view_items = [a.text() for a in menu.actions() if a.text()]
    assert not any("Tag Refer" in t for t in view_items), view_items
    # The corner now holds a pair: post browser, then Tag Referencer.
    host = mw.menuBar().cornerWidget(Qt.Corner.TopRightCorner)
    buttons = host.findChildren(QToolButton)
    assert len(buttons) == 2
    assert "Post Browser" in buttons[0].text()
    btn = buttons[1]
    assert isinstance(btn, QToolButton)
    assert "Tag Referencer" in btn.text()   # carries a globe icon
    assert hasattr(mw._tag_tree, "reference_requested")
    assert hasattr(mw._file_state_panel, "reference_requested")
    mw._file_state_panel.reference_requested.emit("long_hair")
    assert getattr(mw, "_tag_ref_win", None) is not None
    assert "long_hair" in mw._tag_ref_win.header.text()
    print("OK: Tag Referencer has a permanent menu-bar button (no "
          "longer a View entry); panel right-click signals drive the "
          "real open-at-tag path")


def run() -> None:
    test_all_shipped_eras_load()
    test_era_counts_and_stable_verdict()
    test_alias_redirect_and_reverse_aliases()
    test_custom_tag_verdict()
    test_postdating_tag_detected()
    test_pure_verdicts()
    test_prefix_search()
    test_window_render_navigation_and_paths()
    test_follow_current_tag_debounced()
    test_main_window_entry_points()
    print("\nALL PASS: tag reference (offline wave)")


if __name__ == "__main__":
    run()
