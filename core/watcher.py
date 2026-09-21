"""
core/watcher.py

File-system watcher for the currently-displayed image's caption file.

The user can edit caption .txt files outside of TagWalker (Notepad,
VSCode, a sync client, another tool). When they do, the running app's
in-memory tag cache and tag counts would drift from disk reality
unless something notices and reconciles.

This module provides CurrentFileWatcher. It watches exactly one
target at a time — the .txt file (or parent directory, if the image
is currently an orphan) of the currently-displayed image. When the
target changes, the watcher reads the new content and calls into
SessionState.apply_external_change / apply_external_create /
apply_external_delete, which updates in-memory state and counts
without touching the user's review-decision history.

Why only one file at a time
---------------------------
Watching the entire dataset's caption files would cost roughly
N_images file watches. Windows imposes a per-process limit
(historically ~512) on QFileSystemWatcher, so a 10k-image dataset
cannot be fully watched. Even on platforms without a hard limit,
the cost (and noise from save-related multi-event bursts) makes
it impractical. We watch only what the user is looking at right
now; external edits to non-displayed files don't auto-reconcile
in real time but are caught the next time the user navigates to
that image (which re-reads its .txt). If the user wants a forced
global reconciliation they can use the existing "Full Reset"
button to re-scan the dataset.

Self-triggered events
---------------------
When SessionState writes to a .txt via tag_io.write_tags, the
watcher will fire — our own writes look the same as external ones
to the OS. This is handled transparently by SessionState's diff
logic: by the time the queued Qt event runs, the in-memory state
has already been updated synchronously in record_yes / record_no.
apply_external_change compares the new disk content to memory,
sees they match, and does nothing. No suppression flags needed.

Debouncing
----------
Many editors save by deleting the target file and recreating it
under the same name. This fires multiple events per save. A
100 ms single-shot QTimer collapses bursts into one settle action.
Below ~50 ms the file may still be mid-write; above ~200 ms the
user can perceive the lag. 100 ms is a safe middle.

Windows file-replacement quirk
------------------------------
On Windows, os.replace and editor-style atomic saves can briefly
unlink the target before the new file appears. QFileSystemWatcher
may drop the watch during that brief gap. After every event we
re-check the target's existence and re-add the watch if needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from core import tag_io
from core.state import SessionState


# Debounce interval for collapsing rapid-fire file events. Tuned for
# editors that save by delete-then-create (Notepad, VSCode).
DEBOUNCE_MS: int = 100


class CurrentFileWatcher(QObject):
    """Watch the caption file of the currently-displayed image.

    Usage from main_window:

        self.watcher = CurrentFileWatcher(self.state)
        self.watcher.external_change_detected.connect(self._on_external_change)
        # When the displayed image changes:
        self.watcher.set_watched_image(new_image_path)
        # On app shutdown:
        self.watcher.shutdown()

    Modes
    -----
    The watcher has three internal modes:

    - "none"      : nothing being watched (no current image, or path
                    not addressable)
    - "file"      : watching a specific .txt file that exists
    - "directory" : watching the parent directory because the .txt
                    does not currently exist (image is orphan); we
                    will switch to "file" mode if the .txt appears
    """

    # Emitted after we successfully reconcile an external change.
    # The UI can connect this for action-log entries or status bar
    # feedback ("File reloaded from disk").
    external_change_detected = Signal(Path)

    def __init__(
        self,
        state: SessionState,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)

        self._state: SessionState = state

        self._watcher = QFileSystemWatcher(self)
        # fileChanged: a watched file was modified, renamed, or removed.
        # directoryChanged: contents of a watched directory changed
        # (file created, deleted, or renamed within it).
        self._watcher.fileChanged.connect(self._schedule_settle)
        self._watcher.directoryChanged.connect(self._schedule_settle)

        # Debounce timer. setSingleShot means it fires once per
        # start() call and stops; another event during the wait
        # restarts it (extending the settle window).
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._settle)

        # Currently-watched target.
        self._current_image: Optional[Path] = None
        self._current_txt: Optional[Path] = None
        self._mode: str = "none"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_watched_image(self, image_path: Optional[Path]) -> None:
        """Switch to watching the caption file of `image_path`.

        Passing None stops watching anything (useful when the walk
        ends or no image is displayed).

        Idempotent: calling with the same path as currently watched
        is a cheap no-op. Switching paths atomically removes the old
        watch and installs the new one — no transient gap from the
        watcher's perspective.
        """
        if image_path == self._current_image:
            return

        # Tear down any existing watch.
        self._clear_watches()
        self._current_image = image_path
        self._current_txt = None
        self._mode = "none"

        if image_path is None:
            return

        # The .txt path is always derivable from the image path; this
        # mirrors the assumption used by scanner.py and tag_io.py.
        txt_path = image_path.with_suffix(".txt")
        self._current_txt = txt_path

        if txt_path.exists():
            # File mode: watch the .txt directly. Catches content
            # changes and deletions.
            self._watcher.addPath(str(txt_path))
            self._mode = "file"
        else:
            # Directory mode: image is orphan. Watch the parent so
            # we'll notice if the .txt appears. We do NOT also watch
            # the (nonexistent) file path — addPath ignores it silently
            # and we'd never get the "new file" signal anyway.
            parent = txt_path.parent
            if parent.exists():
                self._watcher.addPath(str(parent))
                self._mode = "directory"

    def shutdown(self) -> None:
        """Stop all watches and cancel pending settle.

        Call before the SessionState is destroyed or the app exits.
        Safe to call multiple times.
        """
        self._timer.stop()
        self._clear_watches()
        self._current_image = None
        self._current_txt = None
        self._mode = "none"

    # ------------------------------------------------------------------
    # Internal: event handling
    # ------------------------------------------------------------------

    def _schedule_settle(self, _path: str) -> None:
        """Slot for both fileChanged and directoryChanged. Schedule
        the actual reconciliation work in a debounced manner.

        The _path argument is the path that triggered the event;
        we ignore it because we know what we're watching, and the
        path might be a parent directory anyway (in directory mode).
        """
        # QTimer.start() restarts an already-running timer, which is
        # exactly the debounce behavior we want.
        self._timer.start()

    def _settle(self) -> None:
        """Fires DEBOUNCE_MS after the last filesystem event.

        Reads the current .txt state from disk and notifies the
        SessionState. Whether the state actually changes anything
        is determined by its own diff-against-memory check (which
        also means our own writes are correctly no-op'd here).
        """
        if self._current_image is None or self._current_txt is None:
            return

        img = self._current_image
        txt = self._current_txt

        try:
            file_exists = txt.exists()
        except OSError:
            return  # filesystem in a weird state; bail this tick

        if file_exists:
            # ---- File present ----
            # Either we were already in file mode (content edited)
            # or we were in directory mode and the .txt just appeared.
            try:
                new_tags = tag_io.read_tags(txt)
            except OSError:
                # Race: file existed at exists() but couldn't be read
                # (locked, just deleted, etc.). Bail; another event
                # will fire when state stabilizes.
                return

            previously_orphan = (self._mode == "directory")

            in_memory = self._state.get_image_tags(img)
            had_caption = self._state.has_caption_file(img)

            # Determine if there's anything to do.
            if previously_orphan or not had_caption:
                # The state thinks this image is orphan but a file
                # exists. Apply create.
                self._state.apply_external_create(img, new_tags)
                self.external_change_detected.emit(img)
            elif new_tags != in_memory:
                # Content edit. Note: if new_tags == in_memory this is
                # almost certainly an echo of our own write — no-op.
                self._state.apply_external_change(img, new_tags)
                self.external_change_detected.emit(img)
            # else: no-op (likely self-write echo)

            # Re-arm the file watch. On Windows, atomic saves
            # (delete+create) can drop the watch; re-adding is
            # cheap and idempotent.
            files_being_watched = set(self._watcher.files())
            if str(txt) not in files_being_watched:
                self._watcher.addPath(str(txt))
            # Drop directory-mode watch if we transitioned in.
            if previously_orphan:
                parent_str = str(txt.parent)
                if parent_str in self._watcher.directories():
                    self._watcher.removePath(parent_str)
            self._mode = "file"

        else:
            # ---- File absent ----
            # Either it was just deleted, or we're in directory mode
            # waiting for it to appear (and it still hasn't).
            had_caption = self._state.has_caption_file(img)
            if had_caption:
                # State thought the file existed; reflect deletion.
                self._state.apply_external_delete(img)
                self.external_change_detected.emit(img)

            # Switch to (or remain in) directory mode so we'll see
            # the file if it comes back.
            parent_str = str(txt.parent)
            if parent_str not in self._watcher.directories():
                # Add to dir watch only if the directory itself exists;
                # adding a nonexistent path is silently ignored by
                # QFileSystemWatcher.
                if txt.parent.exists():
                    self._watcher.addPath(parent_str)
            self._mode = "directory"

    # ------------------------------------------------------------------
    # Internal: watch cleanup
    # ------------------------------------------------------------------

    def _clear_watches(self) -> None:
        """Remove every path currently watched.

        QFileSystemWatcher's removePaths takes a list; passing the
        accumulated files() and directories() ensures we don't leak
        watches across image switches.
        """
        files = list(self._watcher.files())
        if files:
            self._watcher.removePaths(files)
        dirs = list(self._watcher.directories())
        if dirs:
            self._watcher.removePaths(dirs)
