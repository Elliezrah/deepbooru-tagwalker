"""
tests/test_getting_started.py

The guide under Help, and that it says the thing that matters.

FIELD OBSERVATION: the program has a dozen tools and, until this
existed, no opinion about which to reach for first. Someone opening it
with a fresh dataset met a menu rather than a path — and for a program
whose whole claim is speed, knowing what to do first matters as much
as each step being quick.

The test that earns its place here is the one on page 5. A new user
assumes the walk means answering every tag, discovers that 64 images
with 200 unique tags is twelve thousand decisions, and concludes the
program does not scale. Telling them plainly that selective walking is
the intended use is worth more than any feature, so it must not be
quietly edited away.

Run: python3 tests/test_getting_started.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from ui.getting_started import PAGES
from ui.main_window import MainWindow


def test_the_guide_gives_an_order() -> None:
    headings = [h for h, _b in PAGES]
    assert len(PAGES) >= 7

    # Numbered steps, in order, so it reads as a path.
    numbered = [h for h in headings if h[0].isdigit()]
    assert [h[0] for h in numbered] == sorted(h[0] for h in numbered)
    assert len(numbered) >= 6

    # Every page says something; a heading with no body is worse than
    # no page.
    for heading, body in PAGES:
        assert heading.strip(), PAGES
        assert len(body) > 80, heading
    print("OK: the guide is an ordered sequence of steps, each with "
          "real content")


def test_the_walk_page_respects_the_design() -> None:
    """CORRECTED, and worth recording why.

    The first version of this page told people NOT to walk every tag.
    That contradicted the program: a tag counts as COMPLETED only when
    every image carrying it has a Yes or a No, and the completion
    percentage exists to drive exactly that. Advising against it was
    advising against the feature the tool is built around.

    The honest framing is that complete auditing is the intended path
    and costs a full day for sixty-four images; stopping short is a
    legitimate choice the percentage keeps visible."""
    walk = next(b for h, b in PAGES if h.startswith("5 \u00b7"))
    assert "COMPLETE audit" in walk
    assert "completion percentage" in walk
    # The cost is stated plainly rather than glossed.
    assert "full day" in walk
    # It must NOT tell people to skip the audit.
    assert "Don't" not in walk and "do NOT" not in walk.lower()

    faster = next(b for h, b in PAGES if h.startswith("5b"))
    assert "Auto-confirm" in faster      # the biggest lever
    assert "filter mode" in faster
    assert "legitimate choice" in faster
    print("OK: the walk page presents a complete audit as the "
          "intended path, states its cost, and names the levers that "
          "shorten it")


def test_it_recommends_a_backup_first() -> None:
    """There is no built-in backup — a deliberate choice, since
    copying a folder is something Windows already does well. So the
    advice has to be given instead."""
    first_heading, first_body = PAGES[0]
    assert "Before you touch" in first_heading
    assert "Copy your caption files" in first_body
    # And it explains why, rather than just instructing.
    assert "undo covers the current session only" in first_body
    print("OK: step zero recommends a backup and says why undo is not "
          "a substitute")


def test_it_opens_from_the_help_menu() -> None:
    s = Settings()
    initialize_theme(s.theme)
    mw = MainWindow(s)
    assert "Getting Started" in mw._act_guide.text()
    mw._action_getting_started()
    dialog = mw._guide_dlg
    assert dialog.windowTitle() == "Getting Started"
    assert len(dialog._pages) == len(PAGES)
    print("OK: the guide opens from Help as a paged booklet")


def test_the_readme_carries_the_same_workflow() -> None:
    """A reader deciding whether to download needs the same answer,
    and the two must not drift apart."""
    readme = (Path(__file__).resolve().parent.parent
              / "README.md").read_text(encoding="utf-8")
    assert "What using it actually looks like" in readme
    assert "complete audit" in readme        # the intended path
    assert "rules are\nunconditional" in readme or \
           "unconditional" in readme          # the conflict caveat
    assert "Auto-confirm" in readme          # the lever that shortens it
    assert "full day" in readme              # cost stated honestly
    assert "Help → Getting Started" in readme  # points at the program
    print("OK: the README states the same workflow and points at the "
          "in-program guide")


def test_the_conflict_pages_carry_the_caveat() -> None:
    """FIELD NOTE: "long_hair and short_hair shouldn't be treated as a
    conflict for two-character scenes."

    Correct, and the feature cannot express the distinction — rules
    are unconditional, with no way to write "only when solo". A rule
    added for a single-character dataset fires on every legitimate
    multi-character image in a mixed one, so the guide has to say so
    before someone writes the rule and stops trusting the tool."""
    pages = dict(PAGES)
    intro = next(b for h, b in PAGES if h.startswith("2b"))
    assert "Conflict Rules" in intro
    assert "EXCLUSION" in intro and "REQUIREMENT" in intro

    caveat = next(b for h, b in PAGES if h.startswith("2c"))
    assert "long_hair" in caveat and "short_hair" in caveat
    assert "two-character" in caveat
    assert "UNCONDITIONAL" in caveat

    # CORRECTED. The first version told people to skip such a rule on
    # a mixed dataset. That was too conservative: the scan gives every
    # violation a "View image" button precisely so the case can be
    # judged on the picture, and nothing is changed until an action is
    # chosen. An over-reporting rule costs time, not accuracy.
    assert "View image" in caveat
    assert "costs you time, not accuracy" in caveat
    assert "not do is fix in bulk" in caveat
    print("OK: the guide explains that unconditional rules are a "
          "reason to review each hit, not to avoid the rule")


def test_the_audit_is_in_the_mechanical_step() -> None:
    """It belongs with the changes that need no judgement."""
    mechanical = next(b for h, b in PAGES if h.startswith("2 "))
    assert "Tag Audit" in mechanical
    assert "201,269" in mechanical           # the bundled list
    assert "undone in one step" in mechanical
    print("OK: the Danbooru tag audit sits in the mechanical-fixes "
          "step, where no judgement is required")


def run() -> None:
    test_the_guide_gives_an_order()
    test_the_walk_page_respects_the_design()
    test_the_conflict_pages_carry_the_caveat()
    test_the_audit_is_in_the_mechanical_step()
    test_it_recommends_a_backup_first()
    test_it_opens_from_the_help_menu()
    test_the_readme_carries_the_same_workflow()
    print("\nALL PASS: getting started")


if __name__ == "__main__":
    run()
