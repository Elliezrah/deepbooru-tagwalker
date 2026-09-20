"""Tests for the time-boxed runtime-warning snooze.

User request: the untested-Python startup alert must be snoozable for
30 days / 3 months / 6 months / 1 year instead of firing every launch.
These tests pin: the dialog's duration mapping, the due() suppression
rules (snooze per exact pair, expiry, legacy permanent ack, re-arm on
any Python/PySide6 change), the settings round-trip, and the startup
helper's store-on-continue / quit / skip-when-snoozed behavior.

Run: python3 tests/test_runtime_snooze.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from PySide6.QtWidgets import QApplication, QDialog

_app = QApplication.instance() or QApplication([])

from core import crashlog
import main as tw_main
from config.settings import Settings


def _reset(settings: Settings) -> None:
    settings.runtime_warning_acknowledged_pair = ""
    settings.runtime_warning_snooze_pair = ""
    settings.runtime_warning_snooze_until = 0.0


def test_dialog_offers_the_requested_durations() -> None:
    dlg = tw_main._RuntimeWarningDialog("msg")
    got = []
    for i in range(dlg.snooze_combo.count()):
        dlg.snooze_combo.setCurrentIndex(i)
        got.append((dlg.snooze_combo.currentText(),
                    dlg.selected_snooze_seconds()))
    assert got[0][1] == 0                          # ask again next launch
    days = [s // 86400 for _, s in got[1:]]
    assert days == [30, 90, 180, 365], days        # 30d / 3m / 6m / 1y
    assert dlg.quit_btn.isDefault()
    print("OK: the dialog offers 30 days / 3 months / 6 months / 1 year, "
          "with 'ask again' as the default and Quit as the safe button")


def test_due_rules() -> None:
    due = crashlog.runtime_warning_due
    pair = "py3.14+pyside6.10.0"
    other = "py3.13+pyside6.9.0"
    now = 1_000_000.0
    assert due(pair, "", "", 0.0, now) is True
    assert due(pair, "", pair, now + 86400, now) is False   # active snooze
    assert due(pair, "", pair, now - 1, now) is True        # expired
    assert due(pair, "", other, now + 86400, now) is True   # pair changed
    assert due(pair, pair, "", 0.0, now) is False           # legacy ack
    assert due(pair, other, "", 0.0, now) is True           # ack re-armed
    print("OK: due() — snooze holds only for the exact pair until expiry; "
          "legacy permanent ack honored; any upgrade re-arms")


def test_settings_round_trip_is_re_runnable() -> None:
    s = Settings()
    _reset(s)
    assert s.runtime_warning_snooze_pair == ""
    assert s.runtime_warning_snooze_until == 0.0
    s.runtime_warning_snooze_pair = "py3.14+pyside6.10.0"
    s.runtime_warning_snooze_until = 1234.5
    assert s.runtime_warning_snooze_pair == "py3.14+pyside6.10.0"
    assert abs(s.runtime_warning_snooze_until - 1234.5) < 1e-6
    _reset(s)  # leave no residue for the next run
    print("OK: snooze settings round-trip (str + float) and reset cleanly")


class _StubDialog:
    """Stands in for _RuntimeWarningDialog: records construction,
    returns a scripted result and snooze choice."""

    constructed = 0
    result = QDialog.DialogCode.Accepted
    snooze = 0

    def __init__(self, message, parent=None):
        type(self).constructed += 1

    def exec(self):
        return type(self).result

    def selected_snooze_seconds(self):
        return type(self).snooze


def _with_mismatch(fn):
    real_msg = crashlog.runtime_mismatch_message
    real_pair = crashlog.runtime_pair_string
    real_dlg = tw_main._RuntimeWarningDialog
    crashlog.runtime_mismatch_message = lambda *a, **k: "simulated mismatch"
    crashlog.runtime_pair_string = lambda *a, **k: "py3.14+pyside6.10.0"
    tw_main._RuntimeWarningDialog = _StubDialog
    try:
        fn()
    finally:
        crashlog.runtime_mismatch_message = real_msg
        crashlog.runtime_pair_string = real_pair
        tw_main._RuntimeWarningDialog = real_dlg


def test_helper_stores_snooze_on_continue() -> None:
    def body():
        s = Settings()
        _reset(s)
        _StubDialog.constructed = 0
        _StubDialog.result = QDialog.DialogCode.Accepted
        _StubDialog.snooze = 30 * 86400
        t0 = time.time()
        assert tw_main._maybe_show_runtime_warning(s) is True
        assert _StubDialog.constructed == 1
        assert s.runtime_warning_snooze_pair == "py3.14+pyside6.10.0"
        assert s.runtime_warning_snooze_until >= t0 + 30 * 86400 - 5
        # Next launch within the window: dialog NOT constructed.
        assert tw_main._maybe_show_runtime_warning(s) is True
        assert _StubDialog.constructed == 1
        _reset(s)

    _with_mismatch(body)
    print("OK: choosing a duration stores the snooze; the next launch "
          "inside the window shows no dialog")


def test_helper_quit_and_ask_again() -> None:
    def body():
        s = Settings()
        _reset(s)
        # Quit → helper returns False, nothing stored.
        _StubDialog.constructed = 0
        _StubDialog.result = QDialog.DialogCode.Rejected
        assert tw_main._maybe_show_runtime_warning(s) is False
        assert s.runtime_warning_snooze_pair == ""
        # Continue with the default "ask again" → nothing stored, so the
        # next launch constructs the dialog again.
        _StubDialog.result = QDialog.DialogCode.Accepted
        _StubDialog.snooze = 0
        assert tw_main._maybe_show_runtime_warning(s) is True
        assert s.runtime_warning_snooze_pair == ""
        n = _StubDialog.constructed
        assert tw_main._maybe_show_runtime_warning(s) is True
        assert _StubDialog.constructed == n + 1
        _reset(s)

    _with_mismatch(body)
    print("OK: Quit stores nothing and signals exit; 'ask again next "
          "launch' warns again next time")


def run() -> None:
    test_dialog_offers_the_requested_durations()
    test_due_rules()
    test_settings_round_trip_is_re_runnable()
    test_helper_stores_snooze_on_continue()
    test_helper_quit_and_ask_again()
    print("\nALL PASS: runtime-warning snooze (dialog, rules, persistence, "
          "startup flow)")


if __name__ == "__main__":
    run()
