"""
core/crashlog.py

Persistent error logging and global exception hooks.

Why this module exists
----------------------
The packaged Windows build runs windowed (no console), so stderr is a
black hole. Before this module, an uncaught exception either showed a
one-shot dialog (main thread) or vanished entirely (worker threads,
native crashes). Neither leaves the user anything they can send when
reporting a problem.

This module makes every failure leave a durable trace:

1. ``sys.excepthook``       — main-thread exceptions: append the full
   traceback to ``error.log`` in the TagWalker config dir, then show
   the familiar error dialog (which now names the log file).
2. ``threading.excepthook`` — worker-thread exceptions (e.g. the
   autocomplete database loader): logged to the same file. A dialog is
   raised on the GUI thread via a queued signal — never directly from
   the worker, because creating widgets off the GUI thread is unsafe.
3. ``sys.unraisablehook``   — destructor-time errors: logged (no dialog;
   these are diagnostics, not user-facing events).
4. ``faulthandler``         — pointed at the same log file so even a
   native (C++/Qt-level) crash, which bypasses Python entirely, leaves
   a stack trace behind.

Every step is individually guarded: a failure to log must never prevent
the dialog, and a failure to show the dialog must never prevent the
log. The module imports no Qt at module level so headless / non-Qt
contexts can import it freely.

The log file is size-capped: when it grows past ``_MAX_LOG_BYTES`` the
oldest half is dropped, so years of use can't fill a disk.
"""

from __future__ import annotations

import faulthandler
import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

# Cap the log so it can never grow unbounded across months of use.
# When exceeded, we keep the newest tail (people report recent errors).
_MAX_LOG_BYTES = 512 * 1024
_TAIL_KEEP_BYTES = 256 * 1024

_app_version: str = "?"

# Keep the faulthandler file handle alive for the whole process — if it
# gets garbage-collected, faulthandler would write to a closed fd on a
# native crash (the one moment it matters).
_faulthandler_file: Any = None

# GUI-thread notifier for worker-thread errors (created in install()
# when a QApplication exists). Emitting its signal from any thread is
# safe; Qt delivers it queued to the GUI thread.
_notifier: Any = None

# Error signatures that already produced a dialog this session. A
# single underlying fault (e.g. the interpreter-poisoning seen on
# Python 3.14, where EVERY subsequent action raises the identical
# phantom TypeError) must not turn into a dialog per click. First
# occurrence: dialog + log. Repeats: log only — the log records every
# occurrence regardless.
_shown_signatures: set = set()


def _error_signature(exc_type, exc_value) -> str:
    try:
        name = getattr(exc_type, "__name__", str(exc_type))
        return f"{name}:{str(exc_value)[:160]}"
    except Exception:
        return "unknown"


def get_log_path() -> Path:
    """Where the error log lives — same convention as the other
    per-user files (conflict rules, audit exceptions):
    TAGWALKER_CONFIG_DIR override for tests, else ~/.tagwalker/."""
    base = os.environ.get("TAGWALKER_CONFIG_DIR")
    if base:
        return Path(base) / "error.log"
    return Path.home() / ".tagwalker" / "error.log"


def _trim_if_huge(path: Path) -> None:
    """Keep the log bounded: drop the oldest content once it passes the
    cap, preserving the newest tail. Best-effort; never raises."""
    try:
        if path.stat().st_size <= _MAX_LOG_BYTES:
            return
        data = path.read_bytes()
        tail = data[-_TAIL_KEEP_BYTES:]
        # Cut at the first newline so we don't start mid-line.
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1:]
        note = (
            "[log trimmed — older entries dropped to keep the file "
            "small]\n"
        ).encode("utf-8")
        path.write_bytes(note + tail)
    except OSError:
        pass


def _header(context: str) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        "\n" + "=" * 70 + "\n"
        f"[{ts}] TagWalker {_app_version} | {context}\n"
        f"Python {sys.version.split()[0]} | {platform.platform()}\n"
        + "-" * 70 + "\n"
    )


def log_error_text(context: str, formatted: str) -> Optional[Path]:
    """Append a formatted error block to the log. Returns the log path
    on success, None on failure. Never raises."""
    try:
        path = get_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _trim_if_huge(path)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(_header(context))
            f.write(formatted)
            if not formatted.endswith("\n"):
                f.write("\n")
        return path
    except Exception:
        return None


