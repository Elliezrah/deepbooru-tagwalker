"""
tests/test_image_properties.py

The per-image properties sheet, and the token badge beside the caption.

Both exist for the same reason: while pruning, the numbers you are
working against are needed continuously, and opening a separate tool
for each image is not that.

The properties sheet is shaped like the Windows one deliberately —
label left, value right, grouped under headings — because that is the
form people already read without instruction. What it adds is what
Explorer cannot know: the caption's token count against the training
limits, and how many decisions this image has taken.

Run: python3 tests/test_image_properties.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PIL import Image
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import prewarm
from core.scanner import scan
from core.state import SessionState
from ui.file_state_panel import FileStatePanel
from ui.image_properties_dialog import ImagePropertiesDialog
from ui.main_window import MainWindow


def _window(folder: Path):
    s = Settings()
    initialize_theme(s.theme)
    mw = MainWindow(s)
    mw._adopt_state(SessionState(scan(folder)), scan_root=None)
    return mw


def test_the_sheet_reports_what_explorer_would_and_more() -> None:
    prewarm.reset_for_tests()
    prewarm.run_all()
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    Image.new("RGB", (1024, 768)).save(folder / "portrait.png")
    (folder / "portrait.txt").write_text(
        ", ".join(["1girl", "solo", "long_hair"]
                  + [f"t{i:03d}" for i in range(90)]), encoding="utf-8")

    mw = _window(folder)
    mw._state.select_tag("1girl")
    mw._state.record_yes()
    dialog = ImagePropertiesDialog(mw._state.all_images[0], mw._state)
    rows = dict(dialog._rows)

    # What a file manager would tell you.
    assert "PNG" in rows["Type"]
    assert "1,024 × 768" in rows["Dimensions"]
    assert "4:3" in rows["Aspect ratio"]
    assert "landscape" in rows["Aspect ratio"]
    assert "bytes" in rows["Size"]          # exact count kept alongside
    assert "portrait.png" in rows["Full path"]

    # What it could not.
    assert "over the 225-token limit" in rows["CLIP tokens"]
    assert rows["Tags"] == "93"
    assert "yes" in rows["Decisions recorded"]

    # Unicode must be rendered, not printed as escapes — an em dash
    # written "\\u2014" in the source reaches the screen literally.
    assert not any("\\u" in str(v) for _k, v in dialog._rows)

    dialog._copy_all()
    assert "Dimensions" in QGuiApplication.clipboard().text()
    print("OK: the properties sheet reports file facts plus the token "
          "count and decisions Explorer cannot know")


def test_an_image_with_no_caption_is_handled() -> None:
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    Image.new("RGB", (64, 64)).save(folder / "lonely.png")

    mw = _window(folder)
    dialog = ImagePropertiesDialog(mw._state.all_images[0], mw._state)
    rows = dict(dialog._rows)
    assert "none" in rows["File"]
    # It stops rather than showing empty token and tag rows.
    assert "CLIP tokens" not in rows
    print("OK: an image with no caption says so instead of showing "
          "blank rows")


def test_the_token_badge_colours_against_the_limits() -> None:
    prewarm.reset_for_tests()
    prewarm.run_all()
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    for i, count in enumerate((5, 45, 130)):
        Image.new("RGB", (24, 24)).save(folder / f"i{i}.png")
        (folder / f"i{i}.txt").write_text(
            ", ".join(f"tag_{j:03d}" for j in range(count)),
            encoding="utf-8")

    mw = _window(folder)
    mw._state.select_tag("tag_000")
    panel = mw.findChildren(FileStatePanel)[0]

    seen = []
    for entry in mw._state.all_images:
        panel._refresh_token_badge(entry)
        seen.append(panel.label_tokens.text())

    assert "29 tok" in seen[0]
    assert "over limit" not in seen[0]
    # The long ones are flagged, and flagged in colour rather than
    # only in words.
    assert all("over limit" in text for text in seen[1:])
    assert all("color:" in text for text in seen)
    print("OK: the caption's token count sits beside it, coloured "
          "against the limits that matter")


def run() -> None:
    test_the_sheet_reports_what_explorer_would_and_more()
    test_an_image_with_no_caption_is_handled()
    test_the_token_badge_colours_against_the_limits()
    print("\nALL PASS: image properties")


if __name__ == "__main__":
    run()
