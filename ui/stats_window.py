"""
ui/stats_window.py

Statistics window (Pass D).

A separate window opened from View → Statistics. Tabbed:

  Overview      — large 1000-dot project-completion swirl + headline
                  numbers (decisions, images, tags, completed, skipped).
  Tag frequency — horizontal bar chart of tag counts, filterable by
                  subfolder, sortable most/least frequent.
  Co-occurrence — pick a tag, see its top and bottom co-occurring tags.

(Progress-over-time is intentionally deferred: it needs a persisted
timestamped decision log, a separate subsystem not yet built. The tab
is omitted rather than shown empty.)

The window reads live from the session state and subscribes to state
changes, so everything stays current while the user works. It owns no
persisted data — all figures derive from the in-memory decisions and
reconstruct correctly after a save/load round-trip.

Completion model (state.get_project_completion, "Option C"):
- denominator = total_tags x non_orphan_images (stable)
- a (image, tag) pair is decided if it has a YES/NO decision OR its
  tag is manually marked complete; skip does not count.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Spacing
from core.state import SessionState, StateChange, TagStatus
from ui.bar_chart import BarChart
from ui.swirl_widget import SwirlWidget


STATS_SWIRL_SIZE = 600
STATS_SWIRL_DOTS = 1000

# Root subfolder is stored as "" internally; show this label instead.
ROOT_LABEL = "(root)"
ALL_FOLDERS_LABEL = "All folders"


# The standing explanation under the co-occurrence panes.
#
# Held as a constant rather than written once at construction: the
# label is also used to report a tag Danbooru has no data for, and
# without something to restore, that message stuck permanently the
# moment a unique tag was selected. Every path through the refresh now
# sets this label, so it can never be left saying something about a
# tag the user has moved on from.
COOCCUR_HELP = (
    "The two right-hand panes compare your captions against "
    "Danbooru's habits, in percentage points.\n"
    "\u2022 <b>You pair more</b>: the signature of what you are "
    "teaching \u2014 or an accidental correlation that will be baked "
    "into it.\n"
    "\u2022 <b>Danbooru pairs more</b>: something the base model "
    "expects alongside this tag that your captions mostly leave "
    "unwritten.")


def _reverse_share(tag: str, partner: str):
    """How often `partner` accompanies `tag`, worked out from the
    PARTNER's stored list.

    Only thirty partners are stored per tag, so a hub tag's own list
    is all ubiquitous entries and ordinary partners fall off it. But
    the pair is usually recorded from the other side, and

        p(partner | tag) = p(tag | partner) x posts(partner)
                                            / posts(tag)

    recovers it. Checked against twelve pairs stored in both
    directions: every one agreed within a few points.

    Returns None when the partner's list does not know this tag
    either, which is the only case where "new" is honest.
    """
    from core import tag_reference as tr
    from core.cooccurrence import get_db

    try:
        rows = get_db().get_cooccurring(partner, limit=99)
    except Exception:
        return None
    share = next((p for name, _o, p in rows if name == tag), None)
    if share is None:
        return None

    def posts(name: str) -> int:
        try:
            info = tr.lookup(name, "mid2024")
        except Exception:
            return 0
        for era in info.era_counts:
            if era.key == "mid2024" and era.count:
                return era.count
        return 0

    tag_posts, partner_posts = posts(tag), posts(partner)
    if not tag_posts or not partner_posts:
        return None
    return min(1.0, share * partner_posts / tag_posts)


def _folder_of(entry) -> str:
    """Which folder an image belongs to, however the dataset was
    loaded.

    One root with subfolders records the subfolder on the entry. But
    several roots loaded at once are each scanned root-only, so their
    subfolder is empty and the folder that matters is the image's own
    directory. Reading only the first would have shown nothing at all
    for the multi-folder case, which is precisely the case this panel
    exists for.
    """
    subfolder = getattr(entry, "subfolder", "") or ""
    if subfolder:
        return subfolder
    path = getattr(entry, "image_path", None)
    return path.parent.name if path is not None else ""


def _repeats_from_folder_name(entry) -> int:
    """kohya reads a leading ``N_`` on a folder name as a repeat count,
    so ``5_character`` means every image in it is seen five times.

    Parsed rather than asked for: it is already encoded in the folder
    name of anyone using the convention, and anyone who is not gets 1,
    which is correct for them.
    """
    from core.prune_advisor import parse_repeats

    leaf = _folder_of(entry).rsplit("/", 1)[-1]
    try:
        return max(1, int(parse_repeats(leaf) or 1))
    except Exception:
        return 1


class StatsWindow(QDialog):
    """Project statistics window. Created once, reused, re-attached on
    session load.
    """

    # Comfortable default size on a normal screen — large enough to show
    # the 600px project swirl plus the numbers column without scrolling.
    PREFERRED_WIDTH = 940
    PREFERRED_HEIGHT = 720

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("TagWalker — Statistics")
        self.setModal(False)
        # A small absolute floor so the window CAN shrink on tiny screens
        # (the scroll area in _build_ui lets content scroll rather than
        # clip). This is only the floor — the actual open size is chosen
        # per-screen in _apply_initial_size(), so on a normal monitor the
        # window opens at the comfortable PREFERRED size, not this floor.
        self.setMinimumSize(420, 360)

        self._state: Optional[SessionState] = None
        self._build_ui()
        self._apply_initial_size()

    def _apply_initial_size(self) -> None:
        """Choose the open size based on the available screen.

        On a screen that can comfortably fit the preferred size, open at
        the preferred size (the previous behavior users expect). On a
        smaller screen, open at ~90% of the available area, clamped to
        the minimum — so the window always opens fully on-screen and
        never larger than the display. The scroll area handles any
        overflow on very small screens.
        """
        from PySide6.QtGui import QGuiApplication
        w = self.PREFERRED_WIDTH
        h = self.PREFERRED_HEIGHT
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            # Cap at 90% of the available area so the window never fills
            # the whole screen edge-to-edge, but never below the minimum.
            max_w = int(avail.width() * 0.90)
            max_h = int(avail.height() * 0.90)
            w = max(self.minimumWidth(), min(w, max_w))
            h = max(self.minimumHeight(), min(h, max_h))
        self.resize(w, h)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL, Spacing.NORMAL,
        )

        self._tabs = QTabWidget()
        self._overview_tab = self._build_overview_tab()
        self._freq_tab = self._build_frequency_tab()
        self._cooccur_tab = self._build_cooccur_tab()
        # Wrap each tab's content in a scroll area. On a small or short
        # window (e.g. a laptop screen), the large project swirl and the
        # stats columns no longer get clipped or force an oversized
        # minimum — the user can scroll to reach everything. On a big
        # screen the scroll bars simply don't appear.
        self._tabs.addTab(self._scrollable(self._overview_tab), "Overview")
        self._tabs.addTab(self._scrollable(self._freq_tab), "Tag frequency")
        self._tabs.addTab(self._scrollable(self._cooccur_tab), "Co-occurrence")
        self._dataset_tab = self._build_dataset_tab()
        self._tabs.addTab(self._scrollable(self._dataset_tab),
                          "Dataset health")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self._tabs)

    def _scrollable(self, inner: QWidget) -> QScrollArea:
        """Wrap a tab widget in a vertical/horizontal scroll area."""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(inner)
        return area

    # ---- Overview tab ----
    def _build_overview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(
            Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE,
        )
        layout.setSpacing(Spacing.LOOSE)

        title = QLabel("Project completion")
        title.setProperty("role", "heading")
        layout.addWidget(title)

        content = QHBoxLayout()
        content.setSpacing(Spacing.LOOSE)

        self._swirl = SwirlWidget(
            size=STATS_SWIRL_SIZE,
            dot_count=STATS_SWIRL_DOTS,
            project_mode=True,
        )
        content.addWidget(self._swirl, 0, Qt.AlignmentFlag.AlignTop)

        numbers = QVBoxLayout()
        numbers.setSpacing(Spacing.NORMAL)
        numbers.addStretch(1)

        self._pct_label = QLabel("0.00%")
        self._pct_label.setStyleSheet(
            f"font-size: 48pt; font-weight: bold; color: {Colors.SUCCESS_GREEN};"
        )
        numbers.addWidget(self._pct_label)

        self._pct_caption = QLabel("of all image-tag decisions made")
        self._pct_caption.setProperty("role", "tertiary")
        numbers.addWidget(self._pct_caption)

        numbers.addSpacing(Spacing.LOOSE)

        self._jobs_label = QLabel("Decisions: 0 / 0")
        numbers.addWidget(self._jobs_label)
        self._images_label = QLabel("Images: 0")
        numbers.addWidget(self._images_label)
        self._tags_label = QLabel("Tags: 0")
        numbers.addWidget(self._tags_label)
        self._tags_done_label = QLabel("Tags completed: 0")
        numbers.addWidget(self._tags_done_label)
        self._skipped_label = QLabel("Skipped (review later): 0")
        numbers.addWidget(self._skipped_label)

        numbers.addStretch(2)
        content.addLayout(numbers, 1)
        layout.addLayout(content)
        return tab

    # ---- Tag-frequency tab ----
    def _build_frequency_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(
            Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE,
        )
        layout.setSpacing(Spacing.NORMAL)

        # Controls row: subfolder filter + sort direction.
        controls = QHBoxLayout()
        controls.setSpacing(Spacing.NORMAL)
        controls.addWidget(QLabel("Folder:"))
        self._freq_folder = QComboBox()
        self._freq_folder.currentIndexChanged.connect(
            lambda _i: self._refresh_frequency()
        )
        controls.addWidget(self._freq_folder)

        controls.addWidget(QLabel("Sort:"))
        self._freq_sort = QComboBox()
        self._freq_sort.addItem("Most frequent", "desc")
        self._freq_sort.addItem("Least frequent", "asc")
        self._freq_sort.currentIndexChanged.connect(
            lambda _i: self._refresh_frequency()
        )
        controls.addWidget(self._freq_sort)
        controls.addStretch(1)

        self._freq_count_label = QLabel("")
        self._freq_count_label.setProperty("role", "tertiary")
        controls.addWidget(self._freq_count_label)
        layout.addLayout(controls)

        # Scrollable bar chart.
        self._freq_chart = BarChart(bar_color=Colors.ACCENT_BLUE)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._freq_chart)
        layout.addWidget(scroll, 1)
        return tab

    # ---- Co-occurrence tab ----
    def _build_cooccur_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(
            Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE, Spacing.LOOSE,
        )
        layout.setSpacing(Spacing.NORMAL)

        controls = QHBoxLayout()
        controls.setSpacing(Spacing.NORMAL)
        controls.addWidget(QLabel("Tag:"))
        self._cooccur_tag = QComboBox()
        self._cooccur_tag.setEditable(True)  # type-to-search
        self._cooccur_tag.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._cooccur_tag.currentIndexChanged.connect(
            lambda _i: self._refresh_cooccur()
        )
        controls.addWidget(self._cooccur_tag, 1)

        # Too-common suppression threshold. A co-occurring tag that
        # appears on more than this % of all images is hidden (filters
        # out ubiquitous tags like 1girl that co-occur with everything).
        # 100% disables the filter. Independent of the main-page hint
        # threshold so the user can tune the stats view separately.
        controls.addWidget(QLabel("Hide tags on >"))
        self._cooccur_threshold = QComboBox()
        for pct in (10, 25, 50, 75, 90, 100):
            label = "100% (off)" if pct == 100 else f"{pct}%"
            self._cooccur_threshold.addItem(label, pct)
        # Default to 50% — a sensible suppression for typical datasets.
        idx = self._cooccur_threshold.findData(50)
        if idx >= 0:
            self._cooccur_threshold.setCurrentIndex(idx)
        self._cooccur_threshold.currentIndexChanged.connect(
            lambda _i: self._refresh_cooccur()
        )
        controls.addWidget(self._cooccur_threshold)
        controls.addWidget(QLabel("of images"))
        layout.addLayout(controls)

        # Two side-by-side charts: top (most) and bottom (least).
        charts = QHBoxLayout()
        charts.setSpacing(Spacing.LOOSE)

        top_col = QVBoxLayout()
        top_title = QLabel("Most often appears with")
        top_title.setProperty("role", "tertiary")
        top_col.addWidget(top_title)
        self._cooccur_top = BarChart(bar_color=Colors.SUCCESS_GREEN)
        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setWidget(self._cooccur_top)
        top_col.addWidget(top_scroll, 1)
        charts.addLayout(top_col, 1)

        # FIELD REPORT: this pane used to show "Least often appears
        # with", which was noise by construction — the bottom of a long
        # tail is arbitrary, so it listed whichever rare tags happened
        # to land there.
        #
        # The useful question is not which partners are rare, but which
        # ones YOUR captions treat differently from the site the base
        # model learned on.
        bottom_col = QVBoxLayout()
        bottom_title = QLabel("You pair these more than Danbooru does")
        bottom_title.setProperty("role", "tertiary")
        bottom_col.addWidget(bottom_title)
        self._cooccur_bottom = BarChart(bar_color=Colors.SUCCESS_GREEN)
        bottom_scroll = QScrollArea()
        bottom_scroll.setWidgetResizable(True)
        bottom_scroll.setWidget(self._cooccur_bottom)
        bottom_col.addWidget(bottom_scroll, 1)
        charts.addLayout(bottom_col, 1)

        missing_col = QVBoxLayout()
        missing_title = QLabel("Danbooru pairs these more than you do")
        missing_title.setProperty("role", "tertiary")
        missing_col.addWidget(missing_title)
        self._cooccur_missing = BarChart(bar_color=Colors.WARNING_AMBER)
        missing_scroll = QScrollArea()
        missing_scroll.setWidgetResizable(True)
        missing_scroll.setWidget(self._cooccur_missing)
        missing_col.addWidget(missing_scroll, 1)
        charts.addLayout(missing_col, 1)

        layout.addLayout(charts, 1)
        # Qt's auto-detection only guesses rich text when the string
        # LOOKS like markup from its first characters; this one opens
        # with a sentence, so the <b> tags were drawn literally.
        self._cooccur_note = QLabel(COOCCUR_HELP)
        self._cooccur_note.setTextFormat(Qt.TextFormat.RichText)
        self._cooccur_note.setWordWrap(True)
        layout.addWidget(self._cooccur_note)
        return tab

    # ------------------------------------------------------------------
    # State binding
    # ------------------------------------------------------------------

    def attach(self, state: Optional[SessionState]) -> None:
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        self._state = state
        if state is not None:
            state.add_listener(self._on_state_change)
        self._swirl.attach(state)
        self._repopulate_selectors()
        self._refresh_all()

    def detach(self) -> None:
        self.attach(None)

    def _on_state_change(self, change: StateChange) -> None:
        kind = change.kind
        # Cheap, frequent events (every Yes/No during a walk): refresh
        # only the Overview numbers. The swirl manages its own redraw.
        # We deliberately do NOT recompute the frequency / co-occurrence
        # charts here — those iterate the whole dataset and would add
        # noticeable per-keystroke lag on large sets, yet their data
        # only changes when the TAG SET changes, not when individual
        # decisions are made.
        if kind in (
            "walk_advanced", "walk_ended", "tag_changed",
            "filter_changed", "tag_selected",
        ):
            self._refresh_numbers()
            return

        # Structural changes (tags added/removed, session reload, or a
        # granular per-image tag edit): the charts' underlying data may
        # have changed, so refresh everything and rebuild selectors.
        if kind in ("tree_rebuilt", "image_changed"):
            if kind == "tree_rebuilt":
                self._repopulate_selectors()
            self._refresh_all()

    def _on_tab_changed(self, index: int) -> None:
        """Refresh the newly-shown tab's data on demand.

        Because frequent decision events skip the expensive charts (see
        _on_state_change), a chart tab could be stale when the user
        switches to it. Refreshing on tab activation guarantees it's
        current without paying the cost on every keystroke.
        """
        widget = self._tabs.widget(index)
        if widget is self._freq_tab:
            self._refresh_frequency()
        elif widget is self._cooccur_tab:
            self._refresh_cooccur()
        elif widget is self._dataset_tab:
            self._refresh_dataset()
        elif widget is self._overview_tab:
            self._refresh_numbers()

    def _repopulate_selectors(self) -> None:
        """Refill the folder and co-occurrence-tag dropdowns from state.

        Guarded so the currentIndexChanged signals don't trigger a
        cascade of refreshes while we're rebuilding the lists.
        """
        # Folder filter.
        self._freq_folder.blockSignals(True)
        self._cooccur_tag.blockSignals(True)
        try:
            prev_folder = self._freq_folder.currentData()
            self._freq_folder.clear()
            self._freq_folder.addItem(ALL_FOLDERS_LABEL, None)
            if self._state is not None:
                for sub in self._state.get_subfolders():
                    label = ROOT_LABEL if sub == "" else sub
                    self._freq_folder.addItem(label, sub)
            # Restore prior selection if still present.
            idx = self._freq_folder.findData(prev_folder)
            if idx >= 0:
                self._freq_folder.setCurrentIndex(idx)

            prev_tag = self._cooccur_tag.currentText()
            self._cooccur_tag.clear()
            if self._state is not None:
                for tg in sorted(self._state.all_tags, key=str.lower):
                    self._cooccur_tag.addItem(tg)
            if prev_tag:
                i = self._cooccur_tag.findText(prev_tag)
                if i >= 0:
                    self._cooccur_tag.setCurrentIndex(i)
        finally:
            self._freq_folder.blockSignals(False)
            self._cooccur_tag.blockSignals(False)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _build_dataset_tab(self) -> QWidget:
        """What the dataset looks like as a training artifact.

        The other tabs report on the WALK — how far through the work
        you are. This one answers the question that decides whether
        training succeeds: is the data any good.
        """
        from ui.segment_bar import SegmentBar

        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel(
            "Everything here is counted, not estimated \u2014 the same "
            "tokeniser your trainer uses, and the tag snapshot chosen "
            "in Settings.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- caption length -------------------------------------
        layout.addWidget(self._section("Caption length"))
        self._ds_caption_note = QLabel("")
        self._ds_caption_note.setWordWrap(True)
        layout.addWidget(self._ds_caption_note)
        self._ds_caption_chart = BarChart(bar_color=Colors.ACCENT_BLUE)
        layout.addWidget(self._ds_caption_chart)

        # --- tags per image -------------------------------------
        layout.addWidget(self._section("Tags per image"))
        self._ds_density_note = QLabel("")
        self._ds_density_note.setWordWrap(True)
        layout.addWidget(self._ds_density_note)
        self._ds_density_chart = BarChart(bar_color=Colors.SUCCESS_GREEN)
        layout.addWidget(self._ds_density_chart)

        # --- how often each tag appears -------------------------
        layout.addWidget(self._section("How often each tag appears"))
        self._ds_freq_note = QLabel("")
        self._ds_freq_note.setWordWrap(True)
        layout.addWidget(self._ds_freq_note)
        self._ds_freq_chart = BarChart(bar_color=Colors.WARNING_AMBER)
        layout.addWidget(self._ds_freq_chart)

        # --- vocabulary against the base model ------------------
        layout.addWidget(self._section("Your vocabulary vs the base model"))
        self._ds_vocab_note = QLabel("")
        self._ds_vocab_note.setWordWrap(True)
        layout.addWidget(self._ds_vocab_note)
        self._ds_vocab_bar = SegmentBar()
        layout.addWidget(self._ds_vocab_bar)

        # --- folders: only when there is something to say -------
        self._ds_folder_section = self._section("Folders and repeats")
        layout.addWidget(self._ds_folder_section)
        self._ds_folder_note = QLabel("")
        self._ds_folder_note.setWordWrap(True)
        layout.addWidget(self._ds_folder_note)
        self._ds_folder_chart = BarChart(bar_color=Colors.ACCENT_BLUE)
        layout.addWidget(self._ds_folder_chart)

        layout.addStretch(1)
        return page

    def _section(self, title: str) -> QLabel:
        label = QLabel(f"<b>{title}</b>")
        label.setContentsMargins(0, 14, 0, 2)
        return label

    def _refresh_dataset(self) -> None:
        from core import clip_token_counter as ctc
        from core import dataset_stats as dstats
        from core import tag_reference as tr
        from core.tag_database import CSV_PRESETS, DEFAULT_CSV_KEY

        for widget in (self._ds_folder_section, self._ds_folder_note,
                       self._ds_folder_chart):
            widget.setVisible(False)
        if self._state is None:
            return

        image_tags = {e: self._state.get_image_tags(e.image_path)
                      for e in self._state.all_images}
        # Reads the same persisted preference the Tag Referencer and
        # the pruning advisor use, so all three judge against one era.
        from config.settings import Settings
        settings = getattr(self, "_settings", None) or Settings()
        era = getattr(settings, "tag_database_choice", "") or ""
        if era not in CSV_PRESETS:
            era = DEFAULT_CSV_KEY
        era_label = CSV_PRESETS.get(era, ("", era, ""))[1]

        counts: dict = {}

        def posts_for(tag: str) -> int:
            if tag not in counts:
                try:
                    info = tr.lookup(tag, era)
                    found = [c.count for c in info.era_counts
                             if c.key == era and c.count is not None]
                    counts[tag] = found[0] if found else 0
                except Exception:
                    counts[tag] = 0
            return counts[tag]

        try:
            ctc.ensure_loaded()
            token_count = ctc.count_tokens
        except Exception:
            # Without the vocabulary a word count is honest enough to
            # show a shape, and the note says so.
            token_count = lambda text: len(text.split())

        limit = 225
        stats = dstats.analyse(
            image_tags, token_count, base_model_count=posts_for,
            era_label=era_label, limit=limit,
            repeats_of=_repeats_from_folder_name,
            folder_of=_folder_of)

        self._ds_caption_chart.set_data(stats.caption_pairs())
        over = stats.over_limit
        share = (round(100 * over / stats.total_images)
                 if stats.total_images else 0)
        self._ds_caption_note.setText(
            f"Median {stats.median_caption} tokens, longest "
            f"{stats.longest_caption}. "
            + (f"<b>{over:,} caption(s) ({share}%) exceed {limit} "
               "tokens</b> and will be cut short during training."
               if over else
               f"Every caption fits within {limit} tokens."))

        self._ds_density_chart.set_data(stats.density_pairs())
        spread = stats.fattest - stats.thinnest
        self._ds_density_note.setText(
            f"From {stats.thinnest} to {stats.fattest} tags per image."
            + ("  A wide spread means some images are described far "
               "more thoroughly than others, so the model is taught "
               "unevenly." if spread >= 20 else ""))

        self._ds_freq_chart.set_data(stats.frequency_pairs())
        self._ds_freq_note.setText(
            f"{stats.total_tags:,} distinct tags. "
            + (f"<b>{stats.rare_tags:,}</b> appear on two images or "
               "fewer \u2014 too few to be learned, though a tag the "
               "base model already knows is still worth keeping to "
               "stop the feature attaching to your trigger."
               if stats.rare_tags else
               "None are so rare they cannot be learned."))

        if stats.vocabulary:
            self._ds_vocab_bar.set_data([
                (stats.vocabulary[0].label, stats.vocabulary[0].count,
                 Colors.SUCCESS_GREEN),
                (stats.vocabulary[1].label, stats.vocabulary[1].count,
                 Colors.WARNING_AMBER),
                (stats.vocabulary[2].label, stats.vocabulary[2].count,
                 Colors.DANGER_RED),
            ])
            self._ds_vocab_note.setText(
                f"Measured against <b>{era_label}</b>. Tags it has "
                "never seen are either your own invented tokens, or "
                "typos \u2014 worth checking which.")

        if stats.folders_meaningful:
            for widget in (self._ds_folder_section, self._ds_folder_note,
                           self._ds_folder_chart):
                widget.setVisible(True)
            self._ds_folder_chart.set_data(
                [(name, exposure) for name, _n, exposure in stats.folders])
            self._ds_folder_note.setText(
                "Effective exposure \u2014 images multiplied by the "
                "repeat count in each folder name. This is what the "
                "trainer actually sees, and it is what decides which "
                "concept dominates.")

    def _refresh_all(self) -> None:
        self._refresh_numbers()
        self._refresh_frequency()
        self._refresh_cooccur()
        self._refresh_dataset()

    def _refresh_numbers(self) -> None:
        if self._state is None:
            self._pct_label.setText("0.00%")
            self._jobs_label.setText("Decisions: 0 / 0")
            self._images_label.setText("Images: 0")
            self._tags_label.setText("Tags: 0")
            self._tags_done_label.setText("Tags completed: 0")
            self._skipped_label.setText("Skipped (review later): 0")
            return

        decided, total = self._state.get_project_completion()
        pct = (100.0 * decided / total) if total else 0.0
        self._pct_label.setText(f"{pct:.2f}%")

        n_images = len(self._state.all_images)
        n_tags = len(self._state.all_tags)
        tags_done = sum(
            1 for tg in self._state.all_tags
            if self._state.get_tag_status(tg) == TagStatus.COMPLETED
        )
        tags_skipped = sum(
            1 for tg in self._state.all_tags
            if self._state.get_tag_status(tg) == TagStatus.SKIPPED
        )

        self._jobs_label.setText(f"Decisions: {decided:,} / {total:,}")
        self._images_label.setText(f"Images: {n_images:,}")
        self._tags_label.setText(f"Tags: {n_tags:,}")
        self._tags_done_label.setText(f"Tags completed: {tags_done:,}")
        self._skipped_label.setText(
            f"Skipped (review later): {tags_skipped:,}"
        )

    def _refresh_frequency(self) -> None:
        if self._state is None:
            self._freq_chart.set_data([])
            self._freq_count_label.setText("")
            return
        folder = self._freq_folder.currentData()
        freqs = self._state.get_tag_frequencies(folder)
        if self._freq_sort.currentData() == "asc":
            freqs = list(reversed(freqs))
        self._freq_chart.set_data(freqs)
        self._freq_count_label.setText(f"{len(freqs):,} tags")

    def _refresh_cooccur(self) -> None:
        if self._state is None:
            self._cooccur_top.set_data([])
            self._cooccur_bottom.set_data([])
            self._cooccur_missing.set_data([])
            return
        tag = self._cooccur_tag.currentText().strip()
        if not tag:
            self._cooccur_top.set_data([])
            self._cooccur_bottom.set_data([])
            self._cooccur_missing.set_data([])
            return
        pct = self._cooccur_threshold.currentData()
        if pct is None:
            pct = 100
        top, _unused_bottom = self._state.get_cooccurrence(
            tag, limit=20, too_common_pct=pct,
        )
        self._cooccur_top.set_data(top)
        self._refresh_divergence(tag)

    def _refresh_divergence(self, tag: str) -> None:
        """Compare this tag's partners in your captions against the
        same tag's partners on Danbooru."""
        from core.cooccurrence import get_db
        from core.dataset_stats import compare_with_danbooru

        self._cooccur_bottom.set_data([])
        self._cooccur_missing.set_data([])
        # Restored on EVERY refresh. The "no data" message below used
        # to be written and never taken back, so it stayed on screen
        # describing a tag that was no longer selected.
        self._cooccur_note.setText(COOCCUR_HELP)
        if self._state is None or not tag:
            return

        # Every partner in your own captions, and how many images
        # carry it alongside the subject tag.
        partner_counts: dict = {}
        tag_images = 0
        for entry in self._state.all_images:
            tags = self._state.get_image_tags(entry.image_path)
            if tag not in tags:
                continue
            tag_images += 1
            for other in tags:
                if other != tag:
                    partner_counts[other] = (
                        partner_counts.get(other, 0) + 1)
        if not tag_images:
            return

        try:
            danbooru = [(name, pct) for name, _ochiai, pct
                        in get_db().get_cooccurring(tag, limit=30)]
        except Exception:
            danbooru = []
        if not danbooru:
            self._cooccur_note.setText(
                f"No Danbooru co-occurrence data for \u201c{tag}\u201d, "
                "so there is nothing to compare against. This is "
                "expected for your own trigger tokens.")
            return

        you_more, they_more = compare_with_danbooru(
            partner_counts, tag_images, danbooru,
            reverse_lookup=lambda partner: _reverse_share(tag, partner))
        self._cooccur_bottom.set_data(
            [(d.label(), d.gap) for d in you_more])
        self._cooccur_missing.set_data(
            [(d.label(), d.gap) for d in they_more])
