"""
core/prune_advisor.py

Ranks caption tags by how much they are worth pruning, for datasets
pressing against a text-encoder token limit.

There is no published professional ruleset for this. Foundation labs
caption in prose and control length when the caption is GENERATED
(re-caption shorter, rather than prune tags), and the academic
"token pruning" literature is about dropping vision-transformer tokens
at inference, which is a different problem entirely. Booru-tagged
LoRA training — discrete tags meeting a hard limit — is the case both
skip. So the rules below are derived from how tag conditioning works
rather than borrowed from an authority, and each candidate carries the
reasoning that produced it.

The governing principle, from which everything else follows:

    Caption what VARIES and what you want to CONTROL.
    Do not caption what is constant and intrinsic to the thing you
    are teaching — the trigger absorbs it either way.

That is why the training goal changes the answer. Teaching a style,
the artist's signature belongs to the trigger and the CONTENT must
stay described, or style and subject fuse. Teaching a character, the
reverse. A tag's frequency is identical in both cases; the verdict is
not.

Three tiers, ordered by how safely they automate:

  Tier 1  statistical, safe to act on without knowing your intent
  Tier 2  redundant, but the call depends on what you are training
  Tier 3  ubiquitous yet worth KEEPING — the "obvious" cuts that are
          traps for the goal you selected

Nothing here writes anything. It produces a ranked list.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# --- thresholds ------------------------------------------------------
# A tag on a handful of images cannot form a stable association: too
# few gradient steps ever see it. It costs tokens on those captions and
# teaches nothing.
RARE_MIN_ABSOLUTE = 2
RARE_MAX_SHARE = 0.005

# Exposure mode. "Can the model learn this tag?" is really a question
# about how many times it was SEEN — images x repeats x epochs — and
# that number does not depend on how big the rest of the dataset is.
# A tag on 5 images gets the same exposure in a 100-image set as in a
# 10,000-image one, so the share-based rule above answers a slightly
# different question than the one it appears to.
#
# The floor is the TOOL's judgement, not a question for the user: a
# person setting up a training run has no way to know where an
# association stabilises, and asking them to supply the number defeats
# the purpose of asking the tool.
#
# It is set low on purpose. A tool that recommends deleting things
# should err towards not recommending: at this floor everything it
# flags is confidently unlearnable, and some marginal tags will pass
# unflagged. That asymmetry is deliberate — a kept tag costs a few
# tokens, a wrongly deleted one costs the trait.
#
# The number itself is a rule of thumb. Where an association actually
# stabilises moves with learning rate, network dimension and how
# visually distinctive the trait is, none of which this tool can see,
# so it is reported alongside the tag's own exposure count rather than
# presented as a law.
EXPOSURE_FLOOR = 50

# Posts in the base model's era above which a tag counts as already
# KNOWN to it. This decides whether "too few images" means anything.
#
# A rare tag the base model has never seen is genuinely unlearnable:
# there is no prior, and three examples will not build one. A rare tag
# the base model knows well is doing an entirely different job — it is
# not teaching the model what an elf is, it is explaining away the
# pointed ears in those three images so they are NOT absorbed into the
# trigger. That job needs the tag present, not present often.
#
# Set low on purpose, same asymmetry as the exposure floor: sparing a
# tag costs tokens, wrongly condemning one costs attribution.
KNOWN_TO_BASE_MODEL = 1000


def parse_repeats(folder_name: str) -> int:
    """kohya names dataset folders `<repeats>_<concept>`, so the
    repeat count is already written down. Anything else means 1.

    This matters more than it looks: with 3x, 2x and 1x folders the
    same tag can get three times the exposure depending only on which
    folder its images live in, and a single global repeats figure
    would quietly average that away.
    """
    head = (folder_name or "").split("_", 1)[0]
    try:
        value = int(head)
    except (TypeError, ValueError):
        return 1
    return value if value > 0 else 1

# At this share the tag is very nearly a constant of the dataset, so
# the trigger token absorbs it whether or not it is written down.
UBIQUITOUS_SHARE = 0.90

# Two tags marking almost the same images. One of them is paying for
# itself twice.
DUPLICATE_JACCARD = 0.90

# Sibling detection — the difference between a tag that is ubiquitous
# and an ATTRIBUTE that is constant.
#
# `1girl` on 90% of a set looks like a constant until you notice the
# other 10% carry `2girls` and `multiple_girls`, tags that never appear
# beside it. Subject count is not constant in that dataset; it varies,
# and the variation was captioned properly. `1girl` is its majority
# value, not a property of the thing being taught.
#
# That distinction decides the advice. Baking in a genuine constant is
# free. Baking in the majority value of a varying attribute costs
# control: the minority tags then have to fight a bias welded into the
# trigger instead of composing with a trigger neutral about it.
#
# Derived from the captions rather than from a list of known pairs —
# a list would never end, and would miss the user's own vocabulary.
# The same test that catches 1girl/2girls catches 1boy/2boys,
# red_eyes/blue_eyes, long_hair/short_hair and anything else built the
# same way.
SIBLING_EXCLUSIVE_TOLERANCE = 0.05   # co-occurrence still called "never"
SIBLING_COVERAGE = 0.50              # share of the gap siblings explain

# A custom tag on almost every image is the trigger, whether or not
# the user told the tool so. Recommending its removal is not merely
# bad advice, it is a broken LoRA: nothing would activate it.
TRIGGER_SHARE = 0.95

# Danbooru's "meta" category holds two unrelated kinds of tag, and
# treating them alike was wrong:
#
#   file bookkeeping   highres, absurdres, translated, tagme...
#                      no visual content whatsoever, safe to drop
#
#   medium / mode      traditional_media, photo_(medium), official_art,
#                      game_cg, scan, watercolor_(medium)...
#                      these ARE the look of the picture
#
# Dropping the second kind is worst for a style LoRA, where the medium
# is close to the whole point: uncaptioned, "this is watercolour" gets
# welded into the trigger and cannot be prompted away afterwards.
#
# Only the first kind is auto-pruned. Listing the SAFE ones rather
# than the unsafe ones is deliberate: a meta tag nobody anticipated
# then takes the ordinary path and is judged on its merits, instead of
# being silently deleted for belonging to a category.
BOOKKEEPING_META = frozenset({
    "highres", "absurdres", "incredibly_absurdres", "hires",
    "translated", "partially_translated", "check_translation",
    "commentary", "commentary_typo", "symbol-only_commentary",
    "tagme", "bad_id", "bad_link", "bad_pixiv_id", "bad_twitter_id",
    "bad_tumblr_id", "bad_deviantart_id", "md5_mismatch",
    "resolution_mismatch", "duplicate", "pixel-perfect_duplicate",
    "image_sample", "revision", "has_bad_revision",
    "has_downscaled_revision", "has_lossy_revision",
    "has_cropped_revision", "has_censored_revision",
    "has_watermarked_revision", "has_artifacted_revision",
})
_BOOKKEEPING_SUFFIXES = ("_request",)


def is_bookkeeping_meta(tag: str) -> bool:
    """True for meta tags carrying no visual content at all."""
    folded = (tag or "").strip().lower()
    return (folded in BOOKKEEPING_META
            or folded.endswith(_BOOKKEEPING_SUFFIXES))

# --- training goals --------------------------------------------------
# Per goal, what each Danbooru category means for pruning:
#   "bake"    - a constant of the thing being taught; droppable
#   "protect" - must stay described, or it fuses into the trigger
MODE_LABELS = {
    "character": "Character",
    "style": "Style",
    "concept": "Concept",
}
MODE_VERDICTS: dict[str, dict[str, str]] = {
    # Teaching one character: their invariants belong to the trigger,
    # but the artists and series in the set must stay described or the
    # character fuses with whichever style dominates.
    "character": {
        "general": "bake", "artist": "protect",
        "copyright": "protect", "character": "protect",
        "custom": "protect",
    },
    # Teaching a style: the artist tags ARE the target. Content must
    # stay described so the style generalises past the subjects that
    # happen to be in the set.
    "style": {
        "general": "protect", "artist": "bake",
        "copyright": "protect", "character": "protect",
        "custom": "protect",
    },
    # Teaching a pose, object or composition: the concept's own tags
    # are constants; everything else varies and must stay described.
    "concept": {
        "general": "bake", "artist": "protect",
        "copyright": "protect", "character": "protect",
        "custom": "protect",
    },
}

MODE_HELP = {
    "character": (
        "Teaching one character.\n\n"
        "Their constants (hair colour, eye colour) can be dropped — "
        "the trigger learns them either way, and dropping makes them "
        "intrinsic. Keep them only if you want to override them at "
        "prompt time.\n\n"
        "Artist and series tags are PROTECTED: if several artists are "
        "in the set and none is captioned, whichever style dominates "
        "gets fused into the character."),
    "style": (
        "Teaching an artist's or a look's style.\n\n"
        "Artist tags are the target and can be dropped into the "
        "trigger.\n\n"
        "Content is PROTECTED — characters, series and general "
        "description must stay written, or the style welds itself to "
        "the subjects that happen to appear and stops generalising."),
    "concept": (
        "Teaching a pose, object, composition or action.\n\n"
        "The concept's own constants can be dropped into the "
        "trigger.\n\n"
        "Everything else is PROTECTED: whatever varies and is left "
        "uncaptioned gets absorbed into the concept, which is the "
        "usual cause of a concept that drags unwanted baggage with it "
        "whenever it fires."),
}

# Reason keys. Ordered as the dialog groups them: rows needing a
# decision from the user first, rows where the tool is explaining
# itself last.
# Decisions the user has already made about a tag, which the advisor
# must respect rather than re-argue.
#
# The advisor reasons from the dataset. It cannot know that a tag is
# being held for images not yet collected, or that a broad tag is kept
# because it is wanted as a negative prompt later — both are
# intentions about the FUTURE, and no amount of counting reaches them.
# Recording them means a tag settled once stops coming back.
DECISION_UNDECIDED = "undecided"
DECISION_KEEP_FUTURE = "keep_future"
DECISION_KEEP_CONTROL = "keep_control"
DECISION_KEEP_OTHER = "keep_other"
DECISION_DROPPED = "dropped"

DECISION_LABELS = {
    DECISION_UNDECIDED: "Not decided",
    DECISION_KEEP_FUTURE: "Keep \u2014 for images I will add later",
    DECISION_KEEP_CONTROL: "Keep \u2014 needed for prompt control",
    DECISION_KEEP_OTHER: "Keep \u2014 my own reason",
    DECISION_DROPPED: "Dropped already",
}

# Every decision except "undecided" means the tag is settled.
SETTLED = frozenset({DECISION_KEEP_FUTURE, DECISION_KEEP_CONTROL,
                     DECISION_KEEP_OTHER, DECISION_DROPPED})

KIND_SETTLED = "settled"
KIND_BROADER = "broader"      # a narrower tag already covers it
KIND_CONFLICT = "conflict"    # goals disagree - only you can settle it
KIND_TRIGGER = "trigger"      # your trigger token
KIND_MAJORITY = "majority"    # majority value of a varying attribute
KIND_KNOWN = "known"          # rare here, familiar to the base model
KIND_GOAL = "goal"            # protected by the goal you chose

# How much of the broader tag's images the narrower one must also
# cover before the broader is judged redundant. Not 100%: a couple of
# stragglers is normal in a hand-checked set.
COVERAGE_FOR_REDUNDANT = 0.95

# How strong the narrower tag must be, RELATIVE to the broader one,
# before dropping the broader is a fair trade.
#
# An absolute floor alone is not enough: it treats 1,001 posts exactly
# like 3,366,013. Measured against real pairs, "breasts" (3,366,013)
# and "framed_breasts" (4,973) differ by a factor of 677 — advising
# the broader be dropped there trades a strong handle for a token
# almost nobody prompts. Meanwhile "collar" (185,335) and
# "sailor_collar" (267,773) are genuinely interchangeable, and the
# narrower is the more common of the two.
COMPARABLE_STRENGTH = 0.15


# Prefixes that reverse or deny the word they precede, rather than
# narrowing it.
_NEGATING = frozenset({"no", "without", "missing", "absent", "lack",
                       "fake", "faux", "imitation"})


def _broader_detail(broad: str, narrow: str, broad_known: int,
                    narrow_known: int, ratio: float,
                    strong: bool) -> str:
    """The trade, stated with both figures, so you can decide."""
    head = (f"\u201c{broad}\u201d and \u201c{narrow}\u201d sit on "
            "essentially the same images, and the second is the more "
            "specific of the two. Both name one feature, so one of "
            "them is paying tokens for nothing.\n\n")

    cost = ("Whichever you drop, you lose it as a handle. A word that "
            "is not in your captions is a word your LoRA has no "
            "opinion about \u2014 including in a negative prompt, "
            "where you might want it to suppress the feature "
            "later.\n\n")

    if strong:
        return (head + cost +
                f"Here the trade is fair. The base model knows "
                f"\u201c{narrow}\u201d well ({narrow_known:,} posts "
                f"against {broad_known:,}), so it can carry the "
                "feature and still answer a prompt.\n\n"
                f"Prefer dropping \u201c{broad}\u201d. It is the "
                "general word you will want elsewhere, and training "
                "it on images that all show one specific arrangement "
                "narrows its meaning inside your LoRA.")

    return (head + cost +
            f"Here the trade is NOT obviously fair. "
            f"\u201c{narrow}\u201d has {narrow_known:,} posts against "
            f"{broad_known:,} for \u201c{broad}\u201d \u2014 about "
            f"{ratio:.1%}. The narrower tag is a much weaker handle: "
            "the model has seen it far less, so it responds to it "
            "less reliably, in prompts and negatives alike.\n\n"
            "Three defensible choices:\n\n"
            f"\u2022 Keep BOTH. Costs a few tokens, keeps the strong "
            "handle and the precise one.\n"
            f"\u2022 Drop \u201c{broad}\u201d anyway, if you never "
            "expect to prompt it plainly and want the tokens.\n"
            f"\u2022 Drop \u201c{narrow}\u201d instead, if the "
            "precision is not something you need to control and the "
            "broader word is the one you will reach for.")


def _parts(tag: str) -> list:
    return [x for x in (tag or "").replace(" ", "_").split("_") if x]


def covers(broader: str, narrower: str) -> bool:
    """True when `narrower` names a more specific version of `broader`.

    Matched on whole underscore-separated words, never raw substring —
    "ear" must not be found inside "bear". Danbooru's naming is
    consistent enough that this catches the common cases:

        pillow  <  head_on_pillow
        mole    <  mole_under_mouth
        skirt   <  pleated_skirt

    It does NOT catch pairs that share no word — cat_ears implies
    animal_ears, and nothing in the tag text says so. Danbooru's own
    implication table would, but the co-occurrence data shipped here
    cannot substitute: p(mole|mole_under_mouth) is 0.641, not 1.0,
    because implications were never applied retroactively to old
    posts. So this is deliberately a partial answer rather than a
    guessed one.
    """
    # A parenthetical qualifier marks a DIFFERENT thing sharing a
    # name, not a narrower version of one. "mole" and "mole_(animal)"
    # are a skin blemish and a burrowing mammal; "bow" and
    # "bow_(weapon)" a ribbon and a weapon. There are 10,521 such
    # pairs in the vocabulary, most of them character variants like
    # "houshou_marine_(nun)".
    if "(" in narrower and "(" not in broader:
        return False

    a, b = _parts(broader), _parts(narrower)
    if not a or not b or len(a) >= len(b):
        return False

    # A negation contains the very word it negates. "no_bra" would
    # otherwise be read as a more specific kind of "bra", and the
    # advice would be to drop "bra" because its own negation covers
    # it — backwards, and confidently so. 78 such pairs exist.
    if b[:len(b) - len(a)] and b[0] in _NEGATING:
        return False

    return any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1))


KIND_LABELS = {
    KIND_SETTLED: ("Already decided",
                   "Tags you have settled. Kept out of the tiers so "
                   "they are not recommended again."),
    KIND_BROADER: ("Covered by a more specific tag",
                   "A narrower tag on the same images already names "
                   "this feature. Keeping the broader one costs "
                   "tokens and narrows a word you may want for "
                   "something else."),
    KIND_CONFLICT: ("Your goals disagree \u2014 decide these",
                    "The only rows that need an answer from you. One "
                    "training goal wants this tag baked in and "
                    "another wants it described."),
    KIND_TRIGGER: ("Trigger tokens \u2014 never drop",
                   "Removing one does not weaken the LoRA, it leaves "
                   "nothing to activate it."),
    KIND_MAJORITY: ("Majority value of an attribute that varies",
                    "Ubiquitous, but the images without it carry tags "
                    "that never appear beside it. Dropping the "
                    "majority value costs control over the attribute."),
    KIND_KNOWN: ("Rare here, but the base model knows them",
                 "Not here to be learned \u2014 here to keep a feature "
                 "attributed to the tag instead of your trigger. That "
                 "works from one image."),
    KIND_GOAL: ("Protected by your training goal",
                "Your goal needs these described rather than "
                "absorbed."),
}

MIXED_GOAL_NOTE = (
    "Training more than one thing at once means the goals disagree "
    "about which tags may be dropped — a style wants content "
    "described, a concept wants its own constants absorbed. Where "
    "they disagree, the tag is listed for you to decide rather than "
    "recommended either way. When in doubt keep it: fusion between "
    "two targets is much harder to undo than a caption that ran a "
    "little long.")


@dataclass
class Candidate:
    tag: str
    count: int
    share: float
    category: str
    token_cost: int          # tokens for the tag plus its separator
    tokens_saved: int        # token_cost * count, across the dataset
    over_limit_hits: int     # over-limit captions containing it
    tier: int
    reason: str              # one line, for the table
    detail: str              # the full argument, for the tooltip
    partner: str = ""        # the other half of a near-duplicate pair
    siblings: list = field(default_factory=list)   # mutually exclusive
    # Why this landed where it did, as a key rather than prose. Tier 3
    # is large on a real dataset — mostly correctly — so it has to be
    # groupable by reason, or the two rows that need a decision are
    # lost among two hundred that do not.
    kind: str = ""
    # What the user has already settled about this tag, if anything.
    decision: str = DECISION_UNDECIDED
    exposure: int = 0        # images x repeats x epochs, when known

    def priority(self) -> tuple:
        """Tags that relieve the captions actually in trouble rank
        above tags that merely save tokens somewhere."""
        return (-(self.token_cost * self.over_limit_hits),
                -self.tokens_saved, self.tag)


@dataclass
class PruneReport:
    total_images: int = 0
    total_tags: int = 0
    epochs: int = 0                 # 0 = share-based rarity
    exposure_floor: int = 0
    era_label: str = ""             # snapshot judged against
    modes: list[str] = field(default_factory=list)
    threshold: int = 0
    over_limit_count: int = 0
    tier1: list[Candidate] = field(default_factory=list)
    tier2: list[Candidate] = field(default_factory=list)
    # Tags already answered for; kept out of the tiers. See DECISION_*.
    settled: list[Candidate] = field(default_factory=list)
    tier3: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def tokens_reclaimable(self) -> int:
        """Tier 1 only — the part that does not depend on intent."""
        return sum(c.tokens_saved for c in self.tier1)


def _fold_tag(tag: str) -> str:
    return (tag or "").strip().lower().replace(" ", "_")


def _conflict_reason(pct: int, category: str, modes) -> str:
    """Name the goals rather than just reporting a disagreement."""
    bake, protect = _goal_split(category, modes)
    if bake and protect:
        return (f"on {pct}% \u2014 {', '.join(bake)} wants it baked "
                f"in, {', '.join(protect)} wants it described")
    return f"on {pct}% \u2014 your goals disagree here"


def _goal_split(category: str, modes) -> tuple[list, list]:
    """(goals wanting it baked in, goals wanting it described).

    "Your goals disagree" is true but unhelpful on its own — the user
    still has to open the tooltip to find out WHICH goal wants what.
    Returning the split lets the row say it outright.
    """
    bake, protect = [], []
    for mode in modes:
        if mode not in MODE_VERDICTS:
            continue
        verdict = MODE_VERDICTS[mode].get(category, "protect")
        (bake if verdict == "bake" else protect).append(
            MODE_LABELS.get(mode, mode))
    return bake, protect


def _verdict_for(category: str, modes) -> str:
    """Combine the selected goals' opinions of a category.

    Disagreement is reported, not resolved: silently picking a side
    would be inventing an intent the user never stated.
    """
    verdicts = {MODE_VERDICTS[m].get(category, "protect")
                for m in modes if m in MODE_VERDICTS}
    if not verdicts:
        return "protect"
    if "bake" in verdicts and "protect" in verdicts:
        return "conflict"
    return "bake" if "bake" in verdicts else "protect"


def _near_duplicates(tag_images, eligible, token_cost,
                     min_count: int = 3):
    """Map broader tag -> the narrower tag that makes it redundant.

    Restricted to `eligible` tags for a reason worth stating: any two
    tags covering 90%+ of a set necessarily overlap by 80%+, so
    running this over ubiquitous tags manufactures "duplicates" out of
    tags that merely happen to both be near-constant. Ubiquity is
    already reported on its own terms; pairing is only informative in
    the middle band, where two tags genuinely marking the same images
    means one is saying what the other already said.

    The BROADER tag is the candidate. The narrower one carries
    everything it does and more, so that is the one to keep. For a
    pair of identical size neither is more specific, and the costlier
    one is offered instead.

    Only pairs of similar size can clear the bar - a much smaller set
    makes the union too large for the ratio to reach it - so tags are
    sorted by count and compared inside a sliding window rather than
    all against all.
    """
    items = sorted(((t, s) for t, s in tag_images.items()
                    if t in eligible and len(s) >= min_count),
                   key=lambda kv: len(kv[1]))
    out: dict[str, str] = {}
    dropped: set[str] = set()
    kept: set[str] = set()
    # Indexed rather than sliced. items[i + 1:] copies the tail of the
    # list on every iteration, which on a 1,600-tag set is most of a
    # million element copies and was the single largest cost in the
    # whole analysis.
    total_items = len(items)
    for i, (narrow, set_a) in enumerate(items):
        size_a = len(set_a)
        for j in range(i + 1, total_items):
            broad, set_b = items[j]
            size_b = len(set_b)
            if size_a / size_b < DUPLICATE_JACCARD:
                break            # and every later one is only bigger
            # |A|+|B|-|A&B| rather than len(A|B): the intersection is
            # needed anyway and the union set never has to be built.
            overlap = len(set_a & set_b)
            union = size_a + size_b - overlap
            if not union or overlap / union < DUPLICATE_JACCARD:
                continue
            if len(set_b) == size_a:
                drop, keep = ((narrow, broad)
                              if token_cost(narrow) >= token_cost(broad)
                              else (broad, narrow))
            else:
                drop, keep = broad, narrow
            # A cluster of tags that always appear together (a template
            # caption block) would otherwise chain into advice that
            # tells the user to drop a tag AND to keep it as another
            # tag's replacement. Contradictory advice is worse than
            # incomplete advice, so each tag takes one role only.
            if drop in kept or drop in dropped or keep in dropped:
                continue
            out[drop] = keep
            dropped.add(drop)
            kept.add(keep)
    return out


def _siblings_of(tag: str, images: set, tag_images: dict,
                 universe: set) -> list[str]:
    """Tags that never appear beside `tag` and account for the images
    it is missing from.

    If most of the gap is explained that way, `tag` is one value of a
    varying attribute rather than a constant of the dataset — and the
    majority value of a varying attribute is the one case where baking
    in costs control instead of being free.

    Returned biggest-first, so the caller can name the strongest one.
    """
    absent = universe - images
    if not absent:
        return []
    found: list[tuple[int, str]] = []
    covered: set = set()
    for other, other_images in tag_images.items():
        if other == tag or not other_images:
            continue
        # "Never together" with room for the odd mistagged image.
        if (len(images & other_images) / len(other_images)
                > SIBLING_EXCLUSIVE_TOLERANCE):
            continue
        hit = other_images & absent
        if not hit:
            continue
        found.append((len(hit), other))
        covered |= hit
    if len(covered) / len(absent) < SIBLING_COVERAGE:
        return []
    # Name the SMALLEST set that accounts for the gap, biggest
    # contributor first, rather than every tag that happens to be
    # statistically disjoint. In a real dataset several unrelated tags
    # can sit mostly inside the gap by coincidence; the ones that
    # explain it on their own are the ones that mean something.
    found.sort(key=lambda pair: (-pair[0], pair[1]))
    chosen: list[str] = []
    running: set = set()
    for _hits, name in found:
        if len(running) / len(absent) >= SIBLING_COVERAGE:
            break
        grown = running | (tag_images[name] & absent)
        if len(grown) > len(running):
            chosen.append(name)
            running = grown
    return chosen


def analyse(
    tag_images: dict,
    total_images: int,
    over_limit_images=(),
    category_of=None,
    token_cost=None,
    modes=("character",),
    epochs: int = 0,
    repeats_of=None,
    base_model_count=None,
    era_label: str = "",
    protected_tags=(),
    decisions=None,
) -> PruneReport:
    """Rank pruning candidates.

    tag_images        : tag -> set of image identifiers carrying it
    total_images      : images in the dataset (captioned or not)
    over_limit_images : identifiers whose caption exceeds the limit
    category_of       : tag -> "general"/"artist"/"character"/
                        "copyright"/"meta"/"custom"
    token_cost        : tag -> tokens it costs including its separator
    modes             : selected training goals
    epochs            : >0 switches the rarity test from "share of the
                        dataset" to "times actually seen in training"
    repeats_of        : image identifier -> its repeat count
    protected_tags    : tags never to recommend dropping whatever the
                        statistics say - the user's trigger tokens,
                        which the app already tracks
    base_model_count  : tag -> posts in the base model's era, used to
                        tell "the model has never seen this" from
                        "the model knows this and the tag is here to
                        keep it off the trigger"
    """
    category_of = category_of or (lambda _t: "general")
    token_cost = token_cost or (lambda t: len(t.split("_")) + 1)
    modes = [m for m in modes if m in MODE_VERDICTS] or ["character"]
    over = set(over_limit_images or ())

    report = PruneReport(
        total_images=total_images,
        total_tags=len(tag_images),
        modes=list(modes),
        over_limit_count=len(over),
    )
    if not tag_images or total_images <= 0:
        report.notes.append("No captions to analyse.")
        return report

    # Whether a count SOURCE exists at all. Without one every tag
    # looks unknown, and the "unknown tag on nearly every image" test
    # for a trigger would fire on ordinary ubiquitous tags. Missing
    # data must degrade to the previous behaviour, never to a
    # confident wrong verdict.
    has_counts = base_model_count is not None
    base_model_count = base_model_count or (lambda _t: 0)
    report.era_label = era_label
    protected = {_fold_tag(t) for t in (protected_tags or ())}
    use_exposure = epochs and epochs > 0
    repeats_of = repeats_of or (lambda _img: 1)
    report.epochs = int(epochs or 0)
    report.exposure_floor = EXPOSURE_FLOOR
    rare_cut = max(RARE_MIN_ABSOLUTE,
                   math.ceil(RARE_MAX_SHARE * total_images))
    # Pairing only means something for tags not already accounted
    # for: meta and rare tags reach Tier 1 on their own terms, and
    # ubiquitous tags overlap one another by construction.
    eligible = {
        tag for tag, images in tag_images.items()
        if (category_of(tag) or "custom") != "meta"
        and rare_cut < len(images) < UBIQUITOUS_SHARE * total_images}
    duplicates = _near_duplicates(tag_images, eligible, token_cost)

    # Broader tags already covered by a narrower one on the same
    # images. Unlike near-duplicate detection this deliberately does
    # NOT skip ubiquitous tags: "pillow" and "head_on_pillow" on every
    # image is exactly the case worth reporting, and the ubiquity rule
    # alone says the same bland thing about both.
    covered_by: dict = {}
    narrow_index = _narrower_index(tag_images)
    for broad, broad_images in tag_images.items():
        if not broad_images:
            continue
        best = None
        for narrow in _narrower_than(broad, narrow_index):
            narrow_images = tag_images[narrow]
            overlap = len(broad_images & narrow_images) / len(broad_images)
            if overlap < COVERAGE_FOR_REDUNDANT:
                continue
            # Prefer the narrower tag the base model knows best; a
            # specific tag it has never seen cannot carry the feature
            # alone, which is the whole premise.
            known = int(base_model_count(narrow) or 0)
            if known < KNOWN_TO_BASE_MODEL:
                continue
            if best is None or known > best[1]:
                best = (narrow, known)
        if best is not None:
            covered_by[broad] = best
    universe: set = set()
    for _images in tag_images.values():
        universe |= _images

    decisions = dict(decisions or {})

    for tag, images in tag_images.items():
        count = len(images)
        if not count:
            continue
        category = category_of(tag) or "custom"
        cost = max(1, int(token_cost(tag)))
        hits = len(images & over)
        share = count / total_images
        base = dict(tag=tag, count=count, share=share,
                    category=category, token_cost=cost,
                    tokens_saved=cost * count, over_limit_hits=hits)

        # --- never recommend dropping the trigger -----------------
        if (_fold_tag(tag) in protected
                or (has_counts
                    and int(base_model_count(tag) or 0) == 0
                    and share >= TRIGGER_SHARE)):
            why = ("it is one of your locked trigger tokens"
                   if _fold_tag(tag) in protected else
                   f"it is on {round(share * 100)}% of the dataset "
                   "and the base model has never seen it")
            report.tier3.append(Candidate(
                **base, tier=3,
                kind=KIND_TRIGGER,
                    reason="your trigger token \u2014 never drop this",
                detail=(
                    f"\u201c{tag}\u201d looks like your trigger: "
                    f"{why}.\n\nBeing on nearly every image is what "
                    "makes an ordinary tag a candidate for baking in. "
                    "For the trigger it is the opposite \u2014 that "
                    "ubiquity is the entire mechanism. It is the "
                    "handle everything else attaches to.\n\nRemove "
                    "it and the LoRA is not weakened, it is inert: "
                    "no phrase is left that activates what you "
                    "trained.\n\nListed here only so you can see "
                    "that the tool considered it and refused.")))
            continue


        # --- Tier 1: no knowledge of intent required ---------------
        if category == "meta" and is_bookkeeping_meta(tag):
            report.tier1.append(Candidate(
                **base, tier=1,
                reason="bookkeeping, not the picture",
                detail=(
                    f"\u201c{tag}\u201d is a Danbooru meta tag: it "
                    "records something about the FILE (resolution, "
                    "source, artefacts) rather than anything visible "
                    "in the image.\n\nThere is nothing for the model "
                    "to learn from it and no reason to prompt with "
                    "it, so it is pure token cost on every caption "
                    "that carries it.")))
            continue
        settled = decisions.get(tag, DECISION_UNDECIDED)
        if settled in SETTLED:
            # Already answered. Repeating a recommendation the user
            # has considered and rejected is how a tool teaches people
            # to stop reading it — so the tag is recorded as settled
            # and kept out of the tiers entirely.
            report.settled.append(Candidate(
                **base, tier=3, kind=KIND_SETTLED, decision=settled,
                reason=DECISION_LABELS.get(settled, settled),
                detail=("You have already decided about this tag. It "
                        "is listed here so the record is complete, "
                        "and left out of the tiers so it is not "
                        "argued again.")))
            continue

        narrower = covered_by.get(tag)
        if narrower is not None:
            narrow_tag, narrow_known = narrower
            broad_known = int(base_model_count(tag) or 0)
            ratio = (narrow_known / broad_known) if broad_known else 1.0
            strong = ratio >= COMPARABLE_STRENGTH

            # TIER 2, not Tier 1. This was Tier 1 — "safe to cut" —
            # and it is not. Dropping the broader tag costs a control
            # handle: a word you can no longer put in a negative
            # prompt to suppress the feature, or in a positive one to
            # ask for it plainly. That is a judgement about how you
            # intend to use the model, which the tool cannot make.
            if strong:
                reason = (f"\u201c{narrow_tag}\u201d covers this "
                          f"({narrow_known:,} vs {broad_known:,} "
                          "posts) \u2014 dropping the broader is a "
                          "fair trade")
            else:
                reason = (f"\u201c{narrow_tag}\u201d covers this, but "
                          f"is far rarer ({narrow_known:,} vs "
                          f"{broad_known:,} posts) \u2014 keeping both "
                          "is defensible")
            report.tier2.append(Candidate(
                **base, tier=2, kind=KIND_BROADER,
                reason=reason,
                detail=_broader_detail(tag, narrow_tag, broad_known,
                                       narrow_known, ratio, strong)))
            continue
        exposure = 0
        if use_exposure:
            exposure = sum(repeats_of(img) for img in images) * epochs
            base["exposure"] = exposure
        too_rare = (exposure < EXPOSURE_FLOOR if use_exposure
                    else count <= rare_cut)
        if too_rare:
            known = int(base_model_count(tag) or 0)
            if known >= KNOWN_TO_BASE_MODEL:
                # Rare here, but the base model knows it well. The tag
                # is not trying to teach anything — it is keeping a
                # feature off the trigger, and that works from one
                # image.
                report.tier3.append(Candidate(
                    **base, tier=3,
                    kind=KIND_KNOWN,
                    reason=(f"rare here, but the base model knows it "
                            f"({known:,} posts) \u2014 keep"),
                    detail=(
                        f"\u201c{tag}\u201d is on only {count} of "
                        f"{total_images} images, which would normally "
                        "make it too rare to learn. It is not, "
                        "because the base model already knows this "
                        f"tag \u2014 {known:,} posts in "
                        + (f"the {era_label} snapshot" if era_label
                           else "the snapshot being judged against")
                        + ".\n\nThat changes what "
                        "the tag is for. You are not teaching the "
                        "model what this is; you are naming a feature "
                        "of those images so it is attributed to the "
                        "TAG and not to your trigger. That job needs "
                        "the tag present, not present "
                        "often.\n\nPrune it and the feature becomes "
                        "unexplained: the model still sees it, and "
                        "attributes it to whatever the caption does "
                        "contain \u2014 most reliably your trigger. A "
                        f"few images is a small dose, but this is "
                        "exactly the mechanism by which a concept "
                        "picks up baggage it then fires with "
                        "later.\n\nRare features are the ones that "
                        "most need naming, not least.")))
                continue
            if use_exposure:
                reason = (f"seen ~{exposure} times \u2014 too few to "
                          f"learn (base model: {known:,} posts)")
                detail = (
                    f"\u201c{tag}\u201d is on {count} image(s), which "
                    f"across {epochs} epoch(s) and their folder "
                    f"repeats means the model sees it about "
                    f"{exposure} times in total.\n\nThat is the "
                    "number that decides whether an association forms "
                    "\u2014 not what fraction of the dataset the tag "
                    "is. A tag on five images gets the same exposure "
                    "in a small set as in a huge one.\n\nBelow about "
                    f"{EXPOSURE_FLOOR} exposures the association tends "
                    "not to stabilise, though the exact figure moves "
                    "with learning rate, network dimension and how "
                    "distinctive the trait is \u2014 treat it as a "
                    f"rule of thumb. Meanwhile it costs {cost} tokens "
                    "on every caption carrying it.\n\nIf this tag "
                    "matters, the fix is more images or more repeats, "
                    "not keeping the label.")
            else:
                # The base-model figure goes in the REASON, not just
                # the tooltip. Field report: well-known tags appeared
                # here and looked like a bug. They were not — each was
                # under the threshold in the SELECTED era — but the
                # row gave no way to tell a correct verdict from a
                # wrong era setting.
                reason = (f"only {count} image(s) \u2014 too few to "
                          f"learn (base model: {known:,} posts)")
                detail = (
                    f"\u201c{tag}\u201d appears on {count} of "
                    f"{total_images} images.\n\nA tag needs to be "
                    "seen many times before a stable association "
                    "forms; at this frequency the model sees it a "
                    "handful of times and learns nothing "
                    f"reliable. It still costs {cost} tokens on each "
                    "caption that carries it.\n\nThis is judged as a "
                    "share of the dataset. Switching to exposure mode "
                    "answers the sharper question \u2014 how many "
                    "times the tag is actually seen, given your epochs "
                    "and folder repeats.\n\nIf this tag matters to "
                    "you, the fix is more images of it, not keeping "
                    "the label.")
            report.tier1.append(
                Candidate(**base, tier=1, reason=reason, detail=detail))
            continue

        # --- Tier 2 / 3: the call depends on the goal --------------
        verdict = _verdict_for(category, modes)
        partner = duplicates.get(tag, "")
        if partner:
            report.tier2.append(Candidate(
                **base, tier=2, partner=partner,
                reason=f"marks almost the same images as {partner}",
                detail=(
                    f"\u201c{tag}\u201d and \u201c{partner}\u201d "
                    "appear on nearly the same images, so between them "
                    "they say one thing twice.\n\n"
                    f"\u201c{partner}\u201d is the narrower of "
                    "the two: it carries everything this one "
                    "does and more. Dropping "
                    f"\u201c{tag}\u201d therefore reclaims "
                    f"{cost * count} tokens across the dataset "
                    "while losing almost nothing.\n\nCheck the "
                    "pair before acting: near-identical coverage "
                    "in YOUR set does not always mean two tags "
                    "mean the same thing.")))
            continue

        if share >= UBIQUITOUS_SHARE:
            siblings = _siblings_of(tag, images, tag_images,
                                    universe)
            if siblings:
                named = ", ".join(siblings[:3])
                more = ("" if len(siblings) <= 3
                        else f" (+{len(siblings) - 3} more)")
                base["siblings"] = siblings
                report.tier3.append(Candidate(
                    **base, tier=3,
                    kind=KIND_MAJORITY,
                    reason=(f"on {round(share * 100)}%, but {named} "
                            "covers the rest \u2014 the attribute "
                            "varies"),
                    detail=(
                        f"\u201c{tag}\u201d is on "
                        f"{round(share * 100)}% of the dataset, which "
                        "normally makes it a candidate to bake into "
                        "the trigger. It is not one.\n\nThe images "
                        "WITHOUT it are covered by tags that never "
                        f"appear beside it: {named}{more}.\n\nSo "
                        "this attribute is not constant in your "
                        "dataset \u2014 it varies, and you captioned "
                        f"the variation correctly. \u201c{tag}\u201d "
                        "is its majority value, not a property of the "
                        "thing you are teaching.\n\nDrop it and the "
                        f"{round(share * 100)}% have nothing "
                        "accounting for that feature, so the trigger "
                        "absorbs it. At generation time the other "
                        "tags then have to OVERRIDE a bias welded "
                        "into your trigger, rather than composing "
                        "with a trigger that is neutral about it. "
                        "That is a fight rather than a composition, "
                        "and it shows up as inconsistency.\n\nIt "
                        f"costs {cost} tokens per caption. Control "
                        "over an attribute that genuinely varies is "
                        "almost always worth more than that.")))
                continue
            pct = round(share * 100)
            if verdict == "bake":
                report.tier2.append(Candidate(
                    **base, tier=2,
                    reason=f"on {pct}% of images — absorbed by the trigger",
                    detail=(
                        f"\u201c{tag}\u201d is on {pct}% of the "
                        "dataset, so it is very nearly a constant. Your "
                        "trigger token learns it whether or not you "
                        "write it down.\n\nThe question is not whether "
                        "the model will learn it — it will — but "
                        "whether you want to CONTROL it later:\n\n"
                        "\u2022 Drop it, and the trait becomes "
                        "intrinsic: it fires whenever the trigger "
                        f"does. Reclaims {cost * count} tokens.\n"
                        "\u2022 Keep it, and you can prompt against it "
                        "to vary the trait.\n\nSame tag, same number, "
                        "opposite answers depending on what you want "
                        "the model to do. Only you know that.")))
            elif verdict == "conflict":
                report.tier2.append(Candidate(
                    **base, tier=2,
                    kind=KIND_CONFLICT,
                    reason=_conflict_reason(pct, category, modes),
                    detail=(
                        f"\u201c{tag}\u201d is on {pct}% of the "
                        "dataset, and the goals you selected want "
                        "opposite things from its category "
                        f"(\u201c{category}\u201d).\n\n"
                        + MIXED_GOAL_NOTE +
                        "\n\nAsk yourself which target this tag "
                        "belongs to. If it is part of the concept you "
                        "are teaching, drop it. If it is content that "
                        "merely happens to be common, keep it, or the "
                        "concept absorbs it.")))
            else:
                report.tier3.append(Candidate(
                    **base, tier=3,
                    kind=KIND_GOAL,
                    reason=f"on {pct}% but protected by your goal",
                    detail=(
                        f"\u201c{tag}\u201d is on {pct}% of the "
                        "dataset, which normally makes a tag a "
                        "pruning candidate \u2014 but for what you are "
                        "training it must stay.\n\nIts category "
                        f"(\u201c{category}\u201d) is one that has to "
                        "remain described so it does not fuse into "
                        "your trigger. Dropping it would save "
                        f"{cost * count} tokens and teach the model "
                        "that this trait is part of the thing you are "
                        "training, which is exactly what you do not "
                        "want here.")))

    report.tier1.sort(key=lambda c: c.priority())
    report.tier2.sort(key=lambda c: c.priority())
    report.tier3.sort(key=lambda c: c.priority())

    if len(modes) > 1:
        report.notes.append(MIXED_GOAL_NOTE)
    if report.tier1:
        report.notes.append(
            f"Tier 1 alone reclaims {report.tokens_reclaimable()} "
            "tokens across the dataset without needing to know what "
            "you are training.")
    if over:
        report.notes.append(
            f"{len(over)} caption(s) are over the limit. Candidates "
            "are ranked by how much they relieve THOSE captions "
            "first.")
    return report


def build_prune_report(report: PruneReport, roots=()) -> str:
    """Markdown, in the same spirit as the other exports: a list to
    act on by hand, not an instruction to the program."""
    import datetime

    lines = ["# TagWalker Pruning Candidates", ""]
    lines.append("Generated: "
                 f"{datetime.datetime.now().isoformat(timespec='seconds')}")
    for r in roots or ():
        lines.append(f"Dataset root: {r}")
    goals = ", ".join(MODE_LABELS.get(m, m) for m in report.modes)
    lines.append(f"Training goal(s): {goals}")
    if report.era_label:
        lines.append(
            f"Judged against tag database: {report.era_label} "
            "(Settings \u2192 tag database)")
    if report.epochs:
        lines.append(
            f"Rarity judged by training exposure: {report.epochs} "
            f"epoch(s), folder repeats applied, floor "
            f"{report.exposure_floor} exposures")
    else:
        lines.append("Rarity judged by share of the dataset")
    lines.append(
        f"Images: {report.total_images} \u00b7 distinct tags: "
        f"{report.total_tags} \u00b7 captions over limit: "
        f"{report.over_limit_count}")
    lines.append("")
    lines.append("## How to read this (for AI reviewers)")
    lines.append("")
    lines.append(
        "Nothing here has been changed on disk. Tier 1 is safe "
        "without knowing the training intent. Tier 2 depends on "
        "whether the user wants the trait baked into the trigger or "
        "left controllable. Tier 3 lists tags that look like "
        "candidates but must be KEPT for the selected goal. "
        "\u201cTokens each\u201d is the tag's cost in one caption "
        "including its separating comma; \u201ctokens total\u201d is "
        "that multiplied by the images carrying it. For getting long "
        "captions under a limit, the per-caption figure is the one "
        "that matters.")
    lines.append("")
    for tier, title in (
        (report.tier1, "Tier 1 \u2014 safe to prune"),
        (report.tier2, "Tier 2 \u2014 your call"),
        (report.tier3, "Tier 3 \u2014 keep, despite looking prunable"),
    ):
        lines.append(f"## {title} ({len(tier)})")
        lines.append("")
        if not tier:
            lines.append("(none)")
            lines.append("")
            continue
        if tier is report.tier3:
            # Same grouping as the window: a flat list of two hundred
            # rows is no more readable in a file than on screen.
            by_kind: dict = {}
            for c in tier:
                by_kind.setdefault(c.kind or "", []).append(c)
            if len(by_kind) > 1:
                for kind in (KIND_CONFLICT, KIND_TRIGGER, KIND_MAJORITY,
                             KIND_KNOWN, KIND_GOAL, ""):
                    rows = by_kind.get(kind)
                    if not rows:
                        continue
                    label, why = KIND_LABELS.get(
                        kind, ("Other", ""))
                    lines.append("")
                    lines.append(f"**{label}** ({len(rows)})")
                    if why:
                        lines.append("")
                        lines.append(why)
                    lines.append("")
                    lines.append("| tag | images | category | "
                                 "tokens each | tokens total | why |")
                    lines.append("|---|---:|---|---:|---:|---|")
                    for c in rows:
                        lines.append(
                            f"| {c.tag} | {c.count} | {c.category} | "
                            f"{c.token_cost} | {c.tokens_saved} | "
                            f"{c.reason} |")
                continue
        lines.append("| tag | images | category | tokens each | "
                     "tokens total | why |")
        lines.append("|---|---:|---|---:|---:|---|")
        for c in tier:
            lines.append(
                f"| {c.tag} | {c.count} | {c.category} | "
                f"{c.token_cost} | {c.tokens_saved} | {c.reason} |")
        lines.append("")
    if report.notes:
        lines.append("## Notes")
        lines.append("")
        for note in report.notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines)


@dataclass
class TagFamily:
    """A broad tag and the narrower tags that specialise it."""

    broad: str
    broad_count: int = 0
    broad_known: int = 0
    members: list = field(default_factory=list)   # (tag, count, known)
    covered: int = 0          # images of `broad` also carrying a member
    # The images carrying the broad tag and NO narrower one. This is
    # the set the user has to inspect before dropping the broad tag —
    # exactly the "12 images that carry thighhighs alone" — and the
    # advisor already computes it to get the coverage count, so it
    # costs nothing to keep instead of discarding.
    uncovered: frozenset = frozenset()

    @property
    def coverage(self) -> float:
        return (self.covered / self.broad_count) if self.broad_count else 0.0

    @property
    def total_members(self) -> int:
        return sum(count for _tag, count, _known in self.members)


def _narrower_index(tags) -> dict:
    """word -> tags containing it, for finding candidates fast.

    covers() requires the broad tag's words to appear as a contiguous
    run inside the narrow one, so every word of the broad tag must be
    present in the narrow one. Looking up just one of those words
    therefore cannot miss a match, and it shrinks the comparison from
    every-tag-against-every-tag to a handful.

    MEASURED: on a real 1,641-tag set the old pairwise scan made
    1.56 million covers() calls, twice — once here and once for the
    tier verdicts — and froze the window for five seconds.
    """
    index: dict = {}
    for tag in tags:
        for word in _parts(tag):
            index.setdefault(word, []).append(tag)
    return index


def _narrower_than(broad: str, index: dict) -> list:
    """Tags that specialise `broad`, found via the word index."""
    words = _parts(broad)
    if not words:
        return []
    # The rarest word narrows hardest. Picking it rather than the
    # first turns a common word like "hair" from a near-full scan
    # into a short list.
    pivot = min(words, key=lambda w: len(index.get(w, ())))
    return [candidate for candidate in index.get(pivot, ())
            if candidate != broad and covers(broad, candidate)]


def find_families(tag_images: dict, base_model_count=None,
                  min_members: int = 1) -> list:
    """Group tags into broad/narrow families.

    This is the table the export was missing. Asked to compare the
    glove variants in a dataset, a language model could not — the
    report carried the advisor's findings but never the vocabulary the
    question was about, so the honest answer was to refuse.

    COVERAGE is the figure that decides most of these cases, and it is
    arithmetic rather than judgement: of the images carrying the broad
    tag, how many also carry one of the narrow ones. At 100% the broad
    tag describes nothing the narrow ones do not. Below that, dropping
    it leaves the remainder undescribed, and the shortfall is exactly
    how many images that is.
    """
    counts = {tag: len(images) for tag, images in tag_images.items()}
    known = (lambda t: int(base_model_count(t) or 0)) \
        if base_model_count else (lambda t: 0)

    index = _narrower_index(tag_images)
    families = []
    for broad, broad_images in tag_images.items():
        narrower = _narrower_than(broad, index)
        if len(narrower) < min_members:
            continue
        members = [(narrow, counts[narrow], known(narrow))
                   for narrow in narrower]
        covered = set()
        for narrow, _count, _k in members:
            covered |= (broad_images & tag_images[narrow])
        members.sort(key=lambda row: -row[1])
        families.append(TagFamily(
            broad=broad, broad_count=counts[broad],
            broad_known=known(broad), members=members,
            covered=len(covered),
            uncovered=frozenset(broad_images - covered)))
    # Largest first: the broad tag on the most images is the one whose
    # removal changes the most captions.
    families.sort(key=lambda fam: (-fam.broad_count, fam.broad))
    return families


# Most a family will list by name before collapsing to a count. Enough
# to act on directly; not so many that one broad tag buries the report.
UNCOVERED_LIST_CAP = 40


def _name_of(path) -> str:
    """Filename for display, tolerant of a str or a Path."""
    try:
        return path.name
    except AttributeError:
        text = str(path).replace("\\", "/")
        return text.rsplit("/", 1)[-1]


def build_families_section(families: list) -> str:
    """The families table, for the export."""
    if not families:
        return ""
    lines = ["## Tag families", "",
             "Broad tags in this set that have narrower forms, and how "
             "completely the narrow ones cover them.", "",
             "**Coverage** is of the broad tag's images: at 100% every "
             "image carrying the broad tag also carries a narrow one, "
             "so the broad tag describes nothing the narrow ones do "
             "not. Below 100%, the shortfall is the number of images "
             "that would be left undescribed if it were dropped.", ""]
    for family in families:
        shortfall = family.broad_count - family.covered
        lines.append(
            f"**{family.broad}** \u2014 {family.broad_count:,} images, "
            f"{family.broad_known:,} base-model posts")
        for tag, count, tag_known in family.members:
            lines.append(f"  - `{tag}` \u2014 {count:,} images, "
                         f"{tag_known:,} base-model posts")
        note = (f"coverage **{family.coverage:.0%}**"
                + (f" \u2014 {shortfall:,} image(s) carry "
                   f"`{family.broad}` and no narrower tag"
                   if shortfall else
                   " \u2014 fully covered"))
        lines.append(f"  - {note}")
        # The specific files that carry the broad tag alone. These are
        # the ones to check before dropping it: if the broad tag goes,
        # their captions fall silent about a feature that is in the
        # picture. Listing them by name turns "12 images somewhere"
        # into a list you can open. Capped so a pathological family
        # cannot flood the report; the count above is always exact.
        if family.uncovered:
            shown = sorted(_name_of(path) for path in family.uncovered)
            lines.append(f"  - carries `{family.broad}` alone:")
            for name in shown[:UNCOVERED_LIST_CAP]:
                lines.append(f"    - {name}")
            if len(shown) > UNCOVERED_LIST_CAP:
                lines.append(f"    - \u2026 and "
                             f"{len(shown) - UNCOVERED_LIST_CAP:,} more")
        lines.append("")
    return "\n".join(lines)


def build_all_captions_section(image_captions) -> str:
    """A full dump of every image's caption, one per image, sorted by
    filename — the raw material the statistical tables summarise away.

    Why this exists: the tables report how OFTEN a tag appears, not how
    it sits ALONGSIDE others in a given image. Some problems only show
    at the per-image level — e.g. a set that expresses an ambiguous eye
    colour by tagging different images with different variants
    (``blue_eyes`` here, ``green_eyes`` there, ``heterochromia``
    elsewhere). A frequency table shows three smallish counts and
    nothing links them; the per-image captions make the pattern
    obvious. Handing this to a model lets it plan token-saving that is
    aware of how the captions are actually written, not just how the
    vocabulary tallies.

    Parameters
    ----------
    image_captions : iterable of (name, tags)
        ``name`` is the display filename (a string); ``tags`` is that
        image's ordered tag list. The caller supplies already-resolved
        pairs so this stays free of any state/Path dependency and is
        trivially testable.

    Efficiency
    ----------
    One pass to materialise, one sort by name, one ``"\\n".join``. No
    per-item string concatenation (which would be O(n^2) on a large
    set) and no repeated lookups — the cost is O(n log n) for the sort
    and O(total characters) to assemble, which is the irreducible
    minimum for "print everything". Captions are joined with the same
    ``", "`` separator used on disk, so each line is exactly what the
    .txt file holds.
    """
    # Materialise once. Each item becomes a (name, caption_text) pair;
    # the caption text is the on-disk form so what the model sees equals
    # what is in the file.
    pairs = [
        (name, ", ".join(tags))
        for name, tags in image_captions
    ]
    if not pairs:
        return ""
    pairs.sort(key=lambda nc: nc[0])

    total_images = len(pairs)
    captioned = sum(1 for _, cap in pairs if cap)
    empty = total_images - captioned

    lines = [
        "## All captions (per image)",
        "",
        "Every image's caption in full, sorted by filename. This is the "
        "raw per-image data behind the frequency tables above: use it to "
        "spot patterns a count cannot show \u2014 for example the same "
        "attribute expressed by different tags across images (ambiguous "
        "eye colour tagged as several variants), near-duplicate captions, "
        "or a tag that only ever appears next to one other.",
        "",
        f"{total_images:,} image(s)"
        + (f", {empty:,} with an empty caption" if empty else ""),
        "",
    ]
    # One line per image: `filename` — the caption exactly as written.
    # An empty caption is called out rather than shown as a blank, so a
    # reader can tell "no tags" from a formatting gap.
    for name, caption in pairs:
        if caption:
            lines.append(f"- `{name}` \u2014 {caption}")
        else:
            lines.append(f"- `{name}` \u2014 *(empty caption)*")
    lines.append("")
    return "\n".join(lines)


def build_tag_index_section(image_captions) -> str:
    """An inverted index: each tag and the exact list of images that
    carry it as a whole tag, sorted alphabetically by tag.

    Why this shape rather than the per-image dump: a question like
    "which images have BOTH tag X and tag Y" is answered by reading two
    authoritative lists and intersecting them, with no text search over
    captions at all. That removes the failure this was built to prevent
    \u2014 a substring search matching `pink_eyes` inside `pink_eyeshadow`,
    or a greedy pattern spanning across image boundaries \u2014 because each
    tag here is a separate, explicitly named entry. `pink_eyes` and
    `pink_eyeshadow` occupy different lines and can never be confused.

    Lists are COMPLETE, not capped: an intersection is only correct if
    both sides are whole, so a tag on a thousand images produces a long
    line by design. The reader who turns this on is doing exact
    set-membership work and needs it.

    Parameters
    ----------
    image_captions : iterable of (name, tags)
        ``name`` is the display filename; ``tags`` is that image's tag
        list. Pairs are already resolved by the caller so this stays
        free of any state/Path dependency and is trivially testable.

    Efficiency
    ----------
    One pass to build the index (each image's tags appended to the
    right buckets), then one sort of the tag names and, per tag, one
    sort of its filenames. Assembly is a single ``"\\n".join`` \u2014 no
    per-item string concatenation. Overall O(T log T + sum of
    per-tag(k log k)) which, since every image contributes its tags
    exactly once, is O(N_tags_total log ...) \u2014 the irreducible cost of
    grouping and ordering, with no quadratic blow-up.
    """
    # Build the inverted index in one pass. Each tag maps to the list
    # of filenames carrying it.
    index: dict[str, list[str]] = {}
    image_count = 0
    for name, tags in image_captions:
        image_count += 1
        for tag in tags:
            bucket = index.get(tag)
            if bucket is None:
                index[tag] = [name]
            else:
                bucket.append(name)

    if not index:
        return ""

    lines = [
        "## Tag index (images per tag)",
        "",
        "Every tag, followed by the exact list of images that carry it "
        "(as a whole tag), sorted alphabetically. This is the inverted "
        "form of the captions: to find images that share two tags, take "
        "the two tags' lists and intersect them \u2014 no searching through "
        "caption text, so a tag can never be confused with a longer one "
        "that contains it (`pink_eyes` and `pink_eyeshadow` are separate "
        "entries here). Lists are complete.",
        "",
        f"{len(index):,} tag(s) across {image_count:,} image(s)",
        "",
    ]
    # Alphabetical by tag; within each tag, filenames sorted so two
    # lists can be compared or intersected by eye.
    for tag in sorted(index):
        members = index[tag]
        members.sort()
        joined = ", ".join(members)
        lines.append(f"- `{tag}` ({len(members):,}): {joined}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# The questionnaire block
#
# Optional, and off unless asked for. Its job is to hand a language
# model everything it needs to answer the questions this tool cannot,
# in the form it needs them — because the failure mode is not bad
# advice, it is confident advice given without the data. A model asked
# to compare the glove variants in a dataset will happily invent an
# answer if the dataset's glove tags were never in front of it.
# ---------------------------------------------------------------------

BRIEFING = """\
## For a language model reading this file

