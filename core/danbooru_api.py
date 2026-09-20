"""
core/danbooru_api.py

Pure contracts for the Tag Reference's online layer
(DESIGN_TAG_REFERENCE.md): URL builders, response parsers, and the
two independent disk caches. NO networking happens here — the Qt
fetcher (ui layer) does transport; tests feed canned JSON. That split
keeps every parsing/caching path offline-testable and keeps core/
Qt-free.

Endpoints used (spec "Available data by endpoint"):
- wiki:  /wiki_pages/<tag>.json  -> title, body (DText), other_names
- posts: batch by id metatag     -> preview/large URLs, rating,
         tag_string, tag_count (decision 17), banned/deleted flags

Batch form note: this environment cannot reach danbooru.donmai.us, so
the anonymous batch form could not be live-verified. BOTH builders
ship — the id-metatag batch (`tags=id:1,2,3`, a single search term)
and a per-id fallback — and the fetch orchestration tries batch
first, falling back per-id on failure. First real run resolves it.

Caches (decisions 4/7): two independent on-disk stores under the
config dir — text (wiki JSON, TTL-based) and images (thumbnails by
post id, LRU with a byte cap) — each with its own clear control.
"""
from __future__ import annotations

import json
import os
import re
import time
import hashlib
from dataclasses import dataclass, field
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import quote

BASE_URL = "https://danbooru.donmai.us"
USER_AGENT = "TagWalker/2.0 (dataset tag auditing; offline-first)"

TEXT_CACHE_TTL_DAYS = 21
IMAGE_CACHE_CAP_BYTES = 200 * 1024 * 1024   # 200 MB


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------
def wiki_url(tag: str) -> str:
    """Wiki page JSON for a tag (folded form)."""
    folded = tag.strip().lower().replace(" ", "_")
    return f"{BASE_URL}/wiki_pages/{quote(folded, safe='')}.json"


def posts_by_ids_url(post_ids: list[int]) -> str:
    """Batch fetch via the id metatag — ONE search term regardless of
    how many ids, so the anonymous term cap is never at risk."""
    ids = ",".join(str(int(i)) for i in post_ids)
    return (f"{BASE_URL}/posts.json?tags="
            + quote(f"id:{ids}", safe="") + f"&limit={len(post_ids)}")


def post_url(post_id: int) -> str:
    """Single-post fallback if the batch form is rejected."""
    return f"{BASE_URL}/posts/{int(post_id)}.json"


# Post browsing. Anonymous searches are capped at two terms, and a
# sort metatag SPENDS one of them — which is why "newest" maps to the
# empty string: it is Danbooru's default ordering, so it costs nothing
# and leaves both terms for actual tags.
ANON_TERM_LIMIT = 2
SORT_ORDERS: dict[str, str] = {
    "score": "order:score",
    "newest": "",
    "oldest": "order:id_asc",
    "random": "order:random",
    "rated": "score:>50",
}
# "Highly rated" leads because it is the one that reliably WORKS:
# order:score asks the server to sort every matching post and times
# out on any common tag, which was happening on most single-tag
# searches. score:>50 filters on an indexed column instead. Best score
# is kept, last, for the narrow searches where it still succeeds.
SORT_LABELS: list[tuple[str, str]] = [
    ("rated", "Highly rated (fast)"),
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("random", "Random"),
    ("score", "Best score (often times out)"),
]
DEFAULT_SORT = SORT_LABELS[0][0]
# Orderings that make the server sort or scan the whole result set.
# On a tag with millions of posts these time out and come back as a
# 500 — `1girl order:score` is the reported case. Newest and oldest
# ride the id index and stay cheap at any size, and "rated" filters on
# an indexed column instead of sorting, so it survives where
# order:score does not.
EXPENSIVE_SORTS = frozenset({"score", "random"})


