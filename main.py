"""
main.py

Entry point for TagWalker.

Run from the source tree with:
    python main.py

Build a standalone Windows .exe with PyInstaller (see build.bat).

Responsibilities
----------------
1. Construct the QApplication exactly once.
2. Set application metadata so QSettings picks the right INI location
   and the OS taskbar groups our window correctly.
3. Apply the dark theme stylesheet from config/theme.py.
4. Install global exception hooks (core/crashlog.py) so any uncaught
   error — main thread, worker thread, or native crash — is appended
   to error.log in the config folder and shown in a dialog, instead of
   vanishing silently (the packaged build has no console).
5. Construct the MainWindow, hand it a Settings instance, show it.
6. Run the event loop until the user closes the window.

No application logic lives here. Everything testable lives in core/
and ui/; main.py is wiring only.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

from config.settings import Settings
from config.theme import apply_theme, initialize_theme
from core import crashlog
from core import tag_database as tdb
from ui.main_window import MainWindow


# Application metadata. QSettings uses organization + application name
# to derive the per-user INI path (%APPDATA%\TagWalker\TagWalker.ini
# on Windows). Setting these BEFORE constructing Settings is what
# routes the file to the canonical location.
APP_ORGANIZATION = "TagWalker"
APP_NAME = "TagWalker"
# 2.x because a public v1 exists: the original prototype, released
# under MIT. This is a different codebase under a different licence,
# and a major bump is how that is communicated to anyone who finds
# both.
APP_VERSION = "2.0.0"


def _resource_path(relative: str) -> Path:
    """Resolve a bundled resource path for both source and frozen runs.

    When running from source, resources live next to this file
    (e.g. ./resources/icon.ico). When PyInstaller freezes the app with
    --onefile, it unpacks bundled data to a temp directory whose path
    is in sys._MEIPASS at runtime. This returns the right base in
    either case, so the window icon loads identically from `python
    main.py` and from the packaged TagWalker.exe.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.abspath(__file__))
    return Path(base) / relative


def _load_app_icon() -> QIcon:
    """Load the application/window icon, if present.

    Looks for resources/icon.ico. Returns an empty QIcon if the file
    isn't there, so a missing icon never blocks startup — the app just
    falls back to the platform default. The same .ico is also baked
    into the .exe via PyInstaller's --icon flag (that governs the file
    icon in Explorer; this governs the live window/taskbar icon).
    """
    ico = _resource_path("resources/icon.ico")
    if ico.exists():
        return QIcon(str(ico))
    return QIcon()


def _install_excepthook() -> None:
    """Route every uncaught error somewhere the user can find it.

    core/crashlog.py installs the full set of hooks:
    - main-thread exceptions   -> error.log + the error dialog
    - worker-thread exceptions -> error.log + dialog via the GUI thread
    - destructor-time errors   -> error.log
    - native (C++-level) crashes -> stack appended to error.log via
      faulthandler

    The log file matters because the packaged Windows build runs
    without a console: stderr is invisible, and a dialog alone leaves
    nothing behind once dismissed. error.log (in the TagWalker config
    folder) is a durable artifact the user can attach to a bug report.
    """
    crashlog.install(APP_VERSION)