def _format_exception(exc_type, exc_value, exc_tb) -> str:
    try:
        if exc_tb is not None:
            return "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)
            )
        # No traceback object. This is the signature of an error raised
        # inside a Qt virtual (paint, sizeHint, a delegate) and swallowed
        # by the C++/Python boundary before a frame could be attached —
        # which is why the user sees "traceback unavailable" and nothing
        # to act on. The live Python stack is the next best locator: it
        # will not point at the exact raising line, but it names where
        # execution was, which for an intermittent scroll error is the
        # difference between a usable report and a dead end.
        header = f"{exc_type!r}: {exc_value!r}\n"
        header += ("(no traceback attached \u2014 error raised inside a "
                   "Qt callback; current stack follows)\n")
        return header + "".join(traceback.format_stack()[:-1])
    except Exception:
        try:
            return f"{exc_type!r}: {exc_value!r} (traceback unavailable)\n"
        except Exception:
            return "(unformattable exception)\n"


def _stderr_print(formatted: str) -> None:
    """Best-effort mirror to stderr for developers running from a
    terminal. In a windowed build stderr may be a dummy or None — a
    failure here must never break the hook."""
    try:
        stream = sys.stderr
        if stream is not None:
            stream.write(formatted)
            stream.flush()
    except Exception:
        pass