def build_post_query(tags: str, sort: str = "score") -> tuple[str, int]:
    """(query string, term count) for a post search.

    Sorting by score surfaces well-regarded work, but it is biased by
    exposure: a post from 2015 has had a decade to collect votes. The
    other orderings exist so recent usage can be seen too.
    """
    terms = [t for t in (tags or "").split() if t]
    order = SORT_ORDERS.get(sort, "")
    if order:
        terms.append(order)
    return " ".join(terms), len(terms)


def posts_search_url(tags: str, sort: str = "score", page: int = 1,
                     limit: int = 20) -> str:
    query, _count = build_post_query(tags, sort)
    return (f"{BASE_URL}/posts.json?tags=" + quote(query, safe="")
            + f"&page={max(1, int(page))}&limit={int(limit)}")


def posts_count_url(tags: str) -> str:
    """URL for the TOTAL number of posts matching a tag query.

    Danbooru's /counts/posts.json returns {"counts": {"posts": N}} for a
    tag search. Only the TAGS matter for a count — the sort order is
    deliberately omitted, since ordering doesn't change how many posts
    match and appending an expensive order metatag can make the count
    slower or fail on very common tags.

    The regular /posts.json endpoint does not report a total, which is
    why this separate call exists; it lets the browser show a post count
    and compute the last page. Deep pagination and counts on extremely
    common tags may still be capped or slow on Danbooru's side, so
    callers must treat a missing/failed count as "unknown" and degrade
    gracefully rather than depending on it.
    """
    terms = [t for t in (tags or "").split() if t]
    query = " ".join(terms)
    return f"{BASE_URL}/counts/posts.json?tags=" + quote(query, safe="")


def parse_post_count(data: object) -> Optional[int]:
    """Extract the integer total from a /counts/posts.json payload.

    Returns None if the shape isn't what we expect, so a malformed or
    error response is treated as "unknown" rather than crashing.
    """
    try:
        counts = data.get("counts") if isinstance(data, dict) else None
        if isinstance(counts, dict):
            n = counts.get("posts")
            if isinstance(n, (int, float)):
                return int(n)
    except Exception:
        pass
    return None


def media_asset_url(asset_id: int) -> str:
    """`!asset #N` embeds a MEDIA ASSET, not a post.

    Character wiki pages use these heavily for outfit breakdowns (the
    "Appearance" sections), so treating them as unfetchable text left
    exactly those sections blank. Assets live at their own endpoint.
    """
    return f"{BASE_URL}/media_assets/{int(asset_id)}.json"


def parse_media_asset(data) -> str:
    """Best display URL from a media-asset response, or "".

    The response carries a list of variants at different sizes. The
    field names are read defensively: this endpoint is less travelled
    than posts.json, so anything unexpected degrades to no image
    rather than an exception.
    """
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return ""
    variants = data.get("variants")
    if isinstance(variants, list):
        by_type = {}
        for v in variants:
            if isinstance(v, dict) and v.get("url"):
                by_type[str(v.get("type") or "")] = str(v["url"])
        for pref in ("720x720", "360x360", "sample", "original",
                     "180x180"):
            if by_type.get(pref):
                return by_type[pref]
        if by_type:
            return next(iter(by_type.values()))
    for key in ("large_file_url", "file_url", "url",
                "preview_file_url"):
        if data.get(key):
            return str(data[key])
    return ""


def tag_search_url(stem: str, limit: int = 8) -> str:
    """Name-similarity lookup for custom tags / typos (spec: the
    no-wiki-page path offers `red_curtian` -> `red_curtain`)."""
    pattern = stem.strip().lower().replace(" ", "_") + "*"
    return (f"{BASE_URL}/tags.json?search%5Bname_matches%5D="
            + quote(pattern, safe="*") + f"&limit={int(limit)}"
            + "&search%5Border%5D=count")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
@dataclass
class WikiPage:
    title: str
    body: str
    other_names: list[str] = field(default_factory=list)
    updated_at: str = ""