### What this is, and what it is not

Someone is pruning the captions of a LoRA training set. This file was
produced by a tool that measured their dataset. It contains the tags
the tool flagged and the broad/narrow families it found - **not their
full vocabulary**. If they ask about a tag that is not here, say so
and ask them to also send the tag statistics export
(View -> Export Tag Statistics), which lists every tag in the set
with its counts. Never state a count that is not in front of you.

### Where the tool stops

It counts, and it is reliable about counting. It knows how many
images carry each tag, how many posts the base model saw for that tag
in the chosen era, and how completely a family's narrow tags cover
their broad one.

It cannot know intent, and three kinds of intent decide most pruning:

- whether a tag is being held for images not yet collected
- whether a tag is wanted for prompt control rather than for the
  model to learn
- which of a broad/narrow pair the person expects to prompt later

Where one of those decides a case, ask. Where it does not, answer.

### The families: audit these directly

For every family listed below, do not ask first. Produce the audit,
because the question is already settled - they want to know whether
to simplify:

1. State the coverage figure and what it means for that family.
2. Give the case FOR dropping the broad tag: tokens saved per
   caption, and across the set.
3. Give the case AGAINST: what control is lost, and how many images
   would be left undescribed if coverage is below 100%.
4. Recommend one, and say what fact would reverse your
   recommendation.

