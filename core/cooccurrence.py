"""Official Danbooru tag co-occurrence lookup.

Bundled, preprocessed once from a May-2025 Danbooru co-occurrence dump
(~3.2M tag pairs) normalized with the April-2026 tag counts. For each
tag we keep its strongest co-occurring partners ranked by the Ochiai
coefficient::

    ochiai(A, B) = count(A, B) / sqrt(count(A) * count(B))

Ochiai surfaces *meaningful* associations (cat_ears -> animal_ears,
maid -> maid_headdress) instead of ubiquitous tags like 1girl that
co-occur with everything. Alongside each partner we store
P(partner | tag) = count(A, B) / count(A) -- "of images tagged `tag`,
the fraction that also carry `partner`" -- which is the intuitive
"you're probably missing this" strength shown in the UI.

This powers the file-state co-occurrence hints when the source
preference is "danbooru" (the default). The dataset-local engine in
SessionState.get_related_tags_for_tag remains available via the
"dataset" preference, for checking internal consistency of the user's
own set.

The raw dump is NOT shipped; only this compact lookup
(resources/cooccurrence_danbooru.json, ~7 MB) is bundled,
and it is loaded lazily on first use.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional


def _resource_path(relative: str) -> Path:
    """Resolve a bundled resource path for both source and frozen runs.

    Mirrors tag_database._resource_path so this module has no dependency
    on the app entry point. Under a PyInstaller --onefile build, bundled
    data is unpacked beneath sys._MEIPASS; from source it is relative to
    the project root (the parent of core/).
    """
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Path(base) / relative


_LOOKUP_FILE = "resources/cooccurrence_danbooru.json"


class CooccurrenceDB:
    """Lazy-loaded co-occurrence lookup: tag -> top partners."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _resource_path(_LOOKUP_FILE)
        # tag -> [[partner, ochiai, p_partner_given_tag], ...] desc by ochiai
        self._data: Optional[dict] = None
        self._load_error: Optional[str] = None

    def ensure_loaded(self) -> bool:
        if self._data is not None:
            return True
        try:
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)
            return True
        except Exception as exc:  # missing/corrupt -> behave as empty
            self._data = {}
            self._load_error = str(exc)
            return False

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    @property
    def is_available(self) -> bool:
        return self.ensure_loaded() and bool(self._data)

    def get_cooccurring(
        self,
        tag: str,
        limit: int = 8,
        min_ochiai: float = 0.0,
    ) -> list[tuple[str, float, float]]:
        """Top co-occurring partners for `tag`.

        Returns ``(partner, ochiai, p_partner_given_tag)`` tuples,
        already sorted by Ochiai descending. `limit` caps the count;
        `min_ochiai` drops weak partners (entries are stored sorted, so
        we can stop early).
        """
        if not tag or not self.ensure_loaded():
            return []
        # Folded for the same reason the tag database folds: a caption
        # written "long hair" must find the row stored as "long_hair".
        # Without this the hints, and the whole comparison panel in the
        # statistics window, went silently empty for anyone whose
        # captions use spaces — and this program ships a tool to
        # convert between the two formats, so both are supported.
        rows = self._data.get(tag)
        if rows is None:
            rows = self._data.get(
                (tag or "").strip().lower().replace(" ", "_"))
        if not rows:
            return []
        out: list[tuple[str, float, float]] = []
        for entry in rows:
            # entry should be [partner, ochiai, p_cond]. Be defensive: a
            # truncated or garbled entry (e.g. from a partially-written or
            # corrupt bundle) must not crash the hint lookup, which runs on
            # every image during the walk. Skip anything malformed.
            if not isinstance(entry, (list, tuple)) or len(entry) < 3:
                continue
            try:
                ochiai = float(entry[1])
                p_cond = float(entry[2])
            except (TypeError, ValueError):
                continue
            partner = entry[0]
            if not isinstance(partner, str):
                continue
            if ochiai < min_ochiai:
                # Entries are stored sorted by ochiai desc, so once we drop
                # below the threshold on a VALID entry, nothing later
                # qualifies either.
                break
            out.append((partner, ochiai, p_cond))
            if len(out) >= limit:
                break
        return out


_INSTANCE: Optional[CooccurrenceDB] = None


def get_db() -> CooccurrenceDB:
    """Process-wide singleton."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = CooccurrenceDB()
    return _INSTANCE
