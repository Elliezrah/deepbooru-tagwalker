"""
core/tag_database.py

Danbooru tag database — lazy-loaded reference for tag auditing.

This module wraps the bundled Danbooru tag list (resources/danbooru_tags.csv)
and provides fast lookups for the CSV cross-reference audit tool (feature F)
and tag auto-complete.

The CSV has four columns per row:
    tag_name, category_id, post_count, aliases

  - category_id: 0=general, 1=artist, 3=copyright, 4=character, 5=meta
  - post_count : how many Danbooru posts use the tag (popularity signal)
  - aliases    : comma-separated "wrong-way" spellings that redirect to
                 this canonical tag (e.g. "pony_tail" -> "ponytail")

Design notes
------------
* LAZY: the database is NOT read at import or app startup. It loads on the
  first call to `get_database().ensure_loaded()` (or any lookup), which the
  audit dialog triggers when the user clicks Scan. Loading ~201k rows takes
  roughly half a second; cross-referencing afterwards is effectively instant
  (O(1) set/dict lookups). Startup cost is therefore zero until the feature
  is actually used, then it's cached for the rest of the session.

* SINGLETON: one shared instance via get_database(), so the half-second load
  happens at most once per run.

* CATEGORIES drive the audit "scope": the user chooses which categories
  count as valid for a given scan (e.g. general+meta+character+copyright by
  default). A tag that exists in the database but in an EXCLUDED category is
  reported distinctly ("real tag, out of scope") rather than as an unknown
  typo, so the user can make an informed delete/keep choice.
"""

from __future__ import annotations

import bisect
import csv
import os
import sys
from pathlib import Path
from typing import Optional


# Category id constants (from the Danbooru tag API).
CAT_GENERAL = "0"
CAT_ARTIST = "1"
CAT_COPYRIGHT = "3"
CAT_CHARACTER = "4"
CAT_META = "5"

CATEGORY_NAMES = {
    CAT_GENERAL: "general",
    CAT_ARTIST: "artist",
    CAT_COPYRIGHT: "copyright",
    CAT_CHARACTER: "character",
    CAT_META: "meta",
}

# The audit scopes the user can choose, mapping a scope key to the set of
# category ids that count as "valid" under that scope. The default (B) treats
# deliberate character/copyright tags as valid while still flagging stray
# artist tags. See the design discussion for the rationale.
SCOPE_GENERAL_META = "general_meta"
SCOPE_DEFAULT = "general_meta_char_copy"
SCOPE_EVERYTHING = "everything"

SCOPES: dict[str, tuple[str, set[str]]] = {
    # key: (human label, set of valid category ids)
    SCOPE_GENERAL_META: (
        "General + Meta",
        {CAT_GENERAL, CAT_META},
    ),
    SCOPE_DEFAULT: (
        "General + Meta + Character + Copyright",
        {CAT_GENERAL, CAT_META, CAT_CHARACTER, CAT_COPYRIGHT},
    ),
    SCOPE_EVERYTHING: (
        "Everything (incl. Artist)",
        {CAT_GENERAL, CAT_ARTIST, CAT_COPYRIGHT, CAT_CHARACTER, CAT_META},
    ),
}


# Audit verdict kinds for a single tag.
VERDICT_VALID = "valid"            # in canonical set AND within scope
VERDICT_OUT_OF_SCOPE = "out_of_scope"  # real tag, but category excluded
VERDICT_ALIAS = "alias"            # matches a known alias -> canonical (fixable)
VERDICT_COMPOUND = "compound"      # color/modifier + registered tag (valid)
VERDICT_UNKNOWN = "unknown"        # not in db at all (likely typo / custom)


