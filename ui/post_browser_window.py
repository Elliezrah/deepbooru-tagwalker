"""
ui/post_browser_window.py

Post browsing: "what else is tagged this way?"

The Tag Referencer answers what a tag MEANS, using the examples wiki
editors chose. This window answers a different question — how the tag
is actually USED across the site — which curated examples cannot,
because five hand-picked images are not a sample. For captioning, both
questions matter.

The distinction is kept visible rather than blurred: results here are
labelled uncurated, and the curated galleries stay where they are.

Deliberate choices:
- Sorting defaults to score, but score is exposure-biased (a 2015 post
  has had a decade to collect votes), so newest / oldest / random are
  all one click away.
- Twenty per page with explicit paging, because near-identical
  variants are usually uploaded consecutively and can fill a page.
- Anonymous searches allow two terms, and a sort metatag spends one.
  Rather than letting a query fail silently, the cost is shown.
- The blacklist applies here from the outset. Uncurated results are
  far likelier to surface blocked content than staff picks are.

Bridged with the Tag Referencer: clicking a tag inside an example
sends the REFERENCER to that tag, so browsing feeds back into reading.
"""
from __future__ import annotations

import json
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QToolButton,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core import blacklist as bl
from core import danbooru_api as dapi
from ui.tag_reference_window import GALLERY_COLS, _ThumbWidget

PAGE_SIZE = 20

# Danbooru limits how deep anonymous pagination can go (roughly the first
# ~1000 pages of any search); paging past this just fails. We never offer
# a "Last" jump beyond it, though the true total post count is still
# shown. Kept a touch conservative.
MAX_REACHABLE_PAGE = 1000

# Searches remembered per session. Generous: the list is two short
# strings per entry and is never persisted.
HISTORY_LIMIT = 50

HELP_PAGES = [
    ("What this window is",
     "Ordinary Danbooru search results \u2014 anyone's posts, in the "
     "order you choose. Not the handful of examples a wiki editor "
     "picked, which is what the Tag Referencer shows.\n\nUse it to "
     "see how a tag is actually USED, across many posts, rather than "
     "what it is defined to mean.\n\nYour blacklist still applies "
     "here. Blocked results are dropped rather than shown as gaps, "
     "and the count of what was hidden is in the status line."),

    ("The two-term budget",
     "Without a Danbooru account a search may use TWO terms. Every "
     "tag costs one \u2014 and so does every metatag, including the "
     "sort order.\n\n"
     "    1girl                        1 term\n"
     "    1girl rating:general         2 terms  \u2713\n"
     "    1girl solo rating:general    3 terms  \u2717\n\n"
     "The sort spends one too, except \u201cNewest first\u201d, "
     "which is Danbooru's own default ordering and therefore free. So "
     "if you want a tag AND a rating filter, set the sort to Newest "
     "first.\n\nThe line above the results shows the running total "
     "as you type, and turns amber when you go over."),

    ("Filtering by rating",
     "Danbooru rates every post. Type the metatag straight into the "
     "search box:\n\n"
     "    rating:general         fully safe for work\n"
     "    rating:sensitive       mildly suggestive \u2014 swimwear,\n"
     "                           revealing clothing, underwear\n"
     "    rating:questionable    between sensitive and explicit\n"
     "    rating:explicit        explicit\n\n"
     "Single letters work too: rating:g, rating:s, rating:q, "
     "rating:e.\n\nPut a minus in front to EXCLUDE instead:\n\n"
     "    -rating:explicit       everything except explicit\n"
     "    -rating:e              the same thing\n\n"
     "NO SPACE after the colon. \u201crating: general\u201d is read "
     "as two separate things and will not filter anything \u2014 it "
     "must be \u201crating:general\u201d.\n\n"
     "\u201crating:safe\u201d is the old name for general, from "
     "before Danbooru split safe into general and sensitive. It still "
     "resolves, but general is the current term."),

    ("Sorting, and other useful filters",
     "The sort dropdown covers the common orderings, but you can "
     "also type them:\n\n"
     "    order:score            highest scoring first\n"
     "    order:random           a random sample\n"
     "    order:id_asc           oldest first\n"
     "    score:>50              only well-received posts\n"
     "    age:<1month            uploaded recently\n\n"
     "A caution about order:score and order:random on a very common "
     "tag: they make the server sort or scan every matching post, "
     "which times out and comes back as an error. \u201cHighly rated "
     "(fast)\u201d uses score:>50 instead \u2014 it filters rather "
     "than sorts, so it survives at any size. Newest and oldest ride "
     "an index and are always cheap.\n\nScore is also biased by "
     "exposure: a post from years ago has had far longer to collect "
     "votes than a good one from last week."),
]


