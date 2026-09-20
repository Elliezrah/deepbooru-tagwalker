"""
core/dataset_stats.py

What the dataset looks like as a training artifact.

The statistics window used to report on the WALK — completion
percentage, decisions made, tags finished. Useful, but it answers "how
far through the work am I", which the progress bar already says. It
never answered "is this dataset any good", which is the question that
decides whether training succeeds.

Everything here is computed from data the program already holds: the
CLIP tokeniser it uses for the token counter, the era snapshots behind
the Tag Referencer, and the captions themselves. No estimates, no
predictions — every number below is a count of something real.

Qt-free on purpose, so the arithmetic can be tested without a window.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Caption length bands, in CLIP tokens. The boundaries are the limits
# that actually bite in training: 75 is one CLIP chunk, 225 is three
# and the common practical ceiling, 512 is where Flux's T5 sits.
LENGTH_BANDS: list[tuple[str, int, int]] = [
    ("0-75", 0, 75),
    ("76-150", 76, 150),
    ("151-225", 151, 225),
    ("226-512", 226, 512),
    ("over 512", 513, 10 ** 9),
]

# Tag frequency bands. The leftmost is the interesting one: a tag on
# one or two images out of a thousand has no chance of being learned,
# so that bar is the "wasted vocabulary" pile.
FREQUENCY_BANDS: list[tuple[str, int, int]] = [
    ("1-2 images", 1, 2),
    ("3-10", 3, 10),
    ("11-50", 11, 50),
    ("51-200", 51, 200),
    ("over 200", 201, 10 ** 9),
]

# Tags-per-image bands. Uneven caption density is a real dataset flaw
# and nothing else surfaces it: if half the images carry eight tags
# and half carry thirty-five, the model is supervised unevenly.
DENSITY_BANDS: list[tuple[str, int, int]] = [
    ("0-5 tags", 0, 5),
    ("6-15", 6, 15),
    ("16-25", 16, 25),
    ("26-40", 26, 40),
    ("over 40", 41, 10 ** 9),
]

# Posts in the selected snapshot above which the base model is taken
# to know a tag well. Matches the pruning advisor's threshold so the
# two tools cannot disagree about the same tag.
KNOWN_WELL = 1000
KNOWN_AT_ALL = 1


# A gap smaller than this is not worth reporting: with 200 images a
# single picture moves a pairing by half a point, so small differences
# are counting noise rather than signal.
MIN_GAP_POINTS = 15

# And a partner has to appear at all before its absence means
# anything.
MIN_PARTNER_IMAGES = 3


@dataclass
class Divergence:
    """One pairing where the dataset and Danbooru disagree."""

    partner: str
    yours_pct: int
    theirs_pct: int
    gap: int                 # positive: you pair it more than they do
    unlisted: bool = False   # absent from Danbooru's stored partners

    def label(self) -> str:
        """Short enough to survive a narrow, non-resizable pane.

        The first version read "(not on their list)" and was cut off
        mid-word in the real window. "new" carries the same meaning in
        four characters, and the pane heading supplies the context.
        """
        if self.unlisted:
            return f"{self.partner}  {self.yours_pct}%  new"
        return (f"{self.partner}  {self.yours_pct}/"
                f"{self.theirs_pct}%")


def compare_with_danbooru(partner_counts: dict, tag_images: int,
                          danbooru_partners, reverse_lookup=None,
                          limit: int = 12) -> tuple[list, list]:
    """Where your captions and Danbooru's habits disagree.

    "Least often appears with" used to fill this space, and it was
    noise by construction: the bottom of a long tail is arbitrary. The
    useful question is not which partners are rare, but which ones
    your dataset treats DIFFERENTLY from the site the base model
    learned on.

    Returns (you_more, they_more):

      you_more  — pairings you use far more than Danbooru. Either the
                  signature of the concept you are teaching, or an
                  accidental correlation that will be baked into it.
      they_more — pairings Danbooru expects and your captions mostly
                  omit. Often a tag you are simply not writing down.

    partner_counts   : partner tag -> images carrying it alongside the
                       subject tag
    tag_images       : how many images carry the subject tag
    danbooru_partners: [(partner, fraction 0..1), ...]
    reverse_lookup   : partner -> the same fraction derived from the
                       PARTNER's own stored list, or None if that side
                       does not know the subject tag either.

                       FIELD BUG: the data stores only thirty partners
                       per tag. For a hub tag like 1girl, with
                       thousands of partners, its own top thirty are
                       all ubiquitous — so ordinary tags like sweat,
                       nude and censored fell off it and were reported
                       as "not on their list" when Danbooru pairs them
                       constantly. Six of seven entries in a real
                       screenshot were wrong this way.

                       They are not missing; they are stored from the
                       other direction. Asking the partner recovers a
                       real figure instead of a false claim.
    """
    if tag_images <= 0:
        return ([], [])

    # FIELD BUG: Danbooru stores tags with underscores, captions may
    # use spaces, and these were compared literally. A user whose
    # captions read "blue eyes" was told both that they pair it far
    # MORE than Danbooru (100% vs 0%) and far LESS (0% vs 30%) — the
    # same tag in both panes, contradicting itself, because neither
    # lookup found the other's spelling.
    #
    # Matched on a folded key; displayed in the caption's own spelling,
    # so the user recognises their own tag.
    def fold(name: str) -> str:
        return (name or "").strip().lower().replace(" ", "_")

    theirs = {fold(name): pct for name, pct in danbooru_partners}
    # folded name -> (spelling as the user wrote it, image count)
    mine = {fold(name): (name, count)
            for name, count in partner_counts.items()}
    # A partner missing from their stored list is not known to be
    # zero — it is only known to be below their smallest stored
    # figure. Reported as "not on their list" rather than as 0%, which
    # would be a claim the data does not support.
    floor = min(theirs.values(), default=0.0)

    you_more: list[Divergence] = []
    they_more: list[Divergence] = []

    for key, (partner, count) in mine.items():
        if count < MIN_PARTNER_IMAGES:
            continue
        yours = 100.0 * count / tag_images
        their_fraction = theirs.get(key)
        if their_fraction is None and reverse_lookup is not None:
            try:
                their_fraction = reverse_lookup(partner)
            except Exception:
                their_fraction = None
        if their_fraction is not None:
            their_pct = 100.0 * their_fraction
            gap = yours - their_pct
            if gap >= MIN_GAP_POINTS:
                you_more.append(Divergence(
                    partner, round(yours), round(their_pct),
                    round(gap)))
        elif yours - 100.0 * floor >= MIN_GAP_POINTS:
            you_more.append(Divergence(
                partner, round(yours), 0, round(yours),
                unlisted=True))

    for key, fraction in theirs.items():
        their_pct = 100.0 * fraction
        partner, count = mine.get(key, (key, 0))
        yours = 100.0 * count / tag_images
        gap = their_pct - yours
        if gap >= MIN_GAP_POINTS:
            they_more.append(Divergence(
                partner, round(yours), round(their_pct), round(gap)))

    you_more.sort(key=lambda d: (-d.gap, d.partner))
    they_more.sort(key=lambda d: (-d.gap, d.partner))
    return (you_more[:limit], they_more[:limit])


@dataclass
class Band:
    label: str
    count: int = 0

    def as_pair(self) -> tuple[str, int]:
        return (self.label, self.count)


@dataclass
class DatasetStats:
    """Every figure the statistics window draws."""

    caption_lengths: list[Band] = field(default_factory=list)
    over_limit: int = 0
    limit_used: int = 225
    longest_caption: int = 0
    median_caption: int = 0

    vocabulary: list[Band] = field(default_factory=list)
    era_label: str = ""

    frequency: list[Band] = field(default_factory=list)
    rare_tags: int = 0

    density: list[Band] = field(default_factory=list)
    thinnest: int = 0
    fattest: int = 0

    folders: list[tuple[str, int, int]] = field(default_factory=list)
    folders_meaningful: bool = False

    total_images: int = 0
    total_tags: int = 0

    def caption_pairs(self) -> list[tuple[str, int]]:
        return [b.as_pair() for b in self.caption_lengths]

    def frequency_pairs(self) -> list[tuple[str, int]]:
        return [b.as_pair() for b in self.frequency]

    def density_pairs(self) -> list[tuple[str, int]]:
        return [b.as_pair() for b in self.density]

    def vocabulary_pairs(self) -> list[tuple[str, int]]:
        return [b.as_pair() for b in self.vocabulary]


def _bands(spec, values) -> list[Band]:
    bands = [Band(label) for label, _lo, _hi in spec]
    for value in values:
        for i, (_label, low, high) in enumerate(spec):
            if low <= value <= high:
                bands[i].count += 1
                break
    return bands


def _median(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def analyse(image_tags: dict, token_count, base_model_count=None,
            era_label: str = "", limit: int = 225,
            repeats_of=None, folder_of=None) -> DatasetStats:
    """Build every figure from the loaded dataset.

    image_tags       : image identifier -> list of tags on it
    token_count      : caption text -> CLIP token count
    base_model_count : tag -> posts in the selected era snapshot, or
                       None to skip the vocabulary breakdown
    repeats_of       : image identifier -> its kohya repeat count
    folder_of        : image identifier -> the folder it belongs to.
                       Supplied by the caller rather than read off the
                       entry, because the two ways of loading put the
                       answer in different places: one root with
                       subfolders records them on the entry, while
                       several roots loaded together are each scanned
                       root-only and are told apart by their own
                       directory name.
    """
    stats = DatasetStats(limit_used=int(limit), era_label=era_label)
    stats.total_images = len(image_tags)

    lengths: list[int] = []
    densities: list[int] = []
    tag_images: dict = {}
    for image, tags in image_tags.items():
        tags = list(tags or [])
        densities.append(len(tags))
        lengths.append(int(token_count(", ".join(tags))) if tags else 0)
        for tag in tags:
            tag_images.setdefault(tag, set()).add(image)

    stats.caption_lengths = _bands(LENGTH_BANDS, lengths)
    stats.over_limit = sum(1 for n in lengths if n > stats.limit_used)
    stats.longest_caption = max(lengths, default=0)
    stats.median_caption = _median(lengths)

    stats.density = _bands(DENSITY_BANDS, densities)
    stats.thinnest = min(densities, default=0)
    stats.fattest = max(densities, default=0)

    counts = [len(v) for v in tag_images.values()]
    stats.frequency = _bands(FREQUENCY_BANDS, counts)
    stats.rare_tags = sum(1 for n in counts if n <= 2)
    stats.total_tags = len(tag_images)

    if base_model_count is not None:
        known_well = rare = unknown = 0
        for tag in tag_images:
            try:
                posts = int(base_model_count(tag) or 0)
            except Exception:
                posts = 0
            if posts >= KNOWN_WELL:
                known_well += 1
            elif posts >= KNOWN_AT_ALL:
                rare += 1
            else:
                unknown += 1
        stats.vocabulary = [
            Band("Known well", known_well),
            Band("Rare there", rare),
            Band("Never seen", unknown),
        ]

    # Per-folder is only drawn when it has something to say. With one
    # folder and no repeats it would be a single bar — a panel that is
    # always present and usually blank reads as broken, so it is left
    # out entirely instead.
    per_folder: dict = {}
    for image in image_tags:
        if folder_of is not None:
            try:
                folder = folder_of(image) or ""
            except Exception:
                folder = ""
        else:
            folder = getattr(image, "subfolder", "") or ""
        repeats = 1
        if repeats_of is not None:
            try:
                repeats = max(1, int(repeats_of(image) or 1))
            except Exception:
                repeats = 1
        images, exposure = per_folder.get(folder, (0, 0))
        per_folder[folder] = (images + 1, exposure + repeats)
    stats.folders = sorted(
        ((name or "(root)", n, exp)
         for name, (n, exp) in per_folder.items()),
        key=lambda row: -row[1])
    stats.folders_meaningful = (
        len(per_folder) > 1
        or any(exp != n for _name, n, exp in stats.folders))
    return stats