# ---------------------------------------------------------------------------
# Color-compound grammar
# ---------------------------------------------------------------------------
#
# Danbooru's tag list can't register every valid "<color>_<noun>" or
# "<shade>_<color>_<noun>" combination — e.g. pink_toenails and
# red_curtains are perfectly good training tags but aren't in the CSV.
# Cross-referencing alone flags them as unknown (false positives).
#
# Instead of pre-generating millions of color combinations, we recognize
# them with a small grammar at scan time: peel recognized leading color /
# shade tokens off a tag and, if what REMAINS is itself a registered tag,
# treat the whole thing as a valid compound. The remainder MUST be a real
# registered tag — so red_curtains (curtains is real) passes, but
# red_curtian (a typo) and pink_zzzgibberish stay flagged. This handles
# infinitely many combinations from a ~50-word vocabulary and never
# rubber-stamps a misspelled noun.

# Base color words. A generous, researched set — over-inclusion is safe
# because the remainder must still be a registered tag for a compound to
# be approved. Includes the colors that actually appear as leading tokens
# in the database plus standard English/anime color vocabulary.
BASE_COLORS = frozenset({
    "red", "orange", "yellow", "green", "blue", "purple", "pink", "brown",
    "black", "white", "grey", "gray", "silver", "gold", "aqua", "cyan",
    "magenta", "violet", "beige", "tan", "maroon", "navy", "olive",
    "turquoise", "lavender", "crimson", "scarlet", "azure", "indigo",
    "teal", "blonde", "platinum", "copper", "bronze", "ivory", "amber",
    "emerald", "ruby", "sapphire", "rose", "peach", "mint", "lime",
    "salmon", "coral", "burgundy", "khaki", "mauve", "cream", "charcoal",
    "rainbow", "multicolored", "colored",
})

# Shade / "temperature" modifiers that can lead a color compound. Drawn
# from the words that actually appear as leading modifier tokens in the
# database, plus standard shade words.
COLOR_MODIFIERS = frozenset({
    "light", "dark", "deep", "pale", "bright", "dull", "vivid", "muted",
    "soft", "hot", "cold", "warm", "cool", "rich", "faded", "pastel",
    "neon", "dim", "deepe", "two-tone", "multicolored", "gradient",
})


def _resource_path(relative: str) -> Path:
    """Resolve a bundled resource path for both source and frozen runs.

    Mirrors main._resource_path but lives here so core/ has no dependency
    on the app entry point. When frozen with PyInstaller --onefile, bundled
    data is unpacked under sys._MEIPASS; from source it's relative to the
    project root (the parent of this file's directory).
    """
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        # core/tag_database.py -> project root is one level up from core/.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Path(base) / relative


def _normalize(tag: str) -> str:
    """Normalize a caption tag for comparison against the database.

    - Strips surrounding whitespace.
    - Removes backslash-escaping of parentheses (\\( \\) -> ( )), so an
      already-escaped tag like "bremerton_\\(azur_lane\\)" matches the raw
      canonical "bremerton_(azur_lane)". (We decided not to ship the escape
      conversion tool, but matching stays escape-insensitive so an escaped
      caption is never falsely flagged as unknown.)
    - Lowercases: Danbooru tags are lowercase by convention.
    - Folds spaces to underscores, so a space-formatted caption
      ("long hair") matches the canonical form ("long_hair").
    """
    t = tag.strip().replace("\\(", "(").replace("\\)", ")")
    # FIELD BUG: spaces were not folded, so a caption written
    # "long hair" never matched the canonical "long_hair" and every
    # multi-word tag in a space-formatted dataset came back "not a
    # known Danbooru tag". The audit was unusable for anyone who does
    # not use underscores — and this program ships a bulk
    # underscore<->space reformat tool, so both formats are supported
    # on purpose.
    #
    # Danbooru tags never contain spaces, so folding cannot collide
    # with a real tag.
    return t.lower().replace(" ", "_")


def _match_format(original: str, suggestion: str) -> str:
    """Write a replacement in the same style as the tag it replaces.

    The database is stored with underscores, but this program ships a
    bulk underscore<->space reformat and supports both. Handing a
    space-formatted dataset an underscored replacement would leave one
    tag written differently from every other, which is exactly the
    inconsistency the audit exists to remove.

    Emoticons are left alone: the underscores in "^_^" are structural,
    not word separators, and reformat_guard already knows which tags
    those are.
    """
    if "_" not in suggestion:
        return suggestion
    if " " not in original or "_" in original:
        return suggestion
    try:
        from core.reformat_guard import is_protected_tag
        if is_protected_tag(suggestion):
            return suggestion
    except Exception:
        pass
    return suggestion.replace("_", " ")


