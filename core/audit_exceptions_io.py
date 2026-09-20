"""Persistence for the Danbooru-audit exception list.

The audit (Tools → Audit tags) flags dataset tags that aren't clean
Danbooru matches — unknown tags, out-of-scope tags, known aliases, color
compounds. Some of those flags are deliberate choices the user has made
and doesn't want to keep seeing: a studio or OC name, a convention the
dataset uses on purpose, a tag that's correct for this collection. The
exception list records those tags so the audit stops flagging them.

Like conflict rules, this knowledge accumulates across datasets, so it
lives next to the app settings rather than in a per-project file. Tags
are stored lowercased for case-insensitive matching against dataset tags.

Format:

    { "version": 1, "tags": ["tag_a", "tag_b", ...] }

Writes are atomic (temp file + os.replace); a corrupt or missing file
yields an empty set, so a bad write can never wedge the app.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, Set


FORMAT_VERSION = 1


def _default_path() -> Path:
    """Where the exception list lives — next to the app config, matching
    how settings and conflict rules pick their location."""
    base = os.environ.get("TAGWALKER_CONFIG_DIR")
    if base:
        return Path(base) / "audit_exceptions.json"
    return Path.home() / ".tagwalker" / "audit_exceptions.json"


def _clean(tags: Iterable) -> Set[str]:
    """Normalize an iterable of tags to a set of trimmed, lowercased,
    non-empty strings."""
    out: Set[str] = set()
    for t in tags:
        s = str(t).strip().lower()
        if s:
            out.add(s)
    return out


def load_exceptions(path: Path = None) -> Set[str]:
    """Load the set of excepted tags (lowercased). A missing or corrupt
    file yields an empty set rather than raising."""
    p = path or _default_path()
    try:
        if not p.exists():
            return set()
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _clean(data.get("tags", []))
    except (OSError, ValueError, json.JSONDecodeError):
        return set()


def save_exceptions(tags: Iterable, path: Path = None) -> bool:
    """Write the exception set atomically. Returns True on success, False
    on error (caller can surface a non-fatal warning)."""
    p = path or _default_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": FORMAT_VERSION,
            "tags": sorted(_clean(tags)),
        }
        fd, tmp = tempfile.mkstemp(
            dir=str(p.parent), prefix=".audit_exc_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # fsync best-effort
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return True
    except OSError:
        return False


def add_exception(tag: str, path: Path = None) -> bool:
    """Add one tag to the exception list (load-modify-save). Returns True
    on a successful save, or if the tag was already present."""
    t = str(tag).strip().lower()
    if not t:
        return False
    tags = load_exceptions(path)
    if t in tags:
        return True
    tags.add(t)
    return save_exceptions(tags, path)


def remove_exception(tag: str, path: Path = None) -> bool:
    """Remove one tag from the exception list. Returns True on a
    successful save, or if the tag wasn't present."""
    t = str(tag).strip().lower()
    tags = load_exceptions(path)
    if t not in tags:
        return True
    tags.discard(t)
    return save_exceptions(tags, path)
