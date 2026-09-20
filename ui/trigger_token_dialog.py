"""
ui/trigger_token_dialog.py

Tools → Set Trigger Token (Front Lock).

Field feature: LoRA training conditions best when trigger tokens sit
FIRST in every caption. This dialog sends one or more tokens
(comma-separated, kept in the given order) to the front across the
dataset. Field-revised defaults: BOTH options start UNCHECKED —
"add to images missing it" can be catastrophic on mixed multi-artist
datasets if triggered by mistake, and locking is opt-in.

With everything off, applying is SAFE on mixed datasets: each image
is only reordered if it already carries at least one of the tokens;
images carrying none are untouched. The batch is one undo step.

The lock list (tokens enforced at the caption front on every future
edit) is managed here too; locks are saved in the SESSION file.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.state import SessionState, _fold_for_match


# Booklet shown by the "?" button: how to pick a trigger token that costs
# few text-encoder tokens without losing the uniqueness a trigger needs.
# Condensed from the full guide; every count here is real (check any
# candidate yourself in Tools -> Token Counter).
TRIGGER_HELP_PAGES = [
    ("Why the trigger's length matters",
     "Text encoders read a caption in fixed chunks (often 75 / 150 / 225 "
     "tokens). Anything past the limit is silently dropped before "
     "training sees it.\n\n"
     "A trigger sits in EVERY caption, so a wasteful one taxes your whole "
     "budget on every image. Trimming it is one of the cheapest wins "
     "available \u2014 e.g. a leetspeak-plus-underscore trigger like "
     "\u201cdr4g0n_x\u201d costs 8 tokens in a caption; a single-token "
     "handle costs 2. That saving repeats across every image.\n\n"
     "Check any candidate in Tools \u2192 Token Counter. The counts are "
     "often surprising, so never guess."),

    ("How the tokenizer really works",
     "The common belief \u2014 \u201cone word = one token unless it has an "
     "underscore\u201d \u2014 is wrong. CLIP uses BPE: a fixed vocabulary "
     "of ~49,000 sub-word pieces. Four consequences:\n\n"
     "1. Common words are 1 token; rare words fragment. \u201ccat\u201d = "
     "1, but \u201cheterochromia\u201d = 3 and \u201conomatopoeia\u201d = "
     "4. Frequency decides it, not length.\n\n"
     "2. An underscore is its OWN token, not a free separator. "
     "\u201clong_hair\u201d = 3 (long + _ + hair); \u201clong hair\u201d = "
     "2.\n\n"
     "3. Every digit is its own token. \u201c843\u201d = 3, "
     "\u201c8430\u201d = 4. Numbers are the worst trigger material.\n\n"
     "4. Leetspeak is doubly expensive: digit-for-letter mixing loses "
     "the whole-word merges AND pays per digit. \u201cdr4g0n\u201d = 5 "
     "tokens; \u201cdragon\u201d = 1."),

    ("The tension: cheap vs. unique",
     "A good trigger must be CHEAP (few tokens) and UNIQUE (rare, so the "
     "model binds it to YOUR concept and it never collides with words "
     "already in your captions or the base model).\n\n"
     "These fight each other: a 1-token string is, by definition, one of "
     "the ~49k COMMON pieces \u2014 usually a real word the model already "
     "has associations for. The very thing that makes it cheap makes it a "
     "weaker unique trigger.\n\n"
     "The floor: a 1-token word plus its comma = 2 tokens in a caption. "
     "You cannot go lower while keeping the trigger as its own "
     "comma-separated tag. Aim for 2; treat 3 as a fair price for a "
     "better mnemonic; higher is waste."),

    ("Strategies, worst to best",
     "\u2022 Numbers (\u201c843\u201d): each digit a token. Avoid.\n"
     "\u2022 Leetspeak (\u201cdr4g0n\u201d): expensive and ugly. Avoid.\n"
     "\u2022 Bare symbol (\u201c~\u201d, \u201c*\u201d): cheap, but symbols "
     "appear everywhere in captions and training \u2014 maximum "
     "contamination, and risky in tooling.\n"
     "\u2022 Common word (\u201ccat\u201d): cheap, but a strong prior "
     "fights training.\n"
     "\u2022 Rare word you'd never caption (\u201ckelvin\u201d): cheap and "
     "safe from caption collision; some model prior remains.\n"
     "\u2022 Doubled syllable (\u201ckoko\u201d): cheap, memorable, and a "
     "nonsense repeat is unlikely to be a loaded concept. Strong "
     "all-rounder.\n"
     "\u2022 Invented pseudo-word (\u201czolven\u201d): most unique, but "
     "usually costs 3 tokens.\n"
     "\u2022 Your handle/name: a real convention, unlikely to appear in "
     "training captions."),

    ("Variants: the trap, and the fix",
     "The most important structural lesson: single-token status almost "
     "never survives adding a letter. \u201cblk\u201d is 1 token; "
     "\u201cblka\u201d is 2.\n\n"
     "So the natural way to build style variants \u2014 one base plus "
     "suffixes (X, X_ai, X_v2) \u2014 fights you twice: the underscore is "
     "its own token, AND gluing a suffix usually breaks the base's "
     "single-token status. Do not build variants by suffixing a shared "
     "base.\n\n"
     "The fix: pick separate short strings that are EACH independently 1 "
     "token. A consonant frame with swapped final letters can read as a "
     "family while each stays at the floor:\n\n"
     "    Base style   blk    (2 in caption)\n"
     "    Variant 2    blm    (2 in caption)\n"
     "    Variant 3    bln    (2 in caption)\n\n"
     "Three variants for 6 tokens total. Verify each one \u2014 you "
     "cannot assume a pattern holds."),

    ("Two contamination checks",
     "Token cost is only half the decision. Uniqueness has two separate "
     "failure modes:\n\n"
     "1. Base-model prior \u2014 is the string a loaded concept? Avoid "
     "cheap-but-heavy tokens. \u201cpng\u201d is 1 token because it is "
     "the image-format token; the model has seen it beside "
     "image/file/download endlessly. Prefer nonsense syllables or words "
     "you'd never caption.\n\n"
     "2. Caption substring collision \u2014 does it already appear inside "
     "your tags? This is the failure mode that actually bites, and it is "
     "checkable in your own data. If the trigger is a substring of tags "
     "you use, any substring-matching tool can confuse them (the "
     "pink_eyes-inside-pink_eyeshadow trap). A distinctive cluster like "
     "\u201cblk\u201d is safe; \u201crin\u201d is risky (it sits inside "
     "earring, string, morning)."),

    ("Quick decision guide",
     "1. Never use numbers, leetspeak, underscores, or bare symbols.\n\n"
     "2. Want ONE trigger, cheapest? A 1-token nonsense syllable or a "
     "rare word you'd never caption. Target 2 tokens in-caption.\n\n"
     "3. Want SEVERAL variants? Pick independent handles that are each 1 "
     "token \u2014 do NOT suffix a shared base. Verify each.\n\n"
     "4. Prize memorability? Accept 3 tokens for a mnemonic; the extra "
     "token is negligible against the win.\n\n"
     "5. Prize maximum uniqueness? An invented pseudo-word, accepting it "
     "may cost 3 tokens.\n\n"
     "6. Always run both contamination checks.\n\n"
     "7. Measure everything in Token Counter. Single-token status is a "
     "quirk of the vocabulary, not a rule."),
]


class TriggerTokenDialog(QDialog):
    def __init__(self, state: SessionState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("Set Trigger Token (Front Lock)")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        intro_row = QHBoxLayout()
        intro = QLabel(
            "Send trigger token(s) to the FRONT of every caption\n"
            "(correct trigger-word conditioning for training).")
        intro_row.addWidget(intro, 1)
        self.btn_help = QPushButton("?")
        self.btn_help.setFixedWidth(28)
        self.btn_help.setToolTip(
            "How to choose a token-efficient trigger")
        self.btn_help.clicked.connect(self._show_help)
        intro_row.addWidget(
            self.btn_help, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(intro_row)

        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText(
            "one or more tokens, comma-separated \u2014 kept in this "
            "order")
        self.token_input.textChanged.connect(self._refresh_preview)
        layout.addWidget(self.token_input)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)

        self.chk_add = QCheckBox(
            "Also add to images that don't have it (orphans included)")
        self.chk_add.setChecked(False)   # field spec: OFF — mistakes
        layout.addWidget(self.chk_add)   # here can be catastrophic

        self.chk_lock = QCheckBox(
            "Keep it locked \u2014 future edits preserve the front "
            "position (saved with the session)")
        self.chk_lock.setChecked(False)  # field spec: OFF by default
        layout.addWidget(self.chk_lock)

        row = QHBoxLayout()
        self.btn_apply = QPushButton("Apply to Dataset")
        self.btn_apply.clicked.connect(self._apply)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_apply)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        layout.addLayout(row)

        layout.addWidget(QLabel("Currently locked tokens:"))
        self.locks_list = QListWidget()
        self.locks_list.setMaximumHeight(90)
        layout.addWidget(self.locks_list)
        self.btn_unlock = QPushButton("Remove Selected Lock")
        self.btn_unlock.clicked.connect(self._remove_lock)
        layout.addWidget(self.btn_unlock,
                         alignment=Qt.AlignmentFlag.AlignLeft)

        tip = QLabel(
            "Trainer tip: with shuffle_caption on, set keep_tokens "
            "to the number of front tokens.")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self._refresh_locks()
        self._refresh_preview()

    # ------------------------------------------------------------------
    def _tokens(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for t in self.token_input.text().split(","):
            t = t.strip()
            f = _fold_for_match(t)
            if t and f not in seen:
                seen.add(f)
                out.append(t)
        return out

    def _counts(self, token: str) -> tuple[int, int, int]:
        """(have_it, already_front, missing) across the dataset."""
        tf = _fold_for_match(token)
        have = front = missing = 0
        for img in self._state.all_images:
            tags = self._state.get_image_tags(img.image_path)
            folds = [_fold_for_match(t) for t in tags]
            if tf in folds:
                have += 1
                if folds and folds[0] == tf:
                    front += 1
            else:
                missing += 1
        return have, front, missing

    def _show_help(self) -> None:
        from ui.paged_help_dialog import PagedHelpDialog

        # Held on an attribute so it is not garbage-collected while open.
        self._help = PagedHelpDialog(
            "Choosing a token-efficient trigger",
            TRIGGER_HELP_PAGES, self)
        self._help.show()
        self._help.raise_()

    def _refresh_preview(self) -> None:
        toks = self._tokens()
        if not toks:
            self.preview.setText("")
            self.btn_apply.setEnabled(False)
            return
        self.btn_apply.setEnabled(True)
        lines = []
        for t in toks:
            have, front, missing = self._counts(t)
            lines.append(
                f"{t}: present on {have} ({front} in front) "
                f"\u00b7 missing on {missing}")
        self.preview.setText("\n".join(lines))

    def _refresh_locks(self) -> None:
        self.locks_list.clear()
        for t in self._state.front_locked_tokens:
            self.locks_list.addItem(t)

    def _remove_lock(self) -> None:
        item = self.locks_list.currentItem()
        if item is None:
            return
        remaining = [t for t in self._state.front_locked_tokens
                     if t != item.text()]
        self._state.set_front_locked_tokens(remaining)
        self._refresh_locks()

    def _apply(self) -> None:
        toks = self._tokens()
        if not toks:
            return
        detail = []
        for t in toks:
            have, front, missing = self._counts(t)
            to_add = missing if self.chk_add.isChecked() else 0
            detail.append(
                f"\u2022 {t}: reorder up to {have - front}, "
                f"add {to_add}")
        msg = ("Send to the front, in this order:\n\n"
               + "\n".join(detail)
               + "\n\nImages carrying none of the tokens are "
                 "untouched"
               + ("" if not self.chk_add.isChecked()
                  else " (add option is ON)")
               + ".\nOne Undo reverts everything.")
        if QMessageBox.question(
            self, "Apply Trigger Token(s)", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) != QMessageBox.StandardButton.Yes:
            return
        result = self._state.apply_trigger_token_front(
            ", ".join(toks),
            add_missing=self.chk_add.isChecked(),
            lock=self.chk_lock.isChecked(),
        )
        failures = self._state.last_write_failures
        summary = (f"Moved to front: {result['moved']}\n"
                   f"Added: {result['added']}\n"
                   f"Already correct / skipped: {result['unchanged']}")
        if failures:
            summary += ("\n\nCould not write (locked/read-only):\n  "
                        + "\n  ".join(failures[:10]))
        QMessageBox.information(self, "Trigger Token Applied", summary)
        self._refresh_locks()
        self._refresh_preview()