# Display vocabulary. Ratings spelled out (a bare "e" means nothing
# to a reader), and the booru category colours users already know from
# the site itself — kept here, Qt-free, so window and popup cannot
# drift apart.
RATING_NAMES = {
    "g": "general",
    "s": "sensitive",
    "q": "questionable",
    "e": "explicit",
}
RATING_COLOURS = {
    "g": "#5fb85f",     # green   - safe
    "s": "#c9b037",     # amber   - mild
    "q": "#e08a3c",     # orange  - borderline
    "e": "#e05a5a",     # red     - explicit
}
CATEGORY_COLOURS = {
    "artist": "#ff8a8b",
    "copyright": "#c797ff",
    "character": "#35c64a",
    "meta": "#ead084",
    "general": "#6ec4f5",
}


def rating_name(code: str) -> str:
    return RATING_NAMES.get((code or "").strip().lower(), "unknown")


# Formats Qt cannot render in a QLabel. The still thumbnail of one of
# these posts is perfectly viewable — it is only the full view that
# fails — so these are downloadable rather than hidden.
VIDEO_EXTS = frozenset({"mp4", "webm", "zip", "swf"})
ANIMATED_EXTS = frozenset({"gif", "apng"})


def is_video(post) -> bool:
    ext = (getattr(post, "file_ext", "") or "").lower()
    if ext:
        return ext in VIDEO_EXTS
    url = (getattr(post, "original_url", "")
           or getattr(post, "large_url", ""))
    tail = url.rsplit(".", 1)[-1].lower() if "." in url else ""
    return tail in VIDEO_EXTS


def rating_colour(code: str) -> str:
    return RATING_COLOURS.get((code or "").strip().lower(), "#999999")


@dataclass
class PostInfo:
    id: int
    preview_url: str
    large_url: str
    rating: str
    tag_string: str
    tag_count: int
    skip: bool = False        # banned, or nothing to display
    skip_reason: str = ""
    # Deleted is NOT skipped. A deleted Danbooru post keeps its files —
    # deletion is a moderation state, not removal — and a wiki editor
    # chose it as an example before it was deleted. Refusing to show
    # the thumbnail while the post-id chip beside it opened the very
    # same picture was simply incoherent.
    deleted: bool = False
    # Per-category tag strings. The artist/copyright/character split is
    # the interesting metadata on an example ("who drew this?"), and it
    # is already in the same response - no extra request.
    tag_string_artist: str = ""
    tag_string_copyright: str = ""
    tag_string_character: str = ""
    tag_string_meta: str = ""
    tag_string_general: str = ""
    # Danbooru serves a shrunk "sample" for quick viewing and keeps
    # the untouched upload separately. large_url is the fast one;
    # original_url is the real file, which is what a training set
    # wants.
    original_url: str = ""
    # "png", "mp4", "webm", "gif"... The thumbnail of a video post is
    # a still image and displays fine; the post itself does not, so
    # this is what tells the difference.
    file_ext: str = ""

    def grouped_tags(self) -> list[tuple[str, str, list[str]]]:
        """(heading, category key, tags) in the order a reader wants:
        who made it, who is in it, what it is from, then the bulk.
        Falls back to one General group when a response carries no
        per-category strings."""
        groups = [
            ("Artist", "artist", self.tag_string_artist.split()),
            ("Character", "character",
             self.tag_string_character.split()),
            ("Copyright", "copyright",
             self.tag_string_copyright.split()),
            ("Meta", "meta", self.tag_string_meta.split()),
            ("General", "general", self.tag_string_general.split()),
        ]
        if not any(tags for _, _, tags in groups):
            return [("Tags", "general", self.tag_string.split())]
        return [g for g in groups if g[2]]


