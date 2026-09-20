"""
ui/tag_reference_window.py

The Tag Reference window (View \u2192 Tag Reference\u2026, and right-click on any
tag). See DESIGN_TAG_REFERENCE.md.

OFFLINE HALF (always available, no network): era counts across the
four dated snapshots with the user's selected model era highlighted,
a drift verdict, alias resolution both directions, and the
co-occurrence "appears together with" list — all click-navigable
with back/forward history, plus "Follow current tag" (400 ms
debounce).

ONLINE HALF (master opt-in in Settings \u2192 Danbooru lookups): the tag's
wiki page rendered faithfully IN DOCUMENT ORDER — prose with
clickable [[wiki links]], the staff-curated example galleries
(`!post #` embeds grouped under the page's own headings), See also,
and footer notes. Example images are a further sub-option, hidden
until hover with a session "Reveal all"; clicking one opens the tag
inspector popup (ui/tag_inspector_popup.py). Post thumbnails are
fetched via ONE id-metatag batch request, falling back to per-id
fetches if the batch form is rejected — the footer notes which path
worked so the first real run settles the open API question. Wiki
JSON and thumbnails live in two independent disk caches.

Failure behaviour per spec: 404 \u2192 the custom-tag similarity path;
429 \u2192 a back-off notice; anything else \u2192 a plain "couldn't reach"
line. The offline half above is never affected, and nothing here can
ever block the tag walk.
"""
from __future__ import annotations

