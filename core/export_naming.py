"""
core/export_naming.py

How a downloaded post is named on disk.

FIELD REPORT: exports were named `danbooru_<id>`, which is stable and
unambiguous and completely useless for finding the picture you saved
ten minutes ago. Once a folder has a few hundred of them, sorting by
name gives you numeric order — which is upload order on Danbooru, not
your order.

So the name is now a preference. Every mode below keeps the post id
somewhere, because it is the only thing that identifies the source
without doubt, and drops the caption sidecar next to the image under
the same stem so the pair never separates.

Timestamps are local time in `YYYYMMDD-HHMMSS` form: it sorts
correctly as text, which is the whole point of putting it first.
"""
from __future__ import annotations

import datetime
import re

# Windows forbids these outright; the control characters below 0x20
# are illegal everywhere worth supporting.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Trailing dots and spaces are legal to create on Linux and quietly
# stripped by Windows, which turns "tag ." and "tag" into a collision.
_TRAILING = " ."

# How many tags to fold into a descriptive name. Enough to recognise
# the picture, short enough to leave room under the path limit.
TAG_WORDS = 4
# A single name component this long leaves room for a deep folder and
# the sidecar's extension inside Windows' 260-character path limit.
MAX_STEM = 80

MODE_ID = "id"
MODE_TIME_ID = "time_id"
MODE_TIME_TAGS = "time_tags"
MODE_ARTIST_ID = "artist_id"

# (key, label, example) — the example is what the Preferences page
# shows, because "time_tags" means nothing until you see one.
MODES: list[tuple[str, str, str]] = [
    (MODE_ID, "Post id",
     "danbooru_7431892.png"),
    (MODE_TIME_ID, "Date, time, post id",
     "20260802-143005_danbooru_7431892.png"),
    (MODE_TIME_TAGS, "Date, time, post id, first few tags",
     "20260802-143005_danbooru_7431892_1girl_solo_long_hair.png"),
    (MODE_ARTIST_ID, "Artist, post id",
     "wada_arco_danbooru_7431892.png"),
]
DEFAULT_MODE = MODE_TIME_ID
_VALID = {key for key, _label, _example in MODES}


def clean_component(text: str) -> str:
    """Make a fragment safe as part of a filename."""
    cleaned = _ILLEGAL.sub("", text or "")
    cleaned = cleaned.replace(" ", "_").strip(_TRAILING)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def _stamp(when=None) -> str:
    when = when or datetime.datetime.now()
    return when.strftime("%Y%m%d-%H%M%S")


def build_stem(post, mode: str = DEFAULT_MODE, when=None) -> str:
    """The filename without its extension.

    `post` is duck-typed: anything with `id`, and optionally
    `tag_string_general` and `tag_string_artist`.
    """
    if mode not in _VALID:
        mode = DEFAULT_MODE
    base = f"danbooru_{getattr(post, 'id', 0)}"

    if mode == MODE_ID:
        stem = base
    elif mode == MODE_TIME_ID:
        stem = f"{_stamp(when)}_{base}"
    elif mode == MODE_TIME_TAGS:
        tags = (getattr(post, "tag_string_general", "") or "").split()
        if not tags:
            tags = (getattr(post, "tag_string", "") or "").split()
        words = "_".join(clean_component(t) for t in tags[:TAG_WORDS])
        stem = f"{_stamp(when)}_{base}"
        if words:
            stem = f"{stem}_{words}"
    else:  # MODE_ARTIST_ID
        artists = (getattr(post, "tag_string_artist", "") or "").split()
        artist = clean_component(artists[0]) if artists else ""
        stem = f"{artist}_{base}" if artist else base

    stem = clean_component(stem)
    if len(stem) > MAX_STEM:
        # Trim the tail, never the head: the timestamp and id are what
        # make the name findable and unique.
        stem = stem[:MAX_STEM].rstrip("_")
    return stem or base