def parse_wiki(data: object) -> Optional[WikiPage]:
    """Wiki JSON -> WikiPage. Tolerant: every field via .get with a
    default; returns None only when there is no usable page (spec:
    404 custom tags take the similarity path instead)."""
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "")
    body = str(data.get("body") or "")
    if not title and not body:
        return None
    names = data.get("other_names")
    other = [str(n) for n in names] if isinstance(names, list) else []
    return WikiPage(title=title, body=body, other_names=other,
                    updated_at=str(data.get("updated_at") or ""))


def _parse_one_post(d: dict) -> PostInfo:
    pid = int(d.get("id") or 0)
    preview = str(d.get("preview_file_url") or "")
    large = str(d.get("large_file_url") or d.get("file_url") or "")
    tag_string = str(d.get("tag_string") or "")
    try:
        tag_count = int(d.get("tag_count"))
    except (TypeError, ValueError):
        tag_count = len(tag_string.split()) if tag_string else 0
    info = PostInfo(
        id=pid, preview_url=preview, large_url=large,
        rating=str(d.get("rating") or "?"),
        tag_string=tag_string, tag_count=tag_count,
        tag_string_artist=str(d.get("tag_string_artist") or ""),
        tag_string_copyright=str(d.get("tag_string_copyright") or ""),
        tag_string_character=str(d.get("tag_string_character") or ""),
        tag_string_meta=str(d.get("tag_string_meta") or ""),
        tag_string_general=str(d.get("tag_string_general") or ""),
        original_url=str(d.get("file_url") or large),
        file_ext=str(d.get("file_ext") or "").lower().lstrip("."))
    info.deleted = bool(d.get("is_deleted"))
    if d.get("is_banned"):
        # Banned posts really do have their files pulled.
        info.skip, info.skip_reason = True, "banned"
    elif not preview:
        info.skip, info.skip_reason = True, "no preview available"
    return info


def parse_posts(data: object,
                wanted_order: Optional[list[int]] = None
                ) -> list[PostInfo]:
    """Posts JSON (list or single dict) -> PostInfo list. The API
    returns batch results in ITS order, not the wiki's — pass the
    embed order (`wanted_order`) to restore the editors' sequence;
    ids the API did not return are simply absent."""
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    posts = [_parse_one_post(d) for d in data if isinstance(d, dict)]
    if wanted_order:
        by_id = {p.id: p for p in posts}
        posts = [by_id[i] for i in wanted_order if i in by_id]
    return posts


# ---------------------------------------------------------------------------
# Disk caches (two, independent — decision 7)
# ---------------------------------------------------------------------------
_KEY_RE = re.compile(r"[^a-z0-9_\-.]+")


def _safe_key(key: str) -> str:
    """A filesystem-safe cache filename stem for a tag.

    The readable part is the tag with unsafe characters folded to "_",
    which keeps ordinary tags (``long_hair``) legible on disk. But that
    folding is lossy: every all-symbol tag (``!``, ``~``, ``?``, ``*``,
    ``@`` ...) collapses to the same ``"_"``, so their cache entries
    used to COLLIDE — looking up ``~`` after ``!`` returned ``!``'s
    cached wiki, which then rendered ``!``'s page and made the verdict
    logic wrongly call ``~`` a real (newer-than-snapshot) tag. Appending
    a short hash of the ORIGINAL key makes the stem unique per distinct
    tag while staying filesystem-safe, so distinct tags can never share
    a cache file again.
    """
    folded = _KEY_RE.sub("_", key.strip().lower())
    digest = hashlib.sha1(
        key.strip().lower().encode("utf-8")).hexdigest()[:10]
    if not folded or folded == "_" * len(folded):
        # All-symbol (or empty) tags have no useful readable part; the
        # hash alone identifies them, prefixed so the stem is valid.
        return f"sym_{digest}"
    return f"{folded}.{digest}"


