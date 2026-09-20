"""Name-pattern image grouping (Feature B).

Detects families of images produced by Windows' multi-file rename, which
names a selection as base_(0), base_(1), base_(2), … When a user renames
a batch of images to "maria_face", Windows yields maria_face_(0).png,
maria_face_(1).png, and so on. This module recognizes that exact pattern
and clusters such files into groups so the UI can collapse them and offer
batch operations.

Design constraints (decided with the user):
  * ONLY the Windows multi-rename pattern is recognized: stem ==
    "<base>_(<n>)" where <n> is a non-negative integer. Nothing looser —
    a loose pattern would wrongly merge unrelated images.
  * Grouping is WITHIN A SINGLE FOLDER. Two files with the same base in
    different folders are different groups (the caller passes one
    folder's images at a time, or we key groups by (subfolder, base)).
  * A base needs 2+ members to be a group. A lone "foo_(0)" with no
    siblings stays an ungrouped standalone image — a one-item group is
    pointless.
  * Pure data: no Qt, no state mutation. Trivial to test in isolation,
    which matters because the filename edge cases are where the bugs
    live (gaps in numbering, parens inside the base, mixed matched and
    unmatched files, etc.).

This module is intentionally tiny and dependency-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional


# The one recognized pattern: <base>_(<digits>) at the END of the stem.
# - base is captured greedily but must be non-empty.
# - The number is plain decimal digits (Windows starts at (1) for the
#   second item but the first keeps its original name OR becomes (0)/(1)
#   depending on Windows version; we accept any non-negative integer and
#   don't assume the sequence starts at any particular value).
# Examples that MATCH: "maria_face_(0)", "x_(12)", "a_b_c_(3)"
# Examples that DON'T: "maria_face", "img_3" (no parens), "foo_(1)_bar"
#                      (number not at end), "foo_()" (no digits).
#
# Separator before the "(n)" may be an underscore ("ellie_(0)") OR a
# space ("ellie (0)") — Windows multi-rename uses a space, while many
# tools/users use an underscore. Both are recognized and normalize to
# the SAME base ("ellie") because the separator is consumed by the
# pattern (not part of the captured base). The base is captured
# non-greedily and any trailing separator stripped, so "ellie_" and
# "ellie " can't form distinct groups.
_PATTERN = re.compile(r"^(?P<base>.+?)[ _]?\((?P<num>\d+)\)$")


@dataclass(frozen=True)
class NameGroup:
    """A detected group of images sharing a base name within one folder.

    Attributes
    ----------
    subfolder : the POSIX subfolder the group lives in (group key part).
    base      : the shared base name, e.g. "maria_face".
    members   : image stems' source objects in numeric order by <n>.
                The element type is whatever the caller passed in
                (kept generic so this module needn't import ImageEntry).
    """
    subfolder: str
    base: str
    members: tuple


def parse_stem(stem: str) -> Optional[tuple[str, int]]:
    """If `stem` matches the Windows multi-rename pattern, return
    (base, number); else None.

    `stem` is the filename without extension (e.g. "maria_face_(0)").
    """
    m = _PATTERN.match(stem)
    if m is None:
        return None
    return (m.group("base"), int(m.group("num")))


def group_images(
    items: Iterable,
    subfolder: str,
    stem_of,
) -> tuple[list[NameGroup], list]:
    """Cluster `items` (all from the SAME folder) into name-pattern groups.

    Parameters
    ----------
    items     : iterable of arbitrary objects (e.g. ImageEntry) for one
                folder.
    subfolder : the folder key these items belong to (stored on each
                produced NameGroup).
    stem_of   : callable mapping an item to its filename stem (without
                extension). Kept as a parameter so this module doesn't
                depend on ImageEntry's shape.

    Returns
    -------
    (groups, ungrouped):
      groups    : list of NameGroup, each with 2+ members, members sorted
                  by their numeric index ascending. Groups are ordered by
                  base name (case-insensitive) for stable display.
      ungrouped : list of items that didn't match the pattern OR whose
                  base had only one member — returned in the order first
                  seen, so the caller can interleave them with groups.

    The caller is responsible for only passing images from one folder;
    the subfolder argument is recorded but not used to filter.
    """
    # base -> list of (num, item)
    buckets: dict[str, list[tuple[int, object]]] = {}
    unmatched: list = []
    # Track first-seen order of items so a single-member base can be
    # returned to the ungrouped list in its original position-ish.
    for item in items:
        parsed = parse_stem(stem_of(item))
        if parsed is None:
            unmatched.append(item)
            continue
        base, num = parsed
        buckets.setdefault(base, []).append((num, item))

    groups: list[NameGroup] = []
    for base, entries in buckets.items():
        if len(entries) < 2:
            # Lone "_(n)" file — not a real group; treat as standalone.
            unmatched.append(entries[0][1])
            continue
        # Sort members by numeric index; ties (shouldn't happen) fall
        # back to stable order.
        entries.sort(key=lambda e: e[0])
        members = tuple(item for (_num, item) in entries)
        groups.append(NameGroup(subfolder=subfolder, base=base,
                                members=members))

    groups.sort(key=lambda g: g.base.lower())
    return groups, unmatched