import json
from urllib.parse import quote, unquote

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QPixmap, QShowEvent
from PySide6.QtCore import QUrl
from PySide6.QtGui import (
    QAction,
    QCursor,
    QDesktopServices,
    QGuiApplication,
)
from PySide6.QtWidgets import (
    QMenu,
    QApplication,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSplitter,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core import blacklist as bl
from core import danbooru_api as dapi
from core import dtext
from core import tag_reference as tr
from core.cooccurrence import get_db as get_cooc_db
from core.tag_database import CSV_PRESETS, DEFAULT_CSV_KEY

# The newest shipped snapshot: anything absent from it that still has
# a wiki page must post-date it.
DEFAULT_NEWEST_KEY = next(iter(CSV_PRESETS))
from core.danbooru_api import rating_colour, rating_name

FOLLOW_DEBOUNCE_MS = 400
COOC_LIMIT = 15
THUMB_SIZE = 150
CAPTION_H = 30
GALLERY_COLS = 4

# Two panes of wiki text need room. Entering compare mode widens the
# window to this if it is narrower, and the previous width is restored
# on the way out.
def _plain_text(html: str) -> str:
    """Caption labels are rendered HTML; the viewer wants the words."""
    import re as _re
    return _re.sub(r"<[^>]+>", "", html or "").strip()


COMPARE_WIDTH = 1100

# Space around the divider between two panes, plus the matching inner
# margin, so neither column's text touches the join.
SPLITTER_GAP = 14
PANE_MARGIN = 6

COOC_HELP_PAGES = [
    ("Where these numbers come from",
     "Bundled Danbooru co-occurrence data \u2014 not your dataset.\n\n"
     "The percentage is how often posts on Danbooru carrying this tag "
     "also carry the partner tag. It describes the site's habits, "
     "which is what a model trained on the site learned.\n\n"
     "\u201cin your set\u201d, beside each row, is the separate "
     "count of how many images in the folders YOU have loaded carry "
     "that partner. Comparing the two is the point: a pairing that is "
     "near-universal on Danbooru but rare in your captions is a habit "
     "the model has that your data does not reinforce."),

    ("Why the percentages are out of order",
     "Rows are ranked by association STRENGTH, not by the raw "
     "percentage, so the numbers will not descend neatly.\n\n"
     "Ranking by percentage alone would put 1girl and solo at the top "
     "of almost every tag on the site \u2014 true, and useless. What "
     "you want to see is which partners are unusually likely GIVEN "
     "this tag, compared to how common they are anyway.\n\nSo a "
     "partner at 30% can outrank one at 60% when the first is far "
     "more specific to this tag."),
]


class _ThumbWidget(QFrame):
    """One curated example: hidden until hover (spec decision 5),
    click opens the tag inspector (decision on the popup + tag-count
    display). Skipped posts (banned/deleted/blank URL) show their
    reason as a placeholder instead of vanishing."""

    def __init__(self, post_id: int, window: "TagReferenceWindow",
                 caption: str = "") -> None:
        super().__init__()
        self._window = window
        self._post_id = post_id
        self._post: dapi.PostInfo | None = None
        self._pix: QPixmap | None = None
        self._hover = False
        self._requested = False
        self._asset_url = ""
        self._caption_text = ""
        self.setFixedSize(THUMB_SIZE,
                          THUMB_SIZE + (CAPTION_H if caption else 0))
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(1)
        self.label = QLabel("\u2026")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        lay.addWidget(self.label, 1)
        self.badge = QLabel("")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.badge.setTextFormat(Qt.TextFormat.RichText)
        badge_font = self.badge.font()
        badge_font.setPointSizeF(max(7.0, badge_font.pointSizeF() - 1.5))
        self.badge.setFont(badge_font)
        lay.addWidget(self.badge)
        # The wiki's own label for this example ("Pumps", "Kitten
        # heels") — the taxonomy the reader came for. Stays clickable.
        self._caption_text = _plain_text(caption)
        if caption:
            self.caption = QLabel(caption)
            self.caption.setWordWrap(True)
            self.caption.setTextFormat(Qt.TextFormat.RichText)
            self.caption.setOpenExternalLinks(False)
            self.caption.linkActivated.connect(window._on_doc_link)
            self.caption.setFixedHeight(CAPTION_H)
            lay.addWidget(self.caption)

    def set_asset(self, url: str) -> None:
        """A media asset has no post behind it — no rating, no tag
        list, nothing to inspect. It is a reference picture (character
        pages use them for outfit breakdowns), so it renders straight
        away rather than hiding behind a hover."""
        self._asset_url = url
        self._post = None
        self.badge.setText("")
        self.label.setText("loading\u2026")

        def got(data: bytes | None) -> None:
            if not data:
                self.label.setText("(reference\nunavailable)")
                return
            pix = QPixmap()
            if not pix.loadFromData(data):
                self.label.setText("(bad data)")
                return
            self._pix = pix.scaled(
                THUMB_SIZE - 8, THUMB_SIZE - 24,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self.label.setText("")
            self.label.setPixmap(self._pix)

        self._window.thumb_bytes(
            f"asset_{abs(self._post_id)}", url, got)

    def set_post(self, post: dapi.PostInfo | None) -> None:
        self._post = post
        if post is None:
            self.label.setText("no data\nreturned")
            return
        if post.skip:
            self.label.setText(f"({post.skip_reason})")
            return
        badge = (f"<span style='color:{rating_colour(post.rating)};'>"
                 f"Rating: {rating_name(post.rating)}</span>")
        if getattr(post, "deleted", False):
            # Worth knowing — it will not turn up in a search — but not
            # a reason to withhold the picture.
            badge = ("<span style='color:#e08a3c;'>deleted</span> \u00b7 "
                     + badge)
        self.badge.setText(badge)
        tip = post.tag_string
        if len(tip) > 600:
            tip = tip[:600] + "\u2026"
        self.setToolTip(tip)
        self._cover()
        if self._window.reveal_all:
            self._reveal()

    def _cover(self) -> None:
        self.label.setPixmap(QPixmap())
        self.label.setText("hover to\nreveal")

    def _reveal(self) -> None:
        if self._post is None or self._post.skip:
            return
        if self._pix is not None:
            self.label.setText("")
            self.label.setPixmap(self._pix)
            return
        if self._requested:
            return
        self._requested = True
        self.label.setText("loading\u2026")

        def got(data: bytes | None) -> None:
            if data is None:
                self.label.setText("(image\nunavailable)")
                return
            pix = QPixmap()
            if pix.loadFromData(data):
                self._pix = pix.scaled(
                    THUMB_SIZE - 8, THUMB_SIZE - 24,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                if self._hover or self._window.reveal_all:
                    self.label.setText("")
                    self.label.setPixmap(self._pix)
            else:
                self.label.setText("(bad image)")

        self._window.thumb_bytes(
            str(self._post.id), self._post.preview_url, got)

    def set_reveal(self, on: bool) -> None:
        if self._asset_url:
            return          # assets are always visible
        if on:
            self._reveal()
        elif not self._hover:
            self._cover()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self._reveal()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        if not self._window.reveal_all and self._pix is not None:
            self._cover()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """A click on a tile must always do something, or say why not.

        Reference images used to return here silently: they carry no
        post, so there was nothing to inspect. But the tile shows a
        picture, and a picture that ignores clicks reads as broken.
        They open a plain viewer instead.
        """
        if self._asset_url:
            self._window.open_asset(
                abs(self._post_id), self._asset_url, self._caption_text)
        elif self._post is None:
            self._window.explain_unopenable(
                "This example never loaded, so there is nothing to "
                "open. Try the tag again, or use the chip above the "
                "grid.")
        elif self._post.skip:
            self._window.explain_unopenable(
                f"post #{self._post.id} cannot be opened \u2014 "
                f"{self._post.skip_reason or 'it is unavailable'}.")
        else:
            self._window.open_inspector(self._post)
        super().mousePressEvent(event)


class _GalleryWidget(QFrame):
    """One `!post #` embed group from the wiki body, in the editors'
    order.

    The clickable chip row is rendered from the parsed ids ALONE and
    therefore needs no network at all — so an example is always
    reachable (click it, the inspector opens with the full image and
    tag list) even when thumbnail fetching fails outright. Thumbnails
    are the bonus on top, and any failure states its reason here
    rather than leaving a silent blank.
    """

    def __init__(self, post_ids: list[int],
                 window: "TagReferenceWindow", images_on: bool,
                 captions: dict[int, str] | None = None,
                 asset_ids: list[int] | None = None) -> None:
        super().__init__()
        captions = captions or {}
        self.post_ids = list(post_ids)
        self.asset_ids = list(asset_ids or [])
        self.asset_slots: dict[int, _ThumbWidget] = {}
        self.slots: dict[int, _ThumbWidget] = {}
        self._window = window
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)

        if not post_ids and self.asset_ids:
            # Assets-only group — a character page's "Appearance"
            # section. There are no posts to chip-link to.
            self.chip_label = QLabel(
                f"{len(self.asset_ids)} reference image(s)")
            self.chip_label.setWordWrap(True)
            lay.addWidget(self.chip_label)
            self.status = QLabel("")
            self.status.setWordWrap(True)
            lay.addWidget(self.status)
            if not images_on:
                self.status.setText(
                    "Reference images are off (Settings \u2192 "
                    "Danbooru lookups).")
                return
            grid = QGridLayout()
            grid.setSpacing(4)
            for i, aid in enumerate(self.asset_ids):
                t = _ThumbWidget(-aid, window, captions.get(-aid, ""))
                self.asset_slots[aid] = t
                grid.addWidget(t, i // GALLERY_COLS,
                               i % GALLERY_COLS)
            lay.addLayout(grid)
            return
        chips = " \u00b7 ".join(
            f'<a href="post:{pid}" style="text-decoration:none;">'
            f"post #{pid}</a>" for pid in post_ids)
        self.chip_label = QLabel(
            f"{len(post_ids)} curated example(s): {chips}")
        self.chip_label.setWordWrap(True)
        self.chip_label.setTextFormat(Qt.TextFormat.RichText)
        self.chip_label.setOpenExternalLinks(False)
        self.chip_label.linkActivated.connect(window._on_doc_link)
        lay.addWidget(self.chip_label)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        if not images_on:
            self.status.setText(
                "Thumbnails are off (Settings \u2192 Danbooru "
                "lookups). Click any example above to open it.")
            return
        grid = QGridLayout()
        grid.setSpacing(4)
        cell = 0
        for pid in post_ids:
            t = _ThumbWidget(pid, window, captions.get(pid, ""))
            self.slots[pid] = t
            grid.addWidget(t, cell // GALLERY_COLS,
                           cell % GALLERY_COLS)
            cell += 1
        # Media assets share the grid: on a character page the outfit
        # references and the example posts are one visual group.
        for aid in self.asset_ids:
            t = _ThumbWidget(-aid, window, captions.get(-aid, ""))
            self.asset_slots[aid] = t
            grid.addWidget(t, cell // GALLERY_COLS,
                           cell % GALLERY_COLS)
            cell += 1
        lay.addLayout(grid)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def fill(self, by_id: dict[int, dapi.PostInfo]) -> None:
        for pid, thumb in self.slots.items():
            thumb.set_post(by_id.get(pid))

    def apply_reveal(self, on: bool) -> None:
        for thumb in self.slots.values():
            thumb.set_reveal(on)


class _ReferencePane(QWidget):
    """One tag's worth of the Tag Referencer: header, era counts,
    verdict, aliases, the wiki document with its example galleries,
    the raw-source view and the co-occurrence table.

    Extracted from TagReferenceWindow so that comparing two tags side
    by side is a matter of building two of these, rather than teaching
    one window to write every value twice. Nothing here changed in the
    move: the methods are the window's own, with the services they
    borrow — settings, caches, the fetcher, the blacklist — reached
    through `_host` instead of `self`.

    A pane owns its own history, its own in-flight request sequence
    and its own post cache, because two panes must be able to be
    looking at different tags, mid-load, without disturbing each
    other.
    """

    def __init__(self, host) -> None:
        super().__init__()
        self._host = host
        # Per-pane, so two panes can be looking at different tags,
        # mid-load, without disturbing each other.
        self._hist: list[str] = []
        self._hpos = -1
        self._online_seq = 0
        self._last_body: str = ""
        self.reveal_all = False
        self._galleries: list = []
        self._posts_by_id: dict = {}
        self._anchors: dict = {}
        self._fetch_note = None
        # What this pane is showing. Set here as well as on load: a
        # pane that has never navigated is still asked for its tag the
        # moment compare mode opens.
        self.tag: str = ""
        self.cooc_tags: list[str] = []
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(PANE_MARGIN, 0, PANE_MARGIN, 0)
        # Which pane the navigation row is driving. Hidden until there
        # are two panes to tell apart.
        self.role = QLabel("")
        self.role.setVisible(False)
        layout.addWidget(self.role)
        self.header = QLabel("")
        self.header.setWordWrap(True)
        self.header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.header)

        # Alias-redirect banner. Shown only when the looked-up text was
        # an alias and the view bounced to the canonical tag's page. The
        # redirect was previously noted inline in the header metadata,
        # in the same muted style as everything else, so it was easy to
        # miss — users looked up an alias and thought the tool had taken
        # them to the wrong page. This gives the redirect its own line
        # with an accent colour and an arrow, so it reads as an event
        # ("your search was redirected") rather than a footnote. Hidden
        # by default; a direct (non-alias) lookup never shows it.
        self.redirect_banner = QLabel("")
        self.redirect_banner.setWordWrap(True)
        self.redirect_banner.setTextFormat(Qt.TextFormat.RichText)
        self.redirect_banner.setVisible(False)
        self.redirect_banner.setStyleSheet(
            "QLabel {"
            " background: #1e3a66;"           # ACCENT_BLUE_BG
            " color: #cfe0ff;"
            " border-left: 3px solid #4a8eff;"  # ACCENT_BLUE
            " border-radius: 3px;"
            " padding: 5px 9px;"
            "}"
        )
        layout.addWidget(self.redirect_banner)

        self.era_toggle = QToolButton()
        self.era_toggle.setCheckable(True)
        self.era_toggle.setChecked(False)      # folded by default
        self.era_toggle.setAutoRaise(True)
        self.era_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.era_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.era_toggle.setText(
            "Danbooru post count by tag-database era")
        self.era_toggle.setToolTip(
            "How many posts carried this tag in each shipped snapshot "
            "\u2014 the drift evidence behind the verdict below.")
        self.era_toggle.toggled.connect(self._toggle_eras)
        layout.addWidget(self.era_toggle)

        self.era_label = QLabel("")
        self.era_label.setWordWrap(True)
        self.era_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.era_label)

        self.era_label.setVisible(False)       # matches era_toggle

        self.verdict = QLabel("")
        self.verdict.setWordWrap(True)
        self.verdict.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.verdict)

        self.aliases = QLabel("")
        self.aliases.setWordWrap(True)
        self.aliases.setVisible(False)
        layout.addWidget(self.aliases)

        # ---- Danbooru wiki document (online half) --------------------
        self.doc_scroll = QScrollArea()
        self.doc_scroll.setWidgetResizable(True)
        self.doc_host = QWidget()
        self.doc_layout = QVBoxLayout(self.doc_host)
        self.doc_layout.setContentsMargins(4, 4, 4, 4)
        self.doc_scroll.setWidget(self.doc_host)
        layout.addWidget(self.doc_scroll, 3)

        self.source_view = QPlainTextEdit()
        self.source_view.setReadOnly(True)
        self.source_view.setVisible(False)
        self.source_view.setPlaceholderText(
            "Raw wiki source appears here.")
        layout.addWidget(self.source_view, 2)

        cooc_row = QHBoxLayout()
        self.cooc_caption = QLabel("")
        self.cooc_caption.setWordWrap(True)
        cooc_row.addWidget(self.cooc_caption, 1)
        self.cooc_help = QToolButton()
        self.cooc_help.setText("?")
        self.cooc_help.setToolTip(
            "What these percentages mean, and where they come from")
        self.cooc_help.clicked.connect(self._host._show_cooc_help)
        cooc_row.addWidget(self.cooc_help)
        layout.addLayout(cooc_row)
        self.cooc_list = QListWidget()
        self.cooc_list.itemClicked.connect(self._host._on_cooc_clicked)
        layout.addWidget(self.cooc_list, 1)

    def reset(self) -> None:
        """Back to the state a freshly-built pane is in."""
        self._hist = []
        self._hpos = -1
        self.tag = ""
        self.cooc_tags = []
        self._posts_by_id = {}
        self._last_body = ""
        self._anchors = {}
        self._online_seq += 1          # abandon any in-flight fetch
        self.reveal_all = False
        self.header.setText("")
        self.redirect_banner.setVisible(False)
        self.era_label.setText("")
        self.verdict.setText("")
        self.aliases.setVisible(False)
        self.cooc_caption.setText("")
        self.cooc_list.clear()
        self.source_view.setPlainText("")
        self._set_doc_status(
            "Look up a tag to see its Danbooru wiki here.")
        self._update_nav_buttons()

    # -- services the panes borrow from the window ---------------
    def thumb_bytes(self, key, url, cb) -> None:
        self._host.thumb_bytes(key, url, cb)

    def open_asset(self, asset_id: int, url: str,
                   caption: str = "") -> None:
        self._host.open_asset(asset_id, url, caption)

    def explain_unopenable(self, message: str) -> None:
        self._set_gallery_status(message)

    def open_inspector(self, post) -> None:
        """Clicking a thumbnail opens the post.

        The galleries are built with their PANE as "window", so this
        has to exist here: without it a click raised AttributeError
        inside a Qt event handler, which Qt swallows, and the
        thumbnail simply did nothing.
        """
        self._host.open_inspector(post)

    def _on_doc_link(self, href: str) -> None:
        """Links inside a pane's document act on THAT pane: a tag
        navigates it, an anchor scrolls it. Posts and external
        addresses are window-level and are handed up."""
        if href.startswith("tag:"):
            tag = unquote(href[4:])
            # Ordinary clicks stay in this pane; Ctrl sends the tag
            # across. linkActivated carries no modifier state, but the
            # keyboard's current state is accurate at this instant.
            mods = QGuiApplication.keyboardModifiers()
            if mods & Qt.KeyboardModifier.ControlModifier:
                self._host.send_to_other_pane(self, tag)
            else:
                self.navigate(tag)
        elif href.startswith("post:"):
            try:
                self.open_inspector_by_id(int(href[5:]))
            except (TypeError, ValueError):
                pass
        elif href.startswith("anchor:"):
            self._scroll_to_anchor(unquote(href[7:]))
        elif href.startswith("copy:"):
            self._host._offer_link(unquote(href[5:]))

    def navigate(self, tag: str) -> None:
        tag = (tag or "").strip()
        if not tag:
            return
        if 0 <= self._hpos < len(self._hist) \
                and self._hist[self._hpos] == tag:
            self._load(tag)
            return
        del self._hist[self._hpos + 1:]
        self._hist.append(tag)
        self._hpos = len(self._hist) - 1
        self._load(tag)
        self._update_nav_buttons()

    def _go_back(self) -> None:
        if self._hpos > 0:
            self._hpos -= 1
            self._load(self._hist[self._hpos])
            self._update_nav_buttons()

    def _go_forward(self) -> None:
        if self._hpos < len(self._hist) - 1:
            self._hpos += 1
            self._load(self._hist[self._hpos])
            self._update_nav_buttons()

    def _update_nav_buttons(self) -> None:
        self._host.btn_back.setEnabled(self._hpos > 0)
        self._host.btn_fwd.setEnabled(self._hpos < len(self._hist) - 1)

    def _load(self, tag: str) -> None:
        rules = self._host._current_block_rules()
        blocked = bl.blocked_tag_reason(tag, rules)
        if blocked:
            self._render_blocked_tag(tag, blocked)
            return
        first = tr._TABLES is None
        if first:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            info = tr.lookup(tag, self._host._selected_era_key())
        finally:
            if first:
                QApplication.restoreOverrideCursor()

        # An alias must not be a way around the list. Danbooru has
        # thousands of them, so checking only the name that was typed
        # left every blocked tag reachable under another spelling —
        # with its counts, verdict, wiki and examples all rendered.
        if info.canonical and info.canonical != tag:
            blocked = bl.blocked_tag_reason(info.canonical, rules)
            if blocked:
                self._render_blocked_tag(info.canonical, blocked)
                return

        head = f"<h2 style='margin:0'>{info.canonical}</h2>"
        bits = []
        if info.category_name:
            bits.append(f"category: {info.category_name}")
        if bits:
            head += "<div>" + " \u00b7 ".join(bits) + "</div>"
        self.header.setText(head)

        # The redirect gets its own accent banner (see construction),
        # showing both the typed alias and the canonical tag landed on,
        # so the redirect is unmistakable. Shown only on an alias
        # lookup; cleared for a direct one so it does not linger from a
        # previous search.
        if info.redirected_from and info.canonical != info.redirected_from:
            self.redirect_banner.setText(
                f"\u21aa \u201c<b>{info.redirected_from}</b>\u201d is an "
                f"alias \u2014 showing <b>{info.canonical}</b>")
            self.redirect_banner.setVisible(True)
        else:
            self.redirect_banner.setText("")
            self.redirect_banner.setVisible(False)

        sel = self._host._selected_era_key()
        lines = []
        for e in info.era_counts:
            count = "\u2014" if e.count is None else f"{e.count:,}"
            mark = " \u25c0 your era" if e.key == sel else ""
            row = f"{e.label}: <b>{count}</b>{mark}"
            if e.key == sel:
                row = f"<u>{row}</u>"
            lines.append(row)
        self.era_label.setText("<br>".join(lines))

        verdict_colour = "#8a8a8a"
        if info.verdict.startswith("postdates"):
            verdict_colour = "#e05a5a"     # model never saw this tag
        elif info.verdict.startswith(("custom", "retired", "renamed")):
            verdict_colour = "#e08a3c"     # needs a human decision
        elif info.verdict.startswith("stable"):
            verdict_colour = "#5fb85f"
        self.verdict.setText(
            f"Verdict: <span style='color:{verdict_colour};'>"
            f"{info.verdict}</span>")

        if info.aliases:
            shown = info.aliases[:8]
            extra = len(info.aliases) - len(shown)
            text = "Also known as: " + ", ".join(shown)
            if extra > 0:
                text += f"  (+{extra} more)"
            self.aliases.setText(text)
            self.aliases.setVisible(True)
        else:
            self.aliases.setVisible(False)

        self.cooc_list.clear()
        partners = get_cooc_db().get_cooccurring(
            info.canonical, limit=COOC_LIMIT)
        self.tag = info.canonical
        self.cooc_tags = [name for name, _o, _p in partners]
        self.cooc_caption.setText(
            f"Commonly tagged with <b>{info.canonical}</b> on "
            "Danbooru:")
        if partners:
            for name, _ochiai, p_cond in partners:
                pct = f"{p_cond * 100:.0f}%"
                mine = self._host._dataset_count(name)
                suffix = (f"   \u00b7   {mine} in your set"
                          if mine is not None else "")
                item = QListWidgetItem(f"{name}    \u2014  {pct}{suffix}")
                item.setData(Qt.ItemDataRole.UserRole, name)
                self.cooc_list.addItem(item)
        else:
            placeholder = QListWidgetItem("no co-occurrence data")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.cooc_list.addItem(placeholder)

        self.suppress_menus()
        self._host.on_pane_loaded(self)
        # Deliberately NOT persisted: closing and reopening the window
        # keeps your page (the instance lives on), but a restart is a
        # clean slate.
        self._load_online(info.canonical)

    def _load_online(self, tag: str) -> None:
        self._online_seq += 1
        seq = self._online_seq
        self.reveal_all = bool(getattr(
            self._host._settings, "danbooru_reveal_by_default", False))
        self._posts_by_id = {}
        self._host._block_rules = self._host._current_block_rules()
        if not getattr(self._host._settings, "danbooru_lookups_enabled",
                       False):
            self._set_doc_status(
                "Danbooru lookups are disabled \u2014 enable them in "
                "Settings \u2192 Danbooru lookups to fetch the "
                "description and curated example galleries here. "
                "Everything above is offline data.")
            return
        cached = self._host._text_cache.get(tag)
        if cached is not None:
            self._render_wiki(tag, cached, seq, from_cache=True)
            return
        self._set_doc_status("loading Danbooru wiki\u2026")

        def cb(payload: bytes | None, status: int) -> None:
            if seq != self._online_seq:
                return
            if payload is None:
                if status == 404:
                    self._similarity(tag, seq)
                elif status == 429:
                    self._set_doc_status(
                        "Danbooru rate limit reached (HTTP 429) \u2014 "
                        "try again in a little while. The offline "
                        "data above is unaffected.")
                else:
                    self._set_doc_status(
                        f"Couldn't reach Danbooru (status {status}). "
                        "The offline data above is unaffected.")
                return
            try:
                data = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._set_doc_status(
                    "Unexpected Danbooru response \u2014 the offline "
                    "data above is unaffected.")
                return
            if dapi.parse_wiki(data) is None:
                self._similarity(tag, seq)
                return
            self._host._text_cache.put(tag, data)
            self._render_wiki(tag, data, seq, from_cache=False)

        self._host._get_fetcher().get(dapi.wiki_url(tag), cb)

    def _render_wiki(self, tag: str, data: object, seq: int,
                     from_cache: bool) -> None:
        page = dapi.parse_wiki(data)
        if page is None:
            self._similarity(tag, seq)
            return
        blocks = dtext.parse_dtext(page.body)
        self._last_body = page.body or ""
        self._correct_custom_verdict(tag)
        self._refresh_source_view(blocks)
        images_on = getattr(self._host._settings, "danbooru_show_images",
                            False)
        self._clear_doc()
        if page.other_names:
            names = ", ".join(page.other_names[:6])
            self.doc_layout.addWidget(
                self._host._doc_label(f"<i>Other names: {names}</i>"))
        for blk in blocks:
            if isinstance(blk, dtext.Heading):
                lvl = min(max(blk.level, 3), 5)
                head = self._host._doc_label(f"<h{lvl}>{blk.html}</h{lvl}>")
                self.doc_layout.addWidget(head)
                if blk.anchor:
                    self._anchors[blk.anchor] = head
            elif isinstance(blk, dtext.Para):
                self.doc_layout.addWidget(self._host._doc_label(blk.html))
            elif isinstance(blk, dtext.Bullets):
                items = "".join(f"<li>{i}</li>" for i in blk.items)
                self.doc_layout.addWidget(
                    self._host._doc_label(f"<ul>{items}</ul>"))
            elif isinstance(blk, dtext.Gallery):
                g = _GalleryWidget(
                    blk.post_ids, self, images_on,
                    getattr(blk, "captions", None),
                    getattr(blk, "asset_ids", None))
                self._galleries.append(g)
                self.doc_layout.addWidget(g)
        if images_on and self._galleries:
            row = QHBoxLayout()
            if not getattr(self._host._settings,
                           "danbooru_reveal_by_default", False):
                btn = QPushButton("Reveal all examples")
                btn.setCheckable(True)
                btn.toggled.connect(self._set_reveal_all)
                row.addWidget(btn)
            # else: the button would have nothing left to do.
            self._fetch_note = QLabel("")
            row.addWidget(self._fetch_note)
            row.addStretch(1)
            host = QWidget()
            host.setLayout(row)
            self.doc_layout.addWidget(host)
        src_bits = ["wiki cached" if from_cache else "wiki fetched"]
        if page.updated_at:
            src_bits.append(f"page updated {page.updated_at[:10]}")
        self.doc_layout.addWidget(
            self._host._doc_label("<i>" + " \u00b7 ".join(src_bits) + "</i>"))
        self.doc_layout.addStretch(1)
        asset_ids = [a for blk in blocks
                     if isinstance(blk, dtext.Gallery)
                     for a in getattr(blk, "asset_ids", [])]
        if asset_ids and images_on:
            self._fetch_assets(asset_ids, seq)
        ids = dtext.extract_post_ids(blocks)
        if images_on and ids:
            self._fetch_posts(ids, seq)

    def _render_blocked_tag(self, tag: str, rule: str) -> None:
        """A blacklisted tag opens nothing at all — no counts, no
        wiki, no examples. Showing the page minus its images would
        still be putting the subject in front of someone who asked not
        to see it."""
        self._online_seq += 1          # abandon anything in flight
        self.tag = tag
        self.cooc_tags = []
        self.header.setText(
            f"<h2 style='margin:0'>{tag}</h2>")
        # A blocked page shows nothing about the tag, and the redirect
        # banner would be stale here — hide it.
        self.redirect_banner.setVisible(False)
        self.era_label.setText("")
        self.verdict.setText(
            "<span style='color:#e05a5a;'>blocked by your blacklist"
            "</span>")
        self.aliases.setVisible(False)
        self.cooc_caption.setText("")
        self.cooc_list.clear()
        self._last_body = ""
        self._set_doc_status(
            f"\u201c{tag}\u201d matches the blacklist rule "
            f"\u201c{rule}\u201d, so nothing is shown for it. "
            "Edit the list in Settings \u2192 Danbooru lookups if "
            "that is not what you want.")
        self.suppress_menus()
        self._host.on_pane_loaded(self)

    def _correct_custom_verdict(self, tag: str) -> None:
        """Offline, a tag in none of the four snapshots reads as
        "custom" — your own invented token. But a tag CREATED after
        the newest snapshot looks identical from offline data alone,
        and calling it custom is simply wrong. A wiki page is proof it
        is a real Danbooru tag, so say so instead.

        This matters more as the shipped snapshot ages: everything
        Danbooru adds from now on falls into this gap.
        """
        try:
            info = tr.lookup(tag, self._host._selected_era_key())
        except Exception:
            return
        if not info.is_custom:
            return
        newest = CSV_PRESETS.get(DEFAULT_NEWEST_KEY, ("", "", ""))[1]
        self.verdict.setText(
            "Verdict: <span style='color:#e08a3c;'>newer than your "
            f"data \u2014 a real Danbooru tag (it has a wiki page), but "
            f"absent from every shipped snapshot, so it was created "
            f"after {newest}. No model trained on these eras has seen "
            "it.</span>")

    def _similarity(self, tag: str, seq: int) -> None:
        self._set_doc_status(
            f"No Danbooru wiki page \u2014 \u201c{tag}\u201d is a "
            "custom tag. Checking similar names\u2026")

        def cb(payload: bytes | None, status: int) -> None:
            if seq != self._online_seq:
                return
            names: list[str] = []
            if payload is not None:
                try:
                    data = json.loads(payload.decode("utf-8"))
                    if isinstance(data, list):
                        names = [str(d.get("name")) for d in data
                                 if isinstance(d, dict)
                                 and d.get("name")]
                except (ValueError, UnicodeDecodeError):
                    pass
            # A tag can exist with no wiki page. The search then
            # returns the tag itself, and suggesting it sends the user
            # straight back here — an endless "did you mean X?" loop
            # pointing at X. Drop self-matches.
            folded = tag.strip().lower().replace(" ", "_")
            names = [n for n in names
                     if n.strip().lower().replace(" ", "_") != folded]
            self._clear_doc()
            known = not tr.lookup(tag, self._host._selected_era_key()).is_custom
            if known:
                self.doc_layout.addWidget(self._host._doc_label(
                    f"\u201c{tag}\u201d is a real Danbooru tag, but "
                    "it has no wiki page \u2014 nobody has written one "
                    "yet. The counts and verdict above still apply."))
            else:
                self.doc_layout.addWidget(self._host._doc_label(
                    f"No Danbooru wiki page \u2014 \u201c{tag}\u201d "
                    "is a custom tag."))
            if names:
                chips = " \u00b7 ".join(
                    f'<a href="tag:{quote(n)}">{n}</a>'
                    for n in names[:8])
                self.doc_layout.addWidget(self._host._doc_label(
                    "Did you mean: " + chips))
            self.doc_layout.addStretch(1)

        self._host._get_fetcher().get(dapi.tag_search_url(tag), cb)

    def _fetch_posts(self, ids: list[int], seq: int) -> None:
        self._set_gallery_status("loading example thumbnails\u2026")

        def batch_cb(payload: bytes | None, status: int) -> None:
            if seq != self._online_seq:
                return
            posts: list[dapi.PostInfo] = []
            if payload is not None:
                try:
                    posts = dapi.parse_posts(
                        json.loads(payload.decode("utf-8")),
                        wanted_order=ids)
                except (ValueError, UnicodeDecodeError):
                    posts = []
            if posts:
                self._distribute(posts, "batch")
                return
            # Batch rejected or empty -> per-id fallback. Whatever
            # happens, the chip row above stays clickable.
            self._set_gallery_status(
                f"batch fetch returned {status}; trying one at a "
                "time\u2026")
            acc: dict[int, dapi.PostInfo] = {}
            fails: dict[str, int] = {"last": 0}
            remaining = {"n": len(ids)}

            def one_done() -> None:
                remaining["n"] -= 1
                if remaining["n"] or seq != self._online_seq:
                    return
                ordered = [acc[i] for i in ids if i in acc]
                if ordered:
                    self._distribute(
                        ordered,
                        f"per-id fallback (batch gave {status})")
                else:
                    self._set_gallery_status(
                        f"Could not load thumbnails \u2014 batch "
                        f"returned {status}, per-id returned "
                        f"{fails['last']}. Click any example above to "
                        "open it directly.")

            for pid in ids:
                def one_cb(payload2: bytes | None, status2: int,
                           pid: int = pid) -> None:
                    if payload2 is not None:
                        try:
                            p = dapi.parse_posts(
                                json.loads(payload2.decode("utf-8")))
                            if p:
                                acc[pid] = p[0]
                        except (ValueError, UnicodeDecodeError):
                            pass
                    else:
                        fails["last"] = status2
                    one_done()

                self._host._get_fetcher().get(dapi.post_url(pid), one_cb)

        self._host._get_fetcher().get(dapi.posts_by_ids_url(ids), batch_cb)

    def _fetch_assets(self, asset_ids: list[int], seq: int) -> None:
        """Media assets resolve one request each — there is no batch
        endpoint for them — so one failure costs only its own tile."""
        for aid in asset_ids:
            def cb(payload: bytes | None, status: int,
                   aid: int = aid) -> None:
                if seq != self._online_seq:
                    return
                url = ""
                if payload is not None:
                    try:
                        url = dapi.parse_media_asset(
                            json.loads(payload.decode("utf-8")))
                    except (ValueError, UnicodeDecodeError):
                        url = ""
                for g in self._galleries:
                    slot = g.asset_slots.get(aid)
                    if slot is None:
                        continue
                    if url:
                        slot.set_asset(url)
                    else:
                        slot.label.setText(
                            f"asset #{aid}\nunavailable")

            self._host._get_fetcher().get(dapi.media_asset_url(aid), cb)

    def _distribute(self, posts: list[dapi.PostInfo],
                    path: str) -> None:
        posts = self._host._apply_blacklist(posts)
        by_id = {p.id: p for p in posts}
        self._posts_by_id.update(by_id)
        for g in self._galleries:
            g.fill(by_id)
            missing = [p for p in g.post_ids if p not in by_id]
            if missing:
                g.set_status(
                    f"{len(missing)} example(s) not returned by the "
                    "API \u2014 click them above to try directly.")
            else:
                g.set_status("")
        note = getattr(self, "_fetch_note", None)
        if note is not None:
            note.setText(f"examples fetched via {path}")

    def _set_gallery_status(self, text: str) -> None:
        for g in self._galleries:
            g.set_status(text)

    def _set_reveal_all(self, on: bool) -> None:
        self.reveal_all = on
        for g in self._galleries:
            g.apply_reveal(on)

    def open_inspector_by_id(self, post_id: int) -> None:
        """Open an example from its id alone (the chip path). Uses an
        already-fetched post when we have one, otherwise fetches just
        that post."""
        cached = self._posts_by_id.get(post_id)
        if cached is not None:
            reason = self._host.blocked_reason(cached)
            if reason:
                self._set_gallery_status(
                    f"post #{post_id} is blocked by your blacklist "
                    f"({reason}).")
                return
            if not cached.skip:
                self._host.open_inspector(cached)
                return
        self._set_gallery_status(f"opening post #{post_id}\u2026")

        def cb(payload: bytes | None, status: int) -> None:
            if payload is None:
                self._set_gallery_status(
                    f"Could not open post #{post_id} "
                    f"(status {status}).")
                return
            try:
                posts = dapi.parse_posts(
                    json.loads(payload.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                posts = []
            if not posts:
                self._set_gallery_status(
                    f"post #{post_id} returned no usable data.")
                return
            self._posts_by_id[post_id] = posts[0]
            reason = self._host.blocked_reason(posts[0])
            if reason:
                self._set_gallery_status(
                    f"post #{post_id} is blocked by your blacklist "
                    f"({reason}).")
                return
            self._set_gallery_status("")
            self._host.open_inspector(posts[0])

        self._host._get_fetcher().get(dapi.post_url(post_id), cb)

    def suppress_menus(self) -> None:
        """Called after each render: the wiki document is rebuilt from
        scratch every navigation, and each new label arrives with Qt's
        default right-click menu unless told otherwise."""
        from ui.zoom_view import suppress_context_menus

        suppress_context_menus(self)

    def _clear_doc(self) -> None:
        self._fetch_note = None
        self._anchors = {}
        while self.doc_layout.count():
            item = self.doc_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._galleries = []

    def _set_doc_status(self, text: str) -> None:
        self._clear_doc()
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        self.doc_layout.addWidget(lbl)
        self.doc_layout.addStretch(1)

    def _scroll_to_anchor(self, anchor: str) -> None:
        """Table-of-Contents jump. Long pages (tag groups especially)
        open with a contents box whose entries are in-page anchors."""
        target = self._anchors.get(anchor)
        if target is None:
            return
        self.doc_scroll.ensureWidgetVisible(target, 0, 0)
        bar = self.doc_scroll.verticalScrollBar()
        bar.setValue(min(bar.maximum(),
                         target.mapTo(self.doc_host,
                                      target.rect().topLeft()).y()))

    def _refresh_source_view(self, blocks) -> None:
        """Raw DText plus a one-line parse summary. If examples ever
        fail to appear again, this says immediately whether the source
        contains references at all and whether we recognised them."""
        if not self.source_view.isVisible():
            return
        body = self._last_body
        if not body:
            self.source_view.setPlainText("(no wiki source loaded)")
            return
        if blocks is None:
            blocks = dtext.parse_dtext(body)
        galleries = [b for b in blocks
                     if isinstance(b, dtext.Gallery)]
        found = sum(len(g.post_ids) for g in galleries)
        raw_refs = len(dtext._post_refs(body))
        summary = (f"parsed {len(blocks)} block(s) \u00b7 "
                   f"{len(galleries)} gallery/galleries \u00b7 "
                   f"{found} example id(s) recognised \u00b7 "
                   f"{raw_refs} post reference(s) present in source\n"
                   + "-" * 60 + "\n")
        self.source_view.setPlainText(summary + body)

    def _toggle_eras(self, on: bool) -> None:
        self.era_label.setVisible(on)
        self.era_toggle.setArrowType(
            Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

    def _toggle_source(self, on: bool) -> None:
        self.source_view.setVisible(on)
        if on:
            self._refresh_source_view(None)


class TagReferenceWindow(QWidget):
    # Lets the main window keep its toolbar button in step when this
    # window is closed by its own title bar rather than by the button.
    closed = Signal()

    def __init__(self, settings, fetcher=None) -> None:
        super().__init__(None)          # independent top-level window
        self._settings = settings
        self._state = None
        self._fetcher = fetcher
        # Built first: the navigation row connects straight to the
        # pane's history methods.
        first = _ReferencePane(self)
        # Index 0 is always the LEFT pane and is always the one the
        # navigation row drives; comparing puts a second, pinned pane
        # beside it. Swapping exchanges which tag sits on which side,
        # so the pinned one can be promoted to the followed one.
        self._panes = [first]
        self._live = 0
        self._compare = False
        self._single_width = 0
        self._geom_restored = False
        self._pending_follow: str | None = None
        self._inspectors: list = []
        self._block_rules: list = []
        self._browser = None
        self._browser_close_hooks: list = []
        # Restarted on every cache write, so it only fires once the
        # writes stop.
        self._evict_timer = QTimer(self)
        self._evict_timer.setSingleShot(True)
        self._evict_timer.setInterval(3000)
        self._evict_timer.timeout.connect(
            lambda: self._img_cache.maybe_evict(force=True))
        # Session-only: a fresh start should be able to revisit
        # anything, and persisting it would need pruning of its own.
        self._discovered: set[str] = set()
        self._text_cache = dapi.TextCache(
            settings.cache_dir("danbooru_text"))
        self._img_cache = dapi.ImageCache(
            settings.cache_dir("danbooru_images"),
            cap_bytes=int(getattr(settings, "danbooru_image_cache_mb",
                                  dapi.DEFAULT_DISK_CACHE_MB))
            * 1024 * 1024)

        self.setWindowTitle("Tag Referencer")
        self.setMinimumSize(460, 560)

        layout = QVBoxLayout(self)

        nav = QHBoxLayout()
        self.btn_back = QPushButton("\u25c0")
        self.btn_back.setFixedWidth(34)
        self.btn_back.clicked.connect(self._nav_back)
        self.btn_fwd = QPushButton("\u25b6")
        self.btn_fwd.setFixedWidth(34)
        self.btn_fwd.clicked.connect(self._nav_forward)
        nav.addWidget(self.btn_back)
        nav.addWidget(self.btn_fwd)
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "search a tag (offline autocomplete)\u2026")
        self.search.returnPressed.connect(self._go_search)
        try:
            from ui.tag_autocomplete import TagAutocomplete
            self._ac = TagAutocomplete(self.search)
        except Exception:
            self._ac = None             # autocomplete is a nicety only
        nav.addWidget(self.search, 1)
        self.btn_on_top = QToolButton()
        self.btn_on_top.setText("\U0001F4CC")
        self.btn_on_top.setCheckable(True)
        self.btn_on_top.setToolTip(
            "Always on top \u2014 keep this window above the main "
            "window instead of letting it fall behind while you "
            "work.")
        self.btn_on_top.setChecked(
            bool(getattr(settings, "tag_reference_always_on_top",
                         False)))
        self.btn_on_top.toggled.connect(self._set_always_on_top)
        nav.addWidget(self.btn_on_top)
        from core import bookmarks as bmk
        from ui.bookmark_button import BookmarkButton

        self._bookmarks = bmk.store_for(settings)
        self.btn_discover = QToolButton()
        self.btn_discover.setText("\U0001F3B2")
        self.btn_discover.clicked.connect(self.discover)
        nav.addWidget(self.btn_discover)

        self.btn_bookmark = BookmarkButton(
            "tag", self._bookmarks,
            current=lambda: ((self.search.text() or "").strip(),
                             (self.search.text() or "").strip()),
            activate=lambda b: self.navigate(b.value))
        nav.addWidget(self.btn_bookmark)

        self.btn_browse = QToolButton()
        self.btn_browse.setText("\u27a1")   # hands the tag forward
        self.btn_browse.setToolTip(
            "Send this tag to the Post Browser \u2014 uncurated "
            "search results, showing how the tag is used in practice "
            "rather than only the examples a wiki editor chose.")
        self.btn_browse.clicked.connect(self._send_tag_to_browser)
        nav.addWidget(self.btn_browse)
        self.btn_compare = QToolButton()
        self.btn_compare.setText("\u29c9")
        self.btn_compare.setCheckable(True)
        self.btn_compare.setToolTip(
            "Compare two tags side by side.\n\nThe left pane follows "
            "whatever you look up; the right one stays where you put "
            "it. Ctrl+click a tag in either pane to send it across, "
            "and use \u21c4 to swap which side is which.")
        self.btn_compare.toggled.connect(self.set_compare)
        nav.addWidget(self.btn_compare)
        self.btn_swap = QToolButton()
        self.btn_swap.setText("\u21c4")
        self.btn_swap.setVisible(False)
        self.btn_swap.setToolTip(
            "Swap the two panes, so the tag you pinned becomes the "
            "one that follows.")
        self.btn_swap.clicked.connect(self.swap_panes)
        nav.addWidget(self.btn_swap)
        self.btn_source = QToolButton()
        self.btn_source.setText("{ }")
        self.btn_source.setCheckable(True)
        self.btn_source.setToolTip(
            "Show the raw wiki source for this tag \u2014 useful for "
            "diagnosing anything that renders oddly.")
        self.btn_source.toggled.connect(self._nav_toggle_source)
        nav.addWidget(self.btn_source)
        layout.addLayout(nav)

        self.chk_follow = QCheckBox("Follow the tag")
        self.chk_follow.setToolTip(
            "When on, selecting a tag anywhere in TagWalker looks it "
            "up here automatically. Turn it off to keep this page "
            "put while you carry on working.")
        layout.addWidget(self.chk_follow)
        self.chk_follow.setChecked(True)
        self.btn_discover.setToolTip(self._discovery_tooltip())

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        # Wiki text on both sides otherwise runs right up to the
        # divider and the two columns read as one.
        self._splitter.setHandleWidth(SPLITTER_GAP)
        self._splitter.addWidget(self._panes[0])
        layout.addWidget(self._splitter, 1)
        from ui.zoom_view import suppress_context_menus_forever
        # Kept as an attribute: a filter that is collected stops
        # working, and does so silently.
        self._menu_guard = suppress_context_menus_forever(self)
        self.compare_strip = QLabel("")
        self.compare_strip.setWordWrap(True)
        self.compare_strip.setVisible(False)
        layout.addWidget(self.compare_strip)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(FOLLOW_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._on_follow_debounce)

        self._set_doc_status(
            "Look up a tag to see its Danbooru wiki here.")
        self._update_nav_buttons()

    # ------------------------------------------------------------------
    # State attachment (follow-the-walk)
    # ------------------------------------------------------------------
    def set_state(self, state) -> None:
        if self._state is not None:
            try:
                self._state.remove_listener(self._on_state_change)
            except Exception:
                pass
        self._state = state
        if state is not None:
            state.add_listener(self._on_state_change)

    def _on_state_change(self, change) -> None:
        if change.kind != "tag_selected":
            return
        if not self.chk_follow.isChecked() or not self.isVisible():
            return
        if not change.tag:
            return
        self._pending_follow = change.tag
        self._debounce.start()

    def _on_follow_debounce(self) -> None:
        if self._pending_follow:
            self.navigate(self._pending_follow)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------



    def reset_session(self) -> None:
        """Return to the state a freshly-started program would be in:
        no history, no page, no open examples. Used by Full reset."""
        for pop in list(self._inspectors):
            try:
                pop.close()
            except Exception:
                pass
        self._inspectors = []
        # A reset means "as if freshly started", so Discover should be
        # willing to offer everything again.
        self._discovered = set()
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        # Delegated: assigning self._hist here would create a WINDOW
        # attribute shadowing the pane's, so the pane would keep its
        # history while every reader saw an empty one.
        # Every pane, not just the live one \u2014 and back to a single
        # pane, because that is what a freshly started window is.
        for pane in list(self._panes):
            pane.reset()
        self.set_compare(False)
        self.search.clear()
        self.btn_source.setChecked(False)
        self._update_nav_buttons()

    def _go_search(self) -> None:
        self.navigate(self.search.text())

    def _on_cooc_clicked(self, item: QListWidgetItem) -> None:
        tag = item.data(Qt.ItemDataRole.UserRole)
        if tag:
            self.navigate(tag)

    def _on_doc_link(self, href: str) -> None:
        if href.startswith("tag:"):
            self.navigate(unquote(href[4:]))
        elif href.startswith("post:"):
            try:
                self.open_inspector_by_id(int(href[5:]))
            except (TypeError, ValueError):
                pass
        elif href.startswith("anchor:"):
            self._scroll_to_anchor(unquote(href[7:]))
        elif href.startswith("copy:"):
            self._offer_link(unquote(href[5:]))


    # ------------------------------------------------------------------
    # Offline rendering
    # ------------------------------------------------------------------
    def _selected_era_key(self) -> str:
        choice = getattr(self._settings, "tag_database_choice", "") or ""
        return choice if choice in CSV_PRESETS else DEFAULT_CSV_KEY


    def _dataset_count(self, tag: str):
        """How many loaded images carry this tag (None if no session
        is attached yet)."""
        if self._state is None:
            return None
        try:
            return self._state.get_tag_count_global(tag)
        except Exception:
            return None

    @staticmethod
    def apply_on_top(widget, on: bool) -> None:
        """Set (or clear) always-on-top on a top-level window.

        setWindowFlags() REPLACES the whole flag set, and the
        decoration hints a window got implicitly at creation are not
        all reported back by windowFlags(). Passing that value
        straight back therefore silently drops the close button —
        which is exactly what happened in the field: the window became
        unclosable the moment always-on-top was introduced. So state
        the decorations explicitly, every time. Geometry is preserved
        because re-showing after a flag change can otherwise move the
        window.
        """
        flags = (Qt.WindowType.Window
                 | Qt.WindowType.WindowTitleHint
                 | Qt.WindowType.WindowSystemMenuHint
                 | Qt.WindowType.WindowMinMaxButtonsHint
                 | Qt.WindowType.WindowCloseButtonHint)
        if on:
            # REVERTED, deliberately. Qt.Tool was tried here to make
            # the pin apply only within this application, and it made
            # things worse on Windows: these windows are created
            # PARENTLESS, so a Tool window has no owner to float above
            # and the OS treats it as a palette for the whole app —
            # it vanished whenever the main window was clicked, and
            # two such windows had no defined order between them.
            #
            # Doing it properly needs both windows parented to the
            # main window at construction time, which is a larger
            # change than the problem justifies. This hint is
            # system-wide, which is its only drawback, and it works.
            flags |= Qt.WindowType.WindowStaysOnTopHint
        # Compare the on-top hint specifically. Qt adds implicit flags
        # of its own, so an equality test on the whole set is never
        # true; comparing the window TYPE was equally wrong once the
        # type stopped changing. What actually varies here is one bit.
        hint = Qt.WindowType.WindowStaysOnTopHint
        if bool(widget.windowFlags() & hint) == bool(flags & hint):
            return
        visible = widget.isVisible()
        # Only preserve geometry for a window that is already on
        # screen. Restoring it on a widget that has never been shown
        # pins the pre-layout default size, which is why inspector
        # popups opened SMALLER whenever the reference window was
        # pinned than when it was not.
        geom = widget.saveGeometry() if visible else None
        widget.setWindowFlags(flags)
        if geom is not None:
            widget.restoreGeometry(geom)
        if visible:
            widget.show()        # flag changes hide the window on Win

    def _set_always_on_top(self, on: bool) -> None:
        self.apply_on_top(self, on)
        for pop in self._inspectors:
            # Otherwise an inspector opens BEHIND a pinned reference
            # window and looks stuck.
            self.apply_on_top(pop, on)
        try:
            self._settings.tag_reference_always_on_top = bool(on)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Online half
    # ------------------------------------------------------------------

    def _current_block_rules(self) -> list:
        """Rebuilt on every page load so a Preferences change takes
        effect without restarting."""
        return bl.rules_for(self._settings)

    def blocked_reason(self, post) -> str | None:
        """Why this post must not be shown, or None. Checked BEFORE
        any image request, so a blocked post never reaches the
        network, let alone the screen."""
        if not self._block_rules:
            self._block_rules = self._current_block_rules()
        return bl.blocked_reason(
            getattr(post, "tag_string", ""),
            getattr(post, "rating", ""), self._block_rules)

    def _apply_blacklist(self, posts: list) -> list:
        if not self._block_rules:
            self._block_rules = self._current_block_rules()
        return bl.apply_to_posts(posts, self._block_rules)

    # ------------------------------------------------------------------
    # The live pane
    #
    # Reaching a pane's widgets and history through the window is how
    # every caller and every test already speaks, so that keeps
    # working: anything the window does not define itself is asked of
    # the pane it is currently driving. When a second pane arrives
    # this is the single place that decides which one "the window"
    # means.
    # ------------------------------------------------------------------
    @property
    def _pane(self):
        """The pane the window is driving.

        A property rather than a stored reference: swapping the panes
        moves which object sits on the left, and a stale attribute
        would quietly leave the window addressing the pinned side.
        """
        return self._panes[self._live]

    # Connecting a signal straight to self._go_back would resolve
    # through __getattr__ ONCE, at connect time, and store a bound
    # method of whichever pane existed then. After a swap the button
    # would still drive that pane — which is exactly what happened:
    # Back moved the pinned side instead of the followed one.
    def _nav_back(self) -> None:
        self._pane._go_back()

    def _nav_forward(self) -> None:
        self._pane._go_forward()

    def _nav_toggle_source(self, on: bool) -> None:
        self._pane._toggle_source(on)

    def _live_pane(self):
        return self._pane

    def __getattr__(self, name: str):
        # Only called for attributes the window does not have. The
        # guard matters during __init__, before _pane exists, and for
        # dunders Qt probes on partially built objects.
        if name.startswith("__") or name == "_pane":
            raise AttributeError(name)
        try:
            panes = object.__getattribute__(self, "_panes")
            live = object.__getattribute__(self, "_live")
        except AttributeError:
            raise AttributeError(name) from None
        return getattr(panes[live], name)

    # ------------------------------------------------------------------
    # Compare mode
    # ------------------------------------------------------------------
    def set_compare(self, on: bool) -> None:
        on = bool(on)
        if on == self._compare:
            return
        self._compare = on
        if on:
            second = _ReferencePane(self)
            self._panes.append(second)
            self._splitter.addWidget(second)
            # A whole subtree arrived at once; the watcher only sees
            # children added to widgets it is already filtering.
            from ui.zoom_view import suppress_context_menus_forever
            self._menu_guard_2 = suppress_context_menus_forever(second)
            self._single_width = self.width()
            # Two panes of wiki text need room; one pane's width would
            # squeeze both into uselessness.
            self.resize(max(self.width(), COMPARE_WIDTH), self.height())
            self._splitter.setSizes([1, 1])
            # A pinned pane with nothing in it is a blank half-window,
            # so it starts on whatever the live pane is showing.
            if self._panes[0].tag:
                second.navigate(self._panes[0].tag)
        else:
            second = self._panes.pop()
            second.setParent(None)
            second.deleteLater()
            self._live = 0
            if self._single_width:
                self.resize(self._single_width, self.height())
        self.btn_swap.setVisible(on)
        self.compare_strip.setVisible(on)
        if self.btn_compare.isChecked() != on:
            self.btn_compare.setChecked(on)
        self._mark_panes()
        self._update_comparison()

    def swap_panes(self) -> None:
        """Exchange the sides. The left pane is the one that follows,
        so this promotes the pinned tag to the followed one."""
        if not self._compare:
            return
        self._panes.reverse()
        # Re-inserting at 0 moves a widget within a splitter.
        self._splitter.insertWidget(0, self._panes[0])
        self._mark_panes()
        self._update_comparison()
        live = self._panes[self._live]
        if live.tag:
            self.search.setText(live.tag)
        self._update_nav_buttons()

    def suppress_menus(self) -> None:
        """The window's own chrome plus every pane inside it. Called
        after each load because the wiki document is rebuilt from
        scratch and its new labels arrive with Qt's default menu."""
        from ui.zoom_view import suppress_context_menus

        suppress_context_menus(self)

    def on_pane_loaded(self, pane) -> None:
        self.suppress_menus()
        """A pane finished showing a tag.

        Everything that depends on WHICH tag is on screen updates
        here, in one place: without it the comparison strip kept
        whatever it said when compare mode opened, and went stale the
        moment the live pane moved on.
        """
        if pane is self._panes[self._live]:
            self.search.setText(pane.tag)
            try:
                self.btn_bookmark.refresh()
            except Exception:
                pass
        self._update_comparison()

    def send_to_other_pane(self, source, tag: str) -> None:
        """Ctrl+click. With one pane there is nowhere else to send it,
        so it behaves like an ordinary click rather than doing
        nothing."""
        if not self._compare or len(self._panes) < 2:
            source.navigate(tag)
            return
        other = self._panes[1] if source is self._panes[0] else self._panes[0]
        other.navigate(tag)
        self._update_comparison()

    def _mark_panes(self) -> None:
        for index, pane in enumerate(self._panes):
            if not self._compare:
                pane.role.setVisible(False)
                continue
            following = index == self._live
            pane.role.setText(
                "<b>following</b> \u2014 this pane tracks what you look up"
                if following else
                "<b>pinned</b> \u2014 stays put; Ctrl+click a tag to "
                "send it here")
            pane.role.setVisible(True)

    def _update_comparison(self) -> None:
        """A thin line of the two things worth comparing directly:
        how common each tag is, and how much company they keep."""
        if not self._compare or len(self._panes) < 2:
            return
        left, right = self._panes[0], self._panes[1]
        if not (left.tag and right.tag):
            self.compare_strip.setText(
                "Look up a tag in each pane. Ctrl+click a tag to send "
                "it to the other side.")
            return
        era = self._selected_era_key()
        label = CSV_PRESETS.get(era, ("", era, ""))[1]

        def posts(tag: str):
            try:
                info = tr.lookup(tag, era)
                for entry in info.era_counts:
                    if entry.key == era and entry.count is not None:
                        return entry.count
            except Exception:
                pass
            return None

        a, b = posts(left.tag), posts(right.tag)
        bits = [f"<b>{left.tag}</b> "
                + (f"{a:,}" if a is not None else "\u2014"),
                f"<b>{right.tag}</b> "
                + (f"{b:,}" if b is not None else "\u2014")]
        line = " vs ".join(bits) + f" posts in {label}"
        first, second = set(left.cooc_tags), set(right.cooc_tags)
        if first and second:
            shared = len(first & second)
            union = len(first | second)
            pct = round(100 * shared / union) if union else 0
            line += (f" \u00b7 {pct}% of their usual company is shared "
                     f"({shared} of {union} partners)")
        self.compare_strip.setText(line)

    def _get_fetcher(self):
        if self._fetcher is None:
            from ui.danbooru_fetcher import DanbooruFetcher
            self._fetcher = DanbooruFetcher(self)
        return self._fetcher


    def _doc_label(self, html: str) -> QLabel:
        lbl = QLabel(html)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setOpenExternalLinks(False)
        lbl.linkActivated.connect(self._on_doc_link)
        return lbl





    def add_browser_close_hook(self, slot) -> None:
        """Called when the post browser closes, so a toolbar button
        elsewhere can follow it."""
        self._browser_close_hooks.append(slot)
        if self._browser is not None:
            try:
                self._browser.closed.connect(slot)
            except Exception:
                pass

    def _open_browser(self, send_tag: bool = False) -> None:
        """Show the post browser.

        FIELD REPORT: opening the browser while a tag was on screen
        silently ran a search for it. Two different actions were
        sharing this one method — the arrow button, which MEANS "send
        this tag over", and the toolbar button, which means only "open
        the browser". Merging them made the second one act like the
        first, so simply opening the window fired a network search the
        user never asked for.

        `send_tag` is therefore explicit and defaults to off: opening
        a window is not a request to search.
        """
        from ui.post_browser_window import PostBrowserWindow

        fresh = getattr(self, "_browser", None) is None
        if fresh:
            self._browser = PostBrowserWindow(
                self._settings, self, fetcher=self._fetcher)
            # Whoever opened it wants their button to track it.
            for slot in self._browser_close_hooks:
                try:
                    self._browser.closed.connect(slot)
                except Exception:
                    pass
        self._browser.show()
        self._browser.raise_()
        self._browser.activateWindow()
        if send_tag:
            tag = (self.search.text() or "").strip()
            if tag:
                self._browser.browse(tag)
        return self._browser

    def _send_tag_to_browser(self) -> None:
        """The arrow button: hand the current tag to the browser and
        run it. This is the one path where a search is intended."""
        self._open_browser(send_tag=True)

    def _offer_link(self, url: str) -> None:
        """External links are never opened. Clicking one offers its
        address for the clipboard instead — enough to be useful (many
        artist pages are nothing BUT social links) without the app
        ever launching a browser."""
        if not url:
            return
        menu = QMenu(self)
        shown = url if len(url) <= 70 else url[:67] + "\u2026"
        header = QAction(shown, menu)
        header.setEnabled(False)
        menu.addAction(header)
        menu.addSeparator()
        act = QAction("Copy link", menu)
        act.triggered.connect(
            lambda: QGuiApplication.clipboard().setText(url))
        menu.addAction(act)
        self._link_menu = menu
        # popup() rather than exec(): a modal loop here would block
        # the reference window (and any headless driver) until it is
        # dismissed, for what is only a two-item courtesy menu.
        menu.popup(QCursor.pos())




    def _show_cooc_help(self) -> None:
        from ui.paged_help_dialog import PagedHelpDialog

        self._cooc_help_win = PagedHelpDialog(
            "Commonly tagged together", COOC_HELP_PAGES, self)
        self._cooc_help_win.show()
        self._cooc_help_win.raise_()

    def _blocked_or_aliased(self, tag: str, rules) -> bool:
        """Blocked directly, or an alias of something blocked.

        Without the second half Discover would keep offering aliases
        of blacklisted tags, each landing on a refusal page \u2014 a
        wasted press, and the reason a test flaked before this was
        found."""
        if bl.blocked_tag_reason(tag, rules):
            return True
        try:
            canonical = tr.lookup(tag, self._selected_era_key()).canonical
        except Exception:
            return False
        return bool(canonical and canonical != tag
                    and bl.blocked_tag_reason(canonical, rules))

    def _discovery_tooltip(self) -> str:
        """Says what the button will actually offer, so the filters
        are visible without opening Preferences."""
        from core import discovery as dsc

        s = self._settings
        try:
            era = self._selected_era_key()
            size = len(dsc.pool(era, s.discovery_scope,
                                s.discovery_min_count))
            label = CSV_PRESETS.get(era, ("", era, ""))[1]
            detail = dsc.describe(label, s.discovery_scope,
                                  s.discovery_min_count,
                                  s.discovery_skip_owned, size)
        except Exception:
            detail = ""
        return ("Discover \u2014 look up a tag at random.\n\n"
                + (detail + "\n\n" if detail else "")
                + "Change what it offers in Settings \u2192 Danbooru "
                "lookups \u2192 Discover.")

    def discover(self) -> None:
        """Pick a tag and look it up. One press, no options here \u2014
        the controls live in Preferences so this stays a button."""
        from core import discovery as dsc

        s = self._settings
        owned = None
        if s.discovery_skip_owned and self._state is not None:
            # all_tags is a PROPERTY. Calling it raised, and a broad
            # except swallowed that, so this filter silently did
            # nothing — the failure looked like "no tags matched".
            owned = set(getattr(self._state, "all_tags", ()) or ())
        # Never offer something this window would then refuse to
        # show: a blacklisted tag renders as a blank page, so it is a
        # wasted press rather than a discovery.
        rules = self._current_block_rules()
        tag = dsc.pick(
            self._selected_era_key(),
            s.discovery_scope, s.discovery_min_count,
            seen=self._discovered if s.discovery_no_repeat else (),
            owned=owned, skip_owned=s.discovery_skip_owned,
            blocked=lambda t: self._blocked_or_aliased(t, rules))
        self.btn_discover.setToolTip(self._discovery_tooltip())
        if tag is None:
            self._set_doc_status(
                "Discover found nothing to offer. Lower the minimum "
                "post count, widen the scope, turn off "
                "\u201cskip tags already in my dataset\u201d, or "
                "shorten your blacklist \u2014 all in Settings "
                "\u2192 Danbooru lookups.")
            return
        self._discovered.add(tag.strip().lower())
        self.navigate(tag)









    def thumb_bytes(self, key: str, url: str, cb) -> None:
        """Image bytes via the image cache, fetching on miss. Shared
        by thumbnails and the inspector popup."""
        data = self._img_cache.get(key)
        if data is not None:
            # Deliberately deferred rather than called straight back.
            #
            # A cached page would otherwise decode all twenty images
            # inside one call stack, with no chance for Qt to process
            # a click or repaint between them. Going through the event
            # loop costs nothing and turns one long block into twenty
            # short ones the window can be used between.
            QTimer.singleShot(0, lambda: cb(data))
            return
        if not url:
            cb(None)
            return

        def done(payload: bytes | None, status: int) -> None:
            if payload is not None:
                self._img_cache.put(key, payload)
                # Trim the cache when the window has gone quiet, not
                # while a page is still arriving: the sweep is
                # proportional to the number of cached files and used
                # to run after EVERY write.
                self._evict_timer.start()
            cb(payload)

        self._get_fetcher().get(url, done)

    def open_asset(self, asset_id: int, url: str,
                   caption: str = "") -> None:
        """Open a wiki reference image at full size.

        One at a time, like the post inspector, so a page of outfit
        references cannot bury the window.
        """
        from ui.asset_viewer_dialog import AssetViewerDialog

        for previous in self._inspectors:
            try:
                previous.close()
            except RuntimeError:
                pass
        viewer = AssetViewerDialog(asset_id, caption, self)
        self._inspectors = [viewer]
        if self.btn_on_top.isChecked():
            self.apply_on_top(viewer, True)
        viewer.show()
        viewer.raise_()
        self.thumb_bytes(f"asset_{asset_id}", url, viewer.set_bytes)

    def explain_unopenable(self, message: str) -> None:
        self._pane._set_gallery_status(message)

    def open_inspector(self, post: dapi.PostInfo) -> None:
        reason = self.blocked_reason(post)
        if reason:
            self._set_gallery_status(
                f"post #{post.id} is blocked by your blacklist "
                f"({reason}).")
            return
        from ui.tag_inspector_popup import TagInspectorPopup
        # One at a time. With two panes each offering examples, a
        # popup per click buries the window in seconds, so a new one
        # replaces the last rather than joining it.
        for previous in self._inspectors:
            try:
                previous.close()
            except RuntimeError:
                pass
        pop = TagInspectorPopup(post, self._state, self)
        self._inspectors = [pop]
        if self.btn_on_top.isChecked():
            self.apply_on_top(pop, True)
        pop.show()
        pop.raise_()

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------
    def showEvent(self, event: QShowEvent) -> None:
        if not self._geom_restored:
            self._geom_restored = True
            geom = getattr(self._settings, "tag_reference_geometry",
                           None)
            if geom is not None and not geom.isEmpty():
                self.restoreGeometry(geom)
        # Re-apply the pin on show: a flag change hides and re-shows
        # the window on Windows, and the type can be lost in between.
        # Guarded by the button so that UNPINNING is not immediately
        # undone by the show() inside apply_on_top itself.
        hint = Qt.WindowType.WindowStaysOnTopHint
        pinned = bool(self.windowFlags() & hint)
        if self.btn_on_top.isChecked() != pinned:
            TagReferenceWindow.apply_on_top(
                self, self.btn_on_top.isChecked())

    def hideEvent(self, event) -> None:  # noqa: N802
        """`closed` is emitted here rather than in closeEvent: Qt
        delivers the close event BEFORE hiding the widget, so a
        listener that checks isVisible() would still see it on screen
        and leave its button lit."""
        super().hideEvent(event)
        self.closed.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self._settings.tag_reference_geometry = self.saveGeometry()
        except Exception:
            pass
        for pop in self._inspectors:
            try:
                pop.close()
            except Exception:
                pass
        # The post browser is deliberately NOT closed here: it has its
        # own toolbar button and its own reason to be open, so
        # dismissing this window should not take it away. Full reset
        # still closes both, because that means "forget everything".
        super().closeEvent(event)