class TextCache:
    """Wiki JSON by tag, TTL-expired. One file per key."""

    def __init__(self, directory: Path,
                 ttl_days: float = TEXT_CACHE_TTL_DAYS) -> None:
        self.dir = Path(directory)
        self.ttl_seconds = float(ttl_days) * 86400.0

    def _path(self, key: str) -> Path:
        return self.dir / f"{_safe_key(key)}.json"

    def get(self, key: str) -> Optional[object]:
        p = self._path(key)
        try:
            if time.time() - p.stat().st_mtime > self.ttl_seconds:
                return None
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def put(self, key: str, data: object) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self._path(key), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError:
            pass                      # caching is best-effort only


    def size_bytes(self) -> int:
        try:
            return sum(p.stat().st_size
                       for p in self.dir.glob("*.json"))
        except OSError:
            return 0

    def clear(self) -> int:
        n = 0
        try:
            for p in self.dir.glob("*.json"):
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
        except OSError:
            pass
        return n


# Writes between eviction sweeps. A sweep is O(files in cache), so
# doing one per write is what made a full cache slow to browse; doing
# one per few hundred writes keeps the cap honest at negligible cost.
EVICT_EVERY = 200

# In-memory cache size. Holds COMPRESSED bytes, never decoded images:
# an 850px preview is 12 KB compressed and 2.8 MB decoded, so caching
# pixmaps would cost 26 GB over an afternoon's browsing against 113 MB
# for the bytes. The decode is cheap (a few ms); the memory is not.
#
# 64 MB is roughly 1,600 thumbnails at the size Danbooru serves —
# comfortably more than a browsing session revisits, and small enough
# to be invisible beside Qt's own footprint.
MEMORY_CAP_BYTES = 64 * 1024 * 1024

# Disk cache sizes offered in Preferences, in megabytes. Zero means
# "do not write images to disk at all"; the memory cache still runs,
# so browsing stays fast within a session and leaves nothing behind.
DISK_CACHE_CHOICES = [
    (0, "Off \u2014 nothing written to disk",
     "Images are kept in memory only and forgotten when you close "
     "the program. Browsing stays fast while it is open. Choose this "
     "on a shared machine, or if you would rather not leave a record "
     "of what you viewed."),
    (50, "Small \u2014 about 1,200 images",
     "Enough for a session's browsing. Re-downloads anything older."),
    (200, "Standard \u2014 about 5,000 images",
     "The default. Covers several days of normal use, so returning "
     "to a tag you looked at yesterday is instant."),
    (500, "Large \u2014 about 12,500 images",
     "Months of regular use. Only worth it on a slow connection."),
]
DEFAULT_DISK_CACHE_MB = 200


