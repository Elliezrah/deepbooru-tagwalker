"""
tests/test_getting_started.py

Tests for the User Guide (Help - User Guide) - the categorized help
browser that replaced the old linear "Getting Started" walkthrough.
Content lives in ui/help_content.HELP_SECTIONS; the browser shows a
category list left and the selected body (scrollable) right, with
{keybind} placeholders resolved to the user's current bindings.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from config.settings import Settings
from config.theme import initialize_theme
from ui.help_content import HELP_SECTIONS
from ui.main_window import MainWindow


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv if sys.argv else ["test"])
    return app


def test_sections_are_substantial_and_titled():
    assert len(HELP_SECTIONS) >= 12
    for title, body in HELP_SECTIONS:
        assert title.strip(), "a section has no title"
        assert len(body) > 80, f"section '{title}' body too thin"
    print("OK: every help section has a title and substantial body")


def test_the_expected_categories_exist():
    titles = [t for t, _ in HELP_SECTIONS]
    assert any(t.startswith("Controls") for t in titles)
    assert any(t.startswith("The tools") for t in titles)
    assert any(t.startswith("Workflow") for t in titles)
    assert any(t.startswith("Safety") for t in titles)
    assert any("Tag Referencer" in t for t in titles)
    assert any("Pruning Advisor" in t for t in titles)
    assert any("Token counter" in t for t in titles)
    print("OK: Controls / The tools / Workflow / Safety categories present")


def test_recommended_workflow_is_present_and_ordered():
    workflow = [t for t, _ in HELP_SECTIONS
                if t.startswith("Workflow \u00b7 ")
                and any(c.isdigit() for c in t)]
    assert len(workflow) >= 6
    nums = [int(next(c for c in t if c.isdigit())) for t in workflow]
    assert nums == sorted(nums), "workflow steps out of order"
    print("OK: recommended workflow present and in order")


def test_the_walk_topic_respects_the_design():
    walk = next(b for t, b in HELP_SECTIONS
                if t.startswith("Workflow") and "Walk" in t)
    assert "COMPLETE audit" in walk
    assert "completion percentage" in walk
    assert "full day" in walk
    faster = next(b for t, b in HELP_SECTIONS if "faster" in t.lower())
    assert "Auto-confirm" in faster
    assert "filter mode" in faster
    assert "deliberate choice" in faster or "legitimate choice" in faster
    print("OK: the walk topic keeps the complete-vs-partial design")


def test_backup_guidance_survives():
    safety = next(b for t, b in HELP_SECTIONS if t.startswith("Safety"))
    assert "Copy your caption files" in safety
    assert "undo covers the current session only" in safety
    print("OK: back-up-first guidance is present")


def test_keybind_placeholders_resolve_in_the_browser():
    _app()
    initialize_theme("dark")
    s = Settings(ini_path=Path(tempfile.mktemp(suffix=".ini")))
    from ui.help_browser_dialog import HelpBrowserDialog
    d = HelpBrowserDialog(s)
    assert d._list.count() == len(HELP_SECTIONS)
    for i in range(d._list.count()):
        if "core loop" in d._list.item(i).text():
            d._list.setCurrentRow(i)
            body = d._body.text()
            assert "{yes}" not in body and "{no}" not in body
            assert "Y" in body
            break
    else:
        raise AssertionError("core loop topic not found")
    print("OK: keybind placeholders resolve to current bindings")


def test_it_opens_from_the_help_menu():
    _app()
    initialize_theme("dark")
    s = Settings(ini_path=Path(tempfile.mktemp(suffix=".ini")))
    mw = MainWindow(s)
    assert "User Guide" in mw._act_guide.text()
    mw._action_getting_started()
    assert mw._guide_dlg.__class__.__name__ == "HelpBrowserDialog"
    print("OK: opens from Help menu as the User Guide browser")


if __name__ == "__main__":
    test_sections_are_substantial_and_titled()
    test_the_expected_categories_exist()
    test_recommended_workflow_is_present_and_ordered()
    test_the_walk_topic_respects_the_design()
    test_backup_guidance_survives()
    test_keybind_placeholders_resolve_in_the_browser()
    test_it_opens_from_the_help_menu()
    print("\nALL PASS: user guide")
