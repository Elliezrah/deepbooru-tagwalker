"""
ui/file_state_panel.py

Bottom-left panel: displays the current image's caption file contents.

Shows what's on disk for the image currently in focus, with light-touch
editing of individual tags so the user can fix bad tags without leaving
the walk flow.

Display content:
  📄 File state · image_name.txt
  [tag1] [tag2] [tag3]  [+ Add]    <- each tag is clickable
  (or "(empty)" if the .txt has no tags)
  (or "No caption file" if the image is an orphan, with [+ Add] enabled)
  Often appears with: tag_a (count), tag_b (count)

Clicking a tag opens a small menu: Remove this tag, Rename….
Clicking [+ Add] opens an input dialog to enter a new tag.

All edits go through SessionState.add_tag_to_image /
remove_tag_from_image / rename_tag_on_image. These are atomic disk
writes with undo entries, so the user can Back out of any edit. Empty
or comma-containing inputs are rejected at the state layer.

Update strategy
---------------
- walk_advanced  : new current image → refresh display
- tag_selected   : tag changed, no current image change → still
                   refresh in case the queue picked a new image
- image_changed  : the displayed image's caption changed (Yes/No,
                   external edit, granular edit). Refresh.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Icons, Spacing, repolish
from core.state import SessionState, StateChange


class FileStatePanel(QFrame):
    """Shows the current image's caption file content with granular edits."""

    # Right-click a tag → Tag Reference (main window owns the
    # reference window).
    reference_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "panel")

        self._state: Optional[SessionState] = None
        # File-state tag search (#13): substring filter on displayed tags.
        self._tag_filter: str = ""
        # Set of co-occurrence hint tags present on the current image,
        # populated by _refresh_hints; used to highlight those tags in
        # the image's own tag list (#18 part c).
        self._present_cooccur_tags: set = set()
        # Group-summary mode (#2a): when a group header is selected in the
        # queue's group view, this panel shows a read-only summary of the
        # group (member count + decision tally) instead of one image's
        # editable tags. None when inactive; otherwise a dict with keys
        # base / members / tag.
        self._group_summary: Optional[dict] = None

        self._build_ui()
        self._refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach(self, state: Optional[SessionState]) -> None:
        if state is self._state:
            return
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        self._state = state
        if state is not None:
            state.add_listener(self._on_state_change)
        self._refresh()

    def detach(self) -> None:
        self.attach(None)

    def _refresh_token_badge(self, img) -> None:
        """Count this caption and colour it against the limits.

        The red "over limit" line is the user's configured token limit
        (default 225). A caption AT the limit still trains cleanly, so
        only STRICTLY over turns red — matching the "over token limit"
        filter. The amber warning tier scales as a fraction of the limit
        (~0.67, preserving the historical 150-of-225 ratio) so the early
        warning stays useful at any limit value.
        """
        from config.theme import Colors

        tags = []
        if self._state is not None:
            tags = self._state.get_image_tags(img.image_path) or []
        if not tags:
            self.label_tokens.setText("")
            return
        # Prefer the state's cached count (invalidated on edit) so the
        # badge and the filter agree and neither recounts unnecessarily.
        try:
            if self._state is not None:
                n = int(self._state.token_count_for(img.image_path))
            else:
                from core import clip_token_counter as ctc
                n = int(ctc.count_tokens(", ".join(tags)))
        except Exception:
            # Vocabulary missing from this build. Silence beats a
            # number that might be wrong.
            self.label_tokens.setText("")
            return

        limit = 225
        if self._state is not None:
            limit = int(getattr(self._state, "_token_limit", 225))
        amber_at = int(limit * 0.67)   # scales with the limit
        if n > limit:
            colour, note = Colors.DANGER_RED, " over limit"
        elif n > amber_at:
            colour, note = Colors.WARNING_AMBER, ""
        else:
            colour, note = Colors.TEXT_SECONDARY, ""
        self.label_tokens.setText(
            f"<span style='color:{colour};'>{n} tok{note}</span>")

    def show_group_summary(self, base: str, members: list, tag) -> None:
        """Switch to a read-only group summary for a selected group
        header (#2a). `members` is the list of ImageEntry in the group;
        `tag` is the tag currently being walked (for the decision tally),
        may be None. Editing controls are hidden/locked because there's no
        single file to edit here — the group is the unit. Selecting an
        individual image row (clear_group_summary) returns to the normal
        editable per-image view."""
        self._group_summary = {
            "base": base, "members": list(members), "tag": tag,
        }
        self._refresh()

    def clear_group_summary(self) -> None:
        """Leave group-summary mode and return to the per-image view."""
        if self._group_summary is not None:
            self._group_summary = None
            self._refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL,
        )
        layout.setSpacing(Spacing.TIGHT)

        # Header row: icon + "File state" + filename.
        header_row = QHBoxLayout()
        header_row.setSpacing(Spacing.TIGHT)
        self.label_header = QLabel(f"{Icons.NO_FILE}  File state")
        self.label_header.setProperty("role", "secondary")
        header_row.addWidget(self.label_header)
        self.label_filename = QLabel("")
        self.label_filename.setProperty("role", "mono")
        header_row.addWidget(self.label_filename, 1)

        # Token count for THIS caption, here rather than in a separate
        # tool. While pruning, the number you are working against is
        # needed continuously, and opening the Token Counter for every
        # image is not that.
        #
        # Coloured against the limits that bite, so the judgement can
        # be made without reading the number.
        self.label_tokens = QLabel("")
        self.label_tokens.setProperty("role", "mono")
        self.label_tokens.setToolTip(
            "CLIP tokens in this caption, counted with the same "
            "tokeniser your trainer uses.\n\n"
            "Trainers encode captions in 75-token chunks. Once a caption "
            "passes your token limit (set in Preferences \u2014 the red "
            "\u201cover limit\u201d line, default 225 = three full chunks) "
            "most trainers cut the tail \u2014 and with caption shuffling "
            "on, a DIFFERENT tail is cut every step, which is how "
            "training quality degrades for no visible reason. A caption "
            "AT the limit still trains cleanly; only strictly over is a "
            "problem. The amber tint is an early warning below the limit.")
        header_row.addWidget(self.label_tokens)
        layout.addLayout(header_row)

        # Tag search box (#13): filter the displayed tags by substring.
        # When a caption file has many tags, this makes finding a
        # specific one fast. Works like the other search boxes; empty =
        # show all tags. The filter only affects what's DISPLAYED, never
        # the underlying file.
        self.tag_search = QLineEdit()
        self.tag_search.setPlaceholderText("Search this image's tags…")
        self.tag_search.setClearButtonEnabled(True)
        self.tag_search.textChanged.connect(self._on_tag_search_changed)
        layout.addWidget(self.tag_search)

        # Tags display + add button. The label uses rich text where each
        # tag is a clickable hyperlink (href encodes the tag name). The
        # linkActivated signal carries the tag back to us; we then open
        # a small per-tag menu (Remove, Rename). Why this approach: a
        # rich-text QLabel handles wrap automatically and stays cheap
        # at any tag count, vs. building a custom FlowLayout of buttons
        # that would need careful re-layout on every refresh.
        content_row = QHBoxLayout()
        content_row.setSpacing(Spacing.TIGHT)
        self.label_content = QLabel("(no image)")
        self.label_content.setProperty("role", "mono")
        self.label_content.setWordWrap(True)
        # Only LinksAccessibleByMouse — no TextSelectable. Selectable
        # text would attach Qt's default Copy / Select-all context menu,
        # which we don't want here (the user didn't ask for it and it's
        # half-broken on rich-text labels with embedded HTML).
        self.label_content.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        # Suppress Qt's default link context menu. With link-interactive
        # text, right-click on a tag normally pops up a browser-style
        # "Copy Link Location" menu — but our "links" are just internal
        # tag references, not real URLs, so copying the href ("tag:foo")
        # produces useless text. Left-click already opens the
        # Remove/Rename menu via linkActivated below; right-click just
        # gets out of the way.
        self.label_content.setContextMenuPolicy(
            Qt.ContextMenuPolicy.NoContextMenu
        )
        self.label_content.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.label_content.linkActivated.connect(self._on_tag_link_activated)

        # Wrap the tags label in a scroll area (#17) so a long caption
        # no longer forces the user to resize the panel to see every
        # tag. The label still word-wraps; the scroll area shows a normal
        # vertical scrollbar when the content exceeds the visible height.
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.content_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.content_scroll.setWidget(self.label_content)
        content_row.addWidget(self.content_scroll, 1)

        # "+" button to add a new tag to this image. It's a TOGGLE:
        # clicking reveals the inline add box below (hidden by default so
        # it doesn't take up space during a normal walk), and clicking
        # again hides it. Sits at the top alongside the tags so it's
        # always reachable.
        btn_col = QVBoxLayout()
        btn_col.setSpacing(Spacing.TIGHT)
        self.btn_add = QPushButton(f"{Icons.PLUS} Add")
        self.btn_add.setProperty("role", "secondary")
        self.btn_add.setCheckable(True)
        self.btn_add.setToolTip(
            "Show/hide the add-tag box for this image.\n"
            "Atomic write, undoable."
        )
        self.btn_add.toggled.connect(self._on_add_toggled)
        btn_col.addWidget(self.btn_add)
        btn_col.addStretch(1)
        content_row.addLayout(btn_col, 0)
        layout.addLayout(content_row, 1)

        # Inline add-tag box with autocomplete, hidden by default and
        # revealed by the Add toggle. Typing suggests canonical Danbooru
        # tags (popularity-ranked); Enter commits. The database loads
        # lazily in the background on first focus, so there's no startup
        # cost and no typing freeze. Wrapped in a container so the whole
        # row (box + any padding) shows/hides as one unit.
        self.add_container = QWidget()
        add_row = QHBoxLayout(self.add_container)
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(Spacing.TIGHT)
        self.add_box = QLineEdit()
        self.add_box.setPlaceholderText("Add a tag…  (autocompletes)")
        self.add_box.setClearButtonEnabled(True)
        self.add_box.returnPressed.connect(self._on_add_box_submit)
        # Permanently connected; clears rejected-add styling on the next
        # keystroke. Idempotent and near-free when no error is showing.
        self._add_box_error_active = False
        self.add_box.textEdited.connect(self._clear_add_box_error)
        add_row.addWidget(self.add_box, 1)
        self.add_container.setVisible(False)  # hidden by default
        layout.addWidget(self.add_container)
        # Attach autocomplete to the inline box.
        from ui.tag_autocomplete import TagAutocomplete
        self._add_autocomplete = TagAutocomplete(self.add_box)

        # Co-occurrence hints: tags that frequently appear alongside the
        # CURRENT WALKING TAG. Each hint is colored by whether the
        # current image has it — green = present, red = missing (#18) —
        # so the user can glance at the file state and immediately see if
        # a high-co-occurrence tag is missing from this image.
        hints_header_row = QHBoxLayout()
        hints_header_row.setContentsMargins(0, 0, 0, 0)
        hints_header_row.setSpacing(Spacing.TIGHT)
        self.label_hints_header = QLabel("Often appears with:")
        self.label_hints_header.setProperty("role", "tertiary")
        hints_header_row.addWidget(self.label_hints_header, 1)
        # Live "too common %" threshold (#18) — moved here from
        # Preferences for quick in-flow experimentation. Tags appearing
        # on more than this percent of all images are dropped from the
        # hints (they're too ubiquitous to be useful signal).
        thr_label = QLabel("hide >")
        thr_label.setProperty("role", "tertiary")
        thr_label.setToolTip(
            "Hide co-occurrence hints for tags that appear on more than "
            "this percentage of all images (too common to be useful)."
        )
        hints_header_row.addWidget(thr_label)
        self.spin_cooccur = QSpinBox()
        self.spin_cooccur.setRange(1, 100)
        self.spin_cooccur.setSuffix("%")
        self.spin_cooccur.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.spin_cooccur.setToolTip(
            "Hide co-occurrence hints for tags appearing on more than "
            "this percent of all images."
        )
        self.spin_cooccur.valueChanged.connect(self._on_cooccur_threshold_changed)
        hints_header_row.addWidget(self.spin_cooccur)
        layout.addLayout(hints_header_row)

        self.label_hints = QLabel("")
        self.label_hints.setProperty("role", "mono")
        self.label_hints.setWordWrap(True)
        self.label_hints.setTextFormat(Qt.TextFormat.RichText)
        # No TextInteractionFlags — purely informational, no context
        # menu, no selection. (Was TextSelectableByMouse before but
        # that attached an unwanted Copy / Select-all menu.)
        self.label_hints.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        layout.addWidget(self.label_hints)
        # Small legend so the green/red coloring is self-explanatory.
        self.label_hints_legend = QLabel(
            f'<span style="color:{Colors.SUCCESS_GREEN};">\u25cf present'
            f'</span>&nbsp;&nbsp;'
            f'<span style="color:{Colors.DANGER_RED};">\u25cf missing</span>'
        )
        self.label_hints_legend.setTextFormat(Qt.TextFormat.RichText)
        self.label_hints_legend.setProperty("role", "tertiary")
        layout.addWidget(self.label_hints_legend)
        self.label_hints_header.setVisible(False)
        self.label_hints.setVisible(False)
        self.label_hints_legend.setVisible(False)
        self.spin_cooccur.setVisible(False)
        thr_label.setVisible(False)
        self._cooccur_thr_label = thr_label

    # ------------------------------------------------------------------
    # State change listener
    # ------------------------------------------------------------------

    def _on_state_change(self, change: StateChange) -> None:
        kind = change.kind
        # filter_changed is included because switching the queue filter
        # (Has tag / Missing tag / orphans / sort) repositions the walk
        # to a new current image via _seek_first_pending — the file-state
        # panel must follow that, otherwise it keeps showing the
        # PREVIOUS image's tags after a filter switch (the reported bug).
        if kind in ("walk_advanced", "tag_selected", "walk_ended",
                    "filter_changed"):
            self._refresh()
        elif kind == "image_changed":
            # Only refresh if the changed image is the one we display.
            if (self._state is not None
                    and self._state.current_image is not None
                    and change.image_path is not None
                    and change.image_path == self._state.current_image.image_path):
                self._refresh()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Update header and content from current state."""
        # Group-summary mode takes precedence: show the read-only group
        # view instead of any single image's tags.
        if self._group_summary is not None:
            self._render_group_summary()
            return
        if self._state is None or self._state.current_image is None:
            self.label_header.setText(f"{Icons.NO_FILE}  File state")
            self.label_filename.setText("")
            self.label_content.setText("(no image)")
            self.label_content.setProperty("role", "mono")
            repolish(self.label_content)
            self.btn_add.setEnabled(False)
            self._hide_hints()
            return

        img = self._state.current_image
        self.label_filename.setText(f"· {img.txt_path.name}")
        # Add button is always available when there's a current image
        # (works for orphans too — it creates the .txt file).
        self.btn_add.setEnabled(True)

        if not self._state.has_caption_file(img.image_path):
            self.label_header.setText(f"{Icons.NO_FILE}  File state")
            self.label_content.setText("No caption file")
            self.label_content.setProperty("role", "danger")
            repolish(self.label_content)
            # Still show hints for the current walking tag — they're
            # informational about the tag, not the image content.
            self._refresh_hints()
            return

        tags = self._state.get_image_tags(img.image_path)
        self._refresh_token_badge(img)
        # Switch icon to indicate a file IS present.
        self.label_header.setText("📄  File state")
        # Refresh the co-occurrence hints FIRST so _present_cooccur_tags
        # is populated before we render the tag list — the tag list
        # highlights tags that are also high-co-occurrence present (#18).
        self._refresh_hints()
        # Apply the file-state tag search filter (#13): show only tags
        # containing the query (case-insensitive). The filter is purely
        # a display aid; the caption file is untouched.
        q = self._tag_filter.strip().lower()
        shown_tags = [t for t in tags if q in t.lower()] if q else tags
        if not tags:
            self.label_content.setText("(empty)")
            self.label_content.setProperty("role", "tertiary")
        elif not shown_tags:
            self.label_content.setText(
                f"(no tags match \u201c{self._html_escape(self._tag_filter)}\u201d)"
            )
            self.label_content.setProperty("role", "tertiary")
        else:
            # Render each tag as a clickable link. The href is the tag
            # name. Tags that are also high-co-occurrence hints present
            # on this image (#18 part c) get a subtle green highlight
            # (background tint) so the user can spot at a glance which of
            # the image's tags are the "expected companions" of the tag
            # being walked.
            from urllib.parse import quote
            link_color = Colors.ACCENT_BLUE
            parts = []
            for t in shown_tags:
                if t in self._present_cooccur_tags:
                    # Highlight: green text + faint background tint.
                    style = (
                        f'color:{Colors.SUCCESS_GREEN};'
                        f' background-color:{Colors.SUCCESS_GREEN}22;'
                        f' text-decoration:none; border-radius:2px;'
                    )
                else:
                    style = f'color:{link_color}; text-decoration:none;'
                parts.append(
                    f'<a href="tag:{quote(t)}" style="{style}">'
                    f'{self._html_escape(t)}</a>'
                )
            self.label_content.setText(", ".join(parts))
            self.label_content.setProperty("role", "mono")
        repolish(self.label_content)

    def _render_group_summary(self) -> None:
        """Render the read-only group summary (#2a): header, member count,
        and a decision tally for the walked tag. Editing controls are
        hidden because the group, not a single file, is the unit here."""
        from core.state import Decision
        gs = self._group_summary
        base = gs["base"]
        members = gs["members"]
        tag = gs["tag"]
        n = len(members)

        # Lock editing: collapse + disable the Add control and hide hints.
        if self.btn_add.isChecked():
            self.btn_add.setChecked(False)
        self.btn_add.setEnabled(False)
        self.add_container.setVisible(False)
        self._hide_hints()

        # Header names the group; filename slot shows the member count.
        self.label_header.setText("\U0001F5C2  Group")  # card-index glyph
        self.label_filename.setText(
            f"\u00b7 {n} image{'s' if n != 1 else ''}"
        )

        # Decision tally for the current tag across the group's members.
        if tag is None or self._state is None:
            self.label_content.setText(
                f"\u201c{self._html_escape(base)}\u201d \u2014 {n} "
                f"image{'s' if n != 1 else ''} in this group.\n\n"
                f"Select an image row to view and edit its tags."
            )
            self.label_content.setProperty("role", "tertiary")
            repolish(self.label_content)
            return

        yes = no = pend = 0
        for e in members:
            d = self._state.get_decision(e.image_path, tag)
            if d == Decision.YES:
                yes += 1
            elif d == Decision.NO:
                no += 1
            else:
                pend += 1
        # A compact, colored tally line. Read-only.
        line = (
            f'<div style="color:{Colors.TEXT_SECONDARY};">'
            f'Group <b>{self._html_escape(base)}</b> '
            f'&mdash; tag <b>{self._html_escape(tag)}</b></div>'
            f'<div style="margin-top:6px;">'
            f'<span style="color:{Colors.SUCCESS_GREEN};">\u25cf {yes} yes'
            f'</span>&nbsp;&nbsp;'
            f'<span style="color:{Colors.DANGER_RED};">\u25cf {no} no'
            f'</span>&nbsp;&nbsp;'
            f'<span style="color:{Colors.TEXT_TERTIARY};">\u25cf {pend} '
            f'pending</span></div>'
            f'<div style="margin-top:8px; color:{Colors.TEXT_TERTIARY};">'
            f'Decide the whole group with Yes / No, or select an image '
            f'row to edit its tags individually.</div>'
        )
        self.label_content.setTextFormat(Qt.TextFormat.RichText)
        self.label_content.setText(line)
        self.label_content.setProperty("role", "mono")
        repolish(self.label_content)

    @staticmethod
    def _html_escape(text: str) -> str:
        """Escape characters that could disrupt the rich-text rendering.
        Tag names should never contain these in practice, but defensive
        escaping keeps display correct if they do.
        """
        return (
            text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
        )

    # ------------------------------------------------------------------
    # Interaction handlers (B4)
    # ------------------------------------------------------------------

    def _on_tag_search_changed(self, text: str) -> None:
        """File-state tag search (#13): re-render the tag list filtered
        to tags containing the query. Display-only; never edits the file.
        """
        self._tag_filter = text
        self._refresh()

    def _on_tag_link_activated(self, link: str) -> None:
        """User clicked a tag link. Open a per-tag menu (Remove, Rename)."""
        if self._state is None or self._state.current_image is None:
            return
        if not link.startswith("tag:"):
            return
        from urllib.parse import unquote
        tag = unquote(link[4:])
        image_path = self._state.current_image.image_path

        menu = QMenu(self)
        ref_action = QAction(f"Tag Reference for '{tag}'…", self)
        ref_action.triggered.connect(
            lambda: self.reference_requested.emit(tag))
        menu.addAction(ref_action)
        menu.addSeparator()
        remove_action = QAction(f"Remove tag '{tag}'", self)
        remove_action.triggered.connect(
            lambda: self._do_remove_tag(image_path, tag)
        )
        menu.addAction(remove_action)
        rename_action = QAction(f"Rename '{tag}'...", self)
        rename_action.triggered.connect(
            lambda: self._do_rename_tag(image_path, tag)
        )
        menu.addAction(rename_action)
        # Position at the cursor — this is a link-click menu, so the
        # global cursor pos is the most natural anchor.
        from PySide6.QtGui import QCursor
        menu.exec(QCursor.pos())

    def _on_add_toggled(self, checked: bool) -> None:
        """The [+ Add] toggle shows/hides the inline add box. When shown,
        focus it so the user can type immediately; when hidden, clear any
        half-typed text so it doesn't linger on the next reveal."""
        self.add_container.setVisible(checked)
        if checked:
            self.add_box.setFocus()
        else:
            self.add_box.clear()

    def _on_add_box_submit(self) -> None:
        """Commit the tag typed in the inline add box to the current
        image. Atomic, undoable. Clears the box on success.

        Feedback is INLINE and NON-BLOCKING: a rejected add (duplicate,
        comma, empty) flashes the box red with a short hint rather than
        popping a modal dialog, so fast tagging never gets interrupted by
        a popup the user has to dismiss. Only a genuine disk error (rare)
        still warrants a blocking warning.
        """
        if self._state is None or self._state.current_image is None:
            return
        tag = self.add_box.text().strip()
        if not tag:
            return
        # Coerce the typed tag to the chosen entry format so it's stored
        # consistently with the rest of the dataset. In underscores mode a
        # typed "long hair" becomes "long_hair" (previously it was stored
        # verbatim as a malformed space-containing tag). In spaces mode it
        # stays "long hair". Commas remain the real separator and are
        # rejected downstream; emoticons are left untouched.
        from config.settings import Settings
        from core import tag_database as _tdb
        tag = _tdb.normalize_typed_tag(tag, Settings().tags_use_spaces)
        image_path = self._state.current_image.image_path
        try:
            added = self._state.add_tag_to_image(image_path, tag)
        except OSError as e:
            # A real disk failure is worth a blocking warning.
            QMessageBox.warning(
                self, "Could not add tag",
                f"Disk write failed: {e}",
            )
            return
        if not added:
            # Non-blocking inline feedback. Distinguish "already present"
            # (the common case the user hit) from a genuinely invalid tag,
            # so the message is actually useful — and show it as a small
            # visible notice, not just a hover tooltip.
            if self._state.is_tag_in_image(image_path, tag):
                msg = f"'{tag}' is already on this image"
            else:
                msg = "Invalid tag (commas aren't allowed)"
            self._flash_add_box_error(msg)
            self._show_add_notice(msg)
            return
        # Success — clear for the next entry, keep focus for fast adds.
        self._clear_add_box_error()
        self.add_box.clear()
        self.add_box.setFocus()

    def _show_add_notice(self, message: str) -> None:
        """Show a small, auto-hiding notice just beneath the add box.

        Used to tell the user, visibly but non-intrusively, why an add was
        rejected (e.g. the tag is already on the image). It's a transient
        floating label — no modal to dismiss — so fast tagging is never
        interrupted. Created lazily and reused; hides itself after a moment.
        """
        notice = getattr(self, "_add_notice", None)
        if notice is None:
            notice = QLabel(self)
            notice.setWindowFlags(
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            notice.setAttribute(
                Qt.WidgetAttribute.WA_ShowWithoutActivating, True
            )
            notice.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            notice.setStyleSheet(
                f"QLabel {{ background-color: {Colors.WARNING_BG};"
                f" color: {Colors.WARNING_AMBER};"
                f" border: 1px solid {Colors.WARNING_AMBER};"
                f" border-radius: 4px; padding: 4px 8px; }}"
            )
            self._add_notice = notice
            self._add_notice_timer = QTimer(self)
            self._add_notice_timer.setSingleShot(True)
            self._add_notice_timer.timeout.connect(notice.hide)
        notice.setText(message)
        notice.adjustSize()
        # Position just below the add box (global coords for the popup).
        below = self.add_box.mapToGlobal(self.add_box.rect().bottomLeft())
        notice.move(below.x(), below.y() + 2)
        notice.show()
        notice.raise_()
        self._add_notice_timer.start(2600)

    def _flash_add_box_error(self, message: str) -> None:
        """Briefly mark the add box as rejected: red border + placeholder
        hint. Cleared when the user edits the box again. Non-blocking."""
        self.add_box.setStyleSheet(
            f"QLineEdit {{ border: 1px solid {Colors.DANGER_RED}; }}"
        )
        # Select the text so the next keystroke replaces it, and show the
        # reason as a tooltip-style placeholder by temporarily swapping.
        self.add_box.selectAll()
        self.add_box.setToolTip(message)
        # _clear_add_box_error is connected once at construction and is
        # idempotent, so no connect/disconnect churn is needed here.
        # (The old disconnect-then-reconnect dance made newer PySide6
        # print "Failed to disconnect" RuntimeWarnings on every flash.)
        self._add_box_error_active = True

    def _clear_add_box_error(self, *args) -> None:
        """Remove the rejected-add styling. Connected permanently to
        textEdited and cheap when no error is showing, so it can fire on
        every keystroke without any disconnect bookkeeping."""
        if not getattr(self, "_add_box_error_active", False):
            return
        self._add_box_error_active = False
        self.add_box.setStyleSheet("")
        self.add_box.setToolTip("")

    def _do_remove_tag(self, image_path, tag: str) -> None:
        """Confirm + remove a tag from the current image."""
        if self._state is None:
            return
        confirm = QMessageBox.question(
            self, "Remove tag",
            f"Remove '{tag}' from {image_path.name}?\n"
            f"(Undoable via Back.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._state.remove_tag_from_image(image_path, tag)
        except OSError as e:
            QMessageBox.warning(self, "Could not remove tag", str(e))

    def _do_rename_tag(self, image_path, old_tag: str) -> None:
        """Prompt + rename a tag on the current image."""
        if self._state is None:
            return
        new_tag, ok = QInputDialog.getText(
            self, "Rename tag",
            f"Rename '{old_tag}' on {image_path.name} to:\n"
            f"(use commas to split into multiple tags, "
            f"e.g. \"1girl, solo\")",
            text=old_tag,
        )
        if not ok:
            return
        new_tag = new_tag.strip()
        if not new_tag or new_tag == old_tag:
            return

        # Split case: the user typed commas to fix a malformed merged
        # tag (e.g. "1girl solo" -> "1girl, solo"). Parse and route to a
        # per-image split. A normal single tag never contains a comma.
        if "," in new_tag:
            parts = [p.strip() for p in new_tag.split(",") if p.strip()]
            if not parts or parts == [old_tag]:
                return
            try:
                ok_split = self._state.split_tag_on_image(
                    image_path, old_tag, parts,
                )
            except OSError as e:
                QMessageBox.warning(self, "Could not split tag", str(e))
                return
            if not ok_split:
                QMessageBox.information(
                    self, "Tag not split",
                    "The split could not be applied. One of the new tags "
                    "may be invalid, or the tag is no longer on this image.",
                )
            return

        try:
            renamed = self._state.rename_tag_on_image(
                image_path, old_tag, new_tag,
            )
        except OSError as e:
            QMessageBox.warning(self, "Could not rename tag", str(e))
            return
        if not renamed:
            QMessageBox.information(
                self, "Tag not renamed",
                "The new name was empty or was the same as the old name.",
            )

    def _refresh_hints(self) -> None:
        """Populate the co-occurrence hints, colored by presence (#18).

        Hints are anchored on the CURRENT WALKING TAG. Each hinted tag
        is colored:
          - GREEN if the current image already has it (good — the
            related tag is present),
          - RED if the current image is missing it (a prompt: should
            this image have it too?).

        This lets the user glance at the file state and instantly see
        whether the tags that usually accompany the tag they're walking
        are present on this particular image. The set of present hint
        tags is also stored (self._present_cooccur_tags) so the image's
        own tag list can highlight those same tags (#18 part c).

        Hidden if there's no current tag or the helper returns nothing.
        """
        self._present_cooccur_tags = set()
        if (self._state is None
                or not self._state.show_cooccur_hints
                or self._state.current_tag is None):
            self._hide_hints()
            return
        cur_tag = self._state.current_tag

        # Pick the engine. "danbooru" (default) = bundled official
        # co-occurrence lookup, Ochiai-ranked; "dataset" = co-occurrence
        # from the user's own images. Display value differs: the official
        # engine shows P(partner|tag) as a percentage ("of images with
        # this tag, how many also have that one"); the dataset engine
        # shows the raw co-occurrence count, as before.
        source = self._state.cooccur_source
        if source == "danbooru":
            from core import cooccurrence as cooc
            raw = cooc.get_db().get_cooccurring(cur_tag, limit=8)
            hints = [(p, f"{int(round(pc * 100))}%") for (p, _o, pc) in raw]
            source_label = "Danbooru"
            show_threshold = False
        else:
            ds = self._state.get_related_tags_for_tag(cur_tag, limit=8)
            hints = [(t, str(c)) for (t, c) in ds]
            source_label = "your dataset"
            show_threshold = True

        if not hints:
            self._hide_hints()
            return
        cur_img = self._state.current_image
        image_tags = set(
            self._state.get_image_tags(cur_img.image_path)
            if cur_img is not None else []
        )
        self.label_hints_header.setText(
            f"Often appears with '{cur_tag}' ({source_label}):"
        )
        parts = []
        for tag, value in hints:
            present = tag in image_tags
            if present:
                self._present_cooccur_tags.add(tag)
            color = Colors.SUCCESS_GREEN if present else Colors.DANGER_RED
            parts.append(
                f'<span style="color:{color};">'
                f'{self._html_escape(tag)} ({value})</span>'
            )
        self.label_hints.setText(", ".join(parts))
        self.label_hints_header.setVisible(True)
        self.label_hints.setVisible(True)
        self.label_hints_legend.setVisible(True)
        # The "too common %" cutoff only applies to the dataset engine;
        # the Danbooru engine is Ochiai-ranked and needs no such control.
        self.spin_cooccur.setVisible(show_threshold)
        self._cooccur_thr_label.setVisible(show_threshold)
        if show_threshold:
            self.spin_cooccur.blockSignals(True)
            self.spin_cooccur.setValue(self._state.cooccur_too_common_pct)
            self.spin_cooccur.blockSignals(False)

    def _hide_hints(self) -> None:
        self._present_cooccur_tags = set()
        self.label_hints_header.setVisible(False)
        self.label_hints.setVisible(False)
        self.label_hints_legend.setVisible(False)
        self.spin_cooccur.setVisible(False)
        self._cooccur_thr_label.setVisible(False)
        self.label_hints.setText("")

    def _on_cooccur_threshold_changed(self, value: int) -> None:
        """Live co-occurrence 'too common %' control (#18). Updates the
        session threshold and re-renders (hints recolor and the tag-list
        highlight set may change)."""
        if self._state is not None:
            self._state.set_cooccur_too_common_pct(value)
            self._refresh()
