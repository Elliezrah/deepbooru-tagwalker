"""
ui/tag_inspector_popup.py

The tag inspector (DESIGN_TAG_REFERENCE.md): click a curated example
and see the larger image plus that post's COMPLETE tag list, with the
post's tag count in the header (decision 17) — how thoroughly the
example is captioned, which is what the reader is there to learn.

Every tag is CLICKABLE: it sends the Tag Reference window straight to
that tag, which is the fastest route from "this example uses a tag I
don't know" to "here is what that tag means".

Tags the current image already carries are dimmed, so the undimmed
ones are what this example has that yours does not. That comparison
is recomputed live from state events — an earlier build captured the
target image once at construction, so after walking to another image
the popup silently described the wrong one.

Deliberately NOT here: a one-click "add tag" control. It shipped, and
it wrote to whichever image had been current when the popup opened
rather than the one on screen. Keeping a long-lived popup's write
target in step with the walk is a sync problem not worth its risk in
a tool whose job is careful caption edits; adding tags belongs in the
main window where the target is unambiguous.

Non-modal, and inherits the reference window's always-on-top state so
it can never open behind it.
"""
from __future__ import annotations

from urllib.parse import quote, unquote

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QMenu,
    QToolButton,
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.media_view import MediaView, is_animated

from core.danbooru_api import (
    is_video,
    CATEGORY_COLOURS,
    PostInfo,
    rating_colour,
    rating_name,
)
from core.state import _fold_for_match

IMAGE_MAX_W = 560
IMAGE_MAX_H = 520
DIM_COLOUR = "#7f7f7f"
_REFRESH_KINDS = {"tag_selected", "walk_advanced", "image_changed",
                  "filter_changed", "tree_rebuilt"}