def format_tag_for_entry(tag: str, use_spaces: bool) -> str:
    """Return `tag` in the user's chosen ENTRY format.

    This is the single source of truth for the "Tag format:
    Underscores/Spaces" setting. Canonical Danbooru tags are stored with
    underscores; when the user prefers spaces, multi-word tags are
    written with spaces on insert (e.g. "long_hair" -> "long hair").

    Setting-driven (unlike `_match_format`, which adapts to surrounding
    text). Matching/lookup is unaffected — `_normalize` folds spaces
    either way — so this changes inserted OUTPUT only.

    Protected tags are never converted:
    - Emoticons like "^_^" or ":o_o:" use underscores structurally, not
      as word separators (reformat_guard knows which tags these are).
    - A tag with no underscore is returned unchanged.
    Underscores mode returns the canonical form untouched.
    """
    if not use_spaces:
        return tag
    if "_" not in tag:
        return tag
    try:
        from core.reformat_guard import is_protected_tag
        if is_protected_tag(tag):
            return tag
    except Exception:
        pass
    return tag.replace("_", " ")


def normalize_typed_tag(typed: str, use_spaces: bool) -> str:
    """Normalize a USER-TYPED tag to the chosen entry format.

    Different from `format_tag_for_entry`, which formats a canonical
    (underscore) tag for display. Here the input is whatever the user
    typed — which may contain spaces or underscores — and we coerce it to
    the chosen format so a single tag is stored consistently with the
    rest of the dataset:

    - Underscores mode: a typed space becomes an underscore, so "long
      hair" is stored as "long_hair" (what the user meant; matches the
      canonical form). This fixes the old silent-malformed-tag case where
      "long hair" was stored verbatim as a broken tag.
    - Spaces mode: left as typed (spaces are intra-tag word separators).

    The comma stays the real tag separator everywhere and is NOT handled
    here — callers reject commas separately (a single tag never contains
    one). Emoticons/protected tags are never altered: their underscores
    are structural, not word separators.

    Whitespace is collapsed/trimmed either way so stray double spaces
    don't produce "long__hair" (underscores) or "long  hair" (spaces).
    """
    t = typed.strip()
    if not t:
        return t
    # Protected tags (emoticons like "^_^") are stored exactly as typed.
    try:
        from core.reformat_guard import is_protected_tag
        if is_protected_tag(t):
            return t
    except Exception:
        pass
    if use_spaces:
        # Collapse internal whitespace runs to single spaces.
        return " ".join(t.split())
    # Underscores mode: any whitespace run becomes a single underscore,
    # and any existing underscores are preserved.
    return "_".join(t.split())


