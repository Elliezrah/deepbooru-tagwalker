"""Tests for the persistent crash log and global exception hooks.

The packaged Windows build has no console, so before core/crashlog.py
an uncaught exception either showed a one-shot dialog (main thread) or
vanished entirely (worker threads; native crashes). These tests pin the
new behavior: every failure appends a durable, size-capped entry to
error.log in the config dir, the dialog names the log file, worker
threads notify the GUI thread safely, and the hooks survive a broken
stderr.

Run: python3 tests/test_crashlog.py
"""
import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_CFG = tempfile.mkdtemp()
os.environ["TAGWALKER_CONFIG_DIR"] = _CFG

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from core import crashlog

_SHOWN: list = []
crashlog._show_error_dialog = (
    lambda brief, formatted, log_path: _SHOWN.append((brief, str(log_path)))
)

_LOG = crashlog.install("test-version-9.9")


def _read() -> str:
    p = os.path.join(_CFG, "error.log")
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def test_install_targets_config_dir() -> None:
    assert _LOG is not None and str(_LOG).startswith(_CFG), _LOG
    assert sys.excepthook is crashlog._sys_hook
    assert threading.excepthook is crashlog._thread_hook
    assert sys.unraisablehook is crashlog._unraisable_hook
    print("OK: install() targets the config dir and replaces all hooks")


def test_main_thread_hook_logs_and_dialogs_with_null_stderr() -> None:
    real = sys.stderr
    sys.stderr = None  # windowed-build worst case
    try:
        try:
            raise ZeroDivisionError("mt-boom")
        except ZeroDivisionError:
            sys.excepthook(*sys.exc_info())
    finally:
        sys.stderr = real
    txt = _read()
    assert "ZeroDivisionError" in txt and "mt-boom" in txt
    assert "test-version-9.9" in txt and "main thread" in txt
    assert len(_SHOWN) == 1 and _CFG in _SHOWN[0][1], _SHOWN
    print("OK: main-thread hook logs (version+context) and shows the "
          "dialog even with stderr=None")


def test_worker_thread_hook_logs_and_notifies_gui() -> None:
    def boom() -> None:
        raise ValueError("worker-boom")

    t = threading.Thread(target=boom, name="tw-test-worker")
    t.start()
    t.join()
    _app.processEvents()  # deliver the queued GUI notification
    txt = _read()
    assert "worker-boom" in txt and "tw-test-worker" in txt
    assert len(_SHOWN) == 2 and "background" in _SHOWN[1][0], _SHOWN
    print("OK: a worker-thread exception is logged and the dialog is "
          "delivered on the GUI thread")


def test_unraisable_hook_logs_and_now_surfaces() -> None:
    """CHANGED. These used to be logged silently, on the reasoning
    that they were destructor-time noise. But an intermittent scroll
    crash reaches users through exactly this hook — an error raised
    inside a Qt callback arrives with no traceback and, before, no
    dialog either, so the user saw a bare "traceback unavailable" and
    had nothing to report.

    Now the hook shows the dialog too, and the formatter attaches the
    live stack when the traceback object is missing, so there is a
    location to act on."""
    before = len(_SHOWN)
    args = types.SimpleNamespace(
        exc_type=TypeError,
        exc_value=TypeError("__init__() should return None"),
        exc_traceback=None,        # the swallowed-in-Qt signature
        err_msg=None,
        object=None,
    )
    sys.unraisablehook(args)
    logged = _read()
    assert "should return None" in logged
    # A locator stack, not a dead end.
    assert "no traceback attached" in logged
    assert "traceback unavailable" not in logged
    # And it now reaches the dialog.
    assert len(_SHOWN) == before + 1, _SHOWN
    print("OK: a traceback-less Qt-callback error is logged with a "
          "live stack and surfaced to the user")


def test_log_is_size_capped() -> None:
    p = os.path.join(_CFG, "error.log")
    with open(p, "a", encoding="utf-8") as f:
        f.write("x" * (700 * 1024))
    try:
        raise KeyError("post-trim-entry")
    except KeyError:
        sys.excepthook(*sys.exc_info())
    size = os.path.getsize(p)
    txt = _read()
    assert size < crashlog._MAX_LOG_BYTES, size
    assert "log trimmed" in txt and "post-trim-entry" in txt
    print("OK: an oversized log is trimmed to its newest tail and keeps "
          "accepting entries")


def test_faulthandler_armed() -> None:
    import faulthandler

    assert faulthandler.is_enabled()
    assert crashlog._faulthandler_file is not None
    print("OK: faulthandler is enabled against the log file (native "
          "crashes leave a stack)")


def test_logging_failure_never_breaks_the_dialog() -> None:
    real = crashlog.get_log_path
    crashlog.get_log_path = lambda: (_ for _ in ()).throw(OSError("no dir"))
    try:
        try:
            raise RuntimeError("log-path-broken")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())  # must not raise
    finally:
        crashlog.get_log_path = real
    assert _SHOWN and _SHOWN[-1][0]  # a dialog still appeared
    print("OK: even if logging itself fails, the hook survives and the "
          "dialog still appears")