def _show_error_dialog(
    brief: str, formatted: str, log_path: Optional[Path]
) -> None:
    """Show the error dialog. GUI thread only. Never raises."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return
        msg = QMessageBox()
        msg.setWindowTitle("TagWalker — unexpected error")
        msg.setIcon(QMessageBox.Icon.Critical)
        msg.setText(brief)
        where = (
            f"\n\nA full report was written to:\n{log_path}\n"
            "Please attach that file when reporting this."
            if log_path is not None
            else "\n\n(The error log could not be written.)"
        )
        msg.setInformativeText(
            "The application may be in an inconsistent state and you "
            "should probably restart." + where
        )
        msg.setDetailedText(formatted)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()
    except Exception:
        pass


class _GuiNotifier:
    """Bridges worker-thread errors onto the GUI thread via a queued
    signal. Constructed lazily in install() only when Qt is up."""

    def __init__(self) -> None:
        from PySide6.QtCore import QObject, Signal

        class _Obj(QObject):
            fired = Signal(str, str, str)  # brief, formatted, log_path

        self._obj = _Obj()
        self._obj.fired.connect(self._on_fired)

    def _on_fired(self, brief: str, formatted: str, log_path: str) -> None:
        _show_error_dialog(
            brief, formatted, Path(log_path) if log_path else None
        )

    def notify(self, brief: str, formatted: str,
               log_path: Optional[Path]) -> None:
        try:
            self._obj.fired.emit(
                brief, formatted, str(log_path) if log_path else ""
            )
        except Exception:
            pass


# The phantom error CPython 3.14 raises for every object construction
# once the C++/Python boundary has corrupted the interpreter's error
# state during an idle-time callback. It is not a real error in any
# one place; it means the interpreter is wedged and only a restart
# clears it.
_PHANTOM_TEXT = "__init__() should return None"


def _looks_like_phantom(exc_type, exc_value) -> bool:
    try:
        return (exc_type is TypeError
                and _PHANTOM_TEXT in str(exc_value))
    except Exception:
        return False


def _write_phantom_notice() -> Optional[Path]:
    """Log the wedged-interpreter case WITHOUT constructing anything
    that could re-raise.

    Once the error state is corrupted, traceback.format_exception and
    even ordinary string building can trip the phantom again, so the
    normal formatter crashes inside the hook and nothing is written —
    which is exactly why the user's error.log stayed empty. This path
    uses only pre-built constant strings and a plain file append.
    """
    try:
        path = get_log_path()
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(
                "\n=== interpreter wedged "
                "(TypeError: __init__ should return None) ===\n"
                "This is a known CPython 3.14 + PySide6 fault, not a "
                "bug in TagWalker: after long idle, a Qt callback can "
                "corrupt Python's error state so every object "
                "construction fails. RESTART the app to clear it. To "
                "avoid it, run on Python 3.10-3.12, or the packaged "
                "TagWalker.exe.\n")
        return path
    except Exception:
        return None


def _sys_hook(exc_type, exc_value, exc_tb) -> None:
    """Main-thread uncaught exception: log, mirror to stderr, dialog.
    An identical error repeating this session logs without the dialog."""
    if _looks_like_phantom(exc_type, exc_value):
        # Do NOT call the normal formatter: on 3.14 it re-triggers the
        # phantom and the hook dies silently. Write a constant notice
        # and show a plain dialog, both allocation-light.
        path = _write_phantom_notice()
        if "phantom" not in _shown_signatures:
            _shown_signatures.add("phantom")
            try:
                _show_error_dialog(
                    "TagWalker's Python environment has become "
                    "unstable after sitting idle.\n\nThis is a known "
                    "fault on Python 3.14; please RESTART the app. To "
                    "avoid it, use the packaged TagWalker.exe or "
                    "Python 3.10-3.12.",
                    "interpreter wedged; see the note in error.log",
                    path)
            except Exception:
                pass
        return

    formatted = _format_exception(exc_type, exc_value, exc_tb)
    log_path = log_error_text("uncaught exception (main thread)", formatted)
    _stderr_print(formatted)
    sig = _error_signature(exc_type, exc_value)
    if sig in _shown_signatures:
        return
    _shown_signatures.add(sig)
    _show_error_dialog(
        "Something unexpected happened.\n\n(If this exact error repeats "
        "this session, it will be logged without this popup.)",
        formatted, log_path,
    )


def _thread_hook(args) -> None:
    """Uncaught exception in a threading.Thread worker: log, then ask
    the GUI thread (if any) to show the dialog. Never touch widgets
    from here — this runs on the worker thread."""
    if args.exc_type is SystemExit:
        return
    formatted = _format_exception(
        args.exc_type, args.exc_value, args.exc_traceback
    )
    name = getattr(args.thread, "name", None) or "worker thread"
    log_path = log_error_text(
        f"uncaught exception (background: {name})", formatted
    )
    _stderr_print(formatted)
    sig = _error_signature(args.exc_type, args.exc_value)
    if sig in _shown_signatures:
        return
    _shown_signatures.add(sig)
    notifier = _notifier
    if notifier is not None:
        notifier.notify(
            "Something unexpected happened in a background task.\n\n"
            "(If this exact error repeats this session, it will be "
            "logged without this popup.)",
            formatted, log_path,
        )


def _unraisable_hook(args) -> None:
    """Destructor-time errors: log only (diagnostic, not user-facing)."""
    formatted = _format_exception(
        getattr(args, "exc_type", None),
        getattr(args, "exc_value", None),
        getattr(args, "exc_traceback", None),
    )
    extra = getattr(args, "err_msg", None)
    if extra:
        formatted = f"{extra}\n{formatted}"
    path = log_error_text("unraisable exception", formatted)
    # These were previously logged silently. An intermittent scroll
    # crash that leaves nothing on screen is one a user cannot report,
    # so the dialog is shown here too — best-effort, never re-raising.
    try:
        _show_error_dialog(
            "An unexpected error occurred inside a display callback. "
            "The application is still running; if anything looks wrong, "
            "restart it.",
            formatted, path)
    except Exception:
        pass


def _enable_faulthandler() -> None:
    """Point faulthandler at the log so a native crash leaves a stack.
    Best-effort; a windowed build may lack usable std streams, which is
    exactly why we hand it a real file."""
    global _faulthandler_file
    try:
        path = get_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _faulthandler_file = open(  # noqa: SIM115 - lifetime is the process
            path, "a", encoding="utf-8", errors="replace"
        )
        faulthandler.enable(file=_faulthandler_file, all_threads=True)
    except Exception:
        _faulthandler_file = None


def install(app_version: str) -> Optional[Path]:
    """Install all hooks. Call once at startup, on the GUI thread,
    after QApplication exists (the worker→GUI notifier needs it; if Qt
    is absent, hooks still install in log-only mode).

    Returns the log path (best-effort; None only if even resolving the
    path failed)."""
    global _app_version, _notifier
    _app_version = str(app_version)

    try:
        log_path: Optional[Path] = get_log_path()
    except Exception:
        log_path = None

    _enable_faulthandler()

    try:
        _notifier = _GuiNotifier()
    except Exception:
        _notifier = None  # Qt not available — log-only mode.

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    sys.unraisablehook = _unraisable_hook
    return log_path


# ---------------------------------------------------------------------------
# Runtime compatibility
# ---------------------------------------------------------------------------
# The combination the app is developed, tested, and packaged against.
# Running from source on a different interpreter/binding pairing can
# fail in ways no in-app hardening can catch: a field report on
# Python 3.14 with a new PySide6 showed the C++/Python boundary
# corrupting the interpreter's error state during idle-time callbacks,
# after which EVERY Python class construction raised a phantom
# "TypeError: __init__() should return None, not 'NoneType'" — Yes/No,
# queue clicks, and re-scans all silently dead — and Python 3.14's own
# traceback formatter then crashed inside the exception hook, hiding
# it all. A version guard at startup is the only reliable defense.
TESTED_PYTHON_MIN = (3, 10)
TESTED_PYTHON_MAX = (3, 12)      # inclusive, by (major, minor)
TESTED_PYSIDE_PREFIX = "6.6."


def runtime_mismatch_message(
    py_version: Optional[tuple] = None,
    pyside_version: Optional[str] = None,
) -> Optional[str]:
    """Return a human-readable warning if the running Python/PySide6
    pair is outside the tested envelope, else None. Version arguments
    exist for tests; by default the live environment is inspected.
    Never raises."""
    try:
        if py_version is None:
            py_version = sys.version_info[:2]
        py_version = tuple(py_version)[:2]
        if pyside_version is None:
            try:
                import PySide6
                pyside_version = getattr(PySide6, "__version__", "?")
            except Exception:
                pyside_version = "?"

        py_bad = not (TESTED_PYTHON_MIN <= py_version <= TESTED_PYTHON_MAX)
        ps_bad = not str(pyside_version).startswith(TESTED_PYSIDE_PREFIX)
        if not py_bad and not ps_bad:
            return None

        py_str = ".".join(str(v) for v in py_version)
        lo = ".".join(str(v) for v in TESTED_PYTHON_MIN)
        hi = ".".join(str(v) for v in TESTED_PYTHON_MAX)
        return (
            f"This copy of TagWalker is running on Python {py_str} with "
            f"PySide6 {pyside_version}, but it is developed and tested "
            f"on Python {lo}\u2013{hi} with PySide6 "
            f"{TESTED_PYSIDE_PREFIX}x.\n\n"
            "On untested combinations the app can fail in invisible "
            "ways (a report on Python 3.14 had Yes/No silently stop "
            "working after the app sat idle).\n\n"
            "Recommended: run the packaged TagWalker.exe, or install "
            f"Python {hi} and 'pip install PySide6==6.6.3.1'."
        )
    except Exception:
        return None


def runtime_pair_string(
    py_version: Optional[tuple] = None,
    pyside_version: Optional[str] = None,
) -> str:
    """Canonical 'py3.14+pyside6.10.0' string for the running (or
    injected) pair — used to store and compare the user's "don't warn
    again" acknowledgment. Never raises."""
    try:
        if py_version is None:
            py_version = sys.version_info[:2]
        py_str = ".".join(str(v) for v in tuple(py_version)[:2])
        if pyside_version is None:
            try:
                import PySide6
                pyside_version = getattr(PySide6, "__version__", "?")
            except Exception:
                pyside_version = "?"
        return f"py{py_str}+pyside{pyside_version}"
    except Exception:
        return "py?+pyside?"