Three things worth weighing in every audit.

A broad tag with a large base-model count is a handle the model
responds to reliably - losing it costs more than losing a rare narrow
one.

A family below 100% coverage has images carrying only the broad tag;
the file lists those by name under "carries ... alone". Dropping the
broad tag makes exactly those captions silent about a feature that is
visible in the picture, so they are the ones to check first.

Some tags describe a PROPERTY that another tag already implies. When
a garment tag names both a material and a form - latex_gloves,
latex_thighhighs - and the set also carries a plain form tag with a
colour (black_gloves), the material can sometimes be carried by a
separate material tag (latex) plus the plain form, letting the
specific compound go. This only holds when the set is consistent: if
any image pairs that colour-form with a DIFFERENT material, dropping
the compound tells the model the wrong thing. So the question to ask
is whether every instance of the form in the set shares the one
material. Where the person says it does, the compound is redundant;
where they are unsure, it is not worth the risk for the tokens.

### The tiers

Tier 1 is safe without knowing intent. Tier 2 depends on whether the
trait should be baked into the trigger or left controllable - that is
an intent question, so ask it. Tier 3 lists tags that look like
candidates and should be kept, with the reason attached.

Tags under "Already decided" have been settled by the user. Do not
re-argue them; treat them as fixed and reason around them.

