"""
core/tag_reference.py

Offline half of the Tag Reference window (see DESIGN_TAG_REFERENCE.md).

Loads EVERY shipped Danbooru snapshot (tag_database.CSV_PRESETS: Jun
2017 / Apr 2023 Pony / Nov 2024 Illustrious-NoobAI / Apr 2026 latest)
into lightweight lookup tables, and answers, with no network access:

- era counts: how many posts carried this tag in each era, with the
  user's selected snapshot highlighted by the window;
- drift verdict: a one-line judgement of how the tag has moved
  relative to the selected model era (postdates it / renamed / stable
  / retired / custom);
- alias resolution both ways: a looked-up alias redirects to its
  canonical form ("heels" -> "high_heels", noted as redirected), and
  the names aliased TO the tag are listed;
- prefix search over the current era's canonical names (the search
  box's offline autocomplete backstop).

This module is pure data + logic (no Qt) so every verdict and
resolution path is directly unit-testable. Tables load lazily on
first use (~2 s for all four CSVs) and are cached for the process.
"""
from __future__ import annotations

import threading as _threading

import bisect
import csv
from dataclasses import dataclass, field
from typing import Optional

from core.tag_database import (
    CATEGORY_NAMES,
    CSV_PRESETS,
    DEFAULT_CSV_KEY,
    _normalize,
    _resource_path,
)


@dataclass(frozen=True)
class EraCount:
    key: str            # preset key, e.g. "mid2024"
    label: str          # human label from CSV_PRESETS
    count: Optional[int]  # posts in that era; None = tag absent


@dataclass
class ReferenceInfo:
    query: str                       # what the caller asked for
    canonical: str                   # resolved display name
    redirected_from: Optional[str]   # set when query was an alias
    category_id: Optional[str]   # string ids, matching tag_database
    category_name: Optional[str]
    era_counts: list[EraCount] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)  # names -> this tag
    verdict: str = ""
    is_custom: bool = False
    is_metatag: bool = False         # a Danbooru search/wiki metatag
                                     # (e.g. "tag_group:attire"), not a
                                     # caption tag — no drift verdict applies


class _EraTable:
    __slots__ = ("counts", "cats", "display", "alias_to", "alias_of",
                 "sorted_names")

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.cats: dict[str, str] = {}   # string ids ("0".."5")
        self.display: dict[str, str] = {}
        self.alias_to: dict[str, str] = {}      # fold(alias) -> canonical
        self.alias_of: dict[str, list[str]] = {}  # fold(canon) -> aliases
        self.sorted_names: list[str] = []


def _load_era(fname: str) -> Optional[_EraTable]:
    path = _resource_path(f"resources/{fname}")
    if not path.exists():
        return None
    table = _EraTable()
    try:
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 3 or not row[0]:
                    continue
                name = row[0]
                fold = _normalize(name)
                try:
                    table.counts[fold] = int(row[2])
                except (ValueError, IndexError):
                    table.counts[fold] = 0
                table.cats[fold] = row[1] if len(row) > 1 else "0"
                table.display[fold] = name
                if len(row) >= 4 and row[3]:
                    for alias in row[3].split(","):
                        alias = alias.strip()
                        if not alias:
                            continue
                        table.alias_to[_normalize(alias)] = name
                        table.alias_of.setdefault(fold, []).append(alias)
    except OSError:
        return None
    table.sorted_names = sorted(table.display.values())
    return table


_TABLES: Optional[dict[str, _EraTable]] = None


_LOAD_LOCK = _threading.Lock()


def ensure_loaded() -> bool:
    """Load every present snapshot once. False if none could load.

    Guarded by a lock because core/prewarm.py starts this on a
    background thread at startup: without it, a lookup arriving while
    that thread is mid-parse would begin a second, duplicate parse and
    the two would race to assign _TABLES.
    """
    global _TABLES
    if _TABLES is not None:
        return bool(_TABLES)
    with _LOAD_LOCK:
        # Re-check: another thread may have finished while this one
        # waited for the lock.
        if _TABLES is not None:
            return bool(_TABLES)
        tables: dict[str, _EraTable] = {}
        for key, (fname, _label, _purpose) in CSV_PRESETS.items():
            t = _load_era(fname)
            if t is not None:
                tables[key] = t
        _TABLES = tables
    return bool(_TABLES)


def loaded_eras() -> list[tuple[str, str]]:
    """(key, label) for each snapshot actually loaded, preset order."""
    if not ensure_loaded():
        return []
    assert _TABLES is not None
    return [(k, CSV_PRESETS[k][1]) for k in CSV_PRESETS if k in _TABLES]


def _current_table() -> Optional[_EraTable]:
    assert _TABLES is not None
    if "current" in _TABLES:
        return _TABLES["current"]
    for k in CSV_PRESETS:
        if k in _TABLES:
            return _TABLES[k]
    return None


