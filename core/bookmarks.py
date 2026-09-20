"""
core/bookmarks.py

Saved posts, tags and searches, kept in one plain JSON file beside the
settings.

The format is deliberately boring and the file deliberately sits next
to the config rather than inside a cache or a database. If the program
stops opening on someone's machine, their bookmarks are still a text
file they can read, copy out, or hand to someone else. That recovery
path is the reason for every format decision here: one file, one flat
list, readable keys, indented output.

Three kinds, sharing one list so there is only ever one file to find:

    post    a Danbooru post id      -> reopens that post
    tag     a tag name              -> reopens its wiki page
    query   a search string + sort  -> re-runs that search

Writes go through a temporary file and an atomic replace, so a crash
mid-save leaves the previous list intact rather than a truncated one.
A corrupt or missing file loads as empty and is never allowed to raise
into the UI: losing bookmarks is bad, but a tool that will not start
because of them is worse.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

FORMAT_VERSION = 1
KINDS = ("post", "tag", "query")

# Enough that nobody will meet it in normal use, low enough that a
# runaway loop cannot grow the file without bound.
MAX_PER_KIND = 500


@dataclass
class Bookmark:
    kind: str
    value: str
    label: str = ""
    sort: str = ""              # query bookmarks only
    added: str = ""

    def key(self) -> tuple:
        return (self.kind, self.value)

    def display(self) -> str:
        return self.label or self.value


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class BookmarkStore:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self._items: list[Bookmark] = []
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        self._items = []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            # Hand-edited into invalid JSON, or a truncated write from
            # before atomic saves. Start empty rather than refusing to
            # run; the file itself is left alone so it can be salvaged.
            return
        entries = data.get("bookmarks") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "")
            value = str(entry.get("value") or "")
            if kind not in KINDS or not value:
                continue
            self._items.append(Bookmark(
                kind=kind, value=value,
                label=str(entry.get("label") or ""),
                sort=str(entry.get("sort") or ""),
                added=str(entry.get("added") or "")))

    def save(self) -> bool:
        payload = {
            "version": FORMAT_VERSION,
            "note": ("TagWalker bookmarks. Plain JSON on purpose: if "
                     "the program will not open, this file is still "
                     "readable."),
            "bookmarks": [asdict(b) for b in self._items],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write beside the target then replace, so an interrupted
            # save cannot leave a half-written bookmarks file.
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".bookmarks-",
                suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    def all(self, kind: str | None = None) -> list[Bookmark]:
        """Newest first — the thing just saved is the thing most
        likely wanted again."""
        items = [b for b in self._items
                 if kind is None or b.kind == kind]
        return list(reversed(items))

    def contains(self, kind: str, value: str) -> bool:
        return any(b.kind == kind and b.value == str(value)
                   for b in self._items)

    def add(self, kind: str, value, label: str = "",
            sort: str = "") -> bool:
        if kind not in KINDS:
            return False
        value = str(value).strip()
        if not value:
            return False
        if self.contains(kind, value):
            return False
        self._items.append(Bookmark(
            kind=kind, value=value, label=label or value, sort=sort,
            added=_now()))
        # Trim this kind only, so a long list of one sort cannot push
        # out another kind entirely.
        same = [b for b in self._items if b.kind == kind]
        if len(same) > MAX_PER_KIND:
            drop = set(id(b) for b in same[:len(same) - MAX_PER_KIND])
            self._items = [b for b in self._items if id(b) not in drop]
        self.save()
        return True

    def remove(self, kind: str, value) -> bool:
        value = str(value)
        before = len(self._items)
        self._items = [b for b in self._items
                       if not (b.kind == kind and b.value == value)]
        if len(self._items) == before:
            return False
        self.save()
        return True

    def toggle(self, kind: str, value, label: str = "",
               sort: str = "") -> bool:
        """Returns True when the item ends up bookmarked."""
        if self.contains(kind, str(value)):
            self.remove(kind, value)
            return False
        return self.add(kind, value, label, sort)


_STORES: dict[str, BookmarkStore] = {}


def get_store(path) -> BookmarkStore:
    """One store per file, shared by every window.

    Three windows each holding their own copy would overwrite one
    another's additions, because each would save its own stale list.
    """
    key = str(Path(path))
    store = _STORES.get(key)
    if store is None:
        store = BookmarkStore(key)
        _STORES[key] = store
    return store


def store_for(settings) -> BookmarkStore:
    """The store beside this Settings object's file, so a test using
    an isolated config directory gets isolated bookmarks too."""
    try:
        path = settings.bookmarks_path
    except Exception:
        path = Path.home() / ".tagwalker_bookmarks.json"
    return get_store(path)