class PostBrowserWindow(QWidget):
    # Lets the main window keep its toolbar button in step when this
    # window is closed by its own title bar rather than by the button.
    closed = Signal()

    def __init__(self, settings, ref_window, fetcher=None) -> None:
        super().__init__(None)          # independent top-level window
        self._settings = settings
        # Declared before any widget is built: _update_history_buttons
        # runs during construction.
        self._hist: list[tuple[str, str]] = []
        self._hpos = -1
        self._replaying = False
        self._ref = ref_window
        self._fetcher = fetcher
        self._img_cache = dapi.ImageCache(
            settings.cache_dir("danbooru_images"))
        self._page = 1
        self._seq = 0
        # Total post count and derived last page for the current search.
        # None = unknown (count not fetched, failed, or beyond Danbooru's
        # deep-pagination limit); navigation degrades gracefully when so.
        self._total_posts: Optional[int] = None
        self._last_page: Optional[int] = None
        self._count_seq = 0
        self._thumbs: dict[int, _ThumbWidget] = {}
        self._inspectors: list = []
        self.reveal_all = bool(getattr(
            settings, "danbooru_reveal_by_default", False))

        self.setWindowTitle("Browse posts")
        self.setMinimumSize(700, 560)
        self.resize(880, 720)

        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("tag (or two)\u2026")
        self.search.returnPressed.connect(self.search_now)
        # Live, so the term cost updates as you type rather than only
        # after a search has already failed.
        self.search.textChanged.connect(self._update_notice)
        bar.addWidget(self.search, 1)
        self.sort = QComboBox()
        for key, label in dapi.SORT_LABELS:
            self.sort.addItem(label, key)
        self.sort.currentIndexChanged.connect(self._on_sort_changed)
        bar.addWidget(self.sort)
        btn = QPushButton("Search")
        btn.clicked.connect(self.search_now)
        bar.addWidget(btn)
        from core import bookmarks as bmk
        from ui.bookmark_button import BookmarkButton

        self._bookmarks = bmk.store_for(settings)
        self.btn_back = QToolButton()
        self.btn_back.setText("\u25c0")
        self.btn_back.setToolTip(
            "Previous search. History is per-session and covers the "
            "query and its sort order.")
        self.btn_back.clicked.connect(self._history_back)
        bar.addWidget(self.btn_back)
        self.btn_fwd = QToolButton()
        self.btn_fwd.setText("\u25b6")
        self.btn_fwd.setToolTip("Next search.")
        self.btn_fwd.clicked.connect(self._history_forward)
        bar.addWidget(self.btn_fwd)

        self.btn_bookmark = BookmarkButton(
            "query", self._bookmarks,
            current=self._current_bookmark,
            activate=self._open_bookmark)
        bar.addWidget(self.btn_bookmark)

        self.btn_export_dir = QToolButton()
        self.btn_export_dir.setText("\U0001F4C1")
        self.btn_export_dir.setToolTip(
            "Open the image export folder \u2014 where "
            "\u201cExport image + tags\u201d writes.")
        self.btn_export_dir.clicked.connect(self._open_export_dir)
        bar.addWidget(self.btn_export_dir)
        self.btn_help = QToolButton()
        self.btn_help.setText("?")
        self.btn_help.setToolTip("How searching here works")
        self.btn_help.clicked.connect(self._show_help)
        bar.addWidget(self.btn_help)
        self.btn_on_top = QToolButton()
        self.btn_on_top.setText("\U0001F4CC")
        self.btn_on_top.setCheckable(True)
        self.btn_on_top.setToolTip(
            "Always on top \u2014 keep this window above the main "
            "window instead of letting it fall behind while you "
            "work.")
        self.btn_on_top.setChecked(
            bool(getattr(settings, "post_browser_always_on_top",
                         True)))
        self.btn_on_top.toggled.connect(self._set_always_on_top)
        bar.addWidget(self.btn_on_top)
        layout.addLayout(bar)

        notice_row = QHBoxLayout()
        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setToolTip(
            "How many search terms this query uses. Press ? for the "
            "full explanation.")
        notice_row.addWidget(self.notice, 1)

        # Small, permanent reminder that tag FORMAT differs here from the
        # rest of the app: Danbooru search uses underscores within a tag
        # and a SPACE to separate two tags (long_hair blue_eyes), no
        # matter which entry format the app is set to. Passive — a hint,
        # not a popup; the full rule is in the tooltip.
        self.lbl_format_tip = QLabel(
            "Tags: use_underscores, space separates two tags")
        self.lbl_format_tip.setProperty("role", "tertiary")
        self.lbl_format_tip.setToolTip(
            "Search here uses Danbooru syntax, which is independent of the "
            "app's tag entry format:\n"
            "\u2022 underscores WITHIN a tag  (long_hair)\n"
            "\u2022 a SPACE SEPARATES two tags  (long_hair blue_eyes)\n\n"
            "So \u201clong hair\u201d searches the single tag \u201clong_hair\u201d "
            "only if written with an underscore; with a space it is read "
            "as two tags, \u201clong\u201d and \u201chair\u201d.")
        notice_row.addWidget(self.lbl_format_tip, 0)
        layout.addLayout(notice_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.host = QWidget()
        self.grid = QGridLayout(self.host)
        self.grid.setSpacing(6)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.host)
        layout.addWidget(self.scroll, 1)

        foot = QHBoxLayout()
        foot.setSpacing(4)

        # Compact page navigation:  |◀ First | ◀10 | ◀ | [page] Go | ▶ | 10▶ | Last ▶|
        self.btn_first = QToolButton()
        self.btn_first.setText("\u25c0 First")
        self.btn_first.setToolTip("Jump to the first page.")
        self.btn_first.clicked.connect(self._go_first)
        foot.addWidget(self.btn_first)

        self.btn_prev10 = QToolButton()
        self.btn_prev10.setText("\u25c010")
        self.btn_prev10.setToolTip("Back 10 pages.")
        self.btn_prev10.clicked.connect(lambda: self._step(-10))
        foot.addWidget(self.btn_prev10)

        self.btn_prev = QToolButton()
        self.btn_prev.setText("\u25c0")
        self.btn_prev.setToolTip("Previous page.")
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        foot.addWidget(self.btn_prev)

        # Jump-to-page box + Go.
        self.page_box = QSpinBox()
        self.page_box.setMinimum(1)
        self.page_box.setMaximum(1)   # widened once a total is known
        self.page_box.setToolTip("Type a page number and press Go (or Enter).")
        self.page_box.setFixedWidth(64)
        self.page_box.lineEdit().returnPressed.connect(self._go_to_typed_page)
        foot.addWidget(self.page_box)

        self.btn_go = QToolButton()
        self.btn_go.setText("Go")
        self.btn_go.setToolTip("Jump to the typed page.")
        self.btn_go.clicked.connect(self._go_to_typed_page)
        foot.addWidget(self.btn_go)

        self.btn_next = QToolButton()
        self.btn_next.setText("\u25b6")
        self.btn_next.setToolTip("Next page.")
        self.btn_next.clicked.connect(lambda: self._step(1))
        foot.addWidget(self.btn_next)

        self.btn_next10 = QToolButton()
        self.btn_next10.setText("10\u25b6")
        self.btn_next10.setToolTip("Forward 10 pages.")
        self.btn_next10.clicked.connect(lambda: self._step(10))
        foot.addWidget(self.btn_next10)

        self.btn_last = QToolButton()
        self.btn_last.setText("Last \u25b6")
        self.btn_last.setToolTip(
            "Jump to the last page. Needs the total post count; disabled "
            "when the count is unknown or beyond Danbooru's page limit.")
        self.btn_last.clicked.connect(self._go_last)
        foot.addWidget(self.btn_last)

        # Page indicator + total count.
        self.lbl_page = QLabel("")
        foot.addWidget(self.lbl_page)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        foot.addWidget(self.status, 1)
        layout.addLayout(foot)

        # Same completion the Tag Referencer offers: typing a tag from
        # memory is the slow part of using this window.
        try:
            from ui.tag_autocomplete import TagAutocomplete
            self._ac = TagAutocomplete(self.search, apply_entry_format=False)
        except Exception:
            self._ac = None

        from ui.zoom_view import suppress_context_menus
        suppress_context_menus(self)
        self._update_history_buttons()
        self._update_notice()
        self._set_status(
            "Enter a tag and press Search, or open this window from a "
            "tag in the Tag Referencer.")
        self._update_paging(0)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def _sort_key(self) -> str:
        return self.sort.currentData() or dapi.DEFAULT_SORT

    def _on_sort_changed(self) -> None:
        self._update_notice()
        if self.search.text().strip():
            self.search_now()

    def _update_notice(self) -> None:
        """Show what the query costs BEFORE it is sent, broken down.

        "2/2 terms" on a single typed tag is baffling until you can
        see that the sort took the second one, so the breakdown is
        spelled out rather than just totalled.
        """
        sort = self._sort_key()
        tag_count = len([t for t in self.search.text().split() if t])
        _query, count = dapi.build_post_query(self.search.text(), sort)
        extra = count - tag_count
        parts = [f"{tag_count} tag" + ("s" if tag_count != 1 else "")]
        if extra:
            parts.append(f"1 for the sort order ({dapi.SORT_ORDERS[sort]})")
        breakdown = " + ".join(parts)
        if count > dapi.ANON_TERM_LIMIT:
            self.notice.setText(
                f"<span style='color:#e08a3c;'><b>Uncurated</b> "
                f"\u00b7 {breakdown} = {count} terms, over the limit "
                f"of {dapi.ANON_TERM_LIMIT}</span>")
        else:
            self.notice.setText(
                f"<b>Uncurated</b> \u00b7 {breakdown} = {count}/"
                f"{dapi.ANON_TERM_LIMIT} terms")

    def browse(self, tags: str) -> None:
        self.search.setText((tags or "").strip())
        self._page = 1
        self._total_posts = None
        self._last_page = None
        self._fetch()
        self._fetch_count()

    def _sync_title(self) -> None:
        query = (self.search.text() or "").strip()
        self.setWindowTitle(
            f"Browse posts \u2014 {query}" if query
            else "Browse posts")

    def search_now(self) -> None:
        self._page = 1
        # A new search invalidates the known total; refetch it.
        self._total_posts = None
        self._last_page = None
        self._fetch()
        self._fetch_count()

    def _fetch_count(self) -> None:
        """Fetch the TOTAL post count for the current query and derive the
        last page. Runs on a new search only (the total doesn't change
        between pages). Best-effort: any failure or unexpected shape
        leaves the total unknown, and navigation degrades to the
        full-page heuristic. Guarded by its own sequence so a stale
        response can't overwrite a newer search's count.
        """
        tags = self.search.text().strip()
        if not tags:
            return
        if not getattr(self._settings, "danbooru_lookups_enabled", False):
            return
        # Don't count a blacklisted search (it isn't fetched anyway).
        rules = bl.rules_for(self._settings)
        for term in tags.split():
            if bl.blocked_tag_reason(term, rules):
                return
        self._count_seq += 1
        seq = self._count_seq
        url = dapi.posts_count_url(tags)

        def cb(payload: bytes | None, status: int) -> None:
            if seq != self._count_seq:
                return
            if payload is None:
                return  # leave total unknown; nav uses the page heuristic
            try:
                total = dapi.parse_post_count(
                    json.loads(payload.decode("utf-8")))
            except Exception:
                total = None
            if total is None:
                return
            self._total_posts = total
            import math
            last = max(1, math.ceil(total / PAGE_SIZE)) if total > 0 else 1
            # Danbooru caps deep pagination for anonymous users; don't
            # offer a Last page beyond that ceiling (paging there would
            # just fail). The count itself still shows.
            capped = min(last, MAX_REACHABLE_PAGE)
            self._last_page = capped
            # Refresh the footer now that the total is known.
            self._update_paging(len(self._thumbs))

        self._get_fetcher().get(url, cb)

    def _step(self, delta: int) -> None:
        target = self._page + delta
        target = max(1, target)
        if self._last_page is not None:
            target = min(target, self._last_page)
        if target == self._page:
            return
        self._page = target
        self._fetch()

    def _go_first(self) -> None:
        if self._page != 1:
            self._page = 1
            self._fetch()

    def _go_last(self) -> None:
        # Only meaningful when the last page is known.
        if self._last_page is None or self._page == self._last_page:
            return
        self._page = self._last_page
        self._fetch()

    def _go_to_typed_page(self) -> None:
        target = max(1, int(self.page_box.value()))
        if self._last_page is not None:
            target = min(target, self._last_page)
        if target == self._page:
            return
        self._page = target
        self._fetch()

    # ------------------------------------------------------------------
    # Fetch + render
    # ------------------------------------------------------------------
    def _get_fetcher(self):
        if self._fetcher is None:
            from ui.danbooru_fetcher import DanbooruFetcher
            self._fetcher = DanbooruFetcher(self)
        return self._fetcher

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _clear_grid(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._thumbs = {}

    def _update_paging(self, count: int) -> None:
        """Refresh the page indicator and enable/disable the nav controls.

        `count` is the number of posts on the CURRENT page — a full page
        is evidence another exists (used when the total is unknown). When
        the total post count is known, navigation is bounded precisely by
        the computed last page.
        """
        page = self._page
        last = self._last_page

        # Page indicator, with total posts and last page when known.
        if self._total_posts is not None and last is not None:
            self.lbl_page.setText(
                f"page {page} / {last}  \u00b7  "
                f"{self._total_posts:,} posts")
        else:
            self.lbl_page.setText(f"page {page}")

        # Widen the jump box to the known range and reflect the current
        # page without retriggering a fetch.
        self.page_box.blockSignals(True)
        self.page_box.setMaximum(last if last is not None else 100000)
        self.page_box.setValue(page)
        self.page_box.blockSignals(False)

        at_first = page <= 1
        # "More pages exist" is either a known last page above us, or —
        # when the total is unknown — a full current page.
        if last is not None:
            has_next = page < last
        else:
            has_next = count >= PAGE_SIZE

        self.btn_first.setEnabled(not at_first)
        self.btn_prev.setEnabled(not at_first)
        self.btn_prev10.setEnabled(not at_first)
        self.btn_next.setEnabled(has_next)
        self.btn_next10.setEnabled(has_next)
        # Last only works when we actually know where last is.
        self.btn_last.setEnabled(last is not None and page < last)

    def _fetch(self) -> None:
        self._update_notice()
        self._sync_title()
        if not self._replaying:
            self._record_history(
                (self.search.text() or "").strip(), self._sort_key())
        try:
            self.btn_bookmark.refresh()
        except Exception:
            pass
        tags = self.search.text().strip()
        if not tags:
            self._set_status("Enter a tag to browse.")
            return
        if not getattr(self._settings, "danbooru_lookups_enabled",
                       False):
            self._clear_grid()
            self._set_status(
                "Danbooru lookups are disabled \u2014 enable them in "
                "Settings \u2192 Danbooru lookups.")
            return
        rules = bl.rules_for(self._settings)
        for term in tags.split():
            reason = bl.blocked_tag_reason(term, rules)
            if reason:
                self._clear_grid()
                self._update_paging(0)
                self._set_status(
                    f"\u201c{term}\u201d is on your blacklist "
                    f"(rule \u201c{reason}\u201d), so it is not "
                    "searched. Every result would be hidden anyway.")
                return
        self._seq += 1
        seq = self._seq
        self._set_status("searching\u2026")
        url = dapi.posts_search_url(tags, self._sort_key(),
                                    self._page, PAGE_SIZE)

        def cb(payload: bytes | None, status: int) -> None:
            if seq != self._seq:
                return
            if payload is None:
                self._clear_grid()
                self._update_paging(0)
                if status == 429:
                    self._set_status(
                        "Danbooru rate limit reached (HTTP 429) \u2014 "
                        "try again shortly.")
                elif (status >= 500
                      and self._sort_key() in dapi.EXPENSIVE_SORTS):
                    # The reported `1girl order:score` failure. The
                    # sort is the cost, not the tag count.
                    self._set_status(
                        f"Danbooru could not sort this search "
                        f"(status {status}). Sorting a very common tag "
                        "by score or at random makes the server scan "
                        "every matching post, which times out. "
                        "\u201cHighly rated (fast)\u201d filters "
                        "instead of sorting and works at any size; "
                        "newest and oldest are always cheap.")
                else:
                    self._set_status(f"Search failed (status {status}).")
                return
            try:
                posts = dapi.parse_posts(
                    json.loads(payload.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                self._clear_grid()
                self._update_paging(0)
                self._set_status("Unexpected response from Danbooru.")
                return
            self._render(posts)

        self._get_fetcher().get(url, cb)

    def _render(self, posts: list) -> None:
        rules = bl.rules_for(self._settings)
        fetched = len(posts)
        posts = bl.apply_to_posts(posts, rules)
        # Dropped outright rather than shown as placeholders: a grid
        # of "blocked" tiles wastes the space the results need, and
        # deleted/previewless posts are equally useless here.
        # Deleted posts are kept in the Tag Referencer, where an editor
        # chose them deliberately. Here the results are uncurated, so a
        # deleted post is only noise.
        posts = [p for p in posts
                 if not p.skip and not getattr(p, "deleted", False)]
        hidden = fetched - len(posts)
        self._clear_grid()
        images_on = bool(getattr(self._settings,
                                 "danbooru_show_images", False))
        if not posts:
            self._update_paging(fetched)
            if hidden:
                self._set_status(
                    f"All {hidden} result(s) on this page were hidden "
                    "\u2014 blocked by your blacklist, deleted, or in "
                    "a format this app cannot display (AVIF, JPEG XL, "
                    "PSD, Flash, ZIP-packed animation). Try the next "
                    "page.")
            else:
                self._set_status(
                    "No posts found. Check the tag spelling.")
            return
        if not images_on:
            ids = ", ".join(f"post #{p.id}" for p in posts)
            label = QLabel(
                f"{len(posts)} result(s). Thumbnails are off "
                f"(Settings \u2192 Danbooru lookups):\n{ids}")
            label.setWordWrap(True)
            self.grid.addWidget(label, 0, 0)
        else:
            for i, post in enumerate(posts):
                thumb = _ThumbWidget(post.id, self)
                self._thumbs[post.id] = thumb
                cols = self._columns()
                self.grid.addWidget(thumb, i // cols,
                                    i % cols)
                thumb.set_post(post)
        # New thumbnails were just built.
        from ui.zoom_view import suppress_context_menus
        suppress_context_menus(self)
        note = f"{len(posts)} result(s)"
        if hidden:
            note += f" \u00b7 {hidden} hidden"
        self._set_status(note)
        # Paging is judged on what Danbooru RETURNED, not on what
        # survived filtering — otherwise a page full of blocked posts
        # would look like the end of the results.
        self._update_paging(fetched)

    # ------------------------------------------------------------------
    # Services the shared thumbnail widget expects
    # ------------------------------------------------------------------
    def thumb_bytes(self, key: str, url: str, cb) -> None:
        data = self._img_cache.get(key)
        if data is not None:
            cb(data)
            return
        if not url:
            cb(None)
            return

        def done(payload: bytes | None, status: int) -> None:
            if payload is not None:
                self._img_cache.put(key, payload)
            cb(payload)

        self._get_fetcher().get(url, done)

    def blocked_reason(self, post) -> str | None:
        return bl.blocked_reason(
            getattr(post, "tag_string", ""),
            getattr(post, "rating", ""), bl.rules_for(self._settings))

    def open_inspector(self, post) -> None:
        reason = self.blocked_reason(post)
        if reason:
            self._set_status(
                f"post #{post.id} is blocked by your blacklist "
                f"({reason}).")
            return
        from ui.tag_inspector_popup import TagInspectorPopup

        state = getattr(self._ref, "_state", None)
        pop = TagInspectorPopup(post, state, self)
        self._inspectors = [p for p in self._inspectors
                            if p.isVisible()]
        self._inspectors.append(pop)
        if self.btn_on_top.isChecked():
            from ui.tag_reference_window import TagReferenceWindow
            TagReferenceWindow.apply_on_top(pop, True)
        pop.show()
        pop.raise_()

    def navigate(self, tag: str) -> None:
        """Tag clicks inside an example belong to the READER, so they
        are handed to the Tag Referencer rather than searched here."""
        if self._ref is None:
            return
        try:
            self._ref.show()
            self._ref.raise_()
            self._ref.navigate(tag)
        except Exception:
            pass

    def _current_bookmark(self):
        """What a saved search needs to be reproducible: the query
        text and the ordering it was run with."""
        query = (self.search.text() or "").strip()
        if not query:
            return None
        sort = self._sort_key()
        label = query
        if sort and sort != dapi.DEFAULT_SORT:
            label = f"{query}  [{dict(dapi.SORT_LABELS)[sort]}]"
        return (query, label, sort)

    def _open_bookmark(self, entry) -> None:
        sort = entry.sort or dapi.DEFAULT_SORT
        for i in range(self.sort.count()):
            if self.sort.itemData(i) == sort:
                self.sort.blockSignals(True)
                self.sort.setCurrentIndex(i)
                self.sort.blockSignals(False)
                break
        self.browse(entry.value)

    def open_inspector_by_id(self, post_id) -> None:
        """Open a post from its id alone \u2014 the path a saved post
        bookmark takes, with no search results to pick it out of."""
        def cb(payload, status):
            posts = []
            if payload is not None:
                try:
                    posts = dapi.parse_posts(
                        json.loads(payload.decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    posts = []
            if not posts:
                self._set_status(
                    f"Could not open post #{post_id} "
                    f"(status {status}).")
                return
            self.open_inspector(posts[0])

        self._get_fetcher().get(dapi.post_url(int(post_id)), cb)

    # ------------------------------------------------------------------
    # Search history
    #
    # The Tag Referencer has had this since it was built; the browser
    # went without, so refining a query meant retyping the one before
    # it. A search is the pair (query, sort): returning to a search
    # that silently reordered itself would not be returning to it.
    # ------------------------------------------------------------------
    def _record_history(self, query: str, sort: str) -> None:
        entry = (query, sort)
        if self._hist and self._hpos >= 0 and self._hist[self._hpos] == entry:
            return
        # A new search after going back discards the forward tail,
        # exactly as a browser does.
        del self._hist[self._hpos + 1:]
        self._hist.append(entry)
        if len(self._hist) > HISTORY_LIMIT:
            self._hist.pop(0)
        self._hpos = len(self._hist) - 1
        self._update_history_buttons()

    def _goto_history(self, index: int) -> None:
        if not (0 <= index < len(self._hist)):
            return
        self._hpos = index
        query, sort = self._hist[index]
        for i in range(self.sort.count()):
            if self.sort.itemData(i) == sort:
                self.sort.blockSignals(True)
                self.sort.setCurrentIndex(i)
                self.sort.blockSignals(False)
                break
        self.search.setText(query)
        self._page = 1
        self._replaying = True
        try:
            self._fetch()
        finally:
            self._replaying = False
        self._update_history_buttons()

    def _history_back(self) -> None:
        self._goto_history(self._hpos - 1)

    def _history_forward(self) -> None:
        self._goto_history(self._hpos + 1)

    def _update_history_buttons(self) -> None:
        self.btn_back.setEnabled(self._hpos > 0)
        self.btn_fwd.setEnabled(self._hpos < len(self._hist) - 1)
        if self._hist and 0 <= self._hpos < len(self._hist):
            self.btn_back.setToolTip(
                f"Previous search ({self._hpos} behind)"
                if self._hpos else "No earlier search")
            ahead = len(self._hist) - self._hpos - 1
            self.btn_fwd.setToolTip(
                f"Next search ({ahead} ahead)" if ahead
                else "No later search")

    def _columns(self) -> int:
        """Thumbnails per row. Five fills the default window; the
        page size is unchanged at 20 either way."""
        try:
            return int(getattr(self._settings,
                               "post_browser_columns", 5)) or 5
        except (TypeError, ValueError):
            return 5

    def _open_export_dir(self) -> None:
        """Reveal the export folder in the system file manager. The
        one place this app hands off to the OS \u2014 and it is a
        folder, never a web address."""
        from pathlib import Path

        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        target = (getattr(self._settings, "danbooru_export_dir", "")
                  or "").strip()
        if not target or not Path(target).is_dir():
            self._set_status(
                "No export folder set \u2014 Settings \u2192 Danbooru "
                "lookups \u2192 Export folder.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(target))))

    def _show_help(self) -> None:
        from ui.paged_help_dialog import PagedHelpDialog

        self._help = PagedHelpDialog(
            "Post Browser", HELP_PAGES, self)
        self._help.show()
        self._help.raise_()

    def _set_always_on_top(self, on: bool) -> None:
        """Reuses the Tag Referencer's flag handling, which states
        every decoration explicitly \u2014 feeding windowFlags() back
        silently drops the close button."""
        from ui.tag_reference_window import TagReferenceWindow

        TagReferenceWindow.apply_on_top(self, on)
        for pop in self._inspectors:
            TagReferenceWindow.apply_on_top(pop, on)
        try:
            self._settings.post_browser_always_on_top = bool(on)
        except Exception:
            pass

    def showEvent(self, event) -> None:  # noqa: N802
        if self.btn_on_top.isChecked() and not (
                self.windowFlags()
                & Qt.WindowType.WindowStaysOnTopHint):
            from ui.tag_reference_window import TagReferenceWindow
            TagReferenceWindow.apply_on_top(self, True)
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        """`closed` is emitted here rather than in closeEvent: Qt
        delivers the close event BEFORE hiding the widget, so a
        listener that checks isVisible() would still see it on screen
        and leave its button lit."""
        super().hideEvent(event)
        self.closed.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        for pop in list(self._inspectors):
            try:
                pop.close()
            except Exception:
                pass
        self._inspectors = []
        super().closeEvent(event)