def drift_verdict(
    era_counts: list[EraCount],
    selected_key: str,
    now_alias_of: Optional[str],
) -> str:
    """Pure one-line judgement of a tag's movement across eras,
    relative to the user's selected model era."""
    by_key = {e.key: e for e in era_counts}
    labels = {e.key: e.label for e in era_counts}
    if now_alias_of:
        return (f"renamed \u2014 now an alias of {now_alias_of} on "
                f"current Danbooru")
    if all(e.count is None for e in era_counts):
        return "custom \u2014 not in any shipped Danbooru snapshot"
    sel = by_key.get(selected_key)
    cur = by_key.get("current") or era_counts[-1]
    sel_label = labels.get(selected_key, selected_key)
    if cur.count is not None and (sel is None or sel.count is None):
        return (f"postdates the {sel_label} snapshot \u2014 a model of "
                f"that era likely never learned this tag")
    if (sel is not None and sel.count is not None
            and cur.count is None):
        return (f"retired \u2014 present in the {sel_label} era but "
                f"absent from the current snapshot; check aliases")
    earliest = next((e for e in era_counts if e.count is not None), None)
    if earliest is not None:
        return f"stable \u2014 present since the {earliest.label} snapshot"
    return ""


# Danbooru search / wiki-listing metatags. A query like "tag_group:attire"
# is a SEARCH DIRECTIVE that resolves to a wiki listing page, not a caption
# tag — so no drift verdict ("stable" / "newer than your data" / "custom")
# applies to it. A term counts as a metatag ONLY when the part before its
# colon is one of these known names; this is the same rule the blacklist
# uses, and it is why ":d" / ":o" / ":p" (real tags with a leading colon and
# an empty prefix) are NOT mistaken for metatags.
_METATAG_PREFIXES = frozenset({
    # wiki / tag-group listing
    "tag_group", "wiki", "pool", "ordpool", "favgroup",
    # common search metatags a user might paste into the referencer
    "rating", "score", "favcount", "id", "md5", "width", "height",
    "mpixels", "ratio", "filesize", "date", "age", "order", "limit",
    "user", "approver", "commenter", "noter", "fav", "ordfav", "parent",
    "child", "source", "status", "tagcount", "gentags", "arttags",
    "chartags", "copytags", "metatags", "is", "has", "filetype",
    "duration", "embedded", "search", "random", "disapproved", "note",
    "comment", "commentary", "delreason",
})


def _metatag_prefix(query: str) -> Optional[str]:
    """If `query` is a Danbooru metatag (known name before the first
    colon), return that name; otherwise None. Requires a NON-EMPTY name
    before the colon, so ":d"/":o"/":p" are never treated as metatags."""
    q = query.strip()
    if ":" not in q:
        return None
    name = q.split(":", 1)[0].strip().lower()
    if name and name in _METATAG_PREFIXES:
        return name
    return None


def lookup(tag: str, selected_key: str = DEFAULT_CSV_KEY) -> ReferenceInfo:
    """Resolve a tag offline: redirect aliases, gather era counts,
    category, reverse aliases and the drift verdict."""
    info = ReferenceInfo(query=tag, canonical=tag, redirected_from=None,
                         category_id=None, category_name=None)
    if not ensure_loaded():
        info.verdict = "tag database unavailable"
        return info
    assert _TABLES is not None

    # A search / wiki metatag ("tag_group:attire") is not a caption tag:
    # it resolves to a wiki listing page. Flag it and give it a
    # search-appropriate verdict, skipping all tag-drift logic (so it is
    # never called "custom" or "newer than your data").
    meta = _metatag_prefix(tag)
    if meta is not None:
        info.is_metatag = True
        info.verdict = (
            f"search query \u2014 \u201c{meta}:\u201d is a Danbooru "
            "metatag, not a caption tag")
        return info

    cur = _current_table()
    fold = _normalize(tag)
    now_alias_of: Optional[str] = None
    if cur is not None:
        if fold in cur.display:
            info.canonical = cur.display[fold]
        elif fold in cur.alias_to:
            info.canonical = cur.alias_to[fold]
            info.redirected_from = tag
            now_alias_of = info.canonical
            fold = _normalize(info.canonical)
    canon_fold = fold

    # Era counts in preset (newest -> oldest) order, skipping absent
    # snapshot files but never absent TAGS (those show count=None).
    ordered = [k for k in CSV_PRESETS if k in _TABLES]
    for key in ordered:
        t = _TABLES[key]
        info.era_counts.append(EraCount(
            key=key, label=CSV_PRESETS[key][1],
            count=t.counts.get(canon_fold)))
        if info.category_id is None and canon_fold in t.cats:
            info.category_id = t.cats[canon_fold]
            info.category_name = CATEGORY_NAMES.get(
                info.category_id, info.category_id)
        if canon_fold in t.display and info.canonical == info.query \
                and info.redirected_from is None:
            info.canonical = t.display[canon_fold]

    if cur is not None:
        info.aliases = list(cur.alias_of.get(canon_fold, []))

    # A redirect only counts as "renamed" for the verdict when the
    # QUERY was the alias; looking up the canonical name directly is
    # not a rename event.
    info.verdict = drift_verdict(
        info.era_counts, selected_key,
        now_alias_of if info.redirected_from else None)
    info.is_custom = all(e.count is None for e in info.era_counts) \
        and info.redirected_from is None
    return info


def search(prefix: str, limit: int = 20) -> list[str]:
    """Prefix search over the current era's canonical names."""
    if not prefix or not ensure_loaded():
        return []
    cur = _current_table()
    if cur is None:
        return []
    p = _normalize(prefix)
    names = cur.sorted_names
    i = bisect.bisect_left(names, p)
    out: list[str] = []
    while i < len(names) and len(out) < limit:
        if not names[i].startswith(p):
            break
        out.append(names[i])
        i += 1
    return out
