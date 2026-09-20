"""
ui/action_log.py

Bottom-right panel: rolling log of recent user actions.

Provides a visual trail of what's been done in this session — useful
context for users walking long tag lists who want a "did I just press
Yes or No on that one?" reference. Capped at 200 entries; oldest are
silently dropped.

Inferring "an action happened" from state notifications
-------------------------------------------------------
SessionState does not currently emit dedicated "user did X" events —
it emits semantic events (image_changed, tag_changed, etc.) that
describe the *state delta* rather than the *user action*. Rather than
extend the state API for the action log alone, we infer the action
from the StateChange kind plus extra context. This is sufficient
for the level of detail an action log needs ("✓ Yes on alice_001 →
1girl"), and keeps the state model uncluttered.

Inference rules (in _on_state_change):
- StateChange("image_changed", path=X)   while a tag is selected
                                        → a Yes/No on (X, current_tag)
- StateChange("tag_changed", tag=T) where status went COMPLETED
                                        → "Completed tag T"
- StateChange("tag_changed", tag=T) where status went SKIPPED
                                        → "Skipped tag T"
- StateChange("tag_selected", tag=T)     → "Selected tag T"
- StateChange("tree_rebuilt")            → "Tree rebuilt (delete or load)"

Whether the change came from a user click vs. an undo / external edit
is not perfectly distinguishable from state alone — but the log is
informational, not authoritative. Calling apply_external_change from
the watcher emits image_changed, which we'll log as "File reloaded
from disk" by checking against a prior cache of last-known tags...
actually simpler: we'll just log "Updated alice_001" without trying
to attribute. Better understated than misleading.

Format per row:
    14:32:18  ✓ Updated alice_001
    14:32:11  ⏳ Selected outdoors
    14:31:55  ✗ Updated bob_002

Newest entries at the top so the user sees recent actions first
without scrolling.

Persistence
-----------
The log lives in memory only. It is intentionally NOT saved to the
session file. Reasoning: the log is a working-context aid, not data
the user would want to read back across sessions. A 200-entry cap
matches the in-memory undo cap.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Fonts, Icons, Spacing
from core.state import Decision, SessionState, StateChange, TagStatus


# Cap on log entries. Matches the in-memory undo limit (100) with
# headroom to avoid the log clipping a user's just-performed action
# before they've seen it.
LOG_MAX_ENTRIES = 200


class ActionLog(QFrame):
    """Scrollable log of recent state changes."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "panel")

        self._state: Optional[SessionState] = None
        # Cache of previously-known tag status, used to detect
        # "completed → not completed" transitions so we only log
        # meaningful changes.
        self._prev_tag_status: dict[str, TagStatus] = {}

        self._build_ui()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach(self, state: Optional[SessionState]) -> None:
        if state is self._state:
            return
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        self._state = state
        # Don't clear the log on detach — the previous session's log
        # entries stay visible until a new state is attached, at which
        # point we reset for the new session.
        if state is not None:
            self._list.clear()
            self._prev_tag_status = {
                t: state.get_tag_status(t) for t in state.all_tags
            }
            state.add_listener(self._on_state_change)
            self._add_entry(
                f"{Icons.FOLDER}  Opened dataset",
                Colors.TEXT_SECONDARY,
            )

    def detach(self) -> None:
        self.attach(None)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL,
        )
        layout.setSpacing(Spacing.TIGHT)

        header = QLabel(f"{Icons.PIN}  Action log")
        header.setProperty("role", "secondary")
        layout.addWidget(header)

        self._list = QListWidget()
        # Read-only: no selection visual, no focus rect.
        self._list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # uniformItemSizes is a Qt perf hint we used in queue_panel;
        # same reasoning applies here for fast scroll over long logs.
        self._list.setUniformItemSizes(True)
        layout.addWidget(self._list, 1)

    # ------------------------------------------------------------------
    # State change listener — infer action descriptions
    # ------------------------------------------------------------------

    def _on_state_change(self, change: StateChange) -> None:
        kind = change.kind
        if self._state is None:
            return

        # ---- Dedicated action-log event ----
        # Operations whose semantics can't be inferred from semantic
        # state events (rename/delete globally, batch ops, per-image
        # tag edits) emit a dedicated action_logged event carrying a
        # ready-made human-readable description in `extra`. We just
        # display it verbatim, color-coded by content.
        if kind == "action_logged":
            text = change.extra or "(action)"
            # Color is a rough cue: undos get muted, deletes get danger
            # red, batches get accent blue, everything else default.
            lower = text.lower()
            if lower.startswith("reloaded") and "from disk" in lower:
                # External reconciliation (a caption changed outside the
                # app). Distinct from user actions: amber, so a mass
                # external change stands out in the log rather than being
                # mistaken for the user's own edits.
                color = Colors.WARNING_AMBER
                icon = "\u27f3"
            elif lower.startswith("undone"):
                color = Colors.TEXT_SECONDARY
                icon = "↶"
            elif "delete" in lower or "remove" in lower:
                color = Colors.DANGER_RED
                icon = Icons.CROSS
            elif "batch" in lower or "auto-confirmed" in lower:
                color = Colors.ACCENT_BLUE
                icon = "⚡"
            elif "rename" in lower or "add" in lower:
                color = Colors.SUCCESS_GREEN
                icon = "✎"
            else:
                color = Colors.TEXT_PRIMARY
                icon = "·"
            self._add_entry(f"{icon}  {text}", color)
            return

        if kind == "image_changed":
            # Skip granular edits: per-image add/remove/rename of tags
            # emit image_changed alongside their own action_logged event,
            # and we don't want to double-log them (the duplicate would
            # interpret the edit as a yes/no on the unrelated current
            # walking tag, which is misleading).
            if change.extra == "granular_edit":
                return
            # Skip undo-originated changes: an undo emits its own
            # "Undone: ..." action_logged entry. Inferring a decision
            # from the RESULTING tag state here would mislabel the undo
            # (restoring "tag present" would read as a fresh YES).
            if change.extra == "undo":
                return
            # Skip skip-originated changes: a skip changes no tags and
            # emits its own "Skipped ..." action_logged entry. State
            # inference here would mislabel it as a Yes/No.
            if change.extra == "skip":
                return
            # Skip external-originated changes: the watcher's
            # apply_external_change emits its own "⟳ Reloaded ..."
            # action_logged entry naming exactly what changed on disk.
            # Inferring a decision from the reconciled tag state here
            # would both double-log and mislabel it as a user Yes/No.
            if change.extra == "external":
                return
            # Only log when we have enough context for a useful entry.
            # If no tag is currently selected, the per-image tag-edit
            # operations (B4: add/remove/rename on image) handle their
            # own logging via action_logged. So we skip the noisy
            # "✎ name ← ?" fallback that used to fire here.
            # FIELD BUG: this used the tag CURRENTLY selected, which
            # is wrong for an undo. Walk 1girl, switch to 1boy, press
            # undo: the right decision is reverted, but the log
            # credited it to 1boy because that is what was selected by
            # then. The reverted entry knows which tag it belonged to,
            # so ask it first and only fall back to the selection.
            reverted = self._state.last_undo_context or {}
            tag = reverted.get("tag") or self._state.current_tag
            if change.image_path is not None and tag is not None:
                name = change.image_path.stem
                in_file = self._state.is_tag_in_image(
                    change.image_path, tag)
                icon = Icons.CHECK if in_file else Icons.CROSS
                color = (
                    Colors.SUCCESS_GREEN if in_file
                    else Colors.DANGER_RED
                )
                self._add_entry(f"{icon}  {name} ← {tag}", color)

        elif kind == "tag_changed" and change.tag:
            # Only log completion / skip transitions, not every recount.
            new_status = self._state.get_tag_status(change.tag)
            prev_status = self._prev_tag_status.get(change.tag, TagStatus.PENDING)
            if new_status != prev_status:
                if new_status == TagStatus.COMPLETED:
                    self._add_entry(
                        f"{Icons.CHECK}  Tag completed: {change.tag}",
                        Colors.SUCCESS_GREEN,
                    )
                elif new_status == TagStatus.SKIPPED:
                    self._add_entry(
                        f"{Icons.SKIP}  Tag skipped: {change.tag}",
                        Colors.WARNING_AMBER,
                    )
                elif (prev_status == TagStatus.COMPLETED
                      and new_status == TagStatus.PENDING):
                    # Undone completion. Useful to log so the user
                    # knows what happened.
                    self._add_entry(
                        f"{Icons.HOURGLASS}  Tag back to pending: {change.tag}",
                        Colors.TEXT_SECONDARY,
                    )
                self._prev_tag_status[change.tag] = new_status

        elif kind == "tag_selected" and change.tag:
            self._add_entry(
                f"{Icons.TAG}  Selected tag: {change.tag}",
                Colors.ACCENT_BLUE,
            )

        elif kind == "walk_ended" and change.extra == "end_of_tree":
            self._add_entry(
                f"{Icons.DONE}  Tree walk complete",
                Colors.SUCCESS_GREEN,
            )

        elif kind == "tree_rebuilt":
            # Refresh status cache so the next tag_changed comparison
            # uses up-to-date baselines.
            self._prev_tag_status = {
                t: self._state.get_tag_status(t)
                for t in self._state.all_tags
            }

    # ------------------------------------------------------------------
    # Log entry insertion + bound enforcement
    # ------------------------------------------------------------------

    def _add_entry(self, text: str, color: str) -> None:
        """Prepend a log entry with current timestamp.

        Newest entries appear at the top. When the entry count exceeds
        LOG_MAX_ENTRIES, the oldest (bottom) entry is dropped — bounded
        memory regardless of session length.
        """
        timestamp = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"{timestamp}  {text}")
        item.setForeground(QBrush(QColor(color)))
        font = QFont()
        font.setPointSize(Fonts.SIZE_SMALL)
        item.setFont(font)
        # insertItem(0, ...) places at top.
        self._list.insertItem(0, item)
        # Evict oldest if over cap. takeItem returns and removes the
        # item at the given index; we discard it (becomes garbage).
        while self._list.count() > LOG_MAX_ENTRIES:
            self._list.takeItem(self._list.count() - 1)
