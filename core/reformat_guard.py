"""Guard for the bulk underscore<->space tag reformat.

Identifies tags that should NOT be naively converted because their
underscores are structural (emoticons / faces / symbols) rather than word
separators. Two layers:

  1. A pattern rule (no maintenance): a tag that contains an underscore but
     NO ASCII letters is a symbol face -- '^_^', '0_0', '>_<', '|_|', '._.',
     '+_+', '=_=', '@_@'. These are caught automatically.

  2. A small curated list of LETTERED emoticons that the pattern misses
     because they contain letters and look like ordinary word-joins --
     'o_o', 'u_u', 'x_x', '>_o'. Verified against the bundled danbooru tag
     list and shipped in resources/reformat_protected_tags.json.

Protected tags are excluded from a reformat BY DEFAULT but still shown in
the preview, where the user can opt a specific one back in. The list need
not be exhaustive: the preview is the backstop.
"""
from __future__ import annotations

import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

_LETTER = re.compile(r"[A-Za-z]")


def _resource_path(relative: str) -> Path:
    """Resolve a bundled resource path for both source and frozen runs.
    Mirrors core.tag_database._resource_path: PyInstaller unpacks bundled
    data under sys._MEIPASS; from source it's relative to the project root
    (the parent of this file's directory)."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Path(base) / relative


@lru_cache(maxsize=1)
def _curated() -> frozenset:
    try:
        data = json.loads(
            _resource_path("resources/reformat_protected_tags.json")
            .read_text(encoding="utf-8")
        )
        tags = data.get("tags", []) if isinstance(data, dict) else []
        return frozenset(t for t in tags if isinstance(t, str))
    except (OSError, ValueError):
        # Missing/corrupt resource -> pattern rule still applies.
        return frozenset()


def is_protected_tag(tag: str) -> bool:
    """True if `tag`'s underscores are structural (emoticon / symbol) and
    it should be skipped by default during an underscore<->space reformat."""
    if not tag:
        return False
    # Pattern rule: underscore present, but no letters -> symbol face.
    if "_" in tag and not _LETTER.search(tag):
        return True
    # Curated lettered emoticons.
    return tag in _curated()


def protected_seed() -> frozenset:
    """The curated lettered-emoticon set (for display / tests)."""
    return _curated()