class ImageCache:
    """Thumbnails/samples by post id, LRU-evicted to a byte cap.
    Access refreshes recency (mtime); eviction removes oldest first
    and never removes the entry just written."""

    def __init__(self, directory: Path,
                 cap_bytes: int = IMAGE_CACHE_CAP_BYTES,
                 memory_cap_bytes: int = MEMORY_CAP_BYTES) -> None:
        self.dir = Path(directory)
        # Zero disables disk caching entirely.
        self.cap_bytes = int(cap_bytes)
        self.memory_cap_bytes = int(memory_cap_bytes)
        self._writes_since_evict = 0
        # Ordered so the front is the least recently used. Values are
        # compressed bytes; see MEMORY_CAP_BYTES.
        self._mem: "OrderedDict[str, bytes]" = OrderedDict()
        self._mem_bytes = 0

    def _path(self, key: str) -> Path:
        return self.dir / f"{_safe_key(key)}.bin"

    def get(self, key: str) -> Optional[bytes]:
        """Memory first, then disk. A disk hit is promoted to memory
        so that a second look costs nothing."""
        data = self._mem.get(key)
        if data is not None:
            self._mem.move_to_end(key)      # most recently used
            return data

        p = self._path(key)
        try:
            data = p.read_bytes()
        except OSError:
            return None
        try:
            # Marks the entry as recently used, for eviction order.
            # Separate from the read so that a failure to touch — a
            # read-only drive, say — still returns the data.
            os.utime(p, None)
        except OSError:
            pass
        self._remember(key, data)
        return data

    def _remember(self, key: str, data: bytes) -> None:
        """Hold bytes in memory, dropping the least recently used once
        over the cap.

        Unlike the disk sweep this is genuinely cheap: the running
        total is tracked as entries come and go, so nothing has to be
        measured or scanned.
        """
        if self.memory_cap_bytes <= 0:
            return
        if key in self._mem:
            self._mem_bytes -= len(self._mem.pop(key))
        self._mem[key] = data
        self._mem_bytes += len(data)
        while self._mem_bytes > self.memory_cap_bytes and self._mem:
            _old_key, old_data = self._mem.popitem(last=False)
            self._mem_bytes -= len(old_data)

    def memory_bytes(self) -> int:
        return self._mem_bytes

    def put(self, key: str, data: bytes) -> None:
        """Write one entry. Eviction is NOT done here.

        FIELD REPORT: "the program softlocks while posts load", and it
        got worse the longer a session ran.

        This method used to sweep the whole cache directory after
        every single write — a glob plus two stat() calls per file —
        on the UI thread.

        THE COST WAS THE MEASUREMENT, NOT THE DELETION. _evict has to
        stat every file to learn the cache's total size, and it does
        that BEFORE it can know whether anything needs deleting. So a
        cache comfortably under its cap paid the full price and threw
        nothing away. Measured on a cache at 140 MB against a 200 MB
        cap: about 100 ms per write, 0 files deleted — roughly two
        seconds of frozen window for a page of twenty thumbnails.

        That is why the freeze scaled with the NUMBER OF CACHED FILES
        rather than with how full the cache was, why it was not
        specific to the first load, and why it would be far worse on a
        slow or network drive.

        The size cap is still enforced, but by maybe_evict() on a
        schedule the caller controls.
        """
        self._remember(key, data)
        if self.cap_bytes <= 0:
            # Disk caching turned off in Preferences. The memory cache
            # above still serves this session.
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._path(key).write_bytes(data)
            self._writes_since_evict += 1
        except OSError:
            pass

    def maybe_evict(self, force: bool = False) -> bool:
        """Trim the cache if it is worth the sweep.

        Called when the program is idle rather than mid-page. Returns
        True if a sweep actually ran.
        """
        if not force and self._writes_since_evict < EVICT_EVERY:
            return False
        self._writes_since_evict = 0
        self._evict(keep=None)
        return True

    def _evict(self, keep: Optional[Path] = None) -> None:
        """Delete oldest-first until the cache is under its cap.

        Unavoidably O(files in cache): finding the total size means
        stat-ing every file, and that has to happen before we can know
        whether any deletion is needed. That is exactly why this must
        not run on the write path — see put().
        """
        try:
            entries = [(p.stat().st_mtime, p.stat().st_size, p)
                       for p in self.dir.glob("*.bin")]
        except OSError:
            return
        total = sum(size for _, size, _ in entries)
        entries.sort()                # oldest first
        for _, size, p in entries:
            if total <= self.cap_bytes:
                break
            if p == keep:
                continue
            try:
                p.unlink()
                total -= size
            except OSError:
                pass

    def set_disk_cap(self, cap_bytes: int) -> None:
        """Change the disk limit and apply it at once.

        Immediate rather than deferred: a setting that appears to do
        nothing until some later moment is a setting people distrust.
        """
        self.cap_bytes = int(cap_bytes)
        if self.cap_bytes <= 0:
            self.clear()
            return
        self._evict(keep=None)

    def size_bytes(self) -> int:
        try:
            return sum(p.stat().st_size
                       for p in self.dir.glob("*.bin"))
        except OSError:
            return 0

    def clear(self) -> int:
        self._mem.clear()
        self._mem_bytes = 0
        n = 0
        try:
            for p in self.dir.glob("*.bin"):
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
        except OSError:
            pass
        return n