class _RuntimeWarningDialog(QDialog):
    """Startup compatibility warning with time-boxed snooze.

    Replaces the earlier permanent "don't warn again" checkbox with the
    user-requested durations (30 days / 3 / 6 months / 1 year) via a
    dropdown — see crashlog.SNOOZE_CHOICES. Quit is the default button;
    continuing is a deliberate act. selected_snooze_seconds() returns 0
    when the user left the default "ask again next launch" entry.
    """

    def __init__(self, message: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("TagWalker — untested Python environment")
        self.setModal(True)
        layout = QVBoxLayout(self)

        label = QLabel(message)
        label.setWordWrap(True)
        layout.addWidget(label)

        self.snooze_combo = QComboBox()
        for text, seconds in crashlog.SNOOZE_CHOICES:
            self.snooze_combo.addItem(text, seconds)
        layout.addWidget(self.snooze_combo)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.continue_btn = QPushButton("Continue anyway")
        self.quit_btn = QPushButton("Quit")
        self.quit_btn.setDefault(True)
        self.continue_btn.clicked.connect(self.accept)
        self.quit_btn.clicked.connect(self.reject)
        buttons.addWidget(self.continue_btn)
        buttons.addWidget(self.quit_btn)
        layout.addLayout(buttons)

    def selected_snooze_seconds(self) -> int:
        try:
            return int(self.snooze_combo.currentData() or 0)
        except (TypeError, ValueError):
            return 0


def _maybe_show_runtime_warning(settings) -> bool:
    """Show the compatibility warning when due. Returns False if the
    user chose Quit (caller should exit), True to continue.

    Suppression rules (crashlog.runtime_warning_due): a legacy
    permanent acknowledgment for this exact pair, or an unexpired
    snooze for this exact pair. Upgrading Python or PySide6 re-arms
    the warning — a new pairing is a new unknown.
    """
    mismatch = crashlog.runtime_mismatch_message()
    if mismatch is None:
        return True
    pair = crashlog.runtime_pair_string()
    if not crashlog.runtime_warning_due(
        pair,
        settings.runtime_warning_acknowledged_pair,
        settings.runtime_warning_snooze_pair,
        settings.runtime_warning_snooze_until,
    ):
        return True

    crashlog.log_error_text(
        "runtime compatibility warning", f"{pair}\n{mismatch}"
    )
    dlg = _RuntimeWarningDialog(mismatch)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False
    snooze = dlg.selected_snooze_seconds()
    if snooze > 0:
        settings.runtime_warning_snooze_pair = pair
        settings.runtime_warning_snooze_until = time.time() + snooze
    return True


def main() -> int:
    """Application entry point. Returns the Qt event-loop exit code."""
    # Set Qt metadata before creating QApplication so any subsystem
    # that reads them (QSettings in particular) sees the right values.
    QCoreApplication.setOrganizationName(APP_ORGANIZATION)
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(APP_VERSION)

    # Windows-only: tell the shell this process is its own application,
    # not "Python". Without an explicit AppUserModelID, Windows groups
    # the app under the python.exe/pythonw.exe identity and shows the
    # generic Python icon in the taskbar even when the window icon is
    # set correctly — a confusing "my icon didn't work" symptom. Must
    # be called before any window is shown. No-op / harmless elsewhere.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{APP_ORGANIZATION}.{APP_NAME}.{APP_VERSION}"
            )
        except Exception:
            # Never let a cosmetic taskbar-grouping tweak block startup.
            pass

    app = QApplication(sys.argv)

    # Set the live window / taskbar icon. (The .exe file icon in
    # Explorer is set separately by PyInstaller's --icon flag at build
    # time; this is the icon shown while the app is running.) Applied
    # at the application level so every window and dialog inherits it.
    app.setWindowIcon(_load_app_icon())

    # Read theme preference and populate the Colors palette BEFORE
    # apply_theme (which builds the stylesheet from Colors) or any UI
    # module is constructed. Theme switching is restart-required, so
    # this single read fully determines the look of this session.
    # Settings is read here directly rather than later because we need
    # the theme name before MainWindow is constructed — UI modules
    # import Colors at attribute-access time, and we want them to see
    # the right values.
    _theme_bootstrap_settings = Settings()
    initialize_theme(_theme_bootstrap_settings.theme)

    apply_theme(app)
    _install_excepthook()

    # Refuse to run silently-broken: an untested Python/PySide6 pairing
    # can corrupt the C++/Python boundary in ways no in-app hardening
    # catches (see core/crashlog.py). Warn when due — the user can
    # snooze it for 30 days up to a year — and let them decide. The
    # packaged .exe always ships the tested pair.
    if not _maybe_show_runtime_warning(_theme_bootstrap_settings):
        return 0

    settings = _theme_bootstrap_settings  # reuse the same instance
    # Point the audit's tag database at the user's chosen Danbooru CSV
    # snapshot (restart-required, like the theme). Must happen before any
    # lookup so autocomplete and the audit tool read the right file.
    tdb.set_active_csv(tdb.resolve_csv_path(settings.tag_database_choice))
    window = MainWindow(settings)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
