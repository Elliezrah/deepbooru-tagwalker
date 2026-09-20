"""UI regression test for the audit exception list.

Covers the manager dialog (add/remove, persisted), the audit dialog's
"Always OK" per-row button (excepts a tag and filters it on re-scan),
and that the Tools-menu action is wired in the main window.

Run: python3 tests/test_audit_exceptions_ui.py
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

from core import audit_exceptions_io as aex
from core.state import SessionState
from core.scanner import scan


def _manager_dialog() -> None:
    from ui.audit_exceptions_dialog import AuditExceptionsDialog
    dlg = AuditExceptionsDialog()
    # Add by hand.
    dlg.add_box.setText("Studio_Foo")
    dlg._on_add()
    assert "studio_foo" in aex.load_exceptions()
    assert any(dlg.list.item(i).text() == "studio_foo"
               for i in range(dlg.list.count()))
    # Select + remove.
    for i in range(dlg.list.count()):
        if dlg.list.item(i).text() == "studio_foo":
            dlg.list.item(i).setSelected(True)
    dlg._on_remove()
    assert "studio_foo" not in aex.load_exceptions()


def _audit_except_flow(app) -> None:
    d = Path(tempfile.mkdtemp()) / "i"
    d.mkdir(parents=True)
    for n in ("img1", "img2"):
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text("1girl, zzz_oc_tag_xyz", encoding="utf-8")
    st = SessionState(scan(d))

    from ui.tag_audit_dialog import TagAuditDialog
    dlg = TagAuditDialog(st)
    dlg._on_scan()
    rows = [r for r in dlg._rows if r.tag == "zzz_oc_tag_xyz"]
    assert len(rows) == 1, "made-up tag should be flagged as unknown"
    assert hasattr(rows[0], "btn_except")

    rows[0]._on_except_clicked()
    assert "zzz_oc_tag_xyz" in aex.load_exceptions()
    assert not any(r.tag == "zzz_oc_tag_xyz" for r in dlg._rows)

    dlg._on_scan()  # re-scan: excepted tag stays gone
    assert not any(r.tag == "zzz_oc_tag_xyz" for r in dlg._rows)

    # Clean up so we don't leave it excepted for other checks.
    aex.remove_exception("zzz_oc_tag_xyz")


def _main_window_action() -> None:
    from config.settings import Settings
    from config.theme import initialize_theme
    s = Settings()
    initialize_theme(s.theme)
    from ui.main_window import MainWindow
    w = MainWindow(s)
    assert hasattr(w, "_act_audit_exceptions")
    assert hasattr(w, "_action_audit_exceptions")


def run() -> None:
    app = QApplication.instance() or QApplication([])
    _manager_dialog()
    _audit_except_flow(app)
    _main_window_action()
    print("OK: audit exception UI verified "
          "(manager add/remove, audit Always-OK button + re-scan filter, "
          "Tools action wired)")


if __name__ == "__main__":
    run()
