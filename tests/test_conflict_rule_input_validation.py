"""Tests for conflict-rule tag-content validation.

A conflict rule's tags flow back into caption files (the scan's "Add
missing" fix writes rule.requires), so a comma or line break in one
must be caught. Two layers:

1. The rule editor rejects invalid tags on save, naming the entries.
2. The scan's apply step — reachable with a malformed rule via a
   hand-edited rules JSON — no longer drops the fix silently: it warns
   which rule tag can't be written and to repair the rule.

Run: python3 tests/test_conflict_rule_input_validation.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox
from PIL import Image

_app = QApplication.instance() or QApplication([])

from core.state import SessionState
from core.scanner import scan
from core.conflict_rules import ConflictRule, RULE_REQUIREMENT
from ui.conflict_rules_dialog import _RuleEditDialog
from ui.conflict_scan_dialog import ConflictScanDialog


def _capture_info(captured):
    QMessageBox.information = staticmethod(
        lambda *a, **k: captured.append(a[2] if len(a) > 2 else "")
    )


def _capture_warn(captured):
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    QMessageBox.warning = staticmethod(
        lambda *a, **k: captured.append(a[2] if len(a) > 2 else "")
    )


def test_editor_rejects_comma_in_requires() -> None:
    dlg = _RuleEditDialog()
    dlg.radio_req.setChecked(True)
    dlg.trigger_box.setText("cat_ears")
    dlg.requires_box.setText("animal_ears, tail")  # two tags in one box
    msgs: list = []
    _capture_info(msgs)
    dlg._on_save()
    assert msgs and "one tag per box" in msgs[-1], msgs
    assert dlg.result() != dlg.DialogCode.Accepted
    # The half-built rule must not have absorbed the bad value.
    assert dlg._rule.requires != "animal_ears, tail"
    print("OK: the editor rejects a comma in the required tag and "
          "does not accept the dialog")


def test_editor_rejects_comma_in_exclusion_tag() -> None:
    dlg = _RuleEditDialog()
    dlg.radio_excl.setChecked(True)
    rows = []
    for i in range(dlg.tag_rows.count()):
        host = dlg.tag_rows.itemAt(i).widget()
        if host is not None and hasattr(host, "_tag_box"):
            rows.append(host)
    assert len(rows) >= 2, "editor should start with tag boxes"
    rows[0]._tag_box.setText("indoors, outdoors")  # comma smuggled in
    rows[1]._tag_box.setText("beach")
    msgs: list = []
    _capture_info(msgs)
    dlg._on_save()
    assert msgs and "one tag per box" in msgs[-1], msgs
    print("OK: the editor rejects a comma in an exclusion tag box")


def test_editor_accepts_valid_requirement() -> None:
    dlg = _RuleEditDialog()
    dlg.radio_req.setChecked(True)
    dlg.trigger_box.setText("cat_ears")
    dlg.requires_box.setText("animal_ears")
    msgs: list = []
    _capture_info(msgs)
    dlg._on_save()
    assert not msgs, msgs
    assert dlg._rule.requires == "animal_ears"
    assert dlg._rule.tags == ["cat_ears"]
    print("OK: a valid requirement rule still saves normally")


def test_scan_apply_names_unwritable_rule_tag() -> None:
    # A malformed rule can only arrive via a hand-edited rules file —
    # simulate that by handing the scan a rule object directly.
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(d / "a.png")
    (d / "a.txt").write_text("cat_ears, solo", encoding="utf-8")
    st = SessionState(scan(d))

    bad = ConflictRule(rule_type=RULE_REQUIREMENT, tags=["cat_ears"],
                       requires="animal_ears, tail", name="hand-edited")
    violations = st.scan_conflicts([bad])
    assert len(violations) == 1, violations  # comma-string never matches

    dlg = ConflictScanDialog(st)
    dlg._populate(violations)
    dlg._rows[0].set_add_missing()

    warns: list = []
    _capture_warn(warns)
    dlg._on_confirm()

    assert warns, "expected a warning about the unwritable rule tag"
    joined = "\n".join(warns)
    assert "animal_ears, tail" in joined, joined
    assert "Edit Conflict Rules" in joined, joined
    # Nothing was written: the caption is unchanged.
    a = next(i.image_path for i in st.all_images if i.image_path.stem == "a")
    assert st.get_image_tags(a) == ["cat_ears", "solo"]
    print("OK: applying a fix from a malformed (hand-edited) rule warns "
          "with the bad tag instead of silently dropping the fix")


def run() -> None:
    test_editor_rejects_comma_in_requires()
    test_editor_rejects_comma_in_exclusion_tag()
    test_editor_accepts_valid_requirement()
    test_scan_apply_names_unwritable_rule_tag()
    print("\nALL PASS: conflict-rule tag-content validation (editor + "
          "scan apply)")


if __name__ == "__main__":
    run()