def test_survives_traceback_formatter_crash() -> None:
    """Field-verified failure (Python 3.14): traceback.format_exception
    itself crashed with AttributeError('NoneType'... 'partition') inside
    the old hook, so nothing was ever shown or written. The hook must
    degrade to a repr entry and still show the dialog."""
    import traceback

    real_fmt = traceback.format_exception

    def broken_fmt(*a, **k):
        raise AttributeError("'NoneType' object has no attribute "
                             "'partition'")

    dialogs_before = len(_SHOWN)
    traceback.format_exception = broken_fmt
    try:
        try:
            raise TypeError(
                "__init__() should return None, not 'NoneType'"
            )
        except TypeError:
            sys.excepthook(*sys.exc_info())
    finally:
        traceback.format_exception = real_fmt
    txt = _read()
    assert "should return None, not 'NoneType'" in txt
    assert "traceback unavailable" in txt
    assert len(_SHOWN) == dialogs_before + 1
    print("OK: a crashing traceback formatter (the Python-3.14 field "
          "failure) still yields a logged repr entry and a dialog")


def test_runtime_mismatch_detector() -> None:
    m = crashlog.runtime_mismatch_message
    # The field combination that produced the silent Yes/No failure.
    warn = m((3, 14), "6.10.0")
    assert warn is not None
    assert "3.14" in warn and "6.10.0" in warn
    assert "PySide6==6.6.3.1" in warn  # names the remedy
    # The tested envelope stays silent.
    assert m((3, 12), "6.6.3.1") is None
    assert m((3, 10), "6.6.2") is None
    # Each side alone is enough to warn.
    assert m((3, 9), "6.6.3.1") is not None
    assert m((3, 13), "6.6.3.1") is not None
    assert m((3, 12), "6.9.1") is not None
    # Unknown PySide version warns rather than guessing.
    assert m((3, 12), "?") is not None
    print("OK: the runtime-compatibility detector warns exactly outside "
          "the tested Python/PySide6 envelope")


def test_repeated_identical_error_shows_one_dialog() -> None:
    """A single recurring fault (e.g. the Python-3.14 poisoning, where
    every action raises the identical phantom TypeError) must produce
    ONE dialog, not a storm — while the log records every occurrence."""
    dialogs_before = len(_SHOWN)
    for _ in range(5):
        try:
            raise ValueError("dedup-storm-boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())
    txt = _read()
    assert txt.count("dedup-storm-boom") >= 5, "log records every repeat"
    assert len(_SHOWN) == dialogs_before + 1, _SHOWN
    assert "logged without this popup" in _SHOWN[-1][0]
    # A DIFFERENT error still gets its own dialog.
    try:
        raise ValueError("dedup-other-boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    assert len(_SHOWN) == dialogs_before + 2
    print("OK: identical repeats show one dialog (all logged); a new "
          "error still gets its own")


def test_runtime_pair_string_and_acknowledgment_slot() -> None:
    pair = crashlog.runtime_pair_string((3, 14), "6.10.0")
    assert pair == "py3.14+pyside6.10.0", pair
    assert crashlog.runtime_pair_string((3, 12), "6.6.3.1") == \
        "py3.12+pyside6.6.3.1"
    # The Settings slot round-trips and a changed pair no longer
    # matches an old acknowledgment. (QSettings persists on disk, so
    # explicitly reset first — this test must be re-runnable.)
    from config.settings import Settings

    s = Settings()
    s.runtime_warning_acknowledged_pair = ""
    assert s.runtime_warning_acknowledged_pair == ""
    s.runtime_warning_acknowledged_pair = pair
    assert s.runtime_warning_acknowledged_pair == pair
    upgraded = crashlog.runtime_pair_string((3, 14), "6.11.0")
    assert s.runtime_warning_acknowledged_pair != upgraded
    s.runtime_warning_acknowledged_pair = ""   # leave no residue
    print("OK: pair string is canonical; acknowledgment stores/compares "
          "per exact pair (an upgrade re-arms the warning)")


def run() -> None:
    test_install_targets_config_dir()
    test_main_thread_hook_logs_and_dialogs_with_null_stderr()
    test_worker_thread_hook_logs_and_notifies_gui()
    test_unraisable_hook_logs_and_now_surfaces()
    test_log_is_size_capped()
    test_faulthandler_armed()
    test_logging_failure_never_breaks_the_dialog()
    test_survives_traceback_formatter_crash()
    test_runtime_mismatch_detector()
    test_repeated_identical_error_shows_one_dialog()
    test_runtime_pair_string_and_acknowledgment_slot()
    print("\nALL PASS: crash log + global exception hooks")


if __name__ == "__main__":
    run()