### Searching the captions

If a captions or tag-index section is included below and you search
it, match WHOLE tags, never raw substrings. Tags are separated by
commas; a tag is the text between commas, trimmed. Two rules prevent
the errors that substring searching causes:

First, a tag can be contained inside a longer tag. A plain substring
search for one tag will also match every longer tag it sits inside of
- for instance a search for any tag that is a prefix of another (one
such pair is a tag ending in a word that also begins a compound tag
present in the set). Anchor on the comma delimiters, or require a
whole-word match, so a shorter tag never matches inside a longer one.

Second, when looking for images that carry TWO tags, do not search
for one tag followed by the other across the text: a caption file is
many images concatenated, and a span-matching pattern will pair a tag
on one image with a tag on a different image. Resolve each tag to its
own set of images first, then intersect the two sets. The tag-index
section, if present, gives those sets directly.
"""


def build_search_notes() -> str:
    """The search-safety guidance as a standalone section.

    Currently the briefing string carries this inline (it is part of
    the AI instructions block and gated with it). Exposed as a function
    too so a test can assert the guidance is present and self-contained
    without parsing the whole briefing.
    """
    # Kept identical to the "Searching the captions" section of the
    # briefing above; if that text changes, change it here as well.
    return (
        "### Searching the captions\n\n"
        "If a captions or tag-index section is included below and you "
        "search it, match WHOLE tags, never raw substrings. Tags are "
        "separated by commas; a tag is the text between commas, "
        "trimmed. Two rules prevent the errors that substring "
        "searching causes:\n\n"
        "First, a tag can be contained inside a longer tag. A plain "
        "substring search for one tag will also match every longer tag "
        "it sits inside of - for instance a search for any tag that is "
        "a prefix of another (one such pair is a tag ending in a word "
        "that also begins a compound tag present in the set). Anchor on "
        "the comma delimiters, or require a whole-word match, so a "
        "shorter tag never matches inside a longer one.\n\n"
        "Second, when looking for images that carry TWO tags, do not "
        "search for one tag followed by the other across the text: a "
        "caption file is many images concatenated, and a span-matching "
        "pattern will pair a tag on one image with a tag on a different "
        "image. Resolve each tag to its own set of images first, then "
        "intersect the two sets. The tag-index section, if present, "
        "gives those sets directly."
    )


def build_questionnaire(report: "PruneReport") -> str:
    """The block, with this dataset's specifics filled in."""
    lines = [BRIEFING, "### This dataset", "",
             f"- {report.total_images:,} images, "
             f"{report.total_tags:,} distinct tags",
             f"- judged against: {report.era_label or 'unknown era'}",
             f"- training goal(s): "
             f"{', '.join(report.modes) or 'not stated'}"]
    if report.settled:
        lines.append("")
        lines.append("### Already decided \u2014 do not re-argue these")
        lines.append("")
        for candidate in report.settled:
            lines.append(f"- `{candidate.tag}` \u2014 "
                         f"{candidate.reason}")
    return "\n".join(lines)
