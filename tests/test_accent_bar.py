"""
tests/test_accent_bar.py

The bar marking the row the program is working on.

FIELD REPORT: Qt's click highlight and the row being walked drift
apart, and bold text alone was not enough to tell them apart at a
glance.

A left-edge bar is the right shape because it COMPOSES. The queue
already paints rows green, red and amber for yes/no/skipped, and Qt
paints its own selection over whatever it likes; a bar occupies a
strip nothing else uses, so it survives every combination.

The hard requirement was "functional under all program colour themes
and easily distinguishable", which is a measurable claim: at least
3.5:1 contrast against every background the bar can sit on, in all
seven themes. Two themes fail with their own accent unmodified — Dark
manages 2.5:1 against its green rows, Solarized Light 2.7:1 — so the
colour is derived rather than hand-picked.

Run: python3 tests/test_accent_bar.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import (
    ACCENT_BAR_MIN_CONTRAST,
    ACCENT_BAR_WIDTH,
    THEME_NAMES,
    THEME_PALETTES,
    Colors,
    accent_bar_colour,
    contrast_ratio,
    initialize_theme,
)
from core.scanner import scan
from core.state import SessionState
from ui.accent_bar import CURRENT_ROLE, AccentBarDelegate

# Every background a row can have when the bar is drawn on it.
ROW_BACKGROUNDS = ["SUCCESS_BG", "DANGER_BG", "WARNING_BG",
                   "BG_ELEVATED", "BG_BASE"]


def test_every_theme_is_legible() -> None:
    for name in THEME_NAMES:
        palette = THEME_PALETTES[name]
        bar = accent_bar_colour(palette)
        for key in ROW_BACKGROUNDS:
            background = palette.get(key)
            if not background:
                continue
            ratio = contrast_ratio(bar, background)
            assert ratio >= ACCENT_BAR_MIN_CONTRAST, (
                f"{name}: bar {bar} on {key} {background} "
                f"is only {ratio:.1f}:1")
    print("OK: the accent bar clears 3.5:1 against every row state in "
          "all seven themes")


def test_the_colour_is_derived_not_fixed() -> None:
    """A single fixed colour cannot work: Cyberpunk's accent is
    yellow and Dracula's is purple. The derivation keeps each theme's
    hue and moves only lightness, and only when it has to."""
    chosen = {name: accent_bar_colour(THEME_PALETTES[name])
              for name in THEME_NAMES}
    assert len(set(chosen.values())) > 1        # genuinely per-theme

    # Themes that already pass are left completely alone.
    for name in ("Nord", "Dracula", "Cyberpunk", "Green", "Light"):
        assert chosen[name] == THEME_PALETTES[name]["ACCENT_BLUE"], name
    # The two that fail are adjusted rather than replaced.
    for name in ("Dark", "Solarized Light"):
        assert chosen[name] != THEME_PALETTES[name]["ACCENT_BLUE"], name

    # And loading a theme publishes it.
    for name in THEME_NAMES:
        initialize_theme(name)
        assert Colors.ACCENT_BAR == chosen[name], name
    initialize_theme("Dark")
    print("OK: each theme gets its own bar colour, adjusted only when "
          "the theme's own accent is not legible enough")


def test_both_panels_mark_the_current_row() -> None:
    from ui.main_window import MainWindow

    s = Settings()
    initialize_theme(s.theme)
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    for i in range(4):
        Image.new("RGB", (8, 8)).save(folder / f"i{i}.png")
        (folder / f"i{i}.txt").write_text("1girl, smile",
                                          encoding="utf-8")

    mw = MainWindow(s)
    mw._adopt_state(SessionState(scan(folder)), scan_root=None)
    mw._state.select_tag("1girl")
    queue, tree = mw._queue_panel, mw._tag_tree

    assert isinstance(queue.list_widget.itemDelegate(),
                      AccentBarDelegate)
    assert isinstance(tree.tree.itemDelegateForColumn(0),
                      AccentBarDelegate)
    assert ACCENT_BAR_WIDTH >= 3                # visible at a glance

    marked = [i for i in range(queue.list_widget.count())
              if queue.list_widget.item(i).data(CURRENT_ROLE)]
    assert len(marked) == 1

    tagged = [t for t, item in tree._tag_items.items()
              if item.data(0, CURRENT_ROLE)]
    assert tagged == ["1girl"]

    # The mark follows the walk, not the last click — which is the
    # entire reason it exists.
    mw._state.select_tag("smile")
    tagged = [t for t, item in tree._tag_items.items()
              if item.data(0, CURRENT_ROLE)]
    assert tagged == ["smile"]
    print("OK: both panels mark exactly one current row, and the mark "
          "follows the walk rather than the selection")


def run() -> None:
    test_every_theme_is_legible()
    test_the_colour_is_derived_not_fixed()
    test_both_panels_mark_the_current_row()
    print("\nALL PASS: accent bar")


if __name__ == "__main__":
    run()