class TagDatabase:
    """In-memory index of the Danbooru tag list. Lazy-loaded."""

    def __init__(self, csv_path: Optional[Path] = None) -> None:
        self._csv_path = csv_path or _resource_path(
            "resources/danbooru_tags.csv"
        )
        self._loaded = False
        self._load_error: Optional[str] = None
        # canonical tag -> category id
        self._category: dict[str, str] = {}
        # canonical tag -> post_count (popularity, for autocomplete ranking)
        self._post_count: dict[str, int] = {}
        # alias spelling -> canonical tag
        self._alias_to_canonical: dict[str, str] = {}
        # sorted canonical names for bisect-based prefix autocomplete
        self._sorted_names: list[str] = []

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def ensure_loaded(self) -> bool:
        """Load and index the CSV if not already done.

        Returns True on success (or if already loaded), False if the file
        is missing or unreadable (load_error is set with the reason). Safe
        to call repeatedly — the work happens at most once.
        """
        if self._loaded:
            return True
        if self._load_error is not None:
            return False
        try:
            self._load()
        except FileNotFoundError:
            self._load_error = (
                f"The tag database CSV was not found: {self._csv_path}"
            )
            return False
        except OSError as e:
            self._load_error = f"Could not read the tag database: {e}"
            return False
        except Exception as e:  # pragma: no cover - defensive
            self._load_error = f"Failed to parse the tag database: {e}"
            return False
        # A readable file that yields zero tags is almost always the wrong
        # format (e.g. a 2-column 'tag,count' export instead of the
        # expected 'tag,category,post_count,aliases'), or an empty file.
        # Surface it as a real error rather than silently loading an empty
        # database — otherwise the audit would flag EVERY tag as unknown
        # with no explanation.
        if not self._category:
            self._load_error = (
                "The tag database parsed to zero tags. The CSV is empty or "
                "not in the expected 'tag_name,category_id,post_count,"
                "aliases' format (for example a 2-column 'tag,count' export "
                "won't work)."
            )
            return False
        self._loaded = True
        return True

    def _load(self) -> None:
        category: dict[str, str] = {}
        post_count: dict[str, int] = {}
        alias_to_canonical: dict[str, str] = {}
        with open(self._csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 4:
                    continue
                name, cat, count, aliases = row[0], row[1], row[2], row[3]
                name = name.strip()
                if not name:
                    continue
                category[name] = cat.strip()
                try:
                    post_count[name] = int(count)
                except (ValueError, TypeError):
                    post_count[name] = 0
                if aliases.strip():
                    for a in aliases.split(","):
                        a = a.strip()
                        # Skip shorthand aliases beginning with '/' (e.g.
                        # "/lh" for long_hair) — they're Danbooru search
                        # shortcuts, not spellings that appear in captions,
                        # and would cause spurious "fix" suggestions.
                        if not a or a.startswith("/"):
                            continue
                        # First writer wins: the CSV is sorted by post_count
                        # descending, so a collision resolves to the more
                        # popular canonical tag.
                        alias_to_canonical.setdefault(a, name)
        self._category = category
        self._post_count = post_count
        self._alias_to_canonical = alias_to_canonical
        # Pre-sorted canonical names for fast prefix autocomplete via
        # bisect (O(log n) to the first match instead of scanning all
        # ~201k tags on every keystroke).
        self._sorted_names = sorted(category.keys())

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def is_canonical(self, tag: str) -> bool:
        """True if `tag` is a real canonical Danbooru tag (any category)."""
        return _normalize(tag) in self._category

    def category_of(self, tag: str) -> Optional[str]:
        """Category id of a canonical tag, or None if not canonical."""
        return self._category.get(_normalize(tag))

    def alias_target(self, tag: str) -> Optional[str]:
        """If `tag` is a known alias spelling, return its canonical tag;
        else None. Only returns a target when `tag` is NOT itself
        canonical (a tag that's both shouldn't be 'fixed')."""
        norm = _normalize(tag)
        if norm in self._category:
            return None
        return self._alias_to_canonical.get(norm)

    def _is_color_compound(self, norm: str) -> bool:
        """True if `norm` (already normalized) is a recognized color/shade
        compound whose noun part is a registered tag.

        Peels leading color/modifier tokens one at a time; after each peel,
        if the remaining joined string is a registered canonical tag, it's
        a valid compound. Requires at least one modifier peeled and a
        non-empty registered remainder, so:
            deep_blue_curtains -> curtains (registered)        -> True
            green_gloves       -> gloves (registered)          -> True
            red_curtian        -> curtian (NOT registered)     -> False (typo kept)
            pink_zzzgibberish  -> zzzgibberish (not registered)-> False
        The caller checks canonical membership first, so an already-
        registered compound like dark_skin never reaches this method.
        """
        if "_" not in norm:
            return False
        tokens = norm.split("_")
        n = len(tokens)
        i = 0
        # Leave at least one token as the remainder noun.
        while i < n - 1:
            tok = tokens[i]
            if tok in BASE_COLORS or tok in COLOR_MODIFIERS:
                i += 1
                remainder = "_".join(tokens[i:])
                if remainder in self._category:
                    return True
                # else keep peeling further leading modifiers
            else:
                break
        return False

    def classify(self, tag: str, valid_categories: set[str]) -> tuple[str, Optional[str], Optional[str]]:
        """Classify one caption tag for the audit.

        Returns (verdict, detail, category):
          - VERDICT_VALID        : canonical and within scope.
              detail=None, category=<cat id>
          - VERDICT_OUT_OF_SCOPE : canonical but category not in scope.
              detail=None, category=<cat id>
          - VERDICT_ALIAS        : matches a known alias.
              detail=<canonical target>, category=<target's cat id>
          - VERDICT_COMPOUND     : color/shade + registered noun (valid).
              detail=None, category=None
          - VERDICT_UNKNOWN      : not in the database at all.
              detail=None, category=None

        Order matters: canonical first (so a registered compound like
        dark_skin is VALID, not decomposed), then alias, then the color-
        compound grammar, then unknown.
        """
        norm = _normalize(tag)
        cat = self._category.get(norm)
        if cat is not None:
            if cat in valid_categories:
                return (VERDICT_VALID, None, cat)
            return (VERDICT_OUT_OF_SCOPE, None, cat)
        target = self._alias_to_canonical.get(norm)
        if target is not None:
            return (VERDICT_ALIAS, _match_format(tag, target),
                    self._category.get(target))
        if self._is_color_compound(norm):
            return (VERDICT_COMPOUND, None, None)
        return (VERDICT_UNKNOWN, None, None)

    def suggest(self, prefix: str, limit: int = 8) -> list[str]:
        """Auto-complete: canonical tags starting with `prefix`, ranked by
        post_count (most popular first). Used by the tag input boxes.

        Matches against canonical tags only (not aliases) so suggestions
        are always real, insertable tags. Case-insensitive prefix match.

        Uses bisect over a pre-sorted name list so the scan starts at the
        first matching name instead of walking all ~201k tags — fast
        enough to run on every keystroke.
        """
        p = _normalize(prefix)
        if not p:
            return []
        names = self._sorted_names
        start = bisect.bisect_left(names, p)
        matches: list[str] = []
        i = start
        n = len(names)
        # Gather all prefix matches. Cap the gather to avoid pathological
        # work on a 1-char prefix (thousands of matches); we only need
        # enough to rank a few — but to rank by popularity correctly we
        # need them all, so cap generously and accept that a 1-char
        # prefix ranks within the first chunk.
        GATHER_CAP = 4000
        while i < n and names[i].startswith(p) and len(matches) < GATHER_CAP:
            matches.append(names[i])
            i += 1
        matches.sort(key=lambda nm: (-self._post_count.get(nm, 0), nm))
        return matches[:limit]

    def post_count_of(self, tag: str) -> int:
        return self._post_count.get(_normalize(tag), 0)


# ----------------------------------------------------------------------
# Module-level singleton
# ----------------------------------------------------------------------

_INSTANCE: Optional[TagDatabase] = None


def get_database() -> TagDatabase:
    """Return the shared TagDatabase instance (created but NOT loaded).

    Call .ensure_loaded() before lookups; that's where the one-time read
    happens. The instance persists for the process lifetime so the load
    cost is paid at most once.
    """
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = TagDatabase()
    return _INSTANCE


# ----------------------------------------------------------------------
# Selectable CSV snapshots
# ----------------------------------------------------------------------
# Which Danbooru SNAPSHOT the audit checks against matters: SDXL anime
# finetunes (Pony V6, Illustrious, NoobAI) learned an OLDER tag vocabulary,
# so a too-new CSV flags valid training tags as "wrong" and nudges you
# toward granular tags the models never saw. Users pick the snapshot that
# matches their base model.
#
# Each preset maps a stable key -> (resources filename, short label,
# one-line purpose). A preset only surfaces in the UI if its file is
# actually present in resources/ (so end-2023 appears once dropped in).
CSV_PRESETS: dict[str, tuple[str, str, str]] = {
    "current": (
        "danbooru_tags.csv",
        "Latest \u2014 April 2026 (newest tags)",
        "The newest Danbooru tags, including granular splits (e.g. "
        "presenting -> presenting_body) that post-date the models below.",
    ),
    "mid2024": (
        "danbooru_tags_2024-11.csv",
        "Illustrious / NoobAI \u2014 Nov 2024",
        "Tag vocabulary close to what Illustrious and NoobAI learned, "
        "before the later granular splits. The safest default for most "
        "SDXL anime finetuning.",
    ),
    "pony": (
        "danbooru_tags_2023-04.csv",
        "Pony Diffusion V6 \u2014 Apr 2023",
        "April 2023 vocabulary and counts, matching Pony Diffusion V6 XL. "
        "Re-canonicalized to standard tag forms.",
    ),
    "ancient": (
        "danbooru_tags_2017-06.csv",
        "Ancient \u2014 Jun 2017",
        "A June 2017 snapshot \u2014 only ~14k tags existed then. For very "
        "old models or historical curiosity.",
    ),
}

# Default snapshot when the user hasn't chosen one. The mid-2024 old-rules
# list is the safest match for the common SDXL finetuning case.
DEFAULT_CSV_KEY = "mid2024"


def available_csvs() -> list[tuple[str, str, str]]:
    """Return (key, label, purpose) for each preset whose file exists.

    Populates the settings picker. Presets whose CSV isn't present in
    resources/ are omitted (e.g. end-2023 until the user adds it).
    """
    out: list[tuple[str, str, str]] = []
    for key, (fname, label, purpose) in CSV_PRESETS.items():
        if _resource_path(f"resources/{fname}").exists():
            out.append((key, label, purpose))
    return out


def resolve_csv_path(choice: Optional[str]) -> Path:
    """Resolve a stored setting value to an actual CSV path.

    `choice` is a preset key ("current"/"mid2024"/"pony"/"ancient"), an absolute
    path to a custom CSV, or empty. Falls back gracefully so the audit
    always has *some* list: preset key -> resources/<file> if present;
    absolute path -> used if it exists; otherwise the default preset, then
    the legacy resources/danbooru_tags.csv.
    """
    if choice and os.path.isabs(choice):
        p = Path(choice)
        if p.exists():
            return p
    if choice in CSV_PRESETS:
        p = _resource_path(f"resources/{CSV_PRESETS[choice][0]}")
        if p.exists():
            return p
    p = _resource_path(f"resources/{CSV_PRESETS[DEFAULT_CSV_KEY][0]}")
    if p.exists():
        return p
    return _resource_path("resources/danbooru_tags.csv")


def set_active_csv(path: Optional[Path]) -> None:
    """Point the shared database at `path` and reset it (lazy reload).

    Call once at startup, after reading the user's choice and BEFORE any
    lookup. Resets the singleton so the next ensure_loaded() reads the
    chosen file. Passing None uses the default preset.
    """
    global _INSTANCE
    if path is None:
        path = resolve_csv_path(None)
    _INSTANCE = TagDatabase(path)


def bucket_for(tag: str) -> str:
    """Vocabulary bucket for a tag: "known" (canonical or alias, judged
    on the FOLDED form so "long hair" == long_hair), "color_combo"
    (<color/shade>_<registered tag> compounds not individually
    registered), or "custom" (absent from the database — unique tokens
    and typos alike).

    Single source of truth shared by the tag tree's vocabulary filter
    and the statistics export, both of which reuse the audit's
    classifier — the three features can never disagree about a tag.
    Loads the database on first use; raises RuntimeError if it cannot
    load (silently classifying everything as "custom" on an empty
    database would be a lie). UI callers that pre-check ensure_loaded
    never see the raise."""
    from core.state import _fold_for_match  # lazy: no import cycle

    db = get_database()
    if not db.ensure_loaded():
        raise RuntimeError("Danbooru tag database unavailable")
    verdict, _detail, _cat = db.classify(
        _fold_for_match(tag), frozenset()
    )
    if verdict == VERDICT_COMPOUND:
        return "color_combo"
    if verdict == VERDICT_UNKNOWN:
        return "custom"
    return "known"
