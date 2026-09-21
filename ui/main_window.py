"""
ui/main_window.py

Top-level application window. Assembles every widget, wires every
menu, every keyboard shortcut, and runs the autosave timer and
background directory-scan worker.

Layout (3-column horizontal split + bottom bar):
  ┌──────────────────────────────────────────────────────────────┐
  │  Menu bar (File, Edit, View, Settings, Help)                │
  │ ┌──────────┬──────────────────────────────────┬──────────┐ │
  │ │          │                                  │          │ │
  │ │ TagTree  │     ImagePanel                   │ Queue    │ │
  │ │          │                                  │ Panel    │ │
  │ │          │                                  │          │ │
  │ └──────────┴──────────────────────────────────┴──────────┘ │
  │ ┌──────────────────────────┬───────────────────────────────┐ │
  │ │ FileStatePanel           │ ActionLog                     │ │
  │ └──────────────────────────┴───────────────────────────────┘ │
  │  Status bar  (autosave indicator | walk breadcrumb)         │
  └──────────────────────────────────────────────────────────────┘

The old path bar (current directory + Browse + Full reset) was removed
for a calmer top: loading is under File ▸ Open Multiple Folders (handles
single and multiple), Full Reset is under File, and the loaded-dataset
identity is shown in the window title.

QSplitter handles the column layout so the user can drag dividers.
Splitter positions are saved with window geometry on close, restored
on next launch.

Threading
---------
Directory scanning is the only slow operation. ScanWorker is a
QObject moved to a QThread; it runs scanner.scan() and emits a
signal with the result on completion. Until the result arrives,
the UI shows a "Scanning..." status and disables the load actions
(File ▸ Open Directory / Open Multiple Folders) to prevent
overlapping scans.

The watcher (core/watcher.py) uses Qt's QFileSystemWatcher, which
delivers events on the main thread automatically.

Autosave
--------
A QTimer fires at the user-configured interval. The slot is a no-op
unless three conditions hold: state exists, save path is set, action
counter increased since the last save. The action counter is a
simple monotonic int incremented on every state event that could
warrant saving (image_changed, tag_changed, etc.). On successful
save, the counter is captured as the "last saved" baseline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QObject, QPoint, QRect, QSize, Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import (
    QAction, QCloseEvent, QGuiApplication, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from config.settings import Settings
from config.theme import Colors, Icons, Spacing
from core.persistence import (
    InvalidSessionFileError, ReconciliationReport,
    apply_to_state, load as load_session, save as save_session,
)
from core.scanner import ScanResult, scan, scan_many
from core.state import (
    FilterMode, SessionState, SortMode, StateChange,
)
from core.watcher import CurrentFileWatcher
from ui.action_log import ActionLog
from ui.file_state_panel import FileStatePanel
from ui.image_panel import ImagePanel
from ui.queue_panel import QueuePanel
from ui.settings_dialog import SettingsDialog
from ui.tag_tree import TagTree


# File dialog filter for session files. The double-extension suffix
# matches what persistence.SUGGESTED_EXTENSION recommends, while the
# fallback *.json lets users open files saved without the convention.
SESSION_FILE_FILTER = "TagWalker session (*.tagwalker.json *.json)"


# ---------------------------------------------------------------------------
# Background scan worker
# ---------------------------------------------------------------------------


class ScanWorker(QObject):
    """Runs scanner.scan() on a worker thread.

    Cancellation: the run() method polls an internal flag set by
    cancel(). The flag is checked frequently inside scan() (per its
    cancel_check parameter).
    """

    finished = Signal(object)   # ScanResult
    progress = Signal(int, int)  # processed, total
    failed = Signal(str)         # error message

    def __init__(self, root: Path, generation: int = 0) -> None:
        super().__init__()
        # Accepts one root or a list (multi-folder load).
        self._roots = [root] if isinstance(root, Path) else list(root)
        self._cancelled = False
        # Scan generation id, set by the main window. Read back by the
        # finished/failed slots to discard results from a superseded
        # scan. Stored on the worker (not bound via a lambda) so the
        # signal connections stay plain bound methods — Qt then uses a
        # queued cross-thread connection, running the slots on the main
        # thread. A lambda receiver has no thread affinity and Qt may
        # run it on the WORKER thread, which is fatal for UI calls.
        self.generation = generation

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            result = scan_many(
                self._roots,
                progress_callback=lambda p, t: self.progress.emit(p, t),
                cancel_check=lambda: self._cancelled,
            )
            self.finished.emit(result)
        except Exception as e:
            # Defensive — scan() is designed not to raise, but any
            # uncaught error here should at least surface to the user.
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """The TagWalker top-level window."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.setWindowTitle("TagWalker")

        self._settings: Settings = settings

        # Live application state. None until the user opens a directory.
        # Start loading the bundled reference data now, on a worker
        # thread, rather than charging ~2.5s of parsing to whichever
        # button first needs it.
        from core import prewarm
        prewarm.start(self)

        self._state: Optional[SessionState] = None
        self._watcher: Optional[CurrentFileWatcher] = None

        # Statistics window (Pass D). Created lazily on first open,
        # reused thereafter. Re-attached to the active state on each
        # session adoption so it always reflects the loaded data.
        self._stats_window = None

        # Background scan worker plumbing.
        self._scan_thread: Optional[QThread] = None
        self._scan_worker: Optional[ScanWorker] = None
        # Monotonic scan generation id. Incremented on every _start_scan.
        # _on_scan_finished / _on_scan_failed ignore results that don't
        # match the current generation, so a stale worker whose
        # finished/failed signal was already queued can't clobber a
        # newer scan or load the wrong directory.
        self._scan_generation: int = 0

        # Session file management.
        self._session_path: Optional[Path] = None
        # Action counter — monotonic. Captured on save so we know if
        # there are unsaved changes since the last save.
        self._action_counter: int = 0
        self._last_saved_counter: int = 0

        # QShortcut objects, keyed by setting name for rebind on settings change.
        self._shortcuts: dict[str, QShortcut] = {}

        self._build_ui()
        self._build_menus()
        self._build_shortcuts()
        self._start_autosave_timer()
        self._restore_window_state()
        self._refresh_window_title()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL,
        )
        outer.setSpacing(Spacing.NORMAL)

        # --- Path bar removed --------------------------------------------
        # The horizontal path strip (current-directory label + Browse +
        # Full reset) has been retired to give the top of the UI a calmer,
        # less crowded view. Its functions live elsewhere now:
        #   * Loading a dataset  -> File ▸ Open Multiple Folders (which
        #     handles single AND multiple folders).
        #   * Full reset         -> File ▸ Full Reset (added in _build_menus;
        #     identical behaviour, just relocated).
        #   * The loaded-dataset reminder that the path label used to show
        #     -> the window title, which now reflects single- and
        #     multi-folder loads correctly (see _refresh_window_title).

        # --- Three-column splitter (Tag tree | Image panel | Queue) ---
        self._top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._tag_tree = TagTree()
        self._image_panel = ImagePanel()
        self._queue_panel = QueuePanel()
        # Wire image panel to queue panel so Yes/No can dispatch to
        # batch operations (B6) when multiple rows are selected.
        self._image_panel.set_queue_panel(self._queue_panel)
        self._queue_panel.set_image_panel(self._image_panel)
        # Route the Back button / Backspace through _action_undo so it
        # shares the menu undo's failure warning (a failed revert must
        # not silently look like success on the Back path).
        self._image_panel.set_undo_handler(self._action_undo)
        # Apply the saved group-preview grid size (Feature B).
        self._image_panel.set_group_grid_size(
            self._settings.default_group_grid_cols
        )
        # Show the saved default sort/filter immediately so the bar
        # doesn't display A-Z at launch and then 'flip' to the real
        # default when the first dataset loads.
        try:
            self._queue_panel.filter_bar.show_defaults(
                FilterMode(self._settings.default_filter_mode),
                SortMode(self._settings.default_sort_mode),
                self._settings.default_show_orphans,
            )
        except ValueError:
            pass
        self._top_splitter.addWidget(self._tag_tree)
        self._top_splitter.addWidget(self._image_panel)
        self._top_splitter.addWidget(self._queue_panel)
        # Stretch factors: sidebars take less room than center.
        self._top_splitter.setStretchFactor(0, 1)
        self._top_splitter.setStretchFactor(1, 3)
        self._top_splitter.setStretchFactor(2, 1)
        # Reasonable initial sizes (only used if no saved geometry).
        self._top_splitter.setSizes([200, 700, 250])

        # --- Bottom panels splitter (File state | Action log | Swirl) -
        # The swirl widget lives in its own zone next to the action
        # log. This pairs it visually with the action log and avoids
        # the z-order problems it had when floating inside the image
        # panel (image border would paint over it). Fixed 200x200; the
        # surrounding zone aligns it to the top-left so it doesn't
        # drift as the bottom splitter resizes.
        from ui.swirl_widget import SwirlWidget
        from PySide6.QtWidgets import QSizePolicy
        self._bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._file_state_panel = FileStatePanel()
        # Wire the file-state panel to the queue panel so group-header
        # selection can switch it to a read-only group summary.
        self._queue_panel.set_file_state_panel(self._file_state_panel)
        self._tag_tree.reference_requested.connect(
            self._open_tag_reference)
        self._file_state_panel.reference_requested.connect(
            self._open_tag_reference)
        self._action_log = ActionLog()
        self._swirl_widget = SwirlWidget()
        # Wrap swirl in a small container so the splitter can resize
        # the "zone" independently while the swirl itself stays a
        # fixed 200x200 anchored top-left of its zone. Without the
        # wrapper, the splitter would try to expand the swirl and
        # fight its setFixedSize.
        swirl_zone = QWidget()
        swirl_zone_layout = QVBoxLayout(swirl_zone)
        swirl_zone_layout.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL,
        )
        swirl_zone_layout.setSpacing(0)
        swirl_zone_layout.addWidget(
            self._swirl_widget, 0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
        )
        swirl_zone_layout.addStretch(1)
        swirl_zone.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._swirl_zone = swirl_zone
        self._bottom_splitter.addWidget(self._file_state_panel)
        self._bottom_splitter.addWidget(self._action_log)
        self._bottom_splitter.addWidget(swirl_zone)
        # Initial widths: file state takes the most, action log a bit
        # less, swirl zone fits the 200px widget plus margins.
        self._bottom_splitter.setSizes([400, 350, 240])
        # Make the swirl zone non-stretchy so it stays compact.
        self._bottom_splitter.setStretchFactor(0, 2)
        self._bottom_splitter.setStretchFactor(1, 2)
        self._bottom_splitter.setStretchFactor(2, 0)

        # Apply swirl visibility from settings. The swirl zone itself
        # is hidden when the setting is off (otherwise an empty zone
        # would still take space in the splitter).
        self._set_swirl_visible_from_setting()

        # --- Vertical splitter combining top + bottom ----------------
        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.addWidget(self._top_splitter)
        self._main_splitter.addWidget(self._bottom_splitter)
        self._main_splitter.setStretchFactor(0, 4)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setSizes([550, 150])

        outer.addWidget(self._main_splitter, 1)

        self.setCentralWidget(central)

        # Explicit minimum window size. Without this, the combined
        # minimum-size hints of the nested panels dictate the floor
        # (~780x720), which can be slightly taller than the usable area
        # on a short screen (e.g. a 1366x768 laptop after the taskbar
        # and title bar), pushing controls partly off-screen when
        # maximized. Setting a deliberate, achievable minimum lets Qt
        # lay the panels out gracefully at small sizes instead — the
        # splitters and the (now lower) image-area floor allow the
        # window to shrink to this without clipping.
        self.setMinimumSize(720, 560)

        # --- Status bar ----------------------------------------------
        # Keep the existing save-status (left) and walk-status (right)
        # labels for backward compat — they're used by save flow and
        # the walk-position indicator. Pass C adds two more permanent
        # widgets on the right: a global decisions counter and a
        # shimmer-embellished overall-progress bar.
        self._status = QStatusBar()
        self._lbl_save_status = QLabel("")
        self._lbl_save_status.setProperty("role", "tertiary")
        self._lbl_walk_status = QLabel("")
        self._lbl_walk_status.setProperty("role", "tertiary")
        self._status.addWidget(self._lbl_save_status)
        self._status.addPermanentWidget(self._lbl_walk_status)

        # Pass C: total decisions counter + shimmer progress bar.
        # We use just the shimmer widget from status_bar.py and build
        # the counter inline here, since the existing QStatusBar
        # already manages our save-status and walk-status labels and
        # we want to add to it rather than replace it.
        from ui.status_bar import _ShimmerProgressBar
        self._lbl_total_counter = QLabel("Total: 0 decisions \u00b7 0% complete")
        self._lbl_total_counter.setProperty("role", "tertiary")
        self._progress_overall = _ShimmerProgressBar()
        self._status.addPermanentWidget(self._lbl_total_counter)
        self._status.addPermanentWidget(self._progress_overall)

        self.setStatusBar(self._status)

    # ------------------------------------------------------------------
    # Menus
    # ------------------------------------------------------------------

    def _build_menus(self) -> None:
        mb = self.menuBar()

        # ---- File ----------------------------------------------------
        m_file = mb.addMenu("&File")
        self._act_open_dir = QAction("Open Directory…", self)
        self._act_open_multi = QAction("Open Multiple Folders…", self)
        self._act_open_multi.triggered.connect(self._action_open_multiple)
        self._act_open_dir.triggered.connect(self._action_browse_directory)
        m_file.addAction(self._act_open_dir)
        m_file.addAction(self._act_open_multi)

        m_file.addSeparator()

        self._act_new_session = QAction("New Session", self)
        self._act_new_session.setToolTip(
            "Discard current session state and start fresh (does not "
            "touch caption files on disk)."
        )
        self._act_new_session.triggered.connect(self._action_new_session)
        m_file.addAction(self._act_new_session)

        self._act_load_session = QAction("Load Session…", self)
        self._act_load_session.triggered.connect(self._action_load_session)
        m_file.addAction(self._act_load_session)

        self._act_save_session = QAction("Save Session", self)
        self._act_save_session.triggered.connect(self._action_save_session)
        m_file.addAction(self._act_save_session)

        self._act_save_as = QAction("Save Session As…", self)
        self._act_save_as.triggered.connect(self._action_save_session_as)
        m_file.addAction(self._act_save_as)

        m_file.addSeparator()

        # Full Reset — relocated here from the old path-bar button. Same
        # behaviour (discard session state and re-scan; caption files on
        # disk untouched). Starts disabled until a dataset is loaded; the
        # enable/disable logic in the scan handlers drives _act_full_reset
        # exactly as it drove the old button.
        self._act_full_reset = QAction("Full Reset", self)
        self._act_full_reset.setToolTip(
            "Discard all session state and re-scan the current directory. "
            "Disk caption files are not affected."
        )
        self._act_full_reset.triggered.connect(self._action_full_reset)
        self._act_full_reset.setEnabled(False)
        m_file.addAction(self._act_full_reset)

        m_file.addSeparator()

        self._act_quit = QAction("Quit", self)
        self._act_quit.triggered.connect(self.close)
        m_file.addAction(self._act_quit)

        # Under File, not Tools. Tools act on the dataset that is
        # open; this works on an entirely different folder and writes
        # somewhere else again. Putting it beside the other
        # dataset-wide operations would imply a relationship it does
        # not have.
        m_file.addSeparator()
        self._act_image_editor = QAction("Launch Image Editor\u2026", self)
        self._act_image_editor.setToolTip(
            "Prepare raw images for training \u2014 crop, resize and "
            "bucket. Works on its own input and output folders, and "
            "never modifies an original.")
        self._act_image_editor.triggered.connect(
            self._action_image_editor)
        m_file.addAction(self._act_image_editor)

        # ---- Edit ----------------------------------------------------
        m_edit = mb.addMenu("&Edit")
        self._act_undo = QAction("Undo last action", self)
        self._act_undo.triggered.connect(self._action_undo)
        m_edit.addAction(self._act_undo)

        # Undo everything back to the start of the session.
        #
        # There is deliberately no Redo. Undo entries are closures that
        # capture how to REVERSE an action; nothing captures how to
        # re-apply one, so redo would mean a forward closure at every
        # push site in the file that writes captions to disk. The
        # payoff is thin — in a Yes/No/Skip walk, redo is answering
        # the same way again — and the risk sits in the most
        # safety-critical code in the program.
        self._act_undo_all = QAction("Undo all actions\u2026", self)
        self._act_undo_all.setToolTip(
            "Reverse every decision made since this dataset was "
            "loaded, one at a time, in order. Captions return to "
            "the state they were in on disk when you started.")
        self._act_undo_all.triggered.connect(self._action_undo_all)
        m_edit.addAction(self._act_undo_all)
        m_edit.addSeparator()

        self._act_copy_caption = QAction("Copy current caption", self)
        self._act_copy_caption.setToolTip(
            "Put this image's tags on the clipboard, comma-separated "
            "\u2014 the form a trainer expects.")
        self._act_copy_caption.triggered.connect(
            self._action_copy_caption)
        m_edit.addAction(self._act_copy_caption)

        self._act_select_all = QAction("Select all images in queue", self)
        self._act_select_all.setToolTip(
            "Select every image currently listed, for batch "
            "answering.")
        self._act_select_all.triggered.connect(self._action_select_all)
        m_edit.addAction(self._act_select_all)
        # Delete-tag-globally is a right-click action on the tag tree
        # per our design; we don't add it here to avoid duplicating
        # the destructive operation in two UI surfaces.

        # ---- Tools ---------------------------------------------------
        # Dataset-wide maintenance operations live here, kept apart from
        # the everyday per-action items in File/Edit so a broad,
        # write-everything operation can't be reached by a stray click
        # near common actions. Each Tools action confirms before acting.
        m_tools = mb.addMenu("&Tools")
        self._act_health_check = QAction("Dataset Health Check…", self)
        self._act_health_check.triggered.connect(self._action_health_check)

        self._act_token_counter = QAction("Token Counter…", self)
        self._act_token_counter.triggered.connect(self._action_token_counter)

        self._act_prune_advisor = QAction("Tag Pruning Advisor…", self)
        self._act_prune_advisor.setToolTip(
            "Rank the tags worth cutting when captions are pressing "
            "against the token limit. Produces a list; changes "
            "nothing.")
        self._act_prune_advisor.triggered.connect(
            self._action_prune_advisor)

        self._act_tag_stats = QAction("Export Tag Statistics…", self)
        self._act_tag_stats.triggered.connect(self._action_tag_stats)

        self._act_trigger_token = QAction(
            "Set Trigger Token (Front Lock)…", self)
        self._act_trigger_token.triggered.connect(self._action_trigger_token)

        self._act_clean_dupes = QAction("Clean Up Duplicate Tags…", self)
        self._act_clean_dupes.setToolTip(
            "Scan every caption file in the dataset and remove repeated "
            "tags within the same file (keeping the first occurrence)."
        )
        self._act_clean_dupes.triggered.connect(
            self._action_clean_duplicate_tags
        )
        m_tools.addAction(self._act_trigger_token)
        m_tools.addAction(self._act_clean_dupes)
        m_tools.addAction(self._act_health_check)
        m_tools.addAction(self._act_prune_advisor)


        self._act_reformat_tags = QAction(
            "Reformat Tags (Underscores \u2194 Spaces)\u2026", self
        )
        self._act_reformat_tags.setToolTip(
            "Bulk-convert underscores to spaces (or back) inside every tag "
            "in the dataset, with a preview and a one-step undo."
        )
        self._act_reformat_tags.triggered.connect(self._action_reformat_tags)
        m_tools.addAction(self._act_reformat_tags)
        self._act_audit_tags = QAction(
            "Audit Tags Against Danbooru…", self
        )
        self._act_audit_tags.setToolTip(
            "Cross-reference every tag in your dataset against the "
            "Danbooru tag list to find typos, variants, and unknown tags."
        )
        self._act_audit_tags.triggered.connect(self._action_audit_tags)
        m_tools.addAction(self._act_audit_tags)

        self._act_audit_exceptions = QAction(
            "Audit Exceptions\u2026", self
        )
        self._act_audit_exceptions.setToolTip(
            "Manage the list of tags the audit should never flag "
            "(studio names, OC tags, deliberate conventions)."
        )
        self._act_audit_exceptions.triggered.connect(
            self._action_audit_exceptions
        )
        m_tools.addAction(self._act_audit_exceptions)

        m_tools.addSeparator()
        self._act_conflict_rules = QAction("Edit Conflict Rules…", self)
        self._act_conflict_rules.setToolTip(
            "Create and edit rules for tag combinations that shouldn't "
            "occur together (e.g. indoors vs outdoors)."
        )
        self._act_conflict_rules.triggered.connect(
            self._action_edit_conflict_rules
        )
        m_tools.addAction(self._act_conflict_rules)
        self._act_conflict_scan = QAction("Scan for Tag Conflicts…", self)
        self._act_conflict_scan.setToolTip(
            "Check every image against your conflict rules and resolve "
            "the violations."
        )
        self._act_conflict_scan.triggered.connect(
            self._action_scan_conflicts
        )
        m_tools.addAction(self._act_conflict_scan)

        # ---- View ----------------------------------------------------
        m_view = mb.addMenu("&View")
        self._act_statistics = QAction("Statistics…", self)
        self._act_statistics.triggered.connect(self._action_open_statistics)
        m_view.addAction(self._act_statistics)
        m_view.addAction(self._act_tag_stats)
        m_view.addAction(self._act_token_counter)
        m_view.addSeparator()
        self._act_reset_layout = QAction("Reset window layout", self)
        self._act_reset_layout.triggered.connect(self._action_reset_layout)
        m_view.addAction(self._act_reset_layout)

        # ---- Settings ------------------------------------------------
        m_settings = mb.addMenu("&Settings")
        self._act_preferences = QAction("Preferences…", self)
        self._act_preferences.triggered.connect(self._action_open_settings)
        m_settings.addAction(self._act_preferences)

        # ---- Help ----------------------------------------------------
        m_help = mb.addMenu("&Help")
        # First entry, deliberately. The program's weakness was never
        # a missing tool — it was that opening it presented a menu
        # rather than a path.
        self._act_guide = QAction("User Guide\u2026", self)
        self._act_guide.setToolTip(
            "Controls, what each tool does, the recommended workflow, "
            "and tips \u2014 browse by topic.")
        self._act_guide.triggered.connect(self._action_getting_started)
        m_help.addAction(self._act_guide)
        m_help.addSeparator()
        self._act_about = QAction("About TagWalker", self)
        self._act_about.triggered.connect(self._action_about)
        self._act_diag_log = QAction("Diagnostic Log\u2026", self)
        self._act_diag_log.setToolTip(
            "Errors the program has recorded. Written automatically "
            "\u2014 useful when something went wrong and the message "
            "was dismissed before it could be read.")
        self._act_diag_log.triggered.connect(self._action_diagnostic_log)
        m_help.addAction(self._act_diag_log)
        self._act_open_log = QAction("Open Log File Location", self)
        self._act_open_log.triggered.connect(self._action_open_log_folder)
        m_help.addAction(self._act_open_log)
        m_help.addSeparator()
        self._act_eula = QAction("License Agreement\u2026", self)
        self._act_eula.setToolTip(
            "Re-read the license agreement you accepted, including the "
            "reminder to back up your data.")
        self._act_eula.triggered.connect(self._action_show_eula)
        m_help.addAction(self._act_eula)
        m_help.addAction(self._act_about)

    # ------------------------------------------------------------------

        self._install_tag_referencer_button()
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _build_shortcuts(self) -> None:
        """Create QShortcut objects from current Settings.

        Each shortcut binds to a method on this window (or on the
        ImagePanel for the action shortcuts). When the user changes
        shortcuts in Preferences, we call _rebind_shortcuts to update.
        """
        bindings = {
            "yes":          self._image_panel.trigger_yes,
            "no":           self._image_panel.trigger_no,
            "skip_image":   self._image_panel.trigger_skip_image,
            "skip_tag":     self._image_panel.trigger_skip_tag,
            "back":         self._image_panel.trigger_back,
            "undo":         self._action_undo,
            "save_session": self._action_save_session,
        }
        # nav_prev / nav_next / close_zoom omitted: nav step-without-commit
        # would need a separate state method; close_zoom is handled by
        # the ZoomDialog's keyPressEvent directly.

        for name, handler in bindings.items():
            keystr = self._settings.get_shortcut(name)
            if not keystr:
                continue
            sc = QShortcut(QKeySequence(keystr), self)
            sc.activated.connect(handler)
            self._shortcuts[name] = sc

        # Reflect current shortcut text on the image-panel button labels.
        self._refresh_button_shortcut_labels()

    def _rebind_shortcuts(self) -> None:
        """Tear down existing QShortcut objects and rebuild from Settings.

        Called after the Preferences dialog commits changes. We dispose
        old QShortcut objects to free their key bindings before
        creating fresh ones.
        """
        for sc in self._shortcuts.values():
            sc.setEnabled(False)
            sc.setParent(None)
        self._shortcuts.clear()
        self._build_shortcuts()

    def _refresh_button_shortcut_labels(self) -> None:
        """Update the keyboard hint suffixes on image-panel buttons."""
        hints = {
            "yes":        self._settings.get_shortcut("yes"),
            "no":         self._settings.get_shortcut("no"),
            "skip_image": self._settings.get_shortcut("skip_image"),
            "back":       self._settings.get_shortcut("back"),
            "skip_tag":   self._settings.get_shortcut("skip_tag"),
        }
        self._image_panel.update_shortcut_labels(hints)

    # ------------------------------------------------------------------
    # Autosave
    # ------------------------------------------------------------------

    def _start_autosave_timer(self) -> None:
        """Start the recurring autosave timer.

        The timer always runs; the slot is a no-op unless there's
        something to save (state exists, path is set, actions happened
        since last save).
        """
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave_tick)
        interval_s = self._settings.autosave_interval_seconds
        if interval_s > 0:
            # QTimer takes milliseconds.
            self._autosave_timer.start(interval_s * 1000)

    def _autosave_tick(self) -> None:
        """Fired by the autosave timer."""
        if not self._settings.autosave_enabled:
            return
        if self._state is None or self._session_path is None:
            return
        if self._action_counter == self._last_saved_counter:
            return  # nothing changed since last save
        self._do_save()

    def _bump_action_counter(self) -> None:
        """Increment the action counter and check action-based autosave.

        Called from _on_state_change for events that constitute a
        savable user action.
        """
        self._action_counter += 1
        if not self._settings.autosave_enabled:
            return
        if self._session_path is None:
            return
        threshold = self._settings.autosave_interval_actions
        if threshold > 0 and (
            self._action_counter - self._last_saved_counter >= threshold
        ):
            self._do_save()

    def _do_save(self) -> bool:
        """Write the current state to the configured session path.

        Returns True on success, False on failure (with an error
        message shown to the user). Updates the saved-counter
        baseline on success so the next tick doesn't repeat.

        Refuses to save while a scan is in progress. During a scan the
        old SessionState is still loaded but the user is conceptually
        moving to a different directory (Browse / Full Reset re-scan).
        Saving here would write a snapshot of the dataset they're
        leaving to the (old) session path — exactly the stale write we
        want to avoid, and it would also clobber progress a Full Reset
        was meant to discard. This is the single chokepoint for all
        save paths (manual, Save As, Ctrl+S, autosave), so guarding
        here covers every one.
        """
        if self._state is None or self._session_path is None:
            return False
        if self._scan_in_progress():
            return False
        try:
            save_session(self._state, self._session_path)
        except OSError as e:
            QMessageBox.warning(
                self,
                "Save failed",
                f"Could not save session file:\n{e}",
            )
            return False
        self._last_saved_counter = self._action_counter
        self._lbl_save_status.setText(
            f"{Icons.DISK}  Saved to {self._session_path.name}"
        )
        return True

    # ------------------------------------------------------------------
    # State change listener (drives status bar + action counter)
    # ------------------------------------------------------------------

    def _on_state_change(self, change: StateChange) -> None:
        # Anything that mutates the user's progress counts as an action.
        # locks_changed is included: setting or removing a front-lock is
        # a real, savable change to the session (locks live in the
        # project file), but it can fire on its own — removing a lock, or
        # applying a lock that reorders no captions — without any
        # accompanying tag/tree event. Leaving it out meant a lock-only
        # change did not advance the save baseline, so autosave never
        # fired for it and a close could discard the lock.
        if change.kind in (
            "image_changed", "tag_changed", "tag_selected",
            "tree_rebuilt", "walk_ended", "locks_changed",
        ):
            self._bump_action_counter()

        # Update breadcrumb in status bar. tag_changed catches batch
        # decisions (which don't fire walk_advanced) and image_changed
        # catches granular tag-edit cleanups that may clear pending
        # decisions on the current tag.
        if change.kind in (
            "tag_selected", "walk_advanced", "walk_ended", "tree_rebuilt",
            "tag_changed", "image_changed",
        ):
            self._refresh_walk_status()

        # Total counter + overall progress (Pass C). Recompute on
        # anything that could change the global decision count or the
        # total job count. Granular edits (image_changed with
        # extra='granular_edit') don't change decisions but do change
        # the job count if tags were added/removed; safe to include.
        if change.kind in (
            "walk_advanced", "walk_ended", "tag_changed",
            "filter_changed", "tree_rebuilt", "image_changed",
            "tag_selected",
        ):
            self._refresh_total_counter()

        # Sync the watcher to the new current image.
        if change.kind in ("tag_selected", "walk_advanced", "walk_ended"):
            if self._watcher is not None:
                cur = self._state.current_image if self._state else None
                self._watcher.set_watched_image(
                    cur.image_path if cur is not None else None
                )

        # When a walk ends (the whole tag's queue is done and we advanced
        # off it), move keyboard focus to the tag tree. This matches the
        # natural flow: the user finishes an image queue, then presses
        # Down to move to the next tag — focus needs to be on the tag
        # list for that to work, rather than lingering on the image area.
        if change.kind == "walk_ended":
            if self._tag_tree is not None:
                self._tag_tree.focus_tree()

    def _set_swirl_visible_from_setting(self) -> None:
        """Show or hide the swirl zone according to settings.

        Hides the *zone* (the splitter cell), not just the inner
        widget — otherwise an empty cell would still occupy space in
        the bottom splitter. The splitter automatically redistributes
        when a child is hidden.
        """
        visible = bool(self._settings.show_swirl)
        self._swirl_zone.setVisible(visible)

    def _refresh_total_counter(self) -> None:
        """Recompute dataset-wide YES/NO decision count.

        Counter semantics:
        - Counter shows total YES + NO decisions ever made on this
          dataset. NOT skipped (skip is "review later" — same logic
          as the swirl uses). NOT walk-index-based.
        - Progress bar visualizes the CURRENT tag's completion (not
          dataset-wide). The dataset-wide percentage was misleading
          because total jobs (= sum of all tag counts) can be tens of
          thousands, making the % move imperceptibly. The swirl
          already conveys per-tag progress, so showing the same metric
          numerically in the bar is consistent.

        Iterates every (image, tag) job. Cost is O(images *
        avg_tags_per_image), well under 50ms for realistic datasets.
        Driven by state events; no polling. Bursts naturally fire
        multiple events but Qt collapses event-loop work.
        """
        if self._state is None:
            self._lbl_total_counter.setText("Total: 0 decisions")
            self._progress_overall.setValue(0.0)
            return

        from core.state import Decision

        # Lifetime YES + NO across all (image, tag) pairs.
        # Skip-image deliberately excluded — matches swirl semantics
        # and the user's mental model of "decisions made".
        total_decided = 0
        for path, tag, decision in self._state.iter_decisions():
            if decision == Decision.YES or decision == Decision.NO:
                total_decided += 1

        self._lbl_total_counter.setText(
            f"Total: {total_decided:,} decisions"
        )

        # Progress bar tracks CURRENT TAG completion (matches swirl).
        tag = self._state.current_tag
        if tag is None:
            self._progress_overall.setValue(0.0)
            return
        queue = self._state.get_current_queue()
        if not queue:
            self._progress_overall.setValue(0.0)
            return
        cur_decided = self._state.current_queue_decided()
        self._progress_overall.setValue(cur_decided / len(queue))

    def _refresh_walk_status(self) -> None:
        if self._state is None:
            self._lbl_walk_status.setText("")
            return
        tag = self._state.current_tag
        if tag is None:
            self._lbl_walk_status.setText("")
            return
        cur_img = self._state.current_image
        breadcrumb_parts: list[str] = []
        if cur_img is not None and cur_img.subfolder:
            breadcrumb_parts.append(cur_img.subfolder)
        breadcrumb_parts.append(tag)
        # Show decisions made of total queue size, NOT walk_index.
        # Walk position changes as the user navigates back/forward
        # without making decisions; we want the breadcrumb to reflect
        # actual progress on this tag (matches the swirl + the
        # image-panel progress label).
        from core.state import Decision
        queue = self._state.get_current_queue()
        decided = self._state.current_queue_decided()
        breadcrumb_parts.append(f"{decided}/{self._state.walk_size}")
        self._lbl_walk_status.setText(
            f"{Icons.PIN}  " + " › ".join(breadcrumb_parts)
        )

    # ------------------------------------------------------------------
    # Action: open directory + background scan
    # ------------------------------------------------------------------

    def _scan_in_progress(self) -> bool:
        """True if a background scan is currently running."""
        return self._scan_thread is not None

    def _action_browse_directory(self) -> None:
        if self._scan_in_progress():
            QMessageBox.information(
                self,
                "Scan in progress",
                "A directory scan is already running. Please wait for it "
                "to finish before opening another directory.",
            )
            return
        start_dir = self._settings.last_directory or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select dataset directory",
            start_dir,
        )
        if not chosen:
            return
        self._start_scan(Path(chosen))

    def _action_open_multiple(self) -> None:
        """File \u2192 Open Multiple Folders: popup with a folder
        list, Add/Remove, and an optional remembered set (user
        config). Missing remembered folders are marked, never
        silently dropped; see ui/multi_folder_dialog.py."""
        from ui.multi_folder_dialog import MultiFolderDialog
        dlg = MultiFolderDialog(self._settings, self)
        if not dlg.exec():
            return
        roots = dlg.selected_roots()
        if roots:
            self._start_scan(roots)


    def _start_scan(self, root) -> None:  # Path or list[Path]
        """Kick off a background scan and disable conflicting controls."""
        # Normalize once; every use below must handle the multi case
        # (field crash: the status line assumed a single Path).
        roots = [root] if isinstance(root, Path) else [Path(r) for r in root]
        if self._scan_thread is not None:
            # A scan is already in progress. Disconnect its signals first
            # so a finished/failed callback that's already been emitted
            # (and is sitting queued on the main thread) can't fire into
            # our handlers after we've moved on. Then cancel and join.
            if self._scan_worker is not None:
                try:
                    self._scan_worker.finished.disconnect()
                    self._scan_worker.failed.disconnect()
                    self._scan_worker.progress.disconnect()
                except (RuntimeError, TypeError):
                    pass
                self._scan_worker.cancel()
            self._scan_thread.quit()
            self._scan_thread.wait()

        # New generation. Any in-flight stale result will be ignored.
        self._scan_generation += 1
        generation = self._scan_generation

        # Disable the load actions and Full Reset while a scan runs (they
        # were the path-bar buttons; now they're File-menu actions).
        self._act_open_dir.setEnabled(False)
        self._act_open_multi.setEnabled(False)
        self._act_full_reset.setEnabled(False)
        if len(roots) == 1:
            self._status.showMessage(f"Scanning {roots[0].name}…")
        else:
            self._status.showMessage(
                f"Scanning {len(roots)} folders "
                f"({roots[0].name}, …)…")

        thread = QThread(self)
        worker = ScanWorker(roots, generation=generation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Connect to PLAIN bound methods (not lambdas). Qt sees the
        # receiver (self, in the GUI thread) and uses an automatic
        # queued connection, so the slots run on the main thread —
        # safe to touch the UI. The generation filter is read from the
        # worker inside each slot. (A lambda receiver has no thread
        # affinity; Qt may invoke it on the worker thread, causing the
        # "Cannot set parent / different thread" crash.)
        worker.finished.connect(self._on_scan_finished)
        worker.failed.connect(self._on_scan_failed)
        worker.progress.connect(self._on_scan_progress)
        # Ensure clean shutdown when done.
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._scan_thread = thread
        self._scan_worker = worker
        thread.start()

    def _on_scan_progress(self, processed: int, total: int) -> None:
        if total > 0:
            self._status.showMessage(f"Scanning… {processed}/{total} files")

    def _on_scan_failed(self, error: str) -> None:
        # Ignore results from a superseded scan generation. The emitting
        # worker is self.sender(); compare ITS generation to the current
        # one. A stale worker's queued signal carries the old generation.
        sender = self.sender()
        gen = getattr(sender, "generation", None)
        if gen is not None and gen != self._scan_generation:
            return
        self._act_open_dir.setEnabled(True)
        self._act_open_multi.setEnabled(True)
        # If a session was already loaded before this (failed) re-scan,
        # Full reset must remain available — it operates on the still-
        # loaded session. Without this, a failed re-scan leaves Full
        # reset greyed out even though there's a live session to reset.
        self._act_full_reset.setEnabled(self._state is not None)
        self._scan_thread = None
        self._scan_worker = None
        QMessageBox.critical(self, "Scan failed", f"{error}")
        self._status.clearMessage()

    def _on_scan_finished(self, result: ScanResult) -> None:
        # Ignore results from a superseded scan generation. Without this,
        # a stale worker's queued finished() could load the wrong
        # directory over a newer scan/session.
        sender = self.sender()
        gen = getattr(sender, "generation", None)
        if gen is not None and gen != self._scan_generation:
            return
        # Worker thread cleanup is wired in _start_scan via thread.quit().
        self._scan_thread = None
        self._scan_worker = None
        self._act_open_dir.setEnabled(True)
        self._act_open_multi.setEnabled(True)

        if result.cancelled:
            self._status.showMessage("Scan cancelled", 3000)
            # A cancelled re-scan leaves the previous session intact, so
            # Full reset should be usable again if one is loaded.
            self._act_full_reset.setEnabled(self._state is not None)
            return

        # Build a fresh SessionState and apply default UI mode from
        # Settings. The state must exist before any widget attach so
        # attach() can wire up listeners cleanly.
        new_state = SessionState(result)
        # Apply default UI settings.
        try:
            new_state.set_filter_mode(
                FilterMode(self._settings.default_filter_mode)
            )
        except ValueError:
            pass
        # Token limit for the "over token limit" filter.
        try:
            new_state.set_token_limit(self._settings.token_limit,
                                       self._settings.tokenizer_target)
        except Exception:
            pass
        try:
            new_state.set_sort_mode(
                SortMode(self._settings.default_sort_mode)
            )
        except ValueError:
            pass
        new_state.set_show_orphans(self._settings.default_show_orphans)
        new_state.set_auto_yes(self._settings.default_auto_yes)
        new_state.set_tag_complete_behavior(
            self._settings.default_tag_complete_behavior
        )
        new_state.set_cooccur_too_common_pct(
            self._settings.default_cooccur_too_common_pct
        )
        new_state.set_cooccur_source(
            self._settings.default_cooccur_source
        )
        new_state.set_show_cooccur_hints(
            self._settings.default_show_cooccur_hints
        )

        self._adopt_state(new_state, scan_root=result.root)

        # Remember directory for next file dialog.
        self._settings.last_directory = str(result.root)
        if len(getattr(result, "roots", []) or []) > 1:
            self.statusBar().showMessage(
                f"Loaded {len(result.roots)} folders \u2014 "
                f"{result.total_images} images combined", 8000)
        self._settings.sync()

        self._status.showMessage(
            f"Scanned {result.total_images} images, "
            f"{result.total_tags} unique tags "
            f"({result.duration_seconds:.1f}s)",
            5000,
        )

        # Warn about images that share a caption file (same stem, different
        # extension). These can't be edited independently — editing one
        # leaves the other stale and a later edit overwrites it on disk —
        # so the user should resolve the duplicate before tagging.
        if result.caption_collisions:
            groups = result.caption_collisions
            n_files = sum(len(g) for g in groups)
            sample = "\n".join(
                "  • " + ", ".join(g) for g in groups[:8]
            )
            more = "" if len(groups) <= 8 else f"\n  …and {len(groups) - 8} more"
            QMessageBox.warning(
                self,
                "Images share a caption file",
                f"{n_files} image(s) in {len(groups)} group(s) resolve to "
                "the same .txt caption file because they share a name and "
                "differ only by extension:\n\n" + sample + more +
                "\n\nThey can't be tagged independently — editing one will "
                "overwrite the other's tags. Keep only one image per name "
                "(or rename the duplicates) before editing.",
            )

    def _adopt_state(
        self,
        new_state: SessionState,
        scan_root: Optional[Path] = None,
    ) -> None:
        """Tear down old state, install new state, wire everything up."""
        # Tear down old listeners and watcher cleanly.
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        if self._watcher is not None:
            self._watcher.shutdown()
            self._watcher = None

        self._state = new_state
        new_state.add_listener(self._on_state_change)

        # Hand state to every widget.
        for widget in (
            self._tag_tree,
            self._image_panel,
            self._queue_panel,
            self._file_state_panel,
            self._action_log,
            self._swirl_widget,
        ):
            widget.attach(new_state)

        # Fresh watcher for this session.
        self._watcher = CurrentFileWatcher(new_state, self)
        # Surface external caption changes in the status bar. The watcher
        # already reconciles memory to disk and (now) logs a distinct
        # action-log entry; this adds a passing on-screen notice so a
        # change made outside the app isn't invisible in the moment. The
        # detailed "what changed" is in the action log entry.
        self._watcher.external_change_detected.connect(
            self._on_external_change_detected)

        # If the stats window was opened in a previous session, re-bind
        # it to the new state so its numbers and swirl reflect the
        # newly-loaded data instead of the old session's.
        if self._stats_window is not None:
            self._stats_window.attach(new_state)

        # Reset action counter for new session. Session has no saved
        # baseline yet — every change is "unsaved" until first save.
        self._action_counter = 0
        self._last_saved_counter = 0
        # If we're adopting state from a loaded session, the caller
        # will set _session_path before calling us.

        # The path-bar label that used to show the loaded folder(s) has
        # been removed; the window title now carries the loaded-dataset
        # identity (single- and multi-folder) via _refresh_window_title.
        self._act_full_reset.setEnabled(True)
        self._refresh_window_title()
        self._refresh_walk_status()
        self._refresh_total_counter()

    # ------------------------------------------------------------------
    # Action: Full reset
    # ------------------------------------------------------------------

        if getattr(self, "_tag_ref_win", None) is not None:
            self._tag_ref_win.set_state(new_state)

    def _action_full_reset(self) -> None:
        """Re-scan the current directory and start a fresh session.

        Confirms first because all in-memory progress (decisions, undo
        stack, skipped tags) is discarded. Caption files on disk are
        not touched.
        """
        if self._state is None:
            return
        if self._scan_in_progress():
            # The Full Reset *button* is disabled during a scan, but the
            # File → New Session menu path reaches here too. Without this
            # guard, starting a reset mid-scan would call _start_scan
            # again, overlapping with the in-flight worker and the
            # generation-id bookkeeping — confusing at best, wrong
            # directory loaded at worst.
            QMessageBox.information(
                self,
                "Scan in progress",
                "A directory scan is already running. Please wait for it "
                "to finish before starting a new session.",
            )
            return
        msg = QMessageBox(self)
        msg.setWindowTitle("Full reset")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText("Reset TagWalker to a freshly-started state?")
        msg.setInformativeText(
            "The loaded folder is closed, and decisions, undo history "
            "and skipped tags are discarded.\n"
            "Caption files on disk are NOT affected."
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return
        # A reset means "as if the program had just started": the
        # folder is CLOSED, not re-scanned. Re-scanning kept the
        # dataset loaded, which is not what a reset should mean.
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        if self._watcher is not None:
            self._watcher.shutdown()
            self._watcher = None
        for widget in (
            self._tag_tree,
            self._image_panel,
            self._queue_panel,
            self._file_state_panel,
            self._action_log,
            self._swirl_widget,
        ):
            widget.attach(None)
        self._state = None
        self._session_path = None
        self._lbl_save_status.setText("")
        self._act_full_reset.setEnabled(False)
        self._refresh_window_title()
        # The Tag Referencer forgets too — a reset that left it
        # showing the previous session's tag would not be a reset.
        win = getattr(self, "_tag_ref_win", None)
        if win is not None:
            win.reset_session()
            win.set_state(None)
            win.close()
        self._sync_tag_ref_button()

    # ------------------------------------------------------------------
    # Action: New Session (drop state, keep directory? or no — drop both)
    # ------------------------------------------------------------------

    def _action_new_session(self) -> None:
        """Discard the current session, keep the directory loaded.

        Equivalent to Full reset semantically but reachable from the
        File menu for users who expect it there.
        """
        self._action_full_reset()

    # ------------------------------------------------------------------
    # Action: Load Session
    # ------------------------------------------------------------------

    def _action_load_session(self) -> None:
        if self._scan_in_progress():
            QMessageBox.information(
                self,
                "Scan in progress",
                "A directory scan is running. Please wait for it to finish "
                "before loading a session — otherwise the scan would "
                "overwrite the session you just loaded.",
            )
            return
        if self._state is None:
            QMessageBox.information(
                self,
                "Load session",
                "Open a directory first, then load a saved session for it.",
            )
            return

        # Start the dialog where the dataset was loaded from, so a session
        # file kept alongside its dataset (a common layout) is right there
        # — one hop instead of navigating from wherever the last save
        # happened to be. self._state is guaranteed non-None here (checked
        # above), so root is always available; for a multi-folder load,
        # root is the primary (first) folder. Fall back to the previous
        # behaviour (last save location, then last opened directory) only
        # if the dataset root can't be resolved.
        start = ""
        if self._state is not None and self._state.root is not None:
            start = str(self._state.root)
        if not start:
            start = self._settings.last_session_save or self._settings.last_directory
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Load session",
            start,
            SESSION_FILE_FILTER,
        )
        if not chosen:
            return

        path = Path(chosen)
        try:
            loaded = load_session(path)
        except FileNotFoundError:
            QMessageBox.warning(self, "Load session", "File not found.")
            return
        except InvalidSessionFileError as e:
            QMessageBox.warning(
                self, "Load session", f"Not a valid session file:\n{e}"
            )
            return
        except OSError as e:
            QMessageBox.warning(self, "Load session", f"Could not read:\n{e}")
            return

        # Directory-mismatch case: ask the user explicitly per design.
        # Compare RESOLVED paths so cosmetic differences in
        # representation don't trigger a spurious mismatch warning:
        # forward vs back slashes (J:/dataset vs J:\dataset), trailing
        # slashes, "." segments, or differing case on case-insensitive
        # filesystems all normalize away. resolve() can raise on a path
        # that no longer exists, so fall back to raw comparison if it
        # does (which is the conservative "warn" behavior).
        def _same_dir(a: Path, b: Path) -> bool:
            try:
                ra, rb = a.resolve(), b.resolve()
            except (OSError, RuntimeError):
                return a == b
            if ra == rb:
                return True
            # On Windows, paths are case-insensitive; normcase catches
            # drive-letter / casing differences resolve() may preserve.
            import os as _os
            return _os.path.normcase(str(ra)) == _os.path.normcase(str(rb))

        saved_set = {
            str(Path(q).resolve())
            for q in (loaded.saved_roots or [loaded.saved_root])
        }
        current_set = {
            str(Path(q).resolve())
            for q in (getattr(self._state, "roots", None)
                      or [self._state.root])
        }
        if saved_set != current_set:
            msg = QMessageBox(self)
            msg.setWindowTitle("Directory mismatch")
            msg.setIcon(QMessageBox.Icon.Question)
            msg.setText("Load this session anyway?")
            msg.setInformativeText(
                f"Saved for:\n{loaded.saved_root}\n\n"
                f"Currently open:\n{self._state.root}\n\n"
                "Image and tag references that don't match the current "
                "dataset will be silently dropped."
            )
            msg.setStandardButtons(
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel
            )
            msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if msg.exec() != QMessageBox.StandardButton.Yes:
                return

        report = apply_to_state(loaded, self._state)
        self._session_path = path
        self._action_counter = 0
        self._last_saved_counter = 0
        self._lbl_save_status.setText(
            f"{Icons.DISK}  Loaded {path.name}"
        )
        self._refresh_window_title()
        self._show_load_summary(report)

    def _show_load_summary(self, report: ReconciliationReport) -> None:
        """Display the one-shot summary dialog after a session load."""
        lines: list[str] = []
        lines.append(f"Decisions restored: {report.decisions_restored}")
        if report.decisions_discarded_missing_image:
            lines.append(
                f"Discarded (image missing): "
                f"{report.decisions_discarded_missing_image}"
            )
        if report.decisions_discarded_missing_tag:
            lines.append(
                f"Discarded (tag missing): "
                f"{report.decisions_discarded_missing_tag}"
            )
        if report.new_images:
            lines.append(f"New images (added as pending): {len(report.new_images)}")
        if report.missing_images:
            lines.append(f"Removed images: {len(report.missing_images)}")
        if report.new_tags:
            lines.append(f"New tags discovered: {len(report.new_tags)}")
        if report.disappeared_tags:
            lines.append(f"Tags removed: {len(report.disappeared_tags)}")
        if report.walk_position_was_set and not report.walk_position_restored:
            lines.append(
                "Saved walk position could not be restored "
                "(image or tag no longer exists)."
            )
        lines.append("")
        lines.append(f"Pending tags remaining: {report.pending_tags_remaining}")

        msg = QMessageBox(self)
        msg.setWindowTitle("Session loaded")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText("\n".join(lines))
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()

    # ------------------------------------------------------------------
    # Action: Save Session / Save As
    # ------------------------------------------------------------------

    def _action_save_session(self) -> None:
        if self._state is None:
            return
        if self._scan_in_progress():
            QMessageBox.information(
                self,
                "Scan in progress",
                "A directory scan is running. Please wait for it to finish "
                "before saving — saving now would write the previous "
                "dataset's session.",
            )
            return
        if self._session_path is None:
            # No path yet — Save behaves like Save As.
            self._action_save_session_as()
            return
        self._do_save()

    def _action_save_session_as(self) -> None:
        if self._state is None:
            return
        if self._scan_in_progress():
            QMessageBox.information(
                self,
                "Scan in progress",
                "A directory scan is running. Please wait for it to finish "
                "before saving.",
            )
            return
        start = self._settings.last_session_save or self._settings.last_directory
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Save session as",
            start,
            SESSION_FILE_FILTER,
        )
        if not chosen:
            return
        path = Path(chosen)
        # Ensure a sensible extension if the user didn't type one.
        if path.suffix not in (".json",) and ".tagwalker" not in path.suffixes:
            path = path.with_suffix(".tagwalker.json")
        self._session_path = path
        self._settings.last_session_save = str(path)
        self._settings.sync()
        self._do_save()
        self._refresh_window_title()

    # ------------------------------------------------------------------
    # Action: Undo
    # ------------------------------------------------------------------

    def _action_image_editor(self) -> None:
        """File → Launch Image Editor."""
        from ui.image_editor_window import ImageEditorWindow

        existing = getattr(self, "_image_editor", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        # Independent top-level window, parent=None — the same setup
        # Post Browser and Tag Reference use. A parented window is
        # minimised WITH the main window at the OS level and gets no
        # events of its own, which is why the editor kept coming back
        # un-maximised after a program minimise/restore: it was a child
        # riding the parent's state. Parentless, its window state is
        # entirely its own, so minimising the main window no longer
        # touches it. The trade-off a parent gave us — auto-close and
        # lifetime — is handled explicitly: main window's closeEvent
        # closes it, and WA_DeleteOnClose frees it.
        self._image_editor = ImageEditorWindow(self._settings)
        self._image_editor.setAttribute(
            Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._image_editor.destroyed.connect(
            lambda: setattr(self, "_image_editor", None))
        # The editor applies its own maximised state in showEvent (it
        # reads the setting at construction), so a plain show() is all
        # that is needed here — and it is reliable on Windows, where a
        # showMaximized() before the first show can be overridden by
        # the window's pending geometry.
        self._image_editor.show()

    def _action_undo_all(self) -> None:
        """Reverse the whole session, one entry at a time.

        Deliberately NOT a bulk state reset: replaying the existing
        undo means every action reverses the way it already knows how
        to, so a granular tag edit and a batch operation both come
        back correctly. A separate "restore everything" path would be
        a second implementation of the same thing, and the one that
        gets less testing is the one that is wrong.
        """
        if self._state is None or not self._state.can_undo():
            QMessageBox.information(
                self, "Nothing to Undo",
                "No actions have been recorded in this session.")
            return
        pending = len(self._state._undo_stack)
        answer = QMessageBox.question(
            self, "Undo All Actions",
            f"Reverse all {pending:,} action(s) from this session?\n\n"
            "Captions return to the state they were in on disk when "
            "you loaded this dataset. Bookmarks and settings are not "
            "affected.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        done = 0
        failed = 0
        # Bounded by the stack length: an undo that consumes its entry
        # but reports failure must not spin here forever.
        for _ in range(pending):
            if not self._state.can_undo():
                break
            if self._state.undo():
                done += 1
            else:
                failed += 1
        self._queue_panel.reselect_group_after_undo()
        if failed:
            QMessageBox.warning(
                self, "Undo All Finished",
                f"Reversed {done:,} action(s). {failed:,} could not be "
                "reversed \u2014 see Help \u2192 Diagnostic Log.")
        else:
            self._status.showMessage(f"Reversed {done:,} action(s).", 4000)

    def _action_copy_caption(self) -> None:
        """Current image's tags to the clipboard."""
        from PySide6.QtGui import QGuiApplication

        if self._state is None or self._state.current_image is None:
            self._status.showMessage("No image selected.", 4000)
            return
        tags = self._state.get_image_tags(
            self._state.current_image.image_path) or []
        if not tags:
            self._status.showMessage("This image has no tags.", 4000)
            return
        QGuiApplication.clipboard().setText(", ".join(tags))
        self._status.showMessage(f"Copied {len(tags):,} tag(s) to the clipboard.", 4000)

    def _action_select_all(self) -> None:
        """Select every row currently shown in the queue."""
        if self._state is None:
            return
        self._queue_panel.list_widget.selectAll()
        n = len(self._queue_panel.list_widget.selectedItems())
        self._status.showMessage(f"Selected {n:,} image(s).", 4000)

    def _action_undo(self) -> None:
        if self._state is None:
            return
        had_undo = self._state.can_undo()
        ok = self._state.undo()
        if ok:
            # If the undone action was a group batch op, return the
            # selection/highlight to that group so grouped undo behaves
            # like standard undo (which returns the walk to the undone
            # image). No-op in flat mode or if the group is gone.
            self._queue_panel.reselect_group_after_undo()
        if not ok and had_undo:
            # There WAS something to undo, but the revert failed (e.g. a
            # disk rewrite couldn't complete). Warn the user that state
            # and disk may have diverged so they can re-check the file,
            # rather than letting them believe the undo succeeded.
            err = self._state.last_undo_error or "Unknown error"
            QMessageBox.warning(
                self,
                "Undo failed",
                "The last action could not be fully undone:\n\n"
                f"{err}\n\n"
                "The file on disk and the in-app state may now disagree. "
                "Please re-check the affected image's caption.",
            )

    # ------------------------------------------------------------------
    # Action: Open settings dialog
    # ------------------------------------------------------------------

    def _on_external_change_detected(self, image_path) -> None:
        """A caption file changed outside the app; the watcher already
        reconciled memory and logged a distinct action-log entry. Flash a
        brief status notice so the change is visible in the moment rather
        than silent. Kept short; the specifics (which tags) are in the
        action log.
        """
        try:
            name = image_path.stem
        except Exception:
            name = "a caption"
        self._status.showMessage(
            f"\u27f3 {name} was changed on disk outside the app \u2014 "
            "reloaded (see Action Log for details).", 6000)

    def _action_open_settings(self) -> None:
        dlg = SettingsDialog(self._settings, self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            # Re-apply anything that depends on settings.
            self._rebind_shortcuts()
            # Apply the (possibly changed) group-preview grid size.
            self._image_panel.set_group_grid_size(
                self._settings.default_group_grid_cols
            )
            # Restart autosave timer with new interval.
            interval_s = self._settings.autosave_interval_seconds
            self._autosave_timer.stop()
            if interval_s > 0:
                self._autosave_timer.start(interval_s * 1000)
            # Apply auto-yes preference to the live session. Takes effect
            # on the next walk landing (lazy), so the user's current
            # image is never auto-decided out from under them.
            if self._state is not None:
                self._state.set_auto_yes(self._settings.default_auto_yes)
                self._state.set_tag_complete_behavior(
                    self._settings.default_tag_complete_behavior
                )
                self._state.set_cooccur_too_common_pct(
                    self._settings.default_cooccur_too_common_pct
                )
                self._state.set_cooccur_source(
                    self._settings.default_cooccur_source
                )
                self._state.set_show_cooccur_hints(
                    self._settings.default_show_cooccur_hints
                )
                # Token limit drives the "over token limit" filter and the
                # file-state badge; push it so a filtered queue re-narrows
                # to the new limit immediately.
                self._state.set_token_limit(self._settings.token_limit,
                                            self._settings.tokenizer_target)
                # Co-occurrence cutoff/visibility may have moved; force a
                # file-state refresh so hints recompute (or hide). This
                # also repaints the token badge against the new limit.
                self._file_state_panel._refresh()

            # Appearance: swirl visibility is live (theme is
            # restart-required and applied at startup only).
            self._set_swirl_visible_from_setting()

    # ------------------------------------------------------------------
    # Action: Reset layout
    # ------------------------------------------------------------------

    def _action_reset_layout(self) -> None:
        """Reset splitter positions and re-center the window.

        Sizes are clamped to the available screen so this never produces
        a window taller or wider than the display — the cause of
        controls landing off-screen on small/short monitors. Splitter
        proportions are derived from the resulting window width rather
        than hardcoded pixels, so the three columns stay balanced at any
        resolution.
        """
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            # Never exceed the available screen; cap at comfortable
            # defaults on large displays. max() with the window minimum
            # keeps it sane on very small screens too.
            w = max(self.minimumWidth(), min(1200, int(avail.width() * 0.85)))
            h = max(self.minimumHeight(), min(800, int(avail.height() * 0.85)))
            self.resize(w, h)
            self.move(
                avail.center().x() - w // 2,
                avail.center().y() - h // 2,
            )
        else:
            w, h = 1200, 800
            self.resize(w, h)

        # Proportion the splitters to the actual window width/height
        # instead of fixed pixel sizes, so the columns stay balanced
        # regardless of resolution. Left ~18%, center ~60%, right ~22%.
        left = int(w * 0.18)
        right = int(w * 0.22)
        center = w - left - right
        self._top_splitter.setSizes([left, center, right])
        # Bottom row: file state | action log | compact swirl zone.
        self._bottom_splitter.setSizes(
            [int(w * 0.40), int(w * 0.36), int(w * 0.24)]
        )
        # Vertical: top image/queue zone gets the lion's share.
        self._main_splitter.setSizes([int(h * 0.72), int(h * 0.28)])

    # ------------------------------------------------------------------
    # Action: Statistics
    # ------------------------------------------------------------------

    def _action_open_statistics(self) -> None:
        """Open (or re-show) the statistics window.

        The window is created lazily on first open and reused
        afterward. It's attached to the current state and re-attached
        whenever a new session is adopted (see _adopt_state), so the
        numbers always reflect the loaded session. Non-modal: the user
        can keep working and watch the stats update live.
        """
        if self._stats_window is None:
            from ui.stats_window import StatsWindow
            self._stats_window = StatsWindow(self)
            self._stats_window.attach(self._state)
        else:
            # Ensure it's bound to the current state (it may have been
            # created before a session load).
            self._stats_window.attach(self._state)
        self._stats_window.show()
        self._stats_window.raise_()
        self._stats_window.activateWindow()

    # ------------------------------------------------------------------
    # Action: About
    # ------------------------------------------------------------------

    def _action_health_check(self) -> None:
        """Tools → Dataset Health Check (non-modal window)."""
        if self._state is None:
            QMessageBox.information(
                self, "No Dataset", "Open a dataset folder first.")
            return
        from ui.health_check_dialog import HealthCheckDialog
        self._health_check_dlg = HealthCheckDialog(self._state, self)
        self._health_check_dlg.show()
        self._health_check_dlg.raise_()

    def _install_tag_referencer_button(self) -> None:
        """Tag Referencer earns a permanent control rather than a menu
        entry: it is a second core surface now, not an occasional
        dialog. It rides in the menu bar's right-hand corner — the far
        end of the tab row, under the window buttons."""
        from PySide6.QtWidgets import QToolButton

        from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

        bar = self.menuBar()
        if bar is None or getattr(self, "_btn_tag_ref", None):
            return
        # A corner widget holds one thing, so the two surfaces ride in
        # a container. Browser first, reading left to right towards the
        # window controls.
        host = QWidget(bar)
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)

        browse = QToolButton(host)
        browse.setText("\U0001F3B4 Post Browser")
        browse.setCheckable(True)
        browse.setToolTip(
            "Open or close the post browser \u2014 uncurated Danbooru "
            "search results, for seeing how a tag is used in practice "
            "rather than only the examples a wiki editor chose.\n\n"
            "Shares its tag with the Tag Referencer: whichever tag "
            "that window is showing is the one this opens on.")
        browse.clicked.connect(self._toggle_post_browser)
        row.addWidget(browse)
        self._btn_post_browser = browse

        btn = QToolButton(host)
        btn.setText("\U0001F310 Tag Referencer")
        btn.setCheckable(True)
        btn.setToolTip(
            "Open or close the Tag Referencer (era drift, Danbooru "
            "wiki, curated examples).")
        btn.clicked.connect(self._toggle_tag_reference)
        row.addWidget(btn)
        self._btn_tag_ref = btn

        bar.setCornerWidget(host, Qt.Corner.TopRightCorner)

    def _toggle_tag_reference(self) -> None:
        win = getattr(self, "_tag_ref_win", None)
        if win is not None and win.isVisible():
            win.close()
            self._sync_tag_ref_button()
            return
        self._action_tag_reference()
        self._sync_tag_ref_button()

    def _sync_tag_ref_button(self) -> None:
        btn = getattr(self, "_btn_tag_ref", None)
        if btn is not None:
            win = getattr(self, "_tag_ref_win", None)
            btn.setChecked(bool(win is not None and win.isVisible()))
        self._sync_post_browser_button()

    def _sync_post_browser_button(self) -> None:
        btn = getattr(self, "_btn_post_browser", None)
        if btn is None:
            return
        ref = getattr(self, "_tag_ref_win", None)
        browser = getattr(ref, "_browser", None) if ref else None
        btn.setChecked(bool(browser is not None
                            and browser.isVisible()))

    def _toggle_post_browser(self) -> None:
        """Open or close the post browser from the main window.

        It stays owned by the Tag Referencer so there is only ever one
        of it, and so a tag clicked inside a browsed post still has
        somewhere to land. Opening it this way creates that window
        without showing it \u2014 the browser is usable on its own, and
        the referencer appears if and when a tag is clicked.
        """
        ref = self._ensure_tag_reference()
        browser = getattr(ref, "_browser", None)
        if browser is not None and browser.isVisible():
            browser.close()
            self._sync_post_browser_button()
            return
        # Just open it. Pre-filling the box would be defensible; the
        # previous version went further and ran the search, which is a
        # network request the user did not ask for and cannot easily
        # undo. The arrow button in the Tag Referencer is the deliberate
        # way to send a tag across.
        ref._open_browser()
        self._sync_post_browser_button()

    def _wire_window_buttons(self, win) -> None:
        """Keep the toolbar in step when a window is dismissed by its
        own title bar rather than by the button that opened it."""
        try:
            win.closed.connect(self._sync_tag_ref_button)
        except Exception:
            pass

    def _ensure_tag_reference(self):
        """One persistent Tag Reference window (independent top-level,
        never modal — the user keeps tagging with it open)."""
        if getattr(self, "_tag_ref_win", None) is None:
            from ui.tag_reference_window import TagReferenceWindow
            self._tag_ref_win = TagReferenceWindow(self._settings)
            self._tag_ref_win.set_state(self._state)
            self._wire_window_buttons(self._tag_ref_win)
            self._tag_ref_win.add_browser_close_hook(
                self._sync_post_browser_button)
        return self._tag_ref_win

    def _action_tag_reference(self) -> None:
        win = self._ensure_tag_reference()
        win.show()
        win.raise_()
        win.activateWindow()
        self._sync_tag_ref_button()

    def _open_tag_reference(self, tag: str) -> None:
        """Right-click entry from the tag tree / file panel."""
        win = self._ensure_tag_reference()
        win.show()
        win.raise_()
        win.navigate(tag)
        self._sync_tag_ref_button()

    def _action_prune_advisor(self) -> None:
        if self._state is None:
            QMessageBox.information(
                self, "Tag Pruning Advisor",
                "Open a dataset folder first.")
            return
        from ui.prune_advisor_dialog import PruneAdvisorDialog
        self._prune_advisor_dlg = PruneAdvisorDialog(
            self._state, self, self._settings)
        self._prune_advisor_dlg.show()
        self._prune_advisor_dlg.raise_()

    def _action_token_counter(self) -> None:
        """Stats → Token Counter (non-modal window)."""
        if self._state is None:
            QMessageBox.information(
                self, "No Dataset", "Open a dataset folder first.")
            return
        from ui.token_counter_dialog import TokenCounterDialog
        self._token_counter_dlg = TokenCounterDialog(self._state, self)
        self._token_counter_dlg.show()
        self._token_counter_dlg.raise_()

    def _action_tag_stats(self) -> None:
        """Tools → Export Tag Statistics."""
        if self._state is None:
            QMessageBox.information(
                self, "No Dataset", "Open a dataset folder first.")
            return
        from ui.tag_stats_dialog import TagStatsDialog
        TagStatsDialog(self._state, self).exec()

    def _action_trigger_token(self) -> None:
        """Tools → Set Trigger Token (Front Lock)."""
        if self._state is None:
            QMessageBox.information(
                self, "No Dataset", "Open a dataset folder first.")
            return
        from ui.trigger_token_dialog import TriggerTokenDialog
        TriggerTokenDialog(self._state, self).exec()

    def _action_clean_duplicate_tags(self) -> None:
        """Tools → Clean Up Duplicate Tags.

        Scans the whole dataset for caption files containing repeated
        tags, shows the user the scope, and on confirmation rewrites
        only the affected files (collapsing each tag to its first
        occurrence). Guarded against running with no dataset or during
        a scan. Not undoable — the confirmation says so.
        """
        if self._state is None:
            QMessageBox.information(
                self, "Clean Up Duplicate Tags",
                "Open a dataset first.",
            )
            return
        if self._scan_in_progress():
            QMessageBox.information(
                self, "Scan in progress",
                "A directory scan is running. Please wait for it to "
                "finish before cleaning duplicate tags.",
            )
            return

        # Read-only scan first so the user sees the scope before any write.
        files, dupes = self._state.scan_duplicate_tags()
        if files == 0:
            QMessageBox.information(
                self, "Clean Up Duplicate Tags",
                "No duplicate tags found. Every caption file is already "
                "clean.",
            )
            return

        msg = QMessageBox(self)
        msg.setWindowTitle("Clean Up Duplicate Tags")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText(
            f"Found {dupes} duplicate tag(s) across {files} file(s)."
        )
        msg.setInformativeText(
            "Each repeated tag will be collapsed to its first occurrence. "
            "Only the affected files are rewritten; clean files are left "
            "untouched.\n\n"
            "This cannot be undone. Proceed?"
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return

        modified, removed = self._state.clean_duplicate_tags()
        failures = self._state.last_write_failures
        if failures:
            # Some files couldn't be written even after retries. Name them
            # so the user knows exactly what to unlock and re-run.
            shown = ", ".join(failures[:15]) + (
                " \u2026" if len(failures) > 15 else "")
            QMessageBox.warning(
                self, "Clean Up partially completed",
                f"Removed {removed} duplicate(s) from {modified} file(s).\n\n"
                f"{len(failures)} file(s) could not be written (locked or "
                f"read-only) and still contain duplicates:\n\n{shown}\n\n"
                "Close anything using them and run the cleanup again.",
            )
        else:
            QMessageBox.information(
                self, "Clean Up Duplicate Tags",
                f"Done. Removed {removed} duplicate tag(s) across "
                f"{modified} file(s).",
            )

    def _action_audit_tags(self) -> None:
        """Tools → Audit Tags Against Danbooru.

        Opens the audit dialog. The dialog is lazy: it loads the tag
        database only when the user clicks Scan, so opening it is cheap
        even on a huge dataset. Guarded against no dataset / a scan in
        progress.
        """
        if self._state is None:
            QMessageBox.information(
                self, "Audit Tags",
                "Open a dataset first.",
            )
            return
        if self._scan_in_progress():
            QMessageBox.information(
                self, "Scan in progress",
                "A directory scan is running. Please wait for it to "
                "finish before auditing tags.",
            )
            return
        from ui.tag_audit_dialog import TagAuditDialog
        dlg = TagAuditDialog(self._state, self)
        dlg.exec()

    def _action_reformat_tags(self) -> None:
        """Tools → Reformat Tags. Opens the underscore<->space converter,
        which previews every change (emoticons protected, merges flagged)
        and applies as one undoable step. Guarded against no dataset / a
        scan in progress."""
        if self._state is None:
            QMessageBox.information(
                self, "Reformat Tags",
                "Open a dataset first.",
            )
            return
        if self._scan_in_progress():
            QMessageBox.information(
                self, "Scan in progress",
                "A directory scan is running. Please wait for it to "
                "finish before reformatting tags.",
            )
            return
        from ui.reformat_tags_dialog import ReformatTagsDialog
        dlg = ReformatTagsDialog(self._state, self)
        dlg.exec()

    def _action_edit_conflict_rules(self) -> None:
        """Tools → Edit Conflict Rules. Opens the rule editor (no dataset
        required — rules are global and persist across datasets)."""
        from ui.conflict_rules_dialog import ConflictRulesDialog
        dlg = ConflictRulesDialog(self)
        dlg.exec()

    def _action_audit_exceptions(self) -> None:
        """Tools → Audit Exceptions. No dataset required — the exception
        list is global and persists across datasets, like conflict rules."""
        from ui.audit_exceptions_dialog import AuditExceptionsDialog
        dlg = AuditExceptionsDialog(self)
        dlg.exec()

    def _action_scan_conflicts(self) -> None:
        """Tools → Scan for Tag Conflicts. Needs an open dataset; guarded
        against a directory scan in progress (same as the audit tool)."""
        if self._state is None:
            QMessageBox.information(
                self, "Scan for Tag Conflicts",
                "Open a dataset first.",
            )
            return
        if self._scan_in_progress():
            QMessageBox.information(
                self, "Scan in progress",
                "A directory scan is running. Please wait for it to "
                "finish before scanning for conflicts.",
            )
            return
        from ui.conflict_scan_dialog import ConflictScanDialog
        dlg = ConflictScanDialog(self._state, self)
        dlg.exec()

    def _action_getting_started(self) -> None:
        """Help → User Guide."""
        from ui.help_browser_dialog import HelpBrowserDialog

        self._guide_dlg = HelpBrowserDialog(self._settings, self)
        self._guide_dlg.show()
        self._guide_dlg.raise_()

    def _action_show_eula(self) -> None:
        """Help → License Agreement. Re-read the accepted agreement,
        read-only (no re-acceptance needed)."""
        from ui.eula_dialog import show_review
        show_review(self)

    def _action_diagnostic_log(self) -> None:
        from ui.diagnostic_log_dialog import DiagnosticLogDialog

        self._diag_log_dlg = DiagnosticLogDialog(self)
        self._diag_log_dlg.show()
        self._diag_log_dlg.raise_()

    def _action_open_log_folder(self) -> None:
        """Straight to the folder, for when the file wants opening in
        a real editor rather than read in a window."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from core import crashlog

        folder = crashlog.get_log_path().parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _action_about(self) -> None:
        QMessageBox.about(
            self,
            "About TagWalker",
            "<b>TagWalker</b><br><br>"
            "Stable Diffusion dataset tag auditor.<br>"
            "Walks every image for each tag, lets you confirm or remove.<br><br>"
            "Built with PySide6.",
        )

    # ------------------------------------------------------------------
    # Window geometry persistence
    # ------------------------------------------------------------------

    def _restore_window_state(self) -> None:
        """Apply saved window geometry and splitter positions.

        If no saved geometry, position the window sensibly on the
        primary screen. Sanity check: if the restored position is
        completely off-screen (e.g. dragged onto a now-disconnected
        monitor), fall back to centering.
        """
        geom = self._settings.window_geometry
        state = self._settings.window_state
        if not geom.isEmpty():
            self.restoreGeometry(geom)
            # Off-screen / oversized sanity checks. A geometry saved on
            # a large monitor can be wider or taller than the screen the
            # app is later opened on (e.g. a laptop), which leaves
            # controls off the edge. Clamp to the available area.
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                wgeom = self.frameGeometry()
                if not avail.intersects(wgeom):
                    # Entirely off-screen (e.g. disconnected monitor).
                    self._action_reset_layout()
                else:
                    # On-screen but possibly too big: shrink to fit and
                    # nudge back into view if it spills past an edge.
                    cur = self.size()
                    new_w = min(cur.width(), avail.width())
                    new_h = min(cur.height(), avail.height())
                    if new_w != cur.width() or new_h != cur.height():
                        self.resize(new_w, new_h)
                    fg = self.frameGeometry()
                    nx, ny = fg.x(), fg.y()
                    if fg.right() > avail.right():
                        nx = avail.right() - fg.width()
                    if fg.bottom() > avail.bottom():
                        ny = avail.bottom() - fg.height()
                    nx = max(nx, avail.left())
                    ny = max(ny, avail.top())
                    if (nx, ny) != (fg.x(), fg.y()):
                        self.move(nx, ny)
        else:
            self._action_reset_layout()
        if not state.isEmpty():
            self.restoreState(state)

    def _save_window_state(self) -> None:
        self._settings.window_geometry = self.saveGeometry()
        self._settings.window_state = self.saveState()
        self._settings.sync()

    def _refresh_window_title(self) -> None:
        parts = ["TagWalker"]
        if self._state is not None:
            # Multi-folder loads (playlists) contribute several roots. The
            # old title used only state.root.name — the FIRST root — so a
            # multi-folder session looked identical to a single-folder one
            # on just that first folder, which is misleading. Show the
            # folder count when more than one root contributed, keeping the
            # primary folder's name as the recognisable anchor.
            roots = getattr(self._state, "roots", None) or [self._state.root]
            if len(roots) > 1:
                # Anchor on the primary folder's name plus the total count,
                # so a multi-folder session is visibly distinct from a
                # single-folder one (the old title showed only the first
                # folder, making them indistinguishable). Kept short for a
                # title bar the OS may truncate.
                parts.append(f"{self._state.root.name} ({len(roots)} folders)")
            else:
                parts.append(self._state.root.name)
        if self._session_path is not None:
            parts.append(self._session_path.stem)
        self.setWindowTitle(" — ".join(parts))

    # ------------------------------------------------------------------
    # Close handling: warn on unsaved progress
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        """Confirm if there are unsaved actions and no path yet."""
        if getattr(self, "_tag_ref_win", None) is not None:
            self._tag_ref_win.close()
        # The image editor is a parentless top-level window (so it
        # survives the main window being minimised); parentless windows
        # do not close with their opener, so close it explicitly here,
        # the same way the tag-reference window is handled above.
        if getattr(self, "_image_editor", None) is not None:
            self._image_editor.close()
        # Compute unsaved-action count.
        unsaved = self._action_counter - self._last_saved_counter

        if self._state is not None and unsaved > 0:
            # Case A: never saved this session.
            if self._session_path is None:
                msg = QMessageBox(self)
                msg.setWindowTitle("Unsaved progress")
                msg.setIcon(QMessageBox.Icon.Warning)
                msg.setText(
                    f"You have {unsaved} unsaved action(s)."
                )
                msg.setInformativeText(
                    "Note: this only affects which tags you've reviewed — "
                    "your actual .txt edits are written immediately and "
                    "are safe.\n\nSave session before closing?"
                )
                save_btn = msg.addButton(
                    "Save As…", QMessageBox.ButtonRole.AcceptRole
                )
                discard_btn = msg.addButton(
                    "Close Anyway", QMessageBox.ButtonRole.DestructiveRole
                )
                cancel_btn = msg.addButton(
                    "Cancel", QMessageBox.ButtonRole.RejectRole
                )
                msg.setDefaultButton(cancel_btn)
                msg.exec()
                clicked = msg.clickedButton()
                if clicked is save_btn:
                    self._action_save_session_as()
                    # If user cancelled the save dialog, _session_path
                    # is still None; treat as still-unsaved and don't close.
                    if self._session_path is None:
                        event.ignore()
                        return
                elif clicked is cancel_btn:
                    event.ignore()
                    return
                # else: Close Anyway — fall through.

            # Case B: previously saved this session, autosave off, dirty.
            elif not self._settings.autosave_enabled:
                msg = QMessageBox(self)
                msg.setWindowTitle("Unsaved progress")
                msg.setIcon(QMessageBox.Icon.Warning)
                msg.setText(
                    f"You have {unsaved} action(s) since the last save."
                )
                msg.setInformativeText(
                    "Note: this only affects which tags you've reviewed — "
                    "your actual .txt edits are written immediately and "
                    "are safe.\n\nSave before closing?"
                )
                save_btn = msg.addButton(
                    "Save", QMessageBox.ButtonRole.AcceptRole
                )
                discard_btn = msg.addButton(
                    "Close Anyway", QMessageBox.ButtonRole.DestructiveRole
                )
                cancel_btn = msg.addButton(
                    "Cancel", QMessageBox.ButtonRole.RejectRole
                )
                msg.setDefaultButton(cancel_btn)
                msg.exec()
                clicked = msg.clickedButton()
                if clicked is save_btn:
                    if not self._do_save():
                        event.ignore()
                        return
                elif clicked is cancel_btn:
                    event.ignore()
                    return

            # Case C: previously saved, autosave ON, but dirty. The
            # periodic timer may not have fired since the last edits, or
            # a previous autosave may have failed. With autosave on the
            # user expects their progress to be saved automatically, so
            # we do a final silent save here rather than dropping it.
            # If the save fails we fall back to an explicit prompt so the
            # user isn't silently losing progress to (say) a full disk.
            else:
                if not self._do_save():
                    msg = QMessageBox(self)
                    msg.setWindowTitle("Autosave failed on close")
                    msg.setIcon(QMessageBox.Icon.Warning)
                    msg.setText(
                        "The session could not be autosaved before closing."
                    )
                    msg.setInformativeText(
                        "Your .txt edits are already on disk and safe, but "
                        "this session's review progress wasn't saved.\n\n"
                        "Close anyway?"
                    )
                    close_btn = msg.addButton(
                        "Close Anyway", QMessageBox.ButtonRole.DestructiveRole
                    )
                    cancel_btn = msg.addButton(
                        "Cancel", QMessageBox.ButtonRole.RejectRole
                    )
                    msg.setDefaultButton(cancel_btn)
                    msg.exec()
                    if msg.clickedButton() is cancel_btn:
                        event.ignore()
                        return

        # Window state and clean shutdown.
        self._save_window_state()
        if self._watcher is not None:
            self._watcher.shutdown()
        # Cancel any in-flight scan.
        if self._scan_worker is not None:
            self._scan_worker.cancel()
        if self._scan_thread is not None:
            self._scan_thread.quit()
            self._scan_thread.wait(1000)
        event.accept()
