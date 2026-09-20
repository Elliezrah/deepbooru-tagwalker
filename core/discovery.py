"""
core/discovery.py

Picks a tag at random from a shipped snapshot, for browsing the
vocabulary rather than looking up something already in mind.

The whole design is one filter: a random tag out of a hundred thousand
is almost always a one-off character nobody has ever used, so the
minimum post count is what turns this from noise into something worth
reading. Everything else is refinement.

    scope       which categories are eligible; general-only by
                default, because artist and character names are not
                vocabulary you caption WITH
    min_count   posts the tag must have in the chosen snapshot
    seen        tags already offered this session, so pressing the
                button repeatedly explores instead of circling
    owned       tags already in the user's dataset, optionally
                excluded, which turns browsing into gap-finding

No Qt, and no settings object: the caller passes what it wants, which
keeps the rules testable without a window.
"""
from __future__ import annotations

import random

from core import tag_reference as tr
from core.tag_database import CATEGORY_NAMES

SCOPES = {
    "general": "General tags only",
    "all": "All tag types",
}
DEFAULT_SCOPE = "general"

# A tag on fewer posts than this is unlikely to mean anything to a
# model trained on the era, and the long tail of a booru is mostly
# one-off character names. This is the knob that decides whether the
# button is useful or noise.
DEFAULT_MIN_COUNT = 500

# Tried at random before falling back to filtering the whole pool.
# Cheap while plenty remain unseen, which is nearly always.
_RANDOM_TRIES = 200

_POOLS: dict[tuple, list[str]] = {}


def _fold(tag: str) -> str:
    return (tag or "").strip().lower().replace(" ", "_")


def pool(era_key: str, scope: str = DEFAULT_SCOPE,
         min_count: int = DEFAULT_MIN_COUNT) -> list[str]:
    """Eligible tags for these settings, built once and kept.

    Rebuilding on every press would rescan a hundred thousand rows for
    a single answer.
    """
    key = (era_key, scope, int(min_count))
    cached = _POOLS.get(key)
    if cached is not None:
        return cached
    if not tr.ensure_loaded():
        return []
    table = (tr._TABLES or {}).get(era_key)
    if table is None:
        return []
    wanted_general = scope != "all"
    names: list[str] = []
    for fold, count in table.counts.items():
        if count < min_count:
            continue
        if wanted_general:
            cat = CATEGORY_NAMES.get(table.cats.get(fold, "0"), "")
            if cat != "general":
                continue
        names.append(table.display.get(fold, fold))
    _POOLS[key] = names
    return names


def pick(era_key: str, scope: str = DEFAULT_SCOPE,
         min_count: int = DEFAULT_MIN_COUNT,
         seen=(), owned=None, skip_owned: bool = False,
         blocked=None, rng=None) -> str | None:
    """One tag, or None when the filters leave nothing.

    `blocked` is a predicate for tags the caller will refuse to show —
    the content blacklist. Without it Discover hands out tags at
    exactly the blacklist's coverage rate that the window then blanks
    with "blocked by your blacklist", so a press is simply wasted.
    Someone with a large custom list would meet that constantly.
    """
    names = pool(era_key, scope, min_count)
    if not names:
        return None
    rng = rng or random
    skip = {_fold(t) for t in (seen or ())}
    if skip_owned and owned:
        skip |= {_fold(t) for t in owned}

    def rejected(tag: str) -> bool:
        if _fold(tag) in skip:
            return True
        return bool(blocked and blocked(tag))

    if not skip and blocked is None:
        return rng.choice(names)
    for _ in range(_RANDOM_TRIES):
        candidate = rng.choice(names)
        if not rejected(candidate):
            return candidate
    # Nearly exhausted: pay for the full scan rather than give up.
    remaining = [n for n in names if not rejected(n)]
    return rng.choice(remaining) if remaining else None


def describe(era_label: str, scope: str, min_count: int,
             skip_owned: bool, pool_size: int) -> str:
    """One line for the button's tooltip, so the current filters are
    visible without opening Preferences."""
    bits = [SCOPES.get(scope, scope).lower(),
            f"at least {min_count:,} posts"]
    if skip_owned:
        bits.append("not already in your dataset")
    return (f"{pool_size:,} tags match: " + ", ".join(bits)
            + f" \u2014 in {era_label}")