# The snooze durations offered by the startup warning, per user
# request: time-boxed reminders rather than only a permanent opt-out.
# (label, seconds). The zero entry is the default — warn again on the
# next launch.
SNOOZE_CHOICES: list = [
    ("Ask again next launch", 0),
    ("Don't remind me for 30 days", 30 * 86400),
    ("Don't remind me for 3 months", 90 * 86400),
    ("Don't remind me for 6 months", 180 * 86400),
    ("Don't remind me for 1 year", 365 * 86400),
]


def runtime_warning_due(
    pair: str,
    acknowledged_pair: str,
    snooze_pair: str,
    snooze_until: float,
    now: Optional[float] = None,
) -> bool:
    """Decide whether the startup compatibility warning should be shown
    for the current `pair`, given the stored suppressions.

    - `acknowledged_pair`: legacy permanent opt-out (earlier builds had
      a "don't warn again" checkbox). Still honored for the exact pair
      it was given on.
    - `snooze_pair` + `snooze_until`: time-boxed opt-out. Suppresses
      only while BOTH the pair matches and the deadline hasn't passed.

    Any change to the Python/PySide6 pairing re-arms the warning — a
    new combination is a new unknown. Never raises; on any internal
    doubt it errs toward showing the warning (silence is the failure
    mode this feature exists to prevent).
    """
    try:
        if acknowledged_pair and acknowledged_pair == pair:
            return False
        if snooze_pair and snooze_pair == pair:
            t = time.time() if now is None else float(now)
            if t < float(snooze_until):
                return False
        return True
    except Exception:
        return True