class TagInspectorPopup(QWidget):
    def __init__(self, post: PostInfo, state, ref_window) -> None:
        super().__init__(None)
        self._post = post
        self._state = state
        self._ref = ref_window
        self.setWindowTitle(f"post #{post.id} \u2014 Tag Inspector")
        self.setMinimumSize(560, 620)
        self.resize(620, 760)

        layout = QVBoxLayout(self)
        header = QLabel(
            f"<b>post #{post.id}</b> \u00b7 {post.tag_count} tags "
            f"\u00b7 Rating: <span style='color:"
            f"{rating_colour(post.rating)};'>"
            f"{rating_name(post.rating)}</span>")
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        # Who drew it / who is in it / what it is from. Already in the
        # same response, so this costs nothing extra.
        self.credits = QLabel("")
        self.credits.setWordWrap(True)
        self.credits.setTextFormat(Qt.TextFormat.RichText)
        self.credits.setOpenExternalLinks(False)
        self.credits.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.credits.linkActivated.connect(self._on_tag_clicked)
        layout.addWidget(self.credits)

        # Fixed height so the window does not resize itself for every
        # post: Danbooru images arrive at wildly different dimensions.
        self.media = MediaView()
        self.media.setMinimumHeight(300)
        self.media.setMaximumHeight(IMAGE_MAX_H)
        self.media.setToolTip(
            "Mouse wheel zooms, drag pans \u2014 the same gestures as "
            "the main image viewer. Double-click anywhere to close.")
        layout.addWidget(self.media)

        self.diff_caption = QLabel("")
        self.diff_caption.setWordWrap(True)
        layout.addWidget(self.diff_caption)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.tag_label = QLabel("")
        self.tag_label.setWordWrap(True)
        self.tag_label.setTextFormat(Qt.TextFormat.RichText)
        self.tag_label.setOpenExternalLinks(False)
        self.tag_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        # FIELD REPORT: right-clicking offered Copy / Copy Link
        # Location / Select All. "Copy" needed a selection first, and
        # "Copy Link Location" copied the internal tag: URL rather
        # than anything useful. It was Qt's default menu for
        # selectable rich text, not a feature anyone designed.
        #
        # The Copy dropdown above already copies tags properly, in the
        # three groupings that matter.
        #
        # A first attempt set this on two labels by name and MISSED
        # the rest — the caption, the status line and the picture all
        # still had one. Naming widgets individually is how that
        # happens, so this is now applied to the popup and everything
        # inside it, and re-applied after the tag list is rebuilt.
        self._suppress_context_menus()
        self.tag_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.tag_label.linkActivated.connect(self._on_tag_clicked)
        scroll.setWidget(self.tag_label)
        layout.addWidget(scroll, 1)

        actions = QHBoxLayout()
        self.btn_copy = QToolButton()
        self.btn_copy.setText("Copy tags to clipboard")
        self.btn_copy.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        copy_menu = QMenu(self.btn_copy)
        # Most-wanted first: the general tags are the ones that
        # describe the picture; artist/copyright/meta rarely belong in
        # a training caption.
        for label, mode in (("Only general tags", "general"),
                            ("All", "all"),
                            ("All except metadata", "no_meta")):
            act = QAction(label, copy_menu)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._copy_tags(m))
            copy_menu.addAction(act)
        self.btn_copy.setMenu(copy_menu)
        self._copy_menu = copy_menu
        actions.addWidget(self.btn_copy)

        from core import bookmarks as bmk
        from ui.bookmark_button import BookmarkButton

        settings = getattr(self._ref, "_settings", None)
        self._bookmarks = bmk.store_for(settings) if settings else None
        if self._bookmarks is not None:
            self.btn_bookmark = BookmarkButton(
                "post", self._bookmarks,
                current=self._current_bookmark,
                activate=self._open_bookmark)
            actions.addWidget(self.btn_bookmark)

        self.btn_export = QPushButton("Export image + tags")
        self.btn_export.setToolTip(
            "Save this example's image and its caption side by side "
            "into your export folder (Settings \u2192 Danbooru "
            "lookups).")
        self.btn_export.clicked.connect(self._export)
        actions.addWidget(self.btn_export)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        actions.addStretch(0)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        actions.addWidget(btn_close)
        layout.addLayout(actions)

        if state is not None:
            state.add_listener(self._on_state_change)
        self._render_tags()

        show_images = bool(getattr(
            getattr(self._ref, "_settings", None),
            "danbooru_show_images", False))
        url = post.large_url or post.preview_url
        if not show_images:
            self.media.set_notice(
                "Example images are turned off "
                "(Settings \u2192 Danbooru lookups)")
        elif is_video(post):
            # The video will not render, but the still Danbooru serves
            # for it will — and it is worth seeing. Load the preview
            # and put the message over it rather than instead of it.
            self._show_video_offer()
            still = post.preview_url or post.large_url
            if still:
                self._ref.thumb_bytes(f"{post.id}_prev", still,
                                      self._got_image)
        elif not url:
            self.media.set_notice("(no image URL for this post)")
        else:
            self._ref.thumb_bytes(f"{post.id}_lg", url,
                                  self._got_image)

    # ------------------------------------------------------------------
    # Live comparison against whatever image is current RIGHT NOW
    # ------------------------------------------------------------------
    def _on_state_change(self, change) -> None:
        if change.kind in _REFRESH_KINDS:
            self._render_tags()

    def _current_tags(self) -> tuple[set[str], str]:
        if self._state is None:
            return set(), ""
        try:
            current = self._state.current_image
            if current is None:
                return set(), ""
            tags = self._state.get_image_tags(current.image_path)
            return ({_fold_for_match(t) for t in tags},
                    current.image_path.name)
        except Exception:
            return set(), ""

    def _tag_chip(self, tag: str, category: str,
                  folds: set[str]) -> str:
        """One clickable tag, coloured by booru category. A tag the
        current image already carries is dimmed instead, so the
        undimmed ones read as 'what this example has that I don't'."""
        shared = _fold_for_match(tag) in folds
        colour = (DIM_COLOUR if shared
                  else CATEGORY_COLOURS.get(category, ""))
        style = "text-decoration:none;"
        if colour:
            style += f"color:{colour};"
        return (f'<a href="tag:{quote(tag)}" style="{style}">'
                f"{tag}</a>")

    def _render_credits(self, folds: set[str]) -> None:
        bits = []
        for heading, category, tags in self._post.grouped_tags():
            if category in ("general", "meta") or not tags:
                continue
            chips = ", ".join(self._tag_chip(t, category, folds)
                              for t in tags)
            bits.append(f"<b>{heading}:</b> {chips}")
        self.credits.setText("<br>".join(bits))
        self.credits.setVisible(bool(bits))

    def _suppress_context_menus(self) -> None:
        """No accidental Qt menus anywhere in this window. Re-run
        after the tag list is rebuilt, since that creates labels."""
        from ui.zoom_view import suppress_context_menus

        suppress_context_menus(self)

    def _render_tags(self) -> None:
        folds, name = self._current_tags()
        example = self._post.tag_string.split()
        missing = [t for t in example
                   if _fold_for_match(t) not in folds]
        self._render_credits(folds)
        if name:
            self.diff_caption.setText(
                f"<b>{len(missing)}</b> tag(s) this example has that "
                f"your current image ({name}) lacks \u2014 dimmed tags "
                "are ones you already have. Click any tag to look it "
                "up.")
        else:
            self.diff_caption.setText(
                "Open a dataset image to compare against your "
                "caption. Click any tag to look it up.")
        sections = []
        for heading, category, tags in self._post.grouped_tags():
            chips = ", ".join(self._tag_chip(t, category, folds)
                              for t in tags)
            sections.append(
                f"<div style='margin-bottom:6px;'>"
                f"<b>{heading}</b> ({len(tags)})<br>{chips}</div>")
        self.tag_label.setText("".join(sections))
        # New labels were just created; they arrive with Qt's
        # default menu unless told otherwise.
        self._suppress_context_menus()


    def _on_tag_clicked(self, href: str) -> None:
        if not href.startswith("tag:"):
            return
        tag = unquote(href[4:])
        try:
            self._ref.show()
            self._ref.raise_()
            self._ref.navigate(tag)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Double-click anywhere to dismiss — with a pinned reference
        window you open and close these constantly. Safe again now
        that a single click on the picture does nothing."""
        self.close()
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def _show_video_offer(self) -> None:
        """A video post cannot be displayed, but it can be saved.

        Exporting it is genuinely useful — the file is the reference —
        so the dead end becomes the one action that works. The button
        reuses the ordinary export path, so the caption is written
        beside it exactly as for a still image.
        """
        ext = (self._post.file_ext or "video").upper()
        self.media.set_notice(
            f"{ext} video \u2014 showing its still frame. "
            "Download it to watch.")
        self.btn_export.setText("Download video + tags")
        self.btn_export.setToolTip(
            "Save the video and its caption into your export folder, "
            "then open the folder so you can play it.")

    def _got_image(self, data: bytes | None) -> None:
        if not data:
            self.media.set_notice("(image unavailable)")
            return
        # Animation first: a GIF loaded as a still would silently show
        # only its opening frame, which looks like a complete picture.
        if is_animated(data) and self.media.show_animation(data):
            return
        pix = QPixmap()
        if pix.loadFromData(data):
            self.media.show_pixmap(pix)
            if not is_video(self._post):
                self.media.set_notice("")
        else:
            # A format Qt cannot decode that was not spotted as video
            # (an unusual extension, say). Same offer: take the file.
            self._show_video_offer()

    def _current_bookmark(self):
        """Labelled with a few of its tags, so the saved list reads as
        pictures rather than as a column of bare numbers."""
        post = self._post
        tags = post.tag_string.split()[:4]
        label = f"post #{post.id}"
        if tags:
            label += " \u00b7 " + ", ".join(tags)
        return (str(post.id), label)

    def _open_bookmark(self, entry) -> None:
        opener = getattr(self._ref, "open_inspector_by_id", None)
        if opener is None:
            return
        try:
            opener(int(entry.value))
        except (TypeError, ValueError):
            return
        # One post at a time from this window: the saved one replaces
        # what is on screen rather than stacking another popup.
        self.close()

    def tags_for_mode(self, mode: str) -> list[str]:
        """general = what the picture shows; no_meta = everything
        except file/quality bookkeeping; all = verbatim."""
        post = self._post
        if mode == "general":
            return (post.tag_string_general.split()
                    or post.tag_string.split())
        if mode == "no_meta":
            meta = set(post.tag_string_meta.split())
            return [t for t in post.tag_string.split()
                    if t not in meta]
        return post.tag_string.split()

    def _copy_tags(self, mode: str) -> None:
        tags = self.tags_for_mode(mode)
        QApplication.clipboard().setText(", ".join(tags))
        self.status.setText(f"Copied {len(tags)} tag(s).")

    def _export(self) -> None:
        """Write the example image and its caption into the export
        folder, so a good reference can be kept beside the dataset it
        informed."""
        from pathlib import Path

        settings = getattr(self._ref, "_settings", None)
        target = (getattr(settings, "danbooru_export_dir", "") or
                  "").strip()
        if not target:
            self.status.setText(
                "Set an export folder first: Settings \u2192 Danbooru "
                "lookups \u2192 Export folder.")
            return
        want_original = bool(getattr(
            settings, "danbooru_export_original", False))
        # For a video the "sample" is a still frame, which would make
        # the download pointless. Always take the real file.
        if is_video(self._post):
            want_original = True
        url = ""
        if want_original:
            url = self._post.original_url
        url = url or self._post.large_url or self._post.preview_url
        if not url:
            self.status.setText("This post has no downloadable image.")
            return
        # A distinct cache key: the original and the shrunk sample are
        # different files and must not overwrite each other.
        cache_key = (f"{self._post.id}_orig" if want_original
                     and url == self._post.original_url
                     else f"{self._post.id}_lg")
        self.status.setText(
            "Exporting original\u2026" if cache_key.endswith("_orig")
            else "Exporting\u2026")

        def done(data: bytes | None) -> None:
            if not data:
                self.status.setText(
                    "Export failed: the image could not be fetched.")
                return
            try:
                folder = Path(target)
                folder.mkdir(parents=True, exist_ok=True)
                ext = ".jpg"
                tail = url.rsplit("/", 1)[-1]
                if "." in tail:
                    candidate = "." + tail.rsplit(".", 1)[-1].lower()
                    if len(candidate) <= 5:
                        ext = candidate
                from core import export_naming
                stem = export_naming.build_stem(
                    self._post,
                    getattr(self._ref._settings,
                            "danbooru_export_naming",
                            export_naming.DEFAULT_MODE))
                img_path = folder / f"{stem}{ext}"
                txt_path = folder / f"{stem}.txt"
                img_path.write_bytes(data)
                txt_path.write_text(
                    ", ".join(self.tags_for_mode("no_meta")),
                    encoding="utf-8")
            except OSError as exc:
                self.status.setText(f"Export failed: {exc}")
                return
            self.status.setText(
                f"Exported {img_path.name} + {txt_path.name} to "
                f"{folder}")
            if is_video(self._post):
                # FIELD REPORT: this used to open the folder itself,
                # on the reasoning that the app cannot play a video so
                # the system should. Reasonable once; irritating every
                # time, and a window stealing focus mid-session is
                # worse than a click.
                #
                # The Post Browser has a folder button for exactly
                # this, so the message points at it instead.
                self.status.setText(
                    f"Saved {img_path.name} to {folder}. This app "
                    "cannot play it \u2014 use the \U0001F4C1 button "
                    "in the Post Browser to open the folder.")

        self._ref.thumb_bytes(cache_key, url, done)

    def closeEvent(self, event) -> None:
        try:
            self.media.stop()
        except RuntimeError:
            pass
        if self._state is not None:
            try:
                self._state.remove_listener(self._on_state_change)
            except Exception:
                pass
            self._state = None
        super().closeEvent(event)
