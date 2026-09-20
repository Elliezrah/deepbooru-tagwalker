"""
ui/prune_advisor_dialog.py

Tools -> Tag Pruning Advisor. Ranks the tags worth cutting when a
dataset is pressing against a text-encoder token limit.

This tool CHANGES NOTHING. It produces a ranked, explained list to act
on by hand, using the tools that already exist. Rewriting captions
across a whole dataset is the most destructive thing the application
could do, and it is not worth doing on advice a machine produced from
statistics alone — the decisive question in Tier 2 is what the user
intends the model to do, which no statistic knows.

Every row carries its full argument on hover, so the reasoning is
learnable rather than an opaque verdict.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import prune_advisor as advisor
from core.state import SessionState
from core import multi_tokenizer as mt
from ui.token_counter_dialog import THRESHOLDS

HELP_PAGES = [
    ("The principle everything follows from",
     "Caption what VARIES and what you want to CONTROL.\n\n"
     "Do not caption what is constant and intrinsic to the thing you "
     "are teaching \u2014 the trigger token absorbs it either way.\n\n"
     "Every rule in this tool is that sentence applied to a different "
     "situation."),

    ("Why it asks what you are training",
     "Teaching a style, the artist's signature belongs in the trigger "
     "and the CONTENT must stay described, or style and subject fuse "
     "together.\n\nTeaching a character, the reverse holds.\n\n"
     "The tag's frequency is identical in both cases. The correct "
     "verdict is not. That is why there is no goal-free answer to "
     "give, and why the tool refuses to guess.\n\nSelect every goal "
     "that applies. Where two goals want opposite things from a tag, "
     "it is listed for you to decide rather than recommended either "
     "way."),

    ("Tier 1 \u2014 safe to prune",
     "Independent of what you are training.\n\n"
     "\u2022 Meta tags (highres, absurdres, scan artefacts) describe "
     "the FILE, not the picture. Nothing to learn, no reason to "
     "prompt them.\n\n"
     "\u2022 Tags seen too few times AND unknown to the base model "
     "cannot form an association. There is no prior to build on and "
     "too few examples to build one. If such a tag matters, the fix "
     "is more images or more repeats \u2014 not keeping the "
     "label.\n\nBoth conditions matter. A rare tag the base model "
     "already knows is not here to be learned, and is handled in "
     "Tier 3."),

    ("Which base model it judges against",
     "Whether a tag counts as \u201calready known\u201d depends on "
     "which shipped tag database the advice is measured "
     "against.\n\nThat follows the tag database selected in "
     "Preferences \u2014 the same one the Tag Referencer uses. There "
     "is deliberately no separate selector here: two era controls "
     "could disagree, and then the same tag would get contradictory "
     "verdicts in two windows of the same program.\n\nIf you are "
     "training on a Pony-derived base, set the database to Pony and "
     "the advice re-orients. A tag that postdates your era counts as "
     "unknown, which is correct \u2014 your base model never saw it."),

    ("Why Tier 3 is large",
     "Because most of it is the tool showing its working, not a list "
     "of decisions.\n\nOn a real dataset the great majority of tags "
     "genuinely do belong in \u201ckeep\u201d, so a short Tier 3 "
     "would mean the checks were not running. What matters is being "
     "able to tell the rows apart, so it is grouped by REASON:\n\n"
     "\u2022 Your goals disagree \u2014 the only group that needs an "
     "answer from you. Opens automatically.\n"
     "\u2022 Trigger tokens \u2014 never droppable.\n"
     "\u2022 Majority value of a varying attribute \u2014 1girl "
     "where 2girls also exists.\n"
     "\u2022 Rare here but known to the base model \u2014 kept for "
     "attribution, not for learning.\n"
     "\u2022 Protected by your goal \u2014 your training target "
     "needs them described.\n\nCollapsed groups are safe to ignore. "
     "If you only ever open the first one, the tool is working as "
     "intended."),

    ("Two things it will never recommend",
     "Your TRIGGER TOKEN. Being on nearly every image is what makes "
     "an ordinary tag a candidate for baking in; for the trigger that "
     "ubiquity is the entire mechanism. Remove it and the LoRA is not "
     "weakened but inert \u2014 no phrase activates what you trained. "
     "It is recognised from the locked-token list, or from being a "
     "tag the base model has never seen sitting on almost every "
     "image.\n\nMEDIUM tags. Danbooru files highres and "
     "traditional_media under the same \u201cmeta\u201d category, "
     "but only the first is bookkeeping. photo_(medium), official_art, "
     "game_cg and scan ARE the look of the picture, and dropping them "
     "welds that look into your trigger \u2014 worst of all for a "
     "style LoRA. Only tags with no visual content are auto-pruned."),

    ("Ubiquitous, or just the majority value?",
     "A tag on 90% of the dataset is only a CONSTANT if nothing "
     "contradicts it.\n\nIf the images without it carry tags that "
     "never appear beside it \u2014 1girl against 2girls, red_eyes "
     "against blue_eyes \u2014 then the attribute is not constant. It "
     "varies, you captioned the variation, and the ubiquitous tag is "
     "merely its majority value.\n\nBaking in a real constant is "
     "free. Baking in the majority value of a varying attribute costs "
     "control: the minority tags then have to override a bias welded "
     "into your trigger instead of composing with a trigger neutral "
     "about it.\n\nThe tool works this out from your captions rather "
     "than from a list of known pairs, so it catches your own "
     "vocabulary as readily as the obvious ones. Those tags land in "
     "Tier 3."),

    ("Rare, but the base model knows it",
     "A tag on three images that the base model knows thoroughly is "
     "not trying to teach anything. It is naming a feature so that "
     "feature is attributed to the TAG rather than to your "
     "trigger.\n\nThat job works from one image. Frequency is "
     "irrelevant to it.\n\nPrune it and the feature does not "
     "disappear from the picture \u2014 only from the caption. The "
     "model attributes it to whatever the caption still contains, "
     "most reliably your trigger. That is how a concept quietly "
     "acquires baggage it fires with later.\n\nRare features are "
     "the ones that most need naming, not least. These land in "
     "Tier 3."),

    ("Tier 2 \u2014 redundant, but your call",
     "\u2022 A tag on nearly the whole dataset is a constant, so the "
     "trigger learns it whether or not you write it. Dropping it "
     "makes the trait intrinsic; keeping it lets you prompt against "
     "it later. Same tag, same number, opposite answers depending on "
     "your intent.\n\n"
     "\u2022 Two tags marking almost the same images say one thing "
     "twice. Keep the narrower one \u2014 it carries everything the "
     "broader one does."),

    ("Tier 3 \u2014 keep, despite looking prunable",
     "Tags that are ubiquitous, and therefore look like obvious cuts, "
     "but must stay described for the goal you selected.\n\n"
     "Dropping one of these would fuse something into your trigger "
     "that you wanted to keep separate. These are the traps, which is "
     "why they are shown rather than silently omitted."),

    ("Judging rarity: share vs exposure",
     "\u201cShare of the dataset\u201d calls a tag too rare below "
     "about 0.5% of images. That is a proxy, and an imperfect "
     "one.\n\n\u201cTraining exposure\u201d counts how many times "
     "the tag is actually seen \u2014 images \u00d7 repeats \u00d7 "
     "epochs \u2014 which is what decides whether an association "
     "forms. That number does not change with the size of the rest of "
     "the dataset: a tag on five images gets the same exposure in a "
     "100-image set as in a 10,000-image one.\n\nIt matters most "
     "when repeats differ between folders, since the same tag can get "
     "several times the exposure purely from which folder its images "
     "sit in."),

    ("Where the floor comes from",
     "The tool decides this, not you \u2014 someone configuring a "
     "run has no way to know where an association stabilises, and "
     "asking would defeat the point of asking the tool.\n\nIt is "
     "set deliberately low. A tool that recommends deleting things "
     "should err towards not recommending: everything flagged is "
     "confidently unlearnable, and some marginal tags will pass "
     "unflagged. A kept tag costs a few tokens; a wrongly deleted one "
     "costs the trait.\n\nThe exact figure is a rule of thumb. It "
     "moves with learning rate, network dimension and how "
     "distinctive the trait is, none of which this tool can see, so "
     "each tag's own exposure count is shown beside it."),

    ("Ranking, and what this is not",
     "Candidates are ordered by how much they relieve the captions "
     "actually over the limit, then by total tokens reclaimed. A "
     "tag's cost includes its separating comma.\n\nThere is no "
     "published professional ruleset for this. Foundation labs "
     "caption in prose and control length when the caption is "
     "generated \u2014 they re-caption rather than prune tags \u2014 "
     "and the academic \u201ctoken pruning\u201d literature is about "
     "a different problem entirely.\n\nThese rules are derived from "
     "how tag conditioning works, not borrowed from an authority. "
     "Treat them as informed argument and check them against your own "
     "results."),
]


# Help for the caption-export dropdown. Explains the three modes and,
# crucially, WHEN to reach for the tag index rather than the flat dump
# — the index exists to make "which images share two tags" answerable
# without searching caption text, which is where substring and
# cross-image mistakes come from.
CAPTIONS_HELP_PAGES = [
    ("What these modes add",
     "The report above is statistics: how OFTEN each tag appears. It "
     "does not show how tags sit together on individual images.\n\n"
     "These modes append that per-image detail, in one of two shapes. "
     "Both are off by default \u2014 a plain report stays plain."),

    ("All captions (per image)",
     "Every image's caption in full, one line per image, sorted by "
     "filename:\n\n"
     "    `img_001.png` \u2014 1girl, blue_eyes, smile\n\n"
     "This is the shape to pick when the question is \u201cwhat does "
     "THIS image look like\u201d, or when you want to skim the set the "
     "way the trainer will read it.\n\n"
     "It is the raw text, so anything reading it must match whole "
     "tags, not substrings (see the last page)."),

    ("Tag index (images per tag)",
     "The inverse: every tag, then the exact list of images carrying "
     "it, sorted alphabetically:\n\n"
     "    `pink_eyes` (3): a.png, b.png, c.png\n"
     "    `purple_eyes` (2): b.png, d.png\n\n"
     "This is the shape to pick when the question is \u201cwhich images "
     "carry BOTH tag X and tag Y\u201d. Take the two tags' lists and "
     "intersect them \u2014 here, only b.png is in both. No caption text "
     "is searched, so a tag can never be confused with a longer tag "
     "that contains it, and two tags on different images can never be "
     "paired by accident.\n\n"
     "Lists are complete (never truncated), because an intersection "
     "is only correct if both sides are whole."),

    ("Why the index is the safe one",
     "Searching caption text for two tags at once is where audits go "
     "wrong. A search for a short tag also matches longer tags that "
     "contain it, and a pattern looking for one tag near another can "
     "span across image boundaries and pair tags that are on "
     "different images.\n\n"
     "The tag index sidesteps both: each tag's images are listed "
     "explicitly, so the work becomes set intersection, not text "
     "search. If you are handing the export to a language model to "
     "audit dual-tagging, this is the mode to use \u2014 and turning on "
     "\u201cInclude AI instructions\u201d adds a note telling the model "
     "exactly that."),
]


# Order the Tier 3 sub-groups appear in: decisions first, then the
# tool's own reasoning, least surprising last.
KIND_ORDER = (advisor.KIND_CONFLICT, advisor.KIND_TRIGGER,
              advisor.KIND_MAJORITY, advisor.KIND_KNOWN,
              advisor.KIND_GOAL)


class PruneAdvisorDialog(QDialog):
    def __init__(self, state: SessionState, parent=None,
                 settings=None) -> None:
        super().__init__(parent)
        self._state = state
        if settings is None:
            # Reads the same persisted preferences, so the era it
            # judges against is the one the user last chose either way.
            from config.settings import Settings
            settings = Settings()
        self._settings = settings
        self._report: advisor.PruneReport | None = None
        self.setWindowTitle("Tag Pruning Advisor")
        self.setMinimumSize(780, 560)
        self.setModal(False)

        layout = QVBoxLayout(self)
        header = QLabel(
            "Tags ranked by how much they are worth cutting. "
            "<b>Nothing is changed</b> \u2014 this produces a list to "
            "act on yourself.")
        header.setWordWrap(True)
        layout.addWidget(header)

        self.era_label = QLabel("")
        self.era_label.setWordWrap(True)
        self.era_label.setToolTip(
            "Which shipped tag database the advice is judged "
            "against \u2014 it decides whether a tag counts as "
            "already known to your base model, and therefore whether "
            "being rare in your dataset matters.\n\nThis follows the "
            "tag database chosen in Preferences, so the whole "
            "program stays oriented to one era rather than each tool "
            "assuming its own.")
        layout.addWidget(self.era_label)

        goal_row = QHBoxLayout()
        goal_row.addWidget(QLabel("Training goal:"))
        self._goals: dict[str, QCheckBox] = {}
        for key, label in advisor.MODE_LABELS.items():
            box = QCheckBox(label)
            box.setToolTip(advisor.MODE_HELP[key])
            goal_row.addWidget(box)
            self._goals[key] = box
        self._goals["character"].setChecked(True)
        self.goal_note = QLabel("")
        self.goal_note.setWordWrap(True)
        goal_row.addStretch(1)
        layout.addLayout(goal_row)
        layout.addWidget(self.goal_note)
        for box in self._goals.values():
            box.toggled.connect(self._sync_goal_note)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Judge rarity by:"))
        self.rarity_mode = QComboBox()
        self.rarity_mode.addItem("Share of the dataset", "share")
        self.rarity_mode.addItem("Training exposure", "exposure")
        self.rarity_mode.setToolTip(
            "Share of the dataset: a tag on under ~0.5% of images is "
            "called too rare to learn.\n\nTraining exposure: counts "
            "how many times the tag is actually SEEN \u2014 images "
            "\u00d7 folder repeats \u00d7 epochs \u2014 which is the "
            "quantity that decides whether an association forms. A "
            "tag on five images gets the same exposure in a small set "
            "as in a huge one, so dataset size does not belong in the "
            "answer.\n\nExposure mode also respects per-folder "
            "repeats: the same tag in a 3\u00d7 folder gets three "
            "times the exposure of one in a 1\u00d7 folder.")
        self.rarity_mode.currentIndexChanged.connect(self._sync_mode)
        mode_row.addWidget(self.rarity_mode)
        mode_row.addWidget(QLabel("Epochs:"))
        self.spin_epochs = QSpinBox()
        self.spin_epochs.setRange(1, 1000)
        self.spin_epochs.setValue(10)
        self.spin_epochs.setToolTip(
            "How many epochs your trainer is configured for. This is "
            "a fact about your setup, so the tool asks rather than "
            "guesses \u2014 unlike the learnability threshold, which "
            "is the tool's own judgement.")
        mode_row.addWidget(self.spin_epochs)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.repeat_box = QWidget()
        repeat_layout = QVBoxLayout(self.repeat_box)
        repeat_layout.setContentsMargins(0, 0, 0, 0)
        repeat_head = QHBoxLayout()
        repeat_head.addWidget(QLabel("Repeats per folder:"))
        self.btn_guess_repeats = QPushButton(
            "Fill from folder names")
        self.btn_guess_repeats.setToolTip(
            "kohya's convention writes the count into the folder name "
            "(3_concept). This fills the table from it as a "
            "convenience \u2014 check the numbers against what your "
            "trainer is actually configured with, because the folder "
            "name and the setting can disagree.")
        self.btn_guess_repeats.clicked.connect(self._fill_repeats)
        repeat_head.addWidget(self.btn_guess_repeats)
        repeat_head.addStretch(1)
        repeat_layout.addLayout(repeat_head)
        self.repeat_table = QTableWidget(0, 2)
        self.repeat_table.setHorizontalHeaderLabels(
            ["Folder", "Repeats"])
        self.repeat_table.verticalHeader().setVisible(False)
        self.repeat_table.setColumnWidth(0, 320)
        self.repeat_table.setMaximumHeight(140)
        self.repeat_table.setToolTip(
            "Set the repeat count each folder is trained with. "
            "Defaults to 1 \u2014 nothing is assumed from the folder "
            "name unless you press the fill button.")
        repeat_layout.addWidget(self.repeat_table)
        layout.addWidget(self.repeat_box)
        self._repeat_spins: dict[str, QSpinBox] = {}
        self._build_repeat_table()

        # Tokenizer + token limit. The count that decides "over the limit"
        # is tokenizer-specific (SDXL=CLIP, Flux=T5/Qwen3/Mistral), so the
        # advisor lets you pick which trainer family you're pruning for.
        self._prune_target_key = mt.default_target_key()
        try:
            self._prune_target_key = self._state.tokenizer_target()
        except Exception:
            pass

        trow = QHBoxLayout()
        trow.addWidget(QLabel("Tokenizer:"))
        self._tokenizer = QComboBox()
        for t in mt.all_targets():
            self._tokenizer.addItem(t.label, t.key)
            self._tokenizer.setItemData(
                self._tokenizer.count() - 1, t.blurb, Qt.ItemDataRole.ToolTipRole)
        pi = self._tokenizer.findData(self._prune_target_key)
        self._tokenizer.setCurrentIndex(pi if pi >= 0 else 0)
        self._tokenizer.setToolTip(
            "Which trainer family to prune for. The over-limit count uses "
            "this tokenizer (SDXL=CLIP, Flux=T5/Qwen3/Mistral). Krea 2 and "
            "Anima share Flux.2 Klein's Qwen3 tokenizer. Hover an option "
            "for detail.")
        self._tokenizer.currentIndexChanged.connect(
            self._on_prune_tokenizer_changed)
        trow.addWidget(self._tokenizer)
        trow.addStretch(1)
        layout.addLayout(trow)

        row = QHBoxLayout()
        row.addWidget(QLabel("Token limit:"))
        self._thresh_row = QHBoxLayout()
        self._radios: list[QRadioButton] = []
        row.addLayout(self._thresh_row)
        self._build_prune_threshold_radios()
        row.addStretch(1)
        self.btn_help = QPushButton("?")
        self.btn_help.setFixedWidth(28)
        self.btn_help.setToolTip("How the advice is produced")
        self.btn_help.clicked.connect(self._show_help)
        row.addWidget(self.btn_help)
        self.btn_analyse = QPushButton("Analyse")
        self.btn_analyse.clicked.connect(self.analyse)
        row.addWidget(self.btn_analyse)
        layout.addLayout(row)

        self.summary = QLabel("Press Analyse.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        # Two token columns, not one: "3 tokens on 1080 captions" and
        # "3240 tokens across the dataset" are the same fact, but only
        # the first answers "will this get my long captions under the
        # limit?". A single total makes a cheap tag look like a big win.
        self.tree.setHeaderLabels(
            ["Tag", "Images", "Share", "Category", "Tokens each",
             "Tokens total", "Why"])
        self.tree.setColumnWidth(0, 190)
        self.tree.setColumnWidth(1, 60)
        self.tree.setColumnWidth(2, 60)
        self.tree.setColumnWidth(3, 80)
        self.tree.setColumnWidth(4, 82)
        self.tree.setColumnWidth(5, 82)
        layout.addWidget(self.tree, 1)

        foot = QHBoxLayout()
        self.chk_questionnaire = QCheckBox("Include AI instructions")
        self.chk_questionnaire.setToolTip(
            "Adds a block at the top of the export telling a language "
            "model what this tool can and cannot decide, what to do "
            "with the tables, and which questions to ask back.\n\n"
            "Worth having when you intend to paste the report into a "
            "chat; noise when you are only reading it yourself, which "
            "is why it is off by default.")
        foot.addWidget(self.chk_questionnaire)

        # Caption export mode: none / full per-image dump / inverted
        # tag index. Replaces the old "Include all captions" checkbox.
        # The index mode exists so a model can answer "which images
        # carry both tag X and tag Y" by intersecting two exact lists,
        # instead of searching caption text and risking a substring
        # match (pink_eyes inside pink_eyeshadow) or a span across
        # image boundaries.
        foot.addWidget(QLabel("Captions:"))
        self.cmb_captions = QComboBox()
        self.cmb_captions.addItem("Don't include", "none")
        self.cmb_captions.addItem("All captions (per image)", "captions")
        self.cmb_captions.addItem("Tag index (images per tag)", "index")
        self.cmb_captions.setToolTip(
            "What per-image caption data to append to the export.\n\n"
            "\u2022 Don't include \u2014 statistics only (default).\n"
            "\u2022 All captions \u2014 every image's caption in full, one per "
            "image. Best for seeing what a specific image looks like.\n"
            "\u2022 Tag index \u2014 every tag and the exact images carrying it. "
            "Best for finding images that share two tags: intersect the "
            "two lists, with no risk of a substring or cross-image "
            "mismatch.\n\n"
            "Press the ? for a fuller explanation.")
        foot.addWidget(self.cmb_captions)

        self.btn_captions_help = QPushButton("?")
        self.btn_captions_help.setFixedWidth(28)
        self.btn_captions_help.setToolTip(
            "What the caption export modes do")
        self.btn_captions_help.clicked.connect(self._show_captions_help)
        foot.addWidget(self.btn_captions_help)
        self.btn_export = QPushButton("Export to File\u2026")
        self.btn_export.clicked.connect(self._export_file)
        self.btn_copy = QPushButton("Copy to Clipboard")
        self.btn_copy.clicked.connect(self._copy)
        foot.addWidget(self.btn_export)
        foot.addWidget(self.btn_copy)
        foot.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        foot.addWidget(btn_close)
        layout.addLayout(foot)

        self._sync_goal_note()
        self._sync_mode()
        self._sync_era()

    # ------------------------------------------------------------------
    def goals(self) -> list[str]:
        return [k for k, box in self._goals.items() if box.isChecked()]

    def _prune_target(self):
        return mt.get_target(self._prune_target_key)

    def _build_prune_threshold_radios(self) -> None:
        """(Re)create the token-limit radios for the current tokenizer."""
        for rb in self._radios:
            rb.setParent(None)
        self._radios = []
        for t in self._prune_target().limits:
            rb = QRadioButton(str(t))
            rb.setToolTip(
                "Captions above this count as \u201cover the limit\u201d. "
                "Candidates that relieve those captions rank first.")
            self._radios.append(rb)
            self._thresh_row.addWidget(rb)
        if self._radios:
            self._radios[0].setChecked(True)

    def _on_prune_tokenizer_changed(self, _index: int) -> None:
        key = self._tokenizer.currentData()
        if key == self._prune_target_key:
            return
        self._prune_target_key = key
        self._build_prune_threshold_radios()

    def threshold(self) -> int:
        limits = self._prune_target().limits
        for rb, t in zip(self._radios, limits):
            if rb.isChecked():
                return t
        return limits[0]

    def _locked_tokens(self) -> list[str]:
        """The trigger tokens the app already tracks. Recommending one
        of these be dropped would not weaken the LoRA, it would leave
        nothing to activate it."""
        try:
            return list(self._state.front_locked_tokens)
        except Exception:
            return []

    def era_key(self) -> str:
        """The tag database Preferences is set to. Deliberately not a
        control of its own: a second era selector could disagree with
        the Tag Referencer's and quietly give contradictory verdicts
        about the same tag."""
        from core.tag_database import CSV_PRESETS, DEFAULT_CSV_KEY

        choice = getattr(self._settings, "tag_database_choice", "") or ""
        return choice if choice in CSV_PRESETS else DEFAULT_CSV_KEY

    def era_display(self) -> str:
        from core.tag_database import CSV_PRESETS

        preset = CSV_PRESETS.get(self.era_key())
        return preset[1] if preset else self.era_key()

    def _sync_era(self) -> None:
        self.era_label.setText(
            f"Judged against <b>{self.era_display()}</b> \u2014 change "
            "it in Settings \u2192 tag database.")

    def showEvent(self, event) -> None:  # noqa: N802
        # Preferences may have changed while this window sat open.
        self._sync_era()
        super().showEvent(event)

    def _sync_mode(self) -> None:
        on = self.rarity_mode.currentData() == "exposure"
        self.spin_epochs.setEnabled(on)
        self.repeat_box.setVisible(on)

    def _folders(self) -> list[str]:
        seen: list[str] = []
        try:
            for img in self._state.all_images:
                name = img.image_path.parent.name
                if name not in seen:
                    seen.append(name)
        except Exception:
            return []
        return sorted(seen)

    def _build_repeat_table(self) -> None:
        """One row per folder, defaulting to 1.

        Deliberately NOT pre-filled from the folder name: the name and
        the trainer's actual setting can disagree, and a wrong number
        here would quietly distort every exposure figure downstream.
        The fill button is offered as a convenience the user chooses
        to apply."""
        folders = self._folders()
        self.repeat_table.setRowCount(len(folders))
        self._repeat_spins = {}
        for row, name in enumerate(folders):
            label = QTableWidgetItem(name or "(root)")
            label.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.repeat_table.setItem(row, 0, label)
            spin = QSpinBox()
            spin.setRange(1, 1000)
            spin.setValue(1)
            self.repeat_table.setCellWidget(row, 1, spin)
            self._repeat_spins[name] = spin

    def _fill_repeats(self) -> None:
        for name, spin in self._repeat_spins.items():
            spin.setValue(advisor.parse_repeats(name))

    def _sync_goal_note(self) -> None:
        chosen = self.goals()
        if not chosen:
            self.goal_note.setText(
                "<span style='color:#e08a3c;'>Select at least one "
                "goal \u2014 the same tag gets opposite verdicts "
                "depending on what you are training.</span>")
        elif len(chosen) > 1:
            self.goal_note.setText(
                "<span style='color:#e08a3c;'>Training more than one "
                "thing at once: where the goals disagree, tags are "
                "listed for you to decide rather than recommended. "
                "When in doubt keep them \u2014 fusion between two "
                "targets is far harder to undo than a caption that "
                "ran long.</span>")
        else:
            self.goal_note.setText("")

    # ------------------------------------------------------------------
    def _gather(self):
        """Dataset -> the plain inputs the advisor wants. Kept apart
        from the analysis so the rules stay testable without a
        session."""
        from core import tag_reference as tr

        era = self.era_key()
        from core.state import _fold_for_match

        target_key = self._prune_target_key
        tag_images: dict[str, set] = {}
        over: set = set()
        limit = self.threshold()
        counting = mt.ensure_loaded(target_key)
        total = 0
        for img in self._state.all_images:
            total += 1
            tags = self._state.get_image_tags(img.image_path)
            if not tags:
                continue
            for tag in tags:
                tag_images.setdefault(
                    _fold_for_match(tag), set()).add(img.image_path)
            if counting and mt.count_tokens(
                    ", ".join(tags), target_key) > limit:
                over.add(img.image_path)

        cat_cache: dict[str, str] = {}

        def category_of(tag: str) -> str:
            if tag not in cat_cache:
                try:
                    info = tr.lookup(tag, era)
                    cat_cache[tag] = (info.category_name
                                      or ("custom" if info.is_custom
                                          else "general"))
                except Exception:
                    cat_cache[tag] = "custom"
            return cat_cache[tag]

        known_cache: dict[str, int] = {}

        def base_model_count(tag: str) -> int:
            """Posts in the snapshot the base model was trained
            around. Distinguishes a tag the model has never seen from
            one it knows well that happens to be rare here."""
            if tag not in known_cache:
                try:
                    info = tr.lookup(tag, era)
                    counts = [e.count for e in info.era_counts
                              if e.key == era and e.count is not None]
                    known_cache[tag] = counts[0] if counts else 0
                except Exception:
                    known_cache[tag] = 0
            return known_cache[tag]

        cost_cache: dict[str, int] = {}

        def token_cost(tag: str) -> int:
            if tag not in cost_cache:
                if counting:
                    # +1 for the comma that separates it from its
                    # neighbour: dropping the tag reclaims that too.
                    cost_cache[tag] = mt.count_tokens(tag, target_key) + 1
                else:
                    cost_cache[tag] = len(tag.split("_")) + 1
            return cost_cache[tag]

        spins = self._repeat_spins

        def repeats_of(image_path) -> int:
            spin = spins.get(image_path.parent.name)
            return spin.value() if spin is not None else 1

        return (tag_images, total, over, category_of, token_cost,
                repeats_of, base_model_count)

    def analyse(self) -> None:
        goals = self.goals()
        if not goals:
            QMessageBox.information(
                self, "Tag Pruning Advisor",
                "Select at least one training goal first. The same tag "
                "gets opposite verdicts depending on what you are "
                "teaching, so there is no goal-free answer to give.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            (tag_images, total, over, category_of, token_cost,
             repeats_of, base_model_count) = self._gather()
            use_exposure = self.rarity_mode.currentData() == "exposure"
            # Families are computed alongside the tiers, from the
            # same inputs, so the export cannot describe one dataset
            # and analyse another.
            self._families = advisor.find_families(
                tag_images, base_model_count)
            self._report = advisor.analyse(
                tag_images, total, over, category_of, token_cost,
                goals,
                epochs=self.spin_epochs.value() if use_exposure else 0,
                repeats_of=repeats_of,
                base_model_count=base_model_count,
                era_label=self.era_display(),
                protected_tags=self._locked_tokens())
        finally:
            QApplication.restoreOverrideCursor()
        self._render()

    def _render(self) -> None:
        self.tree.clear()
        report = self._report
        if report is None:
            return
        groups = (
            (report.tier1, "Tier 1 \u2014 safe to prune",
             "Statistical, and independent of what you are training."),
            (report.tier2, "Tier 2 \u2014 your call",
             "Redundant, but whether to cut depends on your intent."),
            (report.tier3, "Tier 3 \u2014 keep, despite looking prunable",
             "Grouped by reason. Most of these are the tool showing "
             "its working; only \u201cyour goals disagree\u201d needs "
             "an answer from you."),
        )
        def add_rows(parent, candidates) -> None:
            for c in candidates:
                item = QTreeWidgetItem(parent, [
                    c.tag, str(c.count), f"{round(c.share * 100)}%",
                    c.category, str(c.token_cost),
                    str(c.tokens_saved), c.reason])
                for col in range(7):
                    item.setToolTip(col, c.detail)

        for candidates, title, blurb in groups:
            parent = QTreeWidgetItem(
                self.tree, [f"{title} ({len(candidates)})", "", "",
                            "", "", "", blurb])
            parent.setFirstColumnSpanned(False)
            parent.setToolTip(0, blurb)

            # FIELD REPORT: "tier 3 is indeed enormous". It is, and
            # mostly correctly — on a real dataset most tags do
            # legitimately belong there. The problem was presentation:
            # an undifferentiated list buries the two rows that need a
            # decision among two hundred that are the tool explaining
            # itself. So sub-group by reason, and put the group that
            # needs an answer first and open.
            by_kind: dict = {}
            for c in candidates:
                by_kind.setdefault(c.kind or "", []).append(c)
            if candidates is report.tier3 and len(by_kind) > 1:
                for kind in KIND_ORDER:
                    rows = by_kind.get(kind)
                    if not rows:
                        continue
                    label, why = advisor.KIND_LABELS.get(
                        kind, (kind or "Other", ""))
                    node = QTreeWidgetItem(
                        parent, [f"{label} ({len(rows)})", "", "", "",
                                 "", "", why])
                    node.setToolTip(0, why)
                    node.setToolTip(6, why)
                    add_rows(node, rows)
                    # Only the group that needs a decision opens.
                    node.setExpanded(kind == advisor.KIND_CONFLICT)
                leftover = by_kind.get("")
                if leftover:
                    node = QTreeWidgetItem(
                        parent, [f"Other ({len(leftover)})", "", "",
                                 "", "", "", ""])
                    add_rows(node, leftover)
            else:
                add_rows(parent, candidates)

            parent.setExpanded(bool(candidates) and candidates is not
                               report.tier3)
        bits = [
            f"{report.total_tags} distinct tags across "
            f"{report.total_images} images",
            f"{report.over_limit_count} caption(s) over "
            f"{self.threshold()} tokens",
            f"Tier 1 reclaims {report.tokens_reclaimable()} tokens",
        ]
        self._sync_era()
        if report.epochs:
            bits.insert(1, f"rarity judged by exposure over "
                           f"{report.epochs} epoch(s), floor "
                           f"{report.exposure_floor}")
        self.summary.setText("  \u00b7  ".join(bits))

    # ------------------------------------------------------------------
    def _show_help(self) -> None:
        from ui.paged_help_dialog import PagedHelpDialog

        # Paged rather than a message box: the explanation is long
        # enough that a box sized to its text runs off the screen.
        self._help = PagedHelpDialog(
            "Tag Pruning Advisor", HELP_PAGES, self)
        self._help.show()
        self._help.raise_()

    def _show_captions_help(self) -> None:
        from ui.paged_help_dialog import PagedHelpDialog

        # Held on a distinct attribute so opening this help does not
        # close the main advice help (and vice versa).
        self._captions_help = PagedHelpDialog(
            "Caption export modes", CAPTIONS_HELP_PAGES, self)
        self._captions_help.show()
        self._captions_help.raise_()

    def _iter_image_captions(self):
        """Yield (filename, tags) for every image, once.

        A generator, so the caller (build_all_captions_section) can
        materialise it in one pass rather than us building an
        intermediate list here as well. get_image_tags returns the
        canonical tag list for each image; the display name is the
        file's own name. No sorting or string work happens here — that
        is the builder's job — so this stays a thin, single-pass
        adapter over the state with no per-image overhead beyond the
        tag-list copy get_image_tags already makes.
        """
        state = self._state
        if state is None:
            return
        for entry in state.all_images:
            path = entry.image_path
            name = path.name if hasattr(path, "name") else str(path)
            yield name, state.get_image_tags(path)

    def _markdown(self) -> str | None:
        if self._report is None:
            QMessageBox.information(
                self, "Tag Pruning Advisor", "Press Analyse first.")
            return None
        roots = getattr(self._state, "roots", None) or []
        body = advisor.build_prune_report(self._report, roots)

        # The families table goes in whether or not the AI briefing
        # does: it is data, and it is the data the report was missing.
        # Asked to compare a set's glove variants, a model could only
        # refuse, because the vocabulary was never in front of it.
        families = getattr(self, "_families", None)
        if families:
            body = (advisor.build_families_section(families)
                    + "\n" + body)

        # The per-image caption data, when asked for, in the shape the
        # dropdown selects. Built only on demand (either shape can be
        # large) and appended AFTER the tables and families, since it is
        # reference data a model consults, not the summary it reads
        # first. Gathering is a single pass over the images; see
        # _iter_image_captions. The two shapes are mutually exclusive:
        # "captions" is image -> tags (what an image looks like),
        # "index" is tag -> images (which images share a tag, safe to
        # intersect without searching caption text).
        mode = self.cmb_captions.currentData()
        if mode == "captions":
            body = body + "\n" + advisor.build_all_captions_section(
                self._iter_image_captions())
        elif mode == "index":
            body = body + "\n" + advisor.build_tag_index_section(
                self._iter_image_captions())

        if self.chk_questionnaire.isChecked():
            # Before everything: a model reading top-down should know
            # what it is looking at before it looks.
            return (advisor.build_questionnaire(self._report)
                    + "\n\n---\n\n" + body)
        return body

    def _export_file(self) -> None:
        text = self._markdown()
        if text is None:
            return
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Pruning Candidates",
            "tagwalker_pruning_candidates.md",
            "Markdown (*.md);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        except OSError as exc:
            QMessageBox.warning(self, "Tag Pruning Advisor",
                                f"Could not write the file: {exc}")
            return
        QMessageBox.information(self, "Tag Pruning Advisor",
                                f"Written to {path}")

    def _copy(self) -> None:
        text = self._markdown()
        if text is None:
            return
        QApplication.clipboard().setText(text)
        QMessageBox.information(self, "Tag Pruning Advisor",
                                "Copied to the clipboard.")
