"""
core/state.py

In-memory model of a TagWalker session.

The SessionState class owns:
- Image and tag inventory (built from a ScanResult)
- Per-image tag cache (kept in sync with disk via this module's writes
  and via external-change notifications from the watcher)
- Per-(image, tag) decision history (yes / no / skipped)
- Per-tag status (pending / completed / skipped)
- Walk position (current tag, current queue, index within queue)
- Undo history (in-memory only, not persisted across sessions)
- Filter and sort settings

What this module does NOT do:
- Scanning (core/scanner.py)
- Disk I/O beyond delegating to core/tag_io.py
- File watching (core/watcher.py)
- Save/load of session JSON (core/persistence.py)
- Anything UI-related (no Qt imports here)

The class is pure Python and individually unit-testable without
PySide6. Listeners are registered via add_listener; the UI layer
wraps state with a Qt adapter in main_window.py.

Thread model:
All public methods must be called from the main thread. The scanner
runs on a worker thread but is only handed off to state on the main
thread. Watcher callbacks must also marshal to the main thread before
calling apply_external_*.
"""

from __future__ import annotations

from functools import lru_cache as _lru_cache

import re
import unicodedata
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from core import tag_io
from core.scanner import ImageEntry, ScanResult


# ---------------------------------------------------------------------------
# Sorting helpers
# ---------------------------------------------------------------------------

_NATSORT_RE = re.compile(r"(\d+)")


# Characters that render as nothing but break string matching. They ride
# in on copy-paste (web pages, IME buffers) and produce "impostor" tags
# that LOOK identical to a real tag yet fail exact comparison — the
# field symptom was a search for the full tag matching nothing while a
# prefix of it matched (the invisible char sat in the unmatched tail).
_ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060")


def _fold_for_match(s: str) -> str:
    """Fold a tag/query for tolerant comparison: lowercase, NFKC (maps
    fullwidth forms like ｌ or ＿ to their ASCII equivalents), strip
    zero-width characters, and treat spaces as underscores. Applied to
    BOTH sides of every search comparison, so visually-identical
    impostor tags match the text the user actually sees."""
    s = unicodedata.normalize("NFKC", s.lower())
    for zw in _ZERO_WIDTH:
        s = s.replace(zw, "")
    return s.replace(" ", "_")


def apply_front_locks(tags: list[str], locked: list[str]) -> list[str]:
    """Stable caption reorder for trigger-token conditioning: every
    locked token PRESENT in `tags` (fold-matched — "Long Hair" ==
    long_hair) moves to the front, in locked-list order; all remaining
    tags keep their relative order. Tokens not present are NOT added
    (the batch op handles adding). Returns the original list object
    when nothing changes, so callers can no-op cheaply."""
    if not locked or not tags:
        return tags
    folds = [_fold_for_match(t) for t in tags]
    picked: list[int] = []
    for lock in locked:
        lf = _fold_for_match(lock)
        for i, f in enumerate(folds):
            if f == lf and i not in picked:
                picked.append(i)
                break
    if not picked:
        return tags
    reordered = [tags[i] for i in picked] + [
        t for i, t in enumerate(tags) if i not in picked
    ]
    return tags if reordered == tags else reordered


# Filenames do not change, so neither does their sort key — but every
# queue rebuild recomputed one for each entry, twice (subfolder and
# filename). Measured on 2,400 names: 4.2 ms per sort uncached, 0.3 ms
# memoised, and a rebuild sorts twice. Bounded rather than unbounded so
# a long session that opens many folders cannot grow it without limit;
# 50,000 covers datasets far larger than anything expected.
@_lru_cache(maxsize=50_000)
def natural_key(s: str):
    """Sort key for natural (human) ordering of strings that embed
    numbers: ``hand_(5)`` sorts before ``hand_(10)`` (not after, as a
    plain lexical sort would do, since '1' < '5').

    The string is split into alternating text and numeric runs; numeric
    runs compare as integers and text runs compare case-insensitively.
    Each chunk is tagged (0 = text, 1 = number) so a text chunk and a
    number chunk are never compared against each other — which would
    raise a TypeError in Python 3.

    Used for both the tag-tree ordering and the image queue ordering so
    files and tags like ``a_(2)`` / ``a_(10)`` line up the way a person
    reads them.
    """
    out = []
    for i, part in enumerate(_NATSORT_RE.split(s)):
        if i & 1:  # odd index -> a run of digits
            out.append((1, int(part)))
        else:
            out.append((0, part.lower()))
    return out



# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Decision(Enum):
    """User decision for a single (image, tag) pair.

    UNPROCESSED is the implicit default and is NEVER stored in the
    decisions dict — its absence from the dict represents this value.
    Storing it explicitly would waste memory (N_images x N_tags entries)
    and bypass the natural "no decision yet" semantics.
    """
    UNPROCESSED = "unprocessed"
    YES = "yes"
    NO = "no"
    SKIPPED = "skipped"


class TagStatus(Enum):
    """Aggregate review status of a single tag across all images."""
    PENDING = "pending"      # at least one image still needs a YES/NO for this tag
    COMPLETED = "completed"  # every non-orphan image has a YES or NO (SKIPPED doesn't count)
    SKIPPED = "skipped"      # user explicitly skipped the entire tag


class FilterMode(Enum):
    """How the walk queue is filtered."""
    ALL = "all"
    HAS_TAG = "has_tag"
    MISSING_TAG = "missing_tag"
    SKIPPED_ONLY = "skipped_only"  # only images SKIPPED for the current tag
    OVER_TOKEN_LIMIT = "over_token_limit"  # captions strictly over the limit


class SortMode(Enum):
    """How the walk queue is ordered."""
    ALPHA_ASC = "alpha_asc"
    ALPHA_DESC = "alpha_desc"
    TAG_COUNT_DESC = "tag_count_desc"   # most tags first
    TAG_COUNT_ASC = "tag_count_asc"     # fewest tags first


class TagSortMode(Enum):
    """How tags are ordered within each folder in the tag tree.

    Folder grouping itself is always alphabetical; this controls the
    ordering of the tag rows under each folder header.
    """
    ALPHA_ASC = "alpha_asc"       # A -> Z (default)
    ALPHA_DESC = "alpha_desc"     # Z -> A
    COUNT_DESC = "count_desc"     # most images first
    COUNT_ASC = "count_asc"       # fewest images first


# ---------------------------------------------------------------------------
# Notification payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateChange:
    """Notification emitted to listeners when state changes.

    The UI layer subscribes via add_listener and translates these into
    Qt signals. Kinds are kept as strings (not enums) so new event
    types can be added without ripple changes; listeners filter on the
    kind they care about and ignore the rest.

    Recognized kinds:
    - "tag_selected"     : a new tag is being walked; walk index reset
    - "walk_advanced"    : moved to next image in current queue
    - "walk_ended"       : current walk finished (queue exhausted or
                           tag skipped). extra="end_of_tree" if no more
                           pending tags ahead in the tree.
    - "image_changed"    : tags for a specific image changed
    - "tag_changed"      : a tag's status, count, or skip state changed
    - "tree_rebuilt"     : tag introduced or removed; sidebar must rebuild
    - "filter_changed"   : filter / sort / show_orphans changed; queue
                           was rebuilt
    - "action_logged"    : dedicated entry for the action log; `extra`
                           carries the human-readable description, `tag`
                           and/or `image_path` may carry context for
                           colorization. Emitted from operations whose
                           semantics are too coarse to infer from the
                           other event kinds alone (global rename/delete,
                           batch decisions, single-image tag edits).
    """
    kind: str
    image_path: Optional[Path] = None
    tag: Optional[str] = None
    extra: str = ""


# ---------------------------------------------------------------------------
# Undo entry
# ---------------------------------------------------------------------------


@dataclass
class UndoEntry:
    """One reversible user action.

    The revert callable captures (via closure) whatever state it needs
    to restore. Each action method builds its own closure inline rather
    than serializing a generic snapshot — this keeps the per-action
    cost proportional to what actually changed, not to total state size.

    `context` is an optional, opaque dict the caller can attach to carry
    UI-level information about the action that the UI needs back when the
    action is undone — e.g. the queue's group key for a group batch op,
    so undo can return the selection to that group. The state never
    interprets it; it just stores it and exposes the reverted entry's
    context via last_undo_context. This keeps such per-action UI metadata
    perfectly in sync with the real undo stack (no parallel UI-side stack
    that could drift).
    """
    description: str
    revert_callable: Callable[[], None]
    context: Optional[dict] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------


def _adjust_counts(
    counts: dict[str, int],
    folder_counts: dict[str, dict[str, int]],
    subfolder: str,
    removed: list[str],
    added: list[str],
) -> tuple[list[str], list[str]]:
    """Apply tag diffs to global and per-folder count maps atomically.

    Returns (introduced, disappeared):
    - introduced: tags whose global count went from 0 to >0
    - disappeared: tags whose global count went from >0 to 0

    Centralized so all four mutation paths (user yes/no, undo, external
    edit, delete tag) use identical accounting. No chance of one path
    forgetting to update per-folder counts while another does.
    """
    introduced: list[str] = []
    disappeared: list[str] = []
    sub = folder_counts.setdefault(subfolder, {})

    for tag in removed:
        # Global
        new_global = counts.get(tag, 0) - 1
        if new_global <= 0:
            counts.pop(tag, None)
            disappeared.append(tag)
        else:
            counts[tag] = new_global
        # Per-folder
        new_sub = sub.get(tag, 0) - 1
        if new_sub <= 0:
            sub.pop(tag, None)
        else:
            sub[tag] = new_sub

    for tag in added:
        if tag not in counts:
            introduced.append(tag)
        counts[tag] = counts.get(tag, 0) + 1
        sub[tag] = sub.get(tag, 0) + 1

    # Clean up the per-folder dict if it became empty, but only if there
    # are no images in that subfolder either. (Empty dict is harmless;
    # we don't bother cleaning.)
    return introduced, disappeared


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class _VersionedDict(dict):
    """A dict that counts its own mutations.

    Used for the decisions map so derived values can tell when they
    are stale. The alternative — bumping a counter by hand at each of
    the fourteen places that write to it — is one missed line away
    from a cache that silently returns yesterday's answer, which is
    the failure mode this codebase keeps meeting.
    """

    __slots__ = ("version",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.version = 0

    def __setitem__(self, key, value) -> None:
        self.version += 1
        super().__setitem__(key, value)

    def __delitem__(self, key) -> None:
        self.version += 1
        super().__delitem__(key)

    def pop(self, *args, **kwargs):
        self.version += 1
        return super().pop(*args, **kwargs)

    def popitem(self):
        self.version += 1
        return super().popitem()

    def clear(self) -> None:
        self.version += 1
        super().clear()

    def update(self, *args, **kwargs) -> None:
        self.version += 1
        super().update(*args, **kwargs)

    def setdefault(self, *args, **kwargs):
        self.version += 1
        return super().setdefault(*args, **kwargs)


class SessionState:
    """Central in-memory model of a TagWalker session."""

    # Bound on undo history. Matches the original beta's behavior so
    # long sessions don't bloat memory with thousands of closures.
    UNDO_LIMIT: int = 100

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, scan_result: ScanResult) -> None:
        # --- Inventory -------------------------------------------------
        self.root: Path = scan_result.root
        self.roots: list[Path] = (
            list(scan_result.roots) if scan_result.roots
            else [scan_result.root]
        )
        self._images: list[ImageEntry] = list(scan_result.images)
        self._images_by_path: dict[Path, ImageEntry] = {
            e.image_path: e for e in self._images
        }
        self._subfolders: list[str] = list(scan_result.subfolders)
        self._images_by_subfolder: dict[str, list[ImageEntry]] = {
            sf: list(imgs) for sf, imgs in scan_result.images_by_subfolder.items()
        }

        # --- Disk mirror -----------------------------------------------
        # In-memory copy of every caption file's contents. Kept in sync
        # with disk on every state-initiated write and on every watcher
        # notification. UI reads from here, never from disk directly.
        self._image_tags: dict[Path, list[str]] = {
            p: list(tags) for p, tags in scan_result.initial_tags.items()
        }
        # Lazy per-image CLIP token-count cache. Populated on demand by
        # token_count_for(); an entry is invalidated (popped) whenever the
        # image's caption changes, so the "over token limit" filter and
        # the file-state badge never recount an unchanged caption. Counting
        # all ~1,200 captions cold is ~60 ms, but caching keeps repeated
        # queue rebuilds during tagging effectively free.
        self._token_count_cache: dict[Path, int] = {}
        # Mirror of "does a .txt file exist for this image". Flips True
        # when an orphan gets a caption file (yes/no on orphan, or
        # external creation). Flips False on external delete.
        self._has_txt: dict[Path, bool] = {
            e.image_path: e.has_txt for e in self._images
        }

        # --- Counts ----------------------------------------------------
        self._tag_counts: dict[str, int] = dict(scan_result.tag_counts)
        self._tag_counts_by_folder: dict[str, dict[str, int]] = {
            sf: dict(c) for sf, c in scan_result.tag_counts_by_subfolder.items()
        }

        # --- Review state ---------------------------------------------
        # Sparse: only stores actual decisions, never UNPROCESSED. So
        # memory grows with user actions, not with N_images x N_tags.
        self._decisions = _VersionedDict()

        # Per-tag aggregate status. Every tag known to exist (i.e. has
        # count > 0 OR was once known and might still have decisions)
        # has an entry. Default PENDING.
        self._tag_status: dict[str, TagStatus] = {
            tag: TagStatus.PENDING for tag in self._tag_counts
        }

        # Tags the user has manually marked complete. These behave like
        # auto-completed tags (status==COMPLETED, auto-advance skips
        # them) but are "sticky" — _update_tag_status and
        # recalculate_all_tag_statuses leave them alone so subsequent
        # decisions on these tags' images don't flip them back to
        # PENDING. The user reverts via undo (immediate) or — if
        # implemented later — a "Reset tag" action.
        #
        # Manually completed differs from SKIPPED only in intent:
        # SKIPPED = "I don't care about this tag right now"
        # COMPLETED (manual) = "I've reviewed this tag, it's done"
        # The distinction is preserved because the user thinks about
        # them differently even though both remove the tag from the
        # to-do queue.
        self._manually_completed: set[str] = set()

        # --- Walk position --------------------------------------------
        self._current_tag: Optional[str] = None
        self._current_queue: list[ImageEntry] = []
        self._walk_index: int = 0

        # --- Multi-select mode ----------------------------------------
        # When on, the queue is driven by a SET of selected tags (with
        # the filter dropdown applying set logic) instead of one walking
        # tag. There is no per-tag walk in this mode: the user browses a
        # filtered image set and edits whole images. Yes/No/Skip-tag are
        # inert (current_tag is None, so _record_decision early-returns).
        self._multi_select_mode: bool = False
        self._selected_tags: list[str] = []
        # Re-entrancy guard for live-shrink (removing an image from the
        # multi-select queue the moment a tag edit makes it stop matching).
        self._in_shrink: bool = False

        # --- Browse mode ----------------------------------------------
        # A no-tag mode for inspecting a folder's images before any
        # captioning has begun. Entered by clicking a folder header. The
        # queue is every image in the chosen folder (honouring the
        # "show images without .txt" toggle exactly as a tag walk does),
        # so fresh uncaptioned images can be selected and captioned from
        # zero via the file-state panel — without first having to pick an
        # arbitrary tag just to make the queue non-empty. current_tag is
        # None here, so Yes/No/Skip record nothing (the UI also disables
        # them); only caption editing is live. `None` for the subfolder
        # means "not browsing"; "" is the root folder, a real value is a
        # named subfolder.
        self._browse_mode: bool = False
        self._browse_subfolder: Optional[str] = None

        # --- Filter / sort --------------------------------------------
        self._filter_mode: FilterMode = FilterMode.ALL
        self._sort_mode: SortMode = SortMode.ALPHA_ASC
        self._show_orphans: bool = False
        # Caption token limit used by the OVER_TOKEN_LIMIT filter. Set by
        # the UI from the preference (default matches the setting default).
        # A caption is "over" when its count is STRICTLY greater than this.
        self._token_limit: int = 225
        # Which tokenizer the count uses (SDXL=CLIP by default). Set from
        # the preference alongside the limit; the OVER_TOKEN_LIMIT filter
        # and the file-state badge count with THIS tokenizer. Changing it
        # invalidates cached counts (a caption's token count is
        # tokenizer-specific).
        from core import multi_tokenizer as _mt
        self._tokenizer_target: str = _mt.default_target_key()
        # Queue search: a substring filter applied on top of filter
        # mode + sort. Walk is restricted to images whose filename
        # contains this substring (case-insensitive). Empty string
        # disables the filter. When non-empty, auto-yes is forced
        # off — bursting through a filtered subset risks the user
        # blasting yes on images they wanted to review individually.
        self._queue_search: str = ""
        # Exact-match toggle for the queue search box. False (default):
        # a single term matches as a substring of filename OR tag, while
        # a comma-separated query matches exact tags (AND). True: the
        # whole query is matched as exact tag(s) (comma = AND), ignoring
        # filenames — so "hand" matches only images tagged exactly
        # "hand", not "hand_on_hip". Switchable from the queue search row.
        self._queue_search_exact: bool = False
        # Search lock (#14): when True, select_tag preserves the queue
        # search filter (and tries to keep the displayed image) across
        # tag changes, so the user can inspect the same filtered image(s)
        # under different tags. When False (default), selecting a tag
        # clears the search as usual.
        self._search_locked: bool = False
        # Tag tree ordering (separate from the queue sort above).
        self._tag_sort_mode: TagSortMode = TagSortMode.ALPHA_ASC
        # Auto-yes: when True, the walk auto-confirms images that already
        # contain the current tag and stops only on images missing it.
        self._auto_yes: bool = False
        # Re-entry guard so the _advance_walk inside an auto-yes record
        # doesn't recursively trigger another auto-yes run.
        self._in_auto_yes: bool = False
        # What to do when a tag's walk finishes: "stop" (end the walk and
        # show a completion message) or "advance" (jump to next pending
        # tag). Default "stop" so the user always notices the transition.
        self._tag_complete_behavior: str = "stop"
        # Co-occurrence "too common" filter: candidate tags appearing on
        # MORE than this percent of all images are dropped from the
        # hints list as too ubiquitous to carry meaningful association
        # (e.g. "1girl" or "sweat" in an anime dataset). Pairs with
        # lift-based ranking. Default 50%.
        self._cooccur_too_common_pct: int = 50

        # Which engine powers the file-state co-occurrence hints:
        # "danbooru" (bundled official lookup, default) or "dataset"
        # (co-occurrence from the user's own loaded images). Synced from
        # Settings.default_cooccur_source when the state is created.
        self._cooccur_source: str = "danbooru"

        # Master on/off for the co-occurrence hints panel. True (default)
        # shows it; False hides it entirely (independent of source +
        # threshold). Synced from Settings.default_show_cooccur_hints.
        self._show_cooccur_hints: bool = True

        # --- Undo ------------------------------------------------------
        self._undo_stack: deque[UndoEntry] = deque(maxlen=self.UNDO_LIMIT)
        # Coalescing: while a coalesce block is open, undo entries pushed
        # inside it are collected and, on close, replaced by ONE combined
        # entry. Lets a single user action that touches several tags (e.g.
        # a multi-select Add that stamps multiple ticked tags) reverse in
        # one Ctrl+Z. We count entries pushed during the block rather than
        # remembering a stack position: the bounded undo deque discards its
        # OLDEST (leftmost) entry when full, which would invalidate a saved
        # index but never touches the in-block entries we just appended at
        # the right. Only the outermost begin/end pair actually coalesces.
        self._coalesce_depth: int = 0
        self._coalesce_pushed: int = 0
        # Multi-select "pass": every Add made since entering multi-select,
        # tracked BY REFERENCE (not by a positional index — the bounded
        # undo deque discards its oldest entries when full, which would
        # invalidate any index). Powers the one-click "Undo all additions
        # from this pass". Reset on every entry/exit of multi-select.
        self._multi_pass_entries: list[UndoEntry] = []

        # Names of caption files a global mutator (rename/delete/split
        # tag) could not write even after retries — surfaced to the user
        # so a locked file is never a silent partial failure. Reset at the
        # start of each such operation.
        self._last_write_failures: list[str] = []
        # Trigger tokens locked to the caption front (field feature:
        # LoRA trigger-word conditioning). Enforced on every FORWARD
        # caption write; undo restores exact previous bytes untouched.
        # Session-persisted (additive key, old files unaffected).
        self._front_locked_tokens: list[str] = []
        # Streak of consecutive persistent write failures. Once it passes
        # _WRITE_RETRY_GIVEUP_STREAK, _write_tags_with_retry stops
        # sleeping between attempts: on a wholly unwritable dataset (e.g.
        # files carrying Windows' read-only attribute after an archive
        # copy) a batch would otherwise burn ~0.24s of sleep per file —
        # minutes of frozen UI on a large set. Any successful write
        # resets the streak, so healthy datasets never notice.
        self._consec_write_failures: int = 0

        # --- Listeners -------------------------------------------------
        self._listeners: list[Callable[[StateChange], None]] = []

        # --- Tree iteration order -------------------------------------
        # The order in which tags should be visited when auto-advancing.
        # Matches the visual tree: subfolders alphabetically, tags
        # alphabetically within each, but each tag appears once globally
        # (at its first subfolder of appearance). This implements the
        # "global tags grouped visually" design — Yes on `1girl` under
        # `train` walks every `1girl` in the dataset, and the tag is
        # marked complete after one pass.
        self._tree_order: list[tuple[str, str]] = []
        self._rebuild_tree_order()

    # ------------------------------------------------------------------
    # Listener management
    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[StateChange], None]) -> None:
        """Subscribe to state-change notifications.

        Listeners are invoked synchronously in the same thread that
        triggered the mutation (always the main thread). A misbehaving
        listener (exception) is caught and silently ignored so it
        cannot wedge state.
        """
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[StateChange], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _emit(self, change: StateChange) -> None:
        # Iterate over a snapshot so a listener can safely remove itself
        # in response to a notification.
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception:
                # In production we would log this. For now, silent
                # continue: a bad UI listener must never corrupt state.
                pass
        # Live-shrink (#4): in multi-select mode a tag edit may have made
        # the edited image stop matching the set filter. Drop it AFTER
        # listeners have seen the image_changed (so the edit is reflected
        # first); _maybe_shrink_multi then emits its own filter_changed.
        # Guarded by _in_shrink against re-entry.
        if (change.kind == "image_changed"
                and self._multi_select_mode
                and change.image_path is not None
                and not self._in_shrink):
            self._maybe_shrink_multi(change.image_path)

    # ------------------------------------------------------------------
    # Read accessors  (UI uses these heavily; all are O(1) or O(small))
    # ------------------------------------------------------------------

    @property
    def all_images(self) -> list[ImageEntry]:
        return list(self._images)

    @property
    def all_tags(self) -> list[str]:
        return sorted(self._tag_counts.keys())

    @property
    def subfolders(self) -> list[str]:
        return list(self._subfolders)

    @property
    def total_images(self) -> int:
        return len(self._images)

    @property
    def orphan_count(self) -> int:
        # Computed on demand. Cheap at any plausible dataset size.
        return sum(1 for v in self._has_txt.values() if not v)

    @property
    def current_tag(self) -> Optional[str]:
        return self._current_tag

    @property
    def current_image(self) -> Optional[ImageEntry]:
        if not self._current_queue:
            return None
        if 0 <= self._walk_index < len(self._current_queue):
            return self._current_queue[self._walk_index]
        return None

    @property
    def walk_index(self) -> int:
        return self._walk_index

    @property
    def walk_size(self) -> int:
        return len(self._current_queue)

    @property
    def filter_mode(self) -> FilterMode:
        return self._filter_mode

    @property
    def sort_mode(self) -> SortMode:
        return self._sort_mode

    @property
    def show_orphans(self) -> bool:
        return self._show_orphans

    def get_image_tags(self, image_path: Path) -> list[str]:
        """Return a copy of the current in-memory tags for an image."""
        return list(self._image_tags.get(image_path, []))

    def token_count_for(self, image_path: Path) -> int:
        """CLIP content-token count of an image's caption, cached.

        The cache is invalidated whenever the caption changes (see
        _invalidate_token_count), so an unchanged caption is counted once.
        Returns 0 for an image with no tags (including orphans), and also
        0 if the tokenizer vocab is unavailable — a missing count must not
        crash the queue build or the badge; callers treat 0 as "not over
        any limit", which is the safe direction.
        """
        cached = self._token_count_cache.get(image_path)
        if cached is not None:
            return cached
        tags = self._image_tags.get(image_path, [])
        if not tags:
            self._token_count_cache[image_path] = 0
            return 0
        try:
            from core import multi_tokenizer as mt
            n = int(mt.count_tokens(", ".join(tags), self._tokenizer_target))
        except Exception:
            # Tokenizer unavailable (missing vocab/lib/file): don't cache
            # (a later successful load should be able to count), and report
            # 0 so nothing is falsely flagged over-limit.
            return 0
        self._token_count_cache[image_path] = n
        return n

    def _invalidate_token_count(self, image_path: Path) -> None:
        """Drop the cached token count for an image whose caption changed."""
        self._token_count_cache.pop(image_path, None)

    def _set_image_tags(self, image_path: Path, tags: list[str]) -> None:
        """Assign an image's in-memory tags AND invalidate its cached token
        count in one place, so no caption mutation can leave a stale count
        behind. Every write to _image_tags[path] should go through here.
        """
        self._image_tags[image_path] = tags
        self._token_count_cache.pop(image_path, None)

    def get_image_by_path(self, image_path: Path) -> Optional[ImageEntry]:
        """Look up an ImageEntry by its image path.

        Returns None if no image is registered at that path. Used by
        UI widgets that need to refresh a specific row's display
        (e.g. when resetting the previous current-image highlight).
        """
        return self._images_by_path.get(image_path)

    def has_caption_file(self, image_path: Path) -> bool:
        return self._has_txt.get(image_path, False)

    def is_tag_in_image(self, image_path: Path, tag: str) -> bool:
        return tag in self._image_tags.get(image_path, [])

    def get_decision(self, image_path: Path, tag: str) -> Decision:
        return self._decisions.get((image_path, tag), Decision.UNPROCESSED)

    def get_tag_status(self, tag: str) -> TagStatus:
        return self._tag_status.get(tag, TagStatus.PENDING)

    def get_tag_count_global(self, tag: str) -> int:
        return self._tag_counts.get(tag, 0)

    def get_images_with_tag(self, tag: str) -> list[Path]:
        """Return every image path whose current caption contains ``tag``.

        Read-only convenience for UI features that want to preview the
        images carrying a tag (e.g. the audit tool's per-row image view).
        Iterates the in-memory caption mirror; fine for a one-off click,
        not a hot path. Order follows the dataset's image order.
        """
        out: list[Path] = []
        for entry in self._images:
            p = entry.image_path
            if tag in self._image_tags.get(p, []):
                out.append(p)
        return out

    def get_tag_count_in_subfolder(self, tag: str, subfolder: str) -> int:
        return self._tag_counts_by_folder.get(subfolder, {}).get(tag, 0)

    def get_decided_count(self, tag: str) -> int:
        """Number of non-orphan images with a YES or NO decision for this tag.

        Matches the project/tag completion semantics: SKIPPED is a
        deferral, not a decision, and orphan images are outside the
        completion universe — so neither counts here. (This method is
        currently unused, but is kept correct so it can be wired into a
        UI counter later without reintroducing the skip/orphan
        inconsistencies the rest of the code was fixed to avoid.)

        Cost: O(N_decisions).
        """
        count = 0
        for (image_path, t), decision in self._decisions.items():
            if t != tag:
                continue
            if decision != Decision.YES and decision != Decision.NO:
                continue  # SKIPPED / UNPROCESSED are not "decided"
            if not self._has_txt.get(image_path, False):
                continue  # orphan image: outside the completion universe
            count += 1
        return count

    def get_tags_in_subfolder(self, subfolder: str) -> list[str]:
        """Tags that appear in at least one image of this subfolder."""
        return sorted(self._tag_counts_by_folder.get(subfolder, {}).keys())

    def get_tree_layout(self) -> dict[str, list[str]]:
        """Return the deduplicated tag layout for the tree UI.

        Returns ``{subfolder: [tag, tag, ...]}`` where each tag
        appears under exactly one subfolder — the first one in
        alphabetical order that contains the tag. This matches the
        canonical iteration order used for auto-advance and prevents
        the same tag from showing up under multiple folders.

        Every subfolder in self.subfolders gets an entry, even if its
        tag list ends up empty (because all of its tags appeared
        under earlier folders). Empty folder headers are still useful
        navigation context — they tell the user the folder exists
        and which folders own which tags.
        """
        layout: dict[str, list[str]] = {sf: [] for sf in self._subfolders}
        for subfolder, tag in self._tree_order:
            layout.setdefault(subfolder, []).append(tag)
        return layout

    def get_current_queue(self) -> list[ImageEntry]:
        return list(self._current_queue)

    def current_queue_decided(self) -> int:
        """How many images in the CURRENT queue are YES or NO.

        MEASURED: three separate progress readouts each computed this
        independently on every keypress — the overall progress bar,
        the walk status line, and the image panel's label. On a
        1,200-image queue that was 3,600 get_decision() calls per
        Yes/No, all producing the same number.

        Cached against a version counter that SessionState bumps
        whenever a decision or the queue itself changes, so repeated
        callers within one update share the work and a genuine change
        still recomputes.
        """
        tag = self._current_tag
        if tag is None:
            return 0
        queue = self.get_current_queue()
        version = (self._decisions.version, tag, len(queue))
        cached = getattr(self, "_decided_cache", None)
        if cached is not None and cached[0] == version:
            return cached[1]
        # YES or NO only. SKIPPED deliberately does not count as
        # decided — all three callers agreed on that, and counting it
        # would silently change what the progress bar means.
        decided = sum(
            1 for img in queue
            if self._decisions.get((img.image_path, tag))
            in (Decision.YES, Decision.NO))
        self._decided_cache = (version, decided)
        return decided

    def get_tag_completion(self, tag: Optional[str] = None) -> tuple[int, int]:
        """Return (decided, total) for a tag, using ALL-mode scope.

        Counts every NON-ORPHAN image as part of the tag's work and
        counts an image as decided when it has a YES or NO for this tag.
        This DELIBERATELY ignores both the queue search filter AND the
        active filter mode (Has tag / Missing tag):

        - Ignoring the search filter: the swirl should reflect progress
          on the whole tag, not whatever a temporary search box shows.
        - Ignoring the filter mode: the sidebar [x] / auto-advance
          (_update_tag_status) and the project stats both define tag
          completion over ALL non-orphan images. If this respected the
          filter, then in Missing-tag mode you could decide every
          filtered image, the swirl would read 100%, yet the sidebar
          would still show the tag pending — the two metrics would
          disagree. Using ALL-mode scope here keeps the swirl, the
          status progress bar, the sidebar, and the stats consistent.

        "Decided" counts YES and NO only; SKIPPED is deferred, not done
        (matching _update_tag_status and get_project_completion).

        Orphan images are excluded entirely — they're outside the
        standard walk and outside the completion universe.

        Returns (0, 0) if no tag is active.
        """
        t = tag if tag is not None else self._current_tag
        if t is None:
            return (0, 0)
        total = 0
        decided = 0
        # A tag whose STATUS is completed — via the strict full walk,
        # via "Mark tag complete", or restored green from a session
        # file — is fully adjudicated by definition: credit every
        # non-orphan pair. For strictly-walked tags this is identical
        # to counting decisions (green only happens when every pair
        # has a YES/NO); for manually-completed tags it's the whole
        # point of the feature. Without this, the per-tag swirl and
        # progress bar showed a marked-complete tag as partial,
        # contradicting the sidebar's green state and the project %
        # (the reported bug).
        fully_credited = (
            self._tag_status.get(t) == TagStatus.COMPLETED
            or t in self._manually_completed
        )
        for img in self._images:
            # ALL-mode scope: every non-orphan image counts, regardless
            # of the current filter or whether the tag is presently on
            # the image (a No that removed it still leaves the pair part
            # of the tag's universe via its recorded decision).
            if not self._has_txt.get(img.image_path, False):
                continue
            total += 1
            if fully_credited:
                decided += 1
                continue
            d = self._decisions.get((img.image_path, t), Decision.UNPROCESSED)
            if d == Decision.YES or d == Decision.NO:
                decided += 1
        return (decided, total)

    def get_project_completion(self) -> tuple[int, int]:
        """Return (decided_jobs, total_jobs) across the WHOLE project,
        using the stable Option-C model.

        DENOMINATOR (total_jobs) = total_tags x non_orphan_images.

        This is the number of yes/no judgements that *would* be made if
        the user walked every tag across every image in ALL filter
        mode. It is deliberately STABLE: it does not depend on which
        tags happen to be present on which images right now, nor on the
        active filter. It changes only when the dataset itself changes
        (a unique tag appears/disappears across the whole set, or an
        image gains/loses its caption file). This is what makes the
        percentage move smoothly and predictably as work is done —
        earlier versions counted "tags currently present", which
        mutated as decisions added/removed tags, so the denominator
        drifted under the user (the reported bug).

        NUMERATOR (decided_jobs) = count of (image, tag) pairs that have
        been adjudicated. A pair counts as decided if EITHER:
        - an explicit YES or NO decision is recorded for it (a No that
          removed the tag still counts — the pair was judged), OR
        - its tag is fully credited: status COMPLETED (strict walk,
          "Mark tag complete", or a green restored from a session
          file) or in the manually-completed set. A green tag in the
          sidebar always reads as fully done here — the two displays
          can never disagree.

        These two contributions are unioned per pair so a manually-
        completed tag that also has explicit decisions isn't
        double-counted. SKIPPED never counts (it's "review later").

        Decisions are clamped to the valid (tag, non-orphan-image)
        space so a stale decision on a since-removed tag or a now-orphan
        image can't push the numerator above the denominator.

        Returns (0, 0) for an empty dataset.
        """
        # Non-orphan images: those with a caption file. These are the
        # only ones in the standard ALL-mode walk.
        non_orphan_paths = [
            img.image_path for img in self._images
            if self._has_txt.get(img.image_path, False)
        ]
        n_images = len(non_orphan_paths)
        # Unique tags known to the project (the tag tree's universe).
        all_tags = set(self._tag_counts.keys())
        n_tags = len(all_tags)

        total = n_tags * n_images
        if total == 0:
            return (0, 0)

        non_orphan_set = set(non_orphan_paths)
        # Fully-credited tags: status COMPLETED (strict walk, manual
        # mark, or restored green from a session file) or in the manual
        # set. Status is the source of truth the sidebar shows, so
        # "every tag is green" must read as 100% here — including
        # greens restored from older session files that predate the
        # manually_completed marker (those carried status only, and
        # previously earned no credit: the reported stuck-below-100%).
        credited = {
            t for t in all_tags
            if self._tag_status.get(t) == TagStatus.COMPLETED
        } | (self._manually_completed & all_tags)

        # Count adjudicated pairs. Start from fully-credited tags: each
        # adjudicates ALL of its (tag, image) pairs across every
        # non-orphan image.
        decided = len(credited) * n_images

        # Add explicit decisions, but only those NOT already covered by
        # a fully-credited tag (avoid double counting) and only within
        # the valid (tag, non-orphan-image) space.
        for (path, tag), d in self._decisions.items():
            if d != Decision.YES and d != Decision.NO:
                continue  # SKIPPED / UNPROCESSED don't count
            if tag in credited:
                continue  # already counted via the credited block
            if tag not in all_tags:
                continue  # stale decision on a removed tag
            if path not in non_orphan_set:
                continue  # decision on an orphan / unknown image
            decided += 1

        # Safety clamp: numerator can never exceed denominator.
        if decided > total:
            decided = total
        return (decided, total)

    def get_tag_frequencies(
        self, subfolder: Optional[str] = None,
    ) -> list[tuple[str, int]]:
        """Return (tag, count) for every tag, sorted by count desc.

        Used by the stats Tag-Frequency tab. If `subfolder` is given,
        counts are restricted to images whose subfolder matches exactly
        (the per-folder count map maintained by the scanner/state);
        otherwise global counts are used.

        Ties broken by tag name ascending for stable ordering.
        """
        if subfolder is None:
            counts = dict(self._tag_counts)
        else:
            counts = dict(self._tag_counts_by_folder.get(subfolder, {}))
        items = [(tg, c) for tg, c in counts.items() if c > 0]
        items.sort(key=lambda kv: (-kv[1], natural_key(kv[0])))
        return items

    def get_subfolders(self) -> list[str]:
        """Return the sorted list of distinct subfolders in the dataset.

        Includes the root (empty-string subfolder) represented as "".
        Used to populate the stats Tag-Frequency subfolder filter.
        """
        subs = set()
        for img in self._images:
            subs.add(img.subfolder)
        return sorted(subs, key=natural_key)

    def get_cooccurrence(
        self,
        tag: str,
        limit: int = 20,
        too_common_pct: int = 100,
        min_count: int = 1,
    ) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
        """Return (top, bottom) co-occurring tags for `tag`.

        Counts, across all images that have `tag`, how often each OTHER
        tag also appears. Returns two lists:
        - top: the `limit` most-co-occurring tags (count desc)
        - bottom: the `limit` least-co-occurring tags (count asc),
          among tags that survive filtering.

        Filtering (so the stats explorer is actually useful rather than
        dominated by ubiquitous tags like 1girl):
        - too_common_pct: drop any co-occurring tag that appears on more
          than this percentage of ALL non-orphan images. 100 disables
          this filter (show everything). This mirrors the main-page
          co-occurrence hint's too-common suppression but is controlled
          independently from the stats UI.
        - min_count: drop tags co-occurring fewer than this many times.

        Ties broken by tag name ascending. If `tag` is unknown or no
        tag survives filtering, both lists are empty.
        """
        if not tag or tag not in self._tag_counts:
            return ([], [])
        related: dict[str, int] = {}
        for img_path, tags in self._image_tags.items():
            if tag not in tags:
                continue
            for other in tags:
                if other == tag:
                    continue
                related[other] = related.get(other, 0) + 1
        if not related:
            return ([], [])

        # Too-common suppression: compute the cutoff against the number
        # of non-orphan images. A tag on more than too_common_pct% of
        # them is considered ubiquitous and dropped (unless pct >= 100).
        if too_common_pct < 100:
            non_orphan_total = sum(
                1 for img in self._images
                if self._has_txt.get(img.image_path, False)
            )
            cutoff = too_common_pct * non_orphan_total / 100.0
            related = {
                t: c for t, c in related.items()
                if self._tag_counts.get(t, 0) <= cutoff
            }
        # Min-count floor.
        if min_count > 1:
            related = {t: c for t, c in related.items() if c >= min_count}
        if not related:
            return ([], [])

        ranked = sorted(related.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        top = ranked[:limit]
        ranked_asc = sorted(related.items(), key=lambda kv: (kv[1], kv[0].lower()))
        bottom = ranked_asc[:limit]
        return (top, bottom)

    @property
    def tree_order(self) -> list[tuple[str, str]]:
        """Canonical iteration order over tags for the tag tree.

        Returns a list of (subfolder, tag) tuples in the order they
        should appear in the sidebar tree. Each tag appears EXACTLY
        ONCE in this list, at its first-occurring subfolder (the
        alphabetically-first subfolder that contains it).

        Used both by:
        - tag_tree UI to lay out tag rows under folder headers
        - auto-advance logic to find the next tag forward in
          display order

        The list is a snapshot copy. Mutations to the returned list
        do not affect state.
        """
        return list(self._tree_order)

    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    def undo_description(self) -> Optional[str]:
        if not self._undo_stack:
            return None
        return self._undo_stack[-1].description

    # ------------------------------------------------------------------
    # Filter & sort setters
    # ------------------------------------------------------------------

    def _queue_active(self) -> bool:
        """True when a queue rebuild is meaningful: a single tag is being
        walked, multi-select mode is driving the queue from a selected
        set, or browse mode is listing a folder. Used by the
        filter/sort/orphan/search setters so a view change re-filters in
        ALL modes (in multi-select and browse, current_tag is None, so a
        bare current_tag check would skip the rebuild)."""
        return (self._current_tag is not None or self._multi_select_mode
                or self._browse_mode)

    def set_filter_mode(self, mode: FilterMode) -> None:
        if mode == self._filter_mode:
            return
        self._filter_mode = mode
        # Queue rebuild is meaningful with a walking tag OR in multi-select.
        if self._queue_active():
            self._rebuild_queue()
            # Do NOT run auto-yes here. Auto-yes fires on an explicit
            # tag pick or a walk advance — actions where the user is
            # actively reviewing. Changing the FILTER is a view change,
            # not a review action; bursting YES decisions from the kept
            # index into the newly-filtered/reordered queue would record
            # decisions the user never asked for (Round-3 #4). Instead we
            # reposition to the first unresolved image so the displayed
            # image is meaningful work rather than whatever happened to
            # land at the stale walk index after reordering (#5).
            self._seek_first_pending()
        # Notification is always emitted: observer widgets (filter bar,
        # status displays) need to know the mode changed even when no
        # queue exists to rebuild. Without this, changing filter mode
        # before selecting a tag would leave widgets out of sync.
        self._emit(StateChange("filter_changed", tag=self._current_tag))

    def set_token_limit(self, limit: int,
                        tokenizer_target: Optional[str] = None) -> None:
        """Set the caption token limit (and optionally the tokenizer) the
        OVER_TOKEN_LIMIT filter and the file-state badge use.

        The token count is tokenizer-specific, so changing the tokenizer
        target invalidates every cached count (they were counted with the
        old tokenizer). Changing only the limit does NOT invalidate counts
        (the counts are unchanged; only the over/under comparison moves).

        Rebuilds the queue only when the OVER_TOKEN_LIMIT filter is active
        (neither setting affects any other filter). No-op if nothing
        changed.
        """
        limit = int(limit)
        tok_changed = (tokenizer_target is not None
                       and tokenizer_target != self._tokenizer_target)
        limit_changed = limit != self._token_limit
        if not tok_changed and not limit_changed:
            return
        if tok_changed:
            self._tokenizer_target = tokenizer_target
            # Counts differ under a different tokenizer — drop the cache so
            # the badge/filter recount with the new one.
            self._token_count_cache.clear()
        self._token_limit = limit
        if (self._filter_mode == FilterMode.OVER_TOKEN_LIMIT
                and self._queue_active()):
            self._rebuild_queue()
            self._seek_first_pending()
            self._emit(StateChange("filter_changed", tag=self._current_tag))

    def tokenizer_target(self) -> str:
        """The tokenizer target currently used for counting."""
        return self._tokenizer_target

    def set_sort_mode(self, mode: SortMode) -> None:
        if mode == self._sort_mode:
            return
        self._sort_mode = mode
        if self._queue_active():
            self._rebuild_queue()
            # See set_filter_mode: reposition, don't auto-yes on a view
            # change. Seeking also prevents silently sitting on a
            # different image at the same stale index after reordering.
            self._seek_first_pending()
        self._emit(StateChange("filter_changed", tag=self._current_tag))

    def set_show_orphans(self, show: bool) -> None:
        if show == self._show_orphans:
            return
        self._show_orphans = show
        if self._queue_active():
            self._rebuild_queue()
            self._seek_first_pending()
        self._emit(StateChange("filter_changed", tag=self._current_tag))

    @property
    def front_locked_tokens(self) -> list[str]:
        """Tokens locked to the front of every caption (copy)."""
        return list(self._front_locked_tokens)

    def set_front_locked_tokens(self, tokens: list[str]) -> None:
        """Replace the locked-token list (fold-deduplicated, first
        spelling wins, order preserved)."""
        seen: set[str] = set()
        cleaned: list[str] = []
        for t in tokens:
            t = t.strip()
            f = _fold_for_match(t)
            if t and f not in seen:
                seen.add(f)
                cleaned.append(t)
        if cleaned == self._front_locked_tokens:
            return
        self._front_locked_tokens = cleaned
        self._emit(StateChange("locks_changed", tag=None))

    def _locked_order(self, tags: list[str]) -> list[str]:
        """Apply the front locks to a forward-write tag list."""
        if not self._front_locked_tokens:
            return tags
        return apply_front_locks(tags, self._front_locked_tokens)

    def apply_trigger_token_front(
        self, token: str, add_missing: bool, lock: bool
    ) -> dict:
        """Send trigger token(s) to the front of every caption.

        `token` is one token or a COMMA-SEPARATED list (field feature:
        multi-trigger datasets). Tokens land at the front in the GIVEN
        order — pair with trainer keep_tokens = N. Per image: tokens
        already present (fold-matched) are reordered to the front;
        with `add_missing`, absent ones are inserted (orphans
        included, creating their .txt). Images carrying NONE of the
        tokens are untouched unless add_missing is set — safe for
        mixed multi-artist datasets. With `lock`, all tokens join
        front_locked_tokens in order. One undo entry reverts every
        file touched, deleting caption files the adds created.

        Returns {"moved", "added", "unchanged", "failed"}.
        """
        raw = [t.strip() for t in token.split(",")]
        seen: set[str] = set()
        toks: list[str] = []
        for t in raw:
            f = _fold_for_match(t)
            if t and f not in seen:
                seen.add(f)
                toks.append(t)
        result = {"moved": 0, "added": 0, "unchanged": 0, "failed": 0}
        if not toks:
            return result
        tfolds = [_fold_for_match(t) for t in toks]
        self._last_write_failures = []
        # (path, before, tokens_added_to_this_image)
        applied: list[tuple[Path, list[str], tuple[str, ...]]] = []

        for img in self.all_images:
            path = img.image_path
            before = self._image_tags.get(path, [])
            present = {_fold_for_match(t) for t in before}
            missing = [t for t, f in zip(toks, tfolds)
                       if f not in present]
            if len(missing) == len(toks) and not add_missing:
                result["unchanged"] += 1
                continue
            if missing and add_missing:
                base = [*missing, *before]
                added_here: tuple[str, ...] = tuple(missing)
            else:
                base = before
                added_here = ()
            new = apply_front_locks(base, toks)
            if not added_here and (new is before or new == before):
                result["unchanged"] += 1
                continue
            if not self._write_tags_with_retry(img.txt_path, new):
                self._last_write_failures.append(img.txt_path.name)
                result["failed"] += 1
                continue
            self._set_image_tags(path, new)
            if added_here:
                self._has_txt[path] = True
                sub = self._tag_counts_by_folder.setdefault(
                    img.subfolder, {})
                for t in added_here:
                    self._tag_counts[t] = \
                        self._tag_counts.get(t, 0) + 1
                    sub[t] = sub.get(t, 0) + 1
                result["added"] += 1
            else:
                result["moved"] += 1
            applied.append((path, list(before), added_here))

        if lock:
            existing = {_fold_for_match(t)
                        for t in self._front_locked_tokens}
            changed = False
            for t in toks:
                if _fold_for_match(t) not in existing:
                    self._front_locked_tokens.append(t)
                    existing.add(_fold_for_match(t))
                    changed = True
            if changed:
                self._emit(StateChange("locks_changed", tag=None))

        if applied:
            def revert() -> None:
                for path, before, added_here in applied:
                    img2 = self._images_by_path.get(path)
                    if img2 is None:
                        continue
                    try:
                        if before:
                            tag_io.write_tags(img2.txt_path, before)
                        else:
                            try:
                                img2.txt_path.unlink()
                            except FileNotFoundError:
                                pass
                    except OSError:
                        continue
                    self._set_image_tags(path, before)
                    if not before:
                        self._has_txt[path] = False
                    if added_here:
                        sub2 = self._tag_counts_by_folder.get(
                            img2.subfolder, {})
                        for t in added_here:
                            n = self._tag_counts.get(t, 0) - 1
                            if n <= 0:
                                self._tag_counts.pop(t, None)
                            else:
                                self._tag_counts[t] = n
                            n2 = sub2.get(t, 0) - 1
                            if n2 <= 0:
                                sub2.pop(t, None)
                            else:
                                sub2[t] = n2
                self._emit(StateChange("tree_rebuilt", tag=None))

            label = ", ".join(toks)
            self._push_undo(UndoEntry(
                f'Trigger token(s) \u2192 front: "{label}" '
                f'({len(applied)} images)', revert))
            self._emit(StateChange("tree_rebuilt", tag=None))
        return result


    def suggest_similar_tags(
        self, query: str, limit: int = 3
    ) -> list[tuple[str, int]]:
        """For a search that matched nothing: return up to `limit`
        dataset tags most similar to `query`, as (raw_tag, image_count),
        best first. Comparison runs on folded forms (_fold_for_match),
        so near-typos (light_smilie), variants (light_smiling) and
        lookalikes are all found; the RAW stored spelling is returned so
        the user sees the real culprit. Empty query or no near matches
        returns []. Never raises."""
        try:
            import difflib

            q = _fold_for_match((query or "").strip().lstrip("-"))
            if not q:
                return []
            folded: dict[str, list[str]] = {}
            for t in self._tag_counts:
                folded.setdefault(_fold_for_match(t), []).append(t)
            near = difflib.get_close_matches(
                q, list(folded.keys()), n=limit * 2, cutoff=0.6
            )
            out: list[tuple[str, int]] = []
            for f in near:
                for raw in folded[f]:
                    out.append((raw, self._tag_counts.get(raw, 0)))
            out.sort(key=lambda p: (-p[1], p[0]))
            return out[:limit]
        except Exception:
            return []

    @property
    def queue_search(self) -> str:
        """Current queue-search substring (lowercased). Empty = no filter."""
        return self._queue_search

    @property
    def queue_search_exact(self) -> bool:
        """Whether the queue search box matches tags exactly (vs substring)."""
        return self._queue_search_exact

    def set_queue_search_exact(self, exact: bool) -> None:
        """Toggle exact-tag matching for the queue search box.

        When True, the query is matched as exact tag(s) — comma-separated
        terms are ANDed, filenames are not matched — so "hand" finds only
        images tagged exactly "hand", not "hand_on_hip". When False
        (default), a single term matches as a substring of filename or
        tag (a comma-separated query is exact-AND regardless, unchanged).

        Rebuilds the queue if a search is currently active; otherwise the
        flag just takes effect on the next search. Emits filter_changed.
        """
        exact = bool(exact)
        if exact == self._queue_search_exact:
            return
        self._queue_search_exact = exact
        if self._queue_active() and self._queue_search:
            self._rebuild_queue()
        self._emit(StateChange("filter_changed", tag=self._current_tag))

    # ------------------------------------------------------------------
    # Multi-select mode (queue driven by a tag SET, not one walking tag)
    # ------------------------------------------------------------------

    @property
    def multi_select_mode(self) -> bool:
        """Whether the tag panel is in multi-select mode."""
        return self._multi_select_mode

    def get_selected_tags(self) -> list[str]:
        """Tags currently checked in multi-select mode (first-seen order)."""
        return list(self._selected_tags)

    def set_multi_select_mode(self, on: bool) -> None:
        """Enter or leave multi-select mode.

        Entering clears the single-tag walk (current_tag -> None) so no
        per-tag walk is active; the queue then reflects the selected set
        (empty selection + ALL shows everything). Leaving clears the
        selection and empties the queue — a normal tag click rebuilds it.
        Emits filter_changed so the queue and panels refresh. Idempotent.
        """
        on = bool(on)
        if on == self._multi_select_mode:
            return
        self._multi_select_mode = on
        # Multi-select and browse mode are mutually exclusive no-tag
        # views; entering multi-select ends any browse listing.
        if on and self._browse_mode:
            self._browse_mode = False
            self._browse_subfolder = None
        # Every entry/exit starts a fresh pass — the running "Undo all"
        # tally only ever covers the current round in multi-select.
        self._multi_pass_entries = []
        self._coalesce_depth = 0
        self._coalesce_pushed = 0
        if on:
            self._current_tag = None
        else:
            self._selected_tags = []
        self._walk_index = 0
        self._rebuild_queue()
        self._emit(StateChange("filter_changed", tag=None))

    def enter_browse_mode(self, subfolder: str) -> None:
        """Browse a folder's images with no active tag (captioning from
        zero). `subfolder` is "" for the root folder or a named
        subfolder. Clears any single-tag walk and leaves multi-select if
        it was on, so browse mode is exclusive. The queue becomes the
        folder's images (gated by the show-orphans toggle). Emits
        filter_changed so the queue and panels refresh, and Yes/No stay
        disabled because current_tag is None. Idempotent for the same
        folder.
        """
        if (self._browse_mode and self._browse_subfolder == subfolder
                and not self._multi_select_mode):
            return
        self._browse_mode = True
        self._browse_subfolder = subfolder
        # Browse mode is a no-tag mode; make sure neither a single-tag
        # walk nor multi-select is also live.
        self._current_tag = None
        self._multi_select_mode = False
        self._selected_tags = []
        # A fresh folder listing starts its own view; drop any stale
        # per-walk search so browse mode doesn't silently inherit it.
        self._queue_search = ""
        self._walk_index = 0
        self._rebuild_queue()
        self._emit(StateChange("filter_changed", tag=None))
        # Log the mode entry the way a tag selection is logged, so the
        # action log records it. Name the folder browsed: "" is the root,
        # a named value is that subfolder. Emitted after the queue is
        # built so the log line reflects a ready state.
        where = "root folder" if subfolder == "" else f"folder '{subfolder}'"
        self._emit(StateChange(
            "action_logged", tag=None,
            extra=f"Browsing {where} (no tag \u2014 caption from zero)"))

    def exit_browse_mode(self) -> None:
        """Leave browse mode, emptying the queue. A tag click (or entering
        another mode) rebuilds it. No-op if not browsing."""
        if not self._browse_mode:
            return
        self._browse_mode = False
        self._browse_subfolder = None
        self._walk_index = 0
        self._rebuild_queue()
        self._emit(StateChange("filter_changed", tag=None))

    @property
    def browse_mode(self) -> bool:
        return self._browse_mode

    @property
    def browse_subfolder(self) -> Optional[str]:
        return self._browse_subfolder

    def set_selected_tags(self, tags: list[str]) -> None:
        """Replace the multi-select tag set and rebuild the queue.

        No-op unless multi-select mode is on. Duplicates removed,
        first-seen order preserved. Emits filter_changed when the set
        actually changes.
        """
        if not self._multi_select_mode:
            return
        seen: set[str] = set()
        cleaned: list[str] = []
        for t in tags:
            if not isinstance(t, str):
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(t)
        if cleaned == self._selected_tags:
            return
        self._selected_tags = cleaned
        self._walk_index = 0
        self._rebuild_queue()
        self._emit(StateChange("filter_changed", tag=None))

    def set_queue_search(self, query: str) -> None:
        """Apply a search filter to the walk queue (filename OR tags —
        see _finalize_queue for the full semantics, including '-tag'
        negation and space/underscore normalization).

        Behavior:
        - Empty string ("" or whitespace-only) clears the filter — walk
          resumes the full queue.
        - Non-empty string filters the queue (case-insensitive). Walk is
          restricted to the filtered subset; pressing Yes/No advances to
          the next visible image, skipping non-matching ones.
        - Auto-yes is SUPPRESSED (not destroyed) while a search is
          active: the user's standing auto-yes preference is preserved
          in _auto_yes, but _run_auto_yes refuses to fire while a
          search is set (see its gate). When the search clears, auto-yes
          resumes automatically — clearing a filter is not a decision
          to abandon auto-yes. This avoids the trap where applying a
          search once silently kills auto-yes for the rest of the
          session.

        Emits filter_changed so observers (queue panel, status widgets)
        refresh.
        """
        q = (query or "").strip().lower()
        if q == self._queue_search:
            return
        self._queue_search = q
        if self._queue_active():
            self._rebuild_queue()
            if not q:
                # Search cleared: reposition the walk to the first
                # image still needing a decision, so the user resumes
                # where work remains rather than landing on an
                # arbitrary already-decided image left over from the
                # filtered walk's end position. If everything is
                # decided, _seek_first_pending leaves the index at 0.
                # _run_auto_yes will now fire if the preference is on
                # (the search gate has lifted).
                self._seek_first_pending()
                self._run_auto_yes()
        self._emit(StateChange("filter_changed", tag=self._current_tag))

    def _seek_first_pending(self) -> None:
        """Move walk_index to the first UNRESOLVED image in the queue.

        "Unresolved" means not yet decided with YES or NO — i.e. either
        UNPROCESSED or SKIPPED. A skipped image is deferred work the
        user wanted to come back to, so resuming should land on it too
        (consistent with jump-to-pending and the YES/NO-only completion
        rule). If everything is decided, clamps to a valid index (0).
        Does not emit — callers emit their own state change.
        """
        tag = self._current_tag
        if tag is None:
            self._walk_index = 0
            return
        for i, img in enumerate(self._current_queue):
            d = self._decisions.get((img.image_path, tag), Decision.UNPROCESSED)
            if d != Decision.YES and d != Decision.NO:
                self._walk_index = i
                return
        # All decided (YES/NO) — clamp.
        self._walk_index = 0

    @property
    def tag_sort_mode(self) -> "TagSortMode":
        return self._tag_sort_mode

    @property
    def auto_yes(self) -> bool:
        return self._auto_yes

    def set_auto_yes(self, enabled: bool) -> None:
        """Set the user's auto-confirm preference.

        Auto-yes confirms images that already have the current tag and
        stops on images missing it. This setter records the standing
        preference; it takes effect on the next walk landing (next
        select_tag or advance), not retroactively on the current image.

        The preference can be set at any time, including while a queue
        search is active. It just won't *run* until the search clears —
        _run_auto_yes gates on search state. This keeps the preference
        and the temporary suppression cleanly separate, so a search
        never silently erases the user's choice.
        """
        self._auto_yes = bool(enabled)

    @property
    def tag_complete_behavior(self) -> str:
        return self._tag_complete_behavior

    def set_tag_complete_behavior(self, behavior: str) -> None:
        """Set what happens when a tag's walk finishes.

        "stop"    -> end the walk and emit walk_ended with
                     extra="tag_complete" so the UI shows a completion
                     message; the user then picks the next tag manually.
        "advance" -> immediately continue to the next pending tag.

        Unknown values fall back to "stop" (the safer default).
        """
        self._tag_complete_behavior = (
            behavior if behavior in ("stop", "advance") else "stop"
        )

    @property
    def cooccur_too_common_pct(self) -> int:
        return self._cooccur_too_common_pct

    def set_cooccur_too_common_pct(self, pct: int) -> None:
        """Set the "too common" percentage cutoff for co-occurrence
        hints. Candidates appearing on more than this percent of all
        images are dropped. 0..100; 0 = filter nothing, 100 = filter all.
        """
        self._cooccur_too_common_pct = max(0, min(100, int(pct)))

    @property
    def cooccur_source(self) -> str:
        return self._cooccur_source

    def set_cooccur_source(self, source: str) -> None:
        """Select the co-occurrence hint engine: "danbooru" (bundled
        official lookup) or "dataset" (the user's own images). Unknown
        values fall back to "danbooru".
        """
        s = str(source).strip()
        self._cooccur_source = s if s in ("danbooru", "dataset") else "danbooru"

    @property
    def show_cooccur_hints(self) -> bool:
        return self._show_cooccur_hints

    def set_show_cooccur_hints(self, show: bool) -> None:
        """Master on/off for the co-occurrence hints panel. When False the
        hints never show, regardless of source or threshold. The caller
        (main window) triggers a file-state refresh after changing it so
        the panel updates live.
        """
        self._show_cooccur_hints = bool(show)

    def get_related_tags_for_tag(
        self,
        tag: str,
        limit: int = 5,
        min_count: int = 2,
    ) -> list[tuple[str, int]]:
        """Suggest tags that frequently appear together with `tag`.

        Tag-based, NOT image-based: the result depends only on `tag`, so
        it stays constant while the user walks different images within
        the same tag. (Earlier version was per-image lift, which made
        the hints jump around per image — confusing for users who
        expected a stable "what tags relate to THIS tag" view.)

        Algorithm:
          1. Find all images that have `tag`.
          2. Count which other tags appear on those images.
          3. Filter out tags appearing on more than
             cooccur_too_common_pct% of all non-orphan images
             (suppresses ubiquitous tags like 1girl, sweat).
          4. Filter out tags with cooccur count below min_count.
          5. Rank by count desc, name asc for ties.

        Returns (tag_name, cooccur_count) tuples. Useful for showing
        the user "while you're tagging long_hair, here are tags that
        often appear with it" — they can mentally check whether their
        current image should have any of those.
        """
        if not tag or tag not in self._tag_counts:
            return []

        # Count tag cooccurrences across images that have `tag`.
        related: dict[str, int] = {}
        for img_path, tags in self._image_tags.items():
            if tag not in tags:
                continue
            for t in tags:
                if t == tag:
                    continue
                related[t] = related.get(t, 0) + 1

        if not related:
            return []

        # Too-common cutoff.
        total_with_captions = sum(
            1 for p in self._image_tags
            if self._has_txt.get(p, False)
        )
        if total_with_captions == 0:
            return []
        too_common_cutoff = (
            self._cooccur_too_common_pct * total_with_captions / 100.0
        )

        # Filter + rank.
        ranked = sorted(
            (
                (t, c) for t, c in related.items()
                if c >= min_count
                and self.get_tag_count_global(t) <= too_common_cutoff
            ),
            key=lambda tc: (-tc[1], tc[0]),
        )
        return ranked[:limit]

    # Kept as a thin compatibility shim — older code paths may still
    # reference this. The image_path argument is ignored; we route to
    # the per-tag helper using the currently walked tag.
    def get_cooccurring_tags(
        self,
        image_path: Path,
        limit: int = 5,
        min_count: int = 3,
    ) -> list[tuple[str, int]]:
        if self._current_tag is None:
            return []
        return self.get_related_tags_for_tag(
            self._current_tag, limit=limit, min_count=min_count,
        )

    def set_tag_sort_mode(self, mode: "TagSortMode") -> None:
        """Reorder tags in the tree (and the auto-advance walk order).

        Rebuilds the tree order and emits tree_rebuilt so the tag tree
        repaints. Does not affect the current walk position — only the
        order future auto-advance will follow.
        """
        if mode == self._tag_sort_mode:
            return
        self._tag_sort_mode = mode
        self._rebuild_tree_order()
        self._emit(StateChange("tree_rebuilt"))

    # ------------------------------------------------------------------
    # Walk control
    # ------------------------------------------------------------------

    def select_tag(self, tag: str, run_auto_yes: bool = True) -> bool:
        """Begin walking `tag`. Resets walk_index to 0.

        Returns False if the tag isn't known to the session. (Caller is
        responsible for not asking for a stale tag.)

        run_auto_yes: when True (the normal interactive case), auto-yes
        fast-forwards past already-tagged images from the queue start.
        Session restore passes False: it must reconstruct the SAVED
        walk position and the SAVED decisions exactly, without auto-yes
        scanning from index 0 and injecting YES decisions on early
        images that weren't in the session file. Letting auto-yes run
        during restore would fabricate decisions and persist them on the
        next save — breaking the "load replaces, never merges" guarantee
        for review state.
        """
        if tag not in self._tag_counts and tag not in self._tag_status:
            return False
        # Selecting a tag leaves browse mode: a tag walk and a no-tag
        # folder listing are mutually exclusive views.
        if self._browse_mode:
            self._browse_mode = False
            self._browse_subfolder = None
        # A queue search is a per-tag-walk view filter. Selecting a new
        # tag (whether the user clicked it, or we auto-advanced here)
        # starts a fresh walk, so the search must reset — otherwise the
        # new tag would silently inherit the previous tag's filter and
        # the queue panel's search box would show stale text.
        #
        # Exception: when the search lock is engaged (#14), the user is
        # deliberately inspecting the same filtered image(s) across
        # different tags, so we preserve the search and try to keep the
        # displayed image. We remember the current image path to restore
        # the walk position after the queue rebuild.
        prev_image_path = None
        if self._search_locked:
            cur = self.current_image
            prev_image_path = cur.image_path if cur is not None else None
        else:
            self._queue_search = ""
        self._current_tag = tag
        self._walk_index = 0
        self._rebuild_queue()
        # Restore the locked image position if that image is still in the
        # rebuilt (filtered) queue for the new tag.
        if self._search_locked and prev_image_path is not None:
            for i, img in enumerate(self._current_queue):
                if img.image_path == prev_image_path:
                    self._walk_index = i
                    break
        self._emit(StateChange("tag_selected", tag=tag))
        # If auto-yes is on, fast-forward past images that already have
        # the tag so the user lands on the first image that needs a
        # decision. Suppressed during session restore, and suppressed
        # while the search lock holds a specific position.
        if run_auto_yes and not self._search_locked:
            self._run_auto_yes()
        return True

    def set_search_locked(self, locked: bool) -> None:
        """Engage/disengage the search lock (#14). When engaged, changing
        the active tag preserves the queue search and the displayed image
        position (if that image is in the new tag's filtered queue)."""
        self._search_locked = bool(locked)

    @property
    def search_locked(self) -> bool:
        return self._search_locked

    def _rebuild_queue(self) -> None:
        """Recompute the current walk queue from filter + sort settings.

        Called on tag selection, filter change, sort change, and orphan
        toggle. The queue is a snapshot — once built, individual image
        rows update in place (their colors change) but the queue length
        and ordering don't change until the next rebuild. This matches
        the original beta and is what the user expects from a "review N
        items in order" workflow.
        """
        if self._multi_select_mode:
            self._rebuild_queue_multi()
            return

        if self._browse_mode:
            self._rebuild_queue_browse()
            return

        tag = self._current_tag
        if tag is None:
            self._current_queue = []
            return

        queue: list[ImageEntry] = []
        for img in self._images:
            has_txt = self._has_txt.get(img.image_path, False)

            if not has_txt:
                # Orphan handling: gated entirely by show_orphans toggle.
                # A "Has Tag" filter trivially excludes orphans (they
                # can't have any tag). A "Missing Tag" filter trivially
                # includes them (every tag is missing). "All" includes
                # them when the toggle is on. "Skipped only" excludes them
                # — an orphan has no .txt and so can't carry a SKIPPED
                # decision for the tag.
                if not self._show_orphans:
                    continue
                if self._filter_mode == FilterMode.HAS_TAG:
                    continue
                if self._filter_mode == FilterMode.SKIPPED_ONLY:
                    continue
                if self._filter_mode == FilterMode.OVER_TOKEN_LIMIT:
                    # An orphan has no caption, so 0 tokens — never over
                    # any limit.
                    continue
                queue.append(img)
                continue

            tags = self._image_tags.get(img.image_path, [])
            if self._filter_mode == FilterMode.HAS_TAG and tag not in tags:
                continue
            if self._filter_mode == FilterMode.MISSING_TAG and tag in tags:
                continue
            if self._filter_mode == FilterMode.OVER_TOKEN_LIMIT:
                # Keep only captions STRICTLY over the limit (a caption AT
                # the limit still trains cleanly). Uses the cached count.
                if self.token_count_for(img.image_path) <= self._token_limit:
                    continue
            if self._filter_mode == FilterMode.SKIPPED_ONLY:
                # Keep only images the user SKIPPED for the current tag.
                # Independent of whether the tag is currently on the image:
                # a skip is recorded against the (image, tag) pair, so it
                # stays valid even if the tag was later added/removed.
                d = self._decisions.get(
                    (img.image_path, tag), Decision.UNPROCESSED
                )
                if d != Decision.SKIPPED:
                    continue
            queue.append(img)

        # Search filter + sort + assign + clamp are shared with the
        # multi-select path; see _finalize_queue.
        self._finalize_queue(queue)

    def _rebuild_queue_browse(self) -> None:
        """Build the browse-mode queue: every image in the chosen folder.

        The "show images without .txt" toggle (self._show_orphans) gates
        uncaptioned images exactly as it does inside a tag walk — ON
        shows captioned and uncaptioned alike, OFF hides the uncaptioned
        ones.

        Tag-based filters (Has Tag / Missing Tag / Skipped only) do NOT
        apply here: there is no active tag for them to key on, so they are
        meaningless in browse mode and are ignored. The one filter that IS
        tag-independent — Over token limit — DOES apply: it asks a
        question about the caption as a whole ("is it too long?"), which
        is exactly the kind of thing you want to catch while browsing.
        When that filter is active, only captions strictly over the limit
        are kept (an uncaptioned image is 0 tokens, so it is never over
        and drops out regardless of the orphan toggle).

        The queue search and sort still apply, via _finalize_queue, so the
        panel's search box and sort control keep working here too.

        _browse_subfolder scopes the listing: "" is the root folder, a
        named value is that subfolder. A subfolder that no longer exists
        (stale after a rescan) yields an empty queue, which is harmless.
        """
        sub = self._browse_subfolder
        if sub is None:
            self._current_queue = []
            return
        over_limit = self._filter_mode == FilterMode.OVER_TOKEN_LIMIT
        images = self._images_by_subfolder.get(sub, [])
        queue: list[ImageEntry] = []
        for img in images:
            has_txt = self._has_txt.get(img.image_path, False)
            if over_limit:
                # Keep only captions strictly over the limit. Uncaptioned
                # images (0 tokens) never qualify, so they drop here even
                # if the orphan toggle is on — a caption at or under the
                # limit trains cleanly and isn't what this filter surfaces.
                if self.token_count_for(img.image_path) <= self._token_limit:
                    continue
                queue.append(img)
                continue
            if not has_txt and not self._show_orphans:
                # Uncaptioned, and the toggle hides them.
                continue
            queue.append(img)
        self._finalize_queue(queue)

    def _finalize_queue(self, queue: list[ImageEntry]) -> None:
        """Apply the queue search filter and sort, then store the result
        and clamp the walk index. Shared by the single-tag rebuild and
        the multi-select rebuild so both honor search + sort identically.

        Search semantics (query already lowercased in set_queue_search):
          - A term prefixed with '~' joins the ANY-OF group (booru-style
            OR): at least one ~term must be present. "~smile,
            ~light_smile" = images with EITHER tag — the union view,
            which neither plain comma lists (AND) nor multi-select's
            set filters (has-ALL / has-NONE) could express. Combines
            freely: "~smile, ~light_smile, -outdoors" = any smile-family
            tag AND not outdoors.
          - A term prefixed with '-' is a NEGATION: images matching it
            are EXCLUDED. This is how you express "images without tag Y"
            for a tag other than the current walk tag — e.g. walking
            medium_hair with the search "-long_hair" (field
            request: the Has/Missing modes key on the CURRENT tag only,
            so before this there was no way to filter by absence of a
            different tag, and combining the modes with a positive
            search produced correct-but-baffling intersections).
          - Spaces match underscores, fullwidth characters match their
            ASCII forms, and zero-width characters are ignored in TAG
            comparisons (see _fold_for_match) — "long hair" finds
            long_hair, and an impostor tag carrying an invisible
            character still matches the text you can actually see.
            Filenames are still matched against the raw text too.
          - Exact toggle on, or a comma-separated list: every positive
            term must be present as an exact tag AND no negated term may
            be (filenames not matched).
          - Otherwise a single term matches a substring of the filename
            OR of any tag (negated: excludes such matches).
        """
        if self._queue_search:
            q = self._queue_search

            fold = _fold_for_match

            if self._queue_search_exact or "," in q:
                raw = [t.strip() for t in q.split(",") if t.strip()]
                pos: list[str] = []
                neg: list[str] = []
                any_of: list[str] = []
                for term in raw:
                    if term.startswith("-"):
                        b = term[1:].strip().lstrip("~").strip()
                        if b:
                            neg.append(fold(b))
                    elif term.startswith("~"):
                        b = term[1:].strip()
                        if b:
                            any_of.append(fold(b))
                    else:
                        pos.append(fold(term))

                def keep(img):
                    tagset = {
                        fold(t)
                        for t in self._image_tags.get(img.image_path, [])
                    }
                    if any(p not in tagset for p in pos):
                        return False
                    if any(n in tagset for n in neg):
                        return False
                    if any_of and not any(o in tagset for o in any_of):
                        return False
                    return True

                queue = [img for img in queue if keep(img)]
            else:
                negated = q.startswith("-")
                body = q[1:].strip() if negated else q
                # A lone "~tag" (no comma) is just a positive term — the
                # OR marker only distinguishes terms within a list.
                body = body.lstrip("~").strip()
                if body:  # a bare "-"/"~" filters nothing
                    fbody = fold(body)

                    def hit(img):
                        name = img.image_path.name.lower()
                        if body in name or fbody in fold(name):
                            return True
                        for t in self._image_tags.get(img.image_path, []):
                            tl = t.lower()
                            if body in tl or fbody in fold(tl):
                                return True
                        return False

                    if negated:
                        queue = [img for img in queue if not hit(img)]
                    else:
                        queue = [img for img in queue if hit(img)]

        # Sort. Sort key computed once per element via the key= argument.
        # natural_key gives human ordering for numbered names
        # (foo_(2) before foo_(10)).
        if self._sort_mode == SortMode.ALPHA_ASC:
            queue.sort(key=lambda e: (natural_key(e.subfolder),
                                      natural_key(e.image_path.name)))
        elif self._sort_mode == SortMode.ALPHA_DESC:
            queue.sort(key=lambda e: (natural_key(e.subfolder),
                                      natural_key(e.image_path.name)),
                       reverse=True)
        elif self._sort_mode == SortMode.TAG_COUNT_DESC:
            queue.sort(key=lambda e: (
                -len(self._image_tags.get(e.image_path, [])),
                natural_key(e.image_path.name),
            ))
        elif self._sort_mode == SortMode.TAG_COUNT_ASC:
            queue.sort(key=lambda e: (
                len(self._image_tags.get(e.image_path, [])),
                natural_key(e.image_path.name),
            ))

        self._current_queue = queue
        # Clamp walk_index to valid range. If queue is empty walk_index
        # stays at 0 (current_image will return None).
        if self._walk_index >= len(queue):
            self._walk_index = max(0, len(queue) - 1) if queue else 0

    def _rebuild_queue_multi(self) -> None:
        """Build the queue in multi-select mode: filter the whole image
        set by the SELECTED tag set, with the filter dropdown applying
        SET logic instead of single-tag logic.

          - HAS_TAG      -> images containing ALL selected tags.
          - MISSING_TAG  -> images containing NONE of the selected tags
                            (the "forgotten image" finder).
          - SKIPPED_ONLY -> images skipped for ANY selected tag.
          - ALL          -> every image (selection ignored).

        With an EMPTY selection the set filters have nothing to test, so
        they yield an empty queue (prompting the user to pick tags);
        ALL still shows everything. Search + sort then apply via the
        shared _finalize_queue.
        """
        queue = [img for img in self._images if self._passes_multi_filter(img)]
        self._finalize_queue(queue)

    def _passes_multi_filter(self, img: ImageEntry) -> bool:
        """Whether one image belongs in the multi-select queue under the
        current selected set + filter mode. Used by both the full rebuild
        and live-shrink so they agree exactly.

          - HAS_TAG      -> image has ALL selected tags.
          - MISSING_TAG  -> image has NONE of the selected tags.
          - SKIPPED_ONLY -> image was skipped for ANY selected tag.
          - ALL          -> always (selection ignored).

        Empty selection: only ALL passes (the set filters are vacuous).
        Orphans (no .txt) can't carry tags or skips, so HAS_TAG and
        SKIPPED_ONLY exclude them; MISSING_TAG includes them (they lack
        every tag); ALL includes them when the orphan toggle is on.
        """
        selected_lower = {t.lower() for t in self._selected_tags}
        # OVER_TOKEN_LIMIT is caption-based, not tag-based: it works with
        # no selected tags. Every OTHER non-ALL filter needs a selection.
        if (not selected_lower
                and self._filter_mode not in (
                    FilterMode.ALL, FilterMode.OVER_TOKEN_LIMIT)):
            return False

        if not self._has_txt.get(img.image_path, False):
            if not self._show_orphans:
                return False
            if self._filter_mode in (
                FilterMode.HAS_TAG, FilterMode.SKIPPED_ONLY,
                FilterMode.OVER_TOKEN_LIMIT,
            ):
                # Orphans have no caption -> 0 tokens -> never over limit.
                return False
            return True

        tagset = {t.lower() for t in self._image_tags.get(img.image_path, [])}
        if self._filter_mode == FilterMode.OVER_TOKEN_LIMIT:
            return self.token_count_for(img.image_path) > self._token_limit
        if self._filter_mode == FilterMode.HAS_TAG:
            return selected_lower.issubset(tagset)
        if self._filter_mode == FilterMode.MISSING_TAG:
            return not (selected_lower & tagset)
        if self._filter_mode == FilterMode.SKIPPED_ONLY:
            return any(
                self._decisions.get(
                    (img.image_path, t), Decision.UNPROCESSED
                ) == Decision.SKIPPED
                for t in self._selected_tags
            )
        return True  # FilterMode.ALL

    def _maybe_shrink_multi(self, path: Path) -> None:
        """Live-shrink (#4): in multi-select mode, after a tag edit the
        edited image may no longer match the set filter (e.g. you gave a
        "forgotten" image its missing tag in the MISSING_TAG finder).
        If so, drop it from the queue immediately and emit filter_changed
        so the list re-renders shorter and the view advances to the image
        that takes its slot.

        Only acts on an image that is currently IN the queue and now
        FAILS the filter; otherwise it leaves the queue alone (so adding
        an unrelated tag, or any edit in ALL mode, changes nothing).
        """
        if not self._multi_select_mode or self._in_shrink:
            return
        idx = -1
        for i, e in enumerate(self._current_queue):
            if e.image_path == path:
                idx = i
                break
        if idx < 0:
            return  # not visible in the queue
        if self._passes_multi_filter(self._current_queue[idx]):
            return  # still matches — keep it

        self._in_shrink = True
        try:
            del self._current_queue[idx]
            # Keep the walk position sensible: if the removed row was
            # before the cursor, shift the cursor back one; then clamp.
            # When the removed row WAS the cursor, the next image slides
            # into its slot (the cursor stays put) — the natural "advance
            # to the next forgotten image" behavior.
            if idx < self._walk_index:
                self._walk_index -= 1
            if self._walk_index >= len(self._current_queue):
                self._walk_index = max(0, len(self._current_queue) - 1)
            self._emit(StateChange("filter_changed", tag=None))
        finally:
            self._in_shrink = False

    def multi_advance(self) -> bool:
        """Multi-select navigation (Skip / Next): move to the next image in
        the filtered queue. Records nothing — there is no single tag to
        decide. Returns True if the cursor moved, False at the end."""
        if not self._multi_select_mode:
            return False
        if self._walk_index < len(self._current_queue) - 1:
            self._walk_index += 1
            self._emit(StateChange("walk_advanced"))
            return True
        return False

    def multi_retreat(self) -> bool:
        """Multi-select navigation (Back / Prev): move to the previous image
        in the filtered queue. Returns True if the cursor moved, False at
        the start."""
        if not self._multi_select_mode:
            return False
        if self._walk_index > 0:
            self._walk_index -= 1
            self._emit(StateChange("walk_advanced"))
            return True
        return False

    def multi_add_selected_to_current(self) -> int:
        """Multi-select 'Yes' / Add: stamp every ticked tag onto the current
        image, then move on. In the MISSING_TAG finder the edited image
        stops matching, so live-shrink drops it from the queue and slides
        the next image under the cursor (the advance happens for free); in
        Has/All mode nothing shrinks, so we advance manually for a
        consistent 'stamp and move to next'. Returns the number of tags
        actually added (0 if they were already present)."""
        if not self._multi_select_mode:
            return 0
        img = self.current_image
        if img is None:
            return 0
        tags = self.get_selected_tags()
        if not tags:
            return 0
        path = img.image_path
        added = 0
        # One Add click = ONE undo step, even when several tags are
        # ticked. Coalesce the per-tag edits so a single Ctrl+Z (or the
        # row's own undo) reverses the whole stamp. Live-shrink may fire
        # mid-loop (Missing mode) but pushes no undo entry, so it doesn't
        # disturb the block.
        self._begin_coalesce()
        try:
            for tag in tags:
                if self.add_tag_to_image(path, tag):
                    added += 1
        finally:
            entry = self._end_coalesce(
                f"Add {added} tag(s) to {path.name}",
                context={"image_paths": [path]},
            )
        # Record this stamp in the current pass so "Undo all" can reverse
        # every addition at once. Nothing added -> nothing to track.
        if entry is not None:
            self._multi_pass_entries.append(entry)
        # Move on, then emit a final event AFTER the pass entry is recorded
        # so the UI's "this pass" tally reflects this stamp. In Missing mode
        # live-shrink already advanced us (and emitted filter_changed mid-
        # edit, BEFORE the append), so we emit one more walk_advanced here
        # to refresh with the now-correct count; in Has/All mode the manual
        # advance below carries that refresh itself.
        cur = self.current_image
        if cur is not None and cur.image_path == path:
            self.multi_advance()
        else:
            self._emit(StateChange("walk_advanced"))
        return added

    def multi_remove_selected_from_current(self) -> int:
        """Multi-select 'No' / Remove on the CURRENT image: pull every
        ticked tag off it, then move on — the mirror of
        multi_add_selected_to_current. In Has mode the edited image stops
        matching the finder, so live-shrink advances for free; otherwise
        we advance manually. In the Missing finder the image already lacks
        the ticked tags, so nothing is removed and this just advances (a
        harmless skip). Like adds, the removal IS folded into the 'Undo all
        this pass' tally, so a bulk remove can be reversed in one click too.
        Returns the number of tags actually removed."""
        if not self._multi_select_mode:
            return 0
        img = self.current_image
        if img is None:
            return 0
        tags = self.get_selected_tags()
        if not tags:
            return 0
        path = img.image_path
        removed = 0
        # One No click = ONE undo step even across several ticked tags.
        self._begin_coalesce()
        try:
            for tag in tags:
                if self.remove_tag_from_image(path, tag):
                    removed += 1
        finally:
            entry = self._end_coalesce(
                f"Remove {removed} tag(s) from {path.name}",
                context={"image_paths": [path]},
            )
        # Record this removal in the current pass too (symmetric with Add),
        # so "Undo all this pass" reverses removals as well as additions.
        if entry is not None:
            self._multi_pass_entries.append(entry)
        cur = self.current_image
        if cur is not None and cur.image_path == path:
            self.multi_advance()
        else:
            self._emit(StateChange("walk_advanced"))
        return removed

    def multi_batch_add_selected(self, image_paths: list[Path]) -> int:
        """Multi-select row-batch 'Add': stamp every ticked tag onto each
        of the given images (the rows the user ctrl/shift-selected in the
        queue) in one disk pass and ONE undo step, without advancing the
        walk. Folded into the 'Undo all this pass' tally. Returns the
        number of images actually changed."""
        return self._multi_batch_apply(image_paths, add=True)

    def multi_batch_remove_selected(self, image_paths: list[Path]) -> int:
        """Multi-select row-batch 'No'/Remove: pull every ticked tag off
        each given image in one disk pass and ONE undo step, without
        advancing the walk. Tracked in the pass tally too, so "Undo all
        this pass" reverses it. Returns the number of images actually
        changed."""
        return self._multi_batch_apply(image_paths, add=False)

    def _multi_batch_apply(self, image_paths: list[Path], add: bool) -> int:
        """Shared core for the multi-select row-batch Add / Remove.

        Adds or removes ALL ticked tags across `image_paths` reusing the
        per-image edit core in quiet mode (so its count/decision/tree
        bookkeeping stays authoritative), coalescing every per-(image,tag)
        edit into ONE undo entry, then emitting a SINGLE consolidated
        refresh + summary. The walk does not advance — the user picked the
        rows deliberately and probably wants to review the result.

        Both add and remove fold the entry into the current pass, so
        "Undo all this pass" covers row-batch edits of either kind. Returns
        the number of images actually changed on disk.
        """
        if not self._multi_select_mode:
            return 0
        tags = self.get_selected_tags()
        if not tags or not image_paths:
            return 0

        # Snapshot which tags exist globally, to know afterwards whether
        # the tag tree needs a rebuild (a tag appeared or vanished).
        def _existing() -> set:
            return {t for t, c in self._tag_counts.items() if c > 0}
        before = _existing()

        changed_paths: list[Path] = []
        self._begin_coalesce()
        try:
            for path in image_paths:
                if self._images_by_path.get(path) is None:
                    continue
                touched = False
                for tag in tags:
                    if add:
                        if self.add_tag_to_image(path, tag, quiet=True):
                            touched = True
                    else:
                        if self.remove_tag_from_image(path, tag, quiet=True):
                            touched = True
                if touched:
                    changed_paths.append(path)
        finally:
            verb = "Add" if add else "Remove"
            entry = self._end_coalesce(
                f"Batch {verb.lower()} {len(tags)} tag(s) on "
                f"{len(changed_paths)} image(s)",
                context={"image_paths": list(changed_paths)},
            )

        if not changed_paths:
            return 0
        if entry is not None:
            self._multi_pass_entries.append(entry)

        # One consolidated set of notifications for the whole batch.
        if before != _existing():
            self._rebuild_tree_order()
            self._emit(StateChange("tree_rebuilt"))
        self._emit(StateChange(
            "action_logged",
            extra=f"Batch {verb.lower()} {len(tags)} tag(s) on "
                  f"{len(changed_paths)} image(s)",
        ))
        # Rows may have entered/left the finder; rebuild once and refresh.
        self._rebuild_queue_multi()
        self._emit(StateChange("filter_changed", tag=None))
        return len(changed_paths)

    def multi_pass_image_count(self) -> int:
        """How many DISTINCT images have been stamped in the current
        multi-select pass (since entering multi-select). Drives the
        'N image(s) changed this pass' label and the 'Undo all' control."""
        paths: set[str] = set()
        for e in self._multi_pass_entries:
            ctx = e.context or {}
            if "image_paths" in ctx:
                for p in ctx["image_paths"]:
                    paths.add(str(p))
            elif "image_path" in ctx:
                paths.add(str(ctx["image_path"]))
        return len(paths)

    def undo_all_multi_pass(self) -> int:
        """Reverse EVERY change (add or remove) made in the current
        multi-select pass in one shot, then refresh the finder. Returns the
        number of distinct images affected (the count BEFORE reversal).
        No-op returning 0 if the pass is empty.

        The pass entries are also removed from the main undo stack (they
        are spent), so a later Ctrl+Z won't try to reverse them again.
        """
        if not self._multi_pass_entries:
            return 0
        count = self.multi_pass_image_count()
        pass_ids = {id(e) for e in self._multi_pass_entries}
        # Reverse newest-first (inverse of application order).
        for e in reversed(self._multi_pass_entries):
            try:
                e.revert_callable()
            except Exception as exc:  # noqa: BLE001 - surface, don't crash
                self._last_undo_error = str(exc) or exc.__class__.__name__
        # Drop the spent entries from the undo stack (match by identity).
        remaining = [e for e in self._undo_stack if id(e) not in pass_ids]
        self._undo_stack = deque(remaining, maxlen=self.UNDO_LIMIT)
        self._multi_pass_entries = []
        # One refresh: the reversed adds may have made images match the
        # finder again (e.g. the Missing finder), so rebuild and re-render.
        if self._multi_select_mode:
            self._rebuild_queue_multi()
            self._emit(StateChange("filter_changed", tag=None))
        return count

    # ------------------------------------------------------------------
    # Decision recording (Yes / No / Skip image)
    # ------------------------------------------------------------------

    def _capture_walk_context(self) -> tuple[Optional[str], list, int]:
        """Snapshot the current walk position for undo.

        Returns (current_tag, queue_snapshot, walk_index). Reverts use
        this so that undoing an action restores not just the walk index
        but the WHOLE walk context — which matters when an action
        completed a tag and auto-advanced to the next one. Without the
        tag + queue, undoing across that boundary would leave the walk
        index pointing into the wrong tag's queue.
        """
        return (self._current_tag, list(self._current_queue), self._walk_index)

    def _restore_walk_context(
        self,
        prev_tag: Optional[str],
        prev_queue: list,
        prev_index: int,
    ) -> None:
        """Restore a walk context captured by _capture_walk_context.

        If the tag changed since the snapshot (i.e. the action that we're
        undoing had crossed a tag boundary via auto-advance), restore the
        full tag context and emit tag_selected so every widget rebuilds
        for the restored tag — the tag tree highlight moves back, the
        queue panel repopulates with the previous tag's queue, and the
        image panel shows the previous image.

        If the tag is unchanged, this is a same-tag undo: just restore
        the index and emit walk_advanced (cheaper, no full rebuild).
        """
        if self._current_tag != prev_tag:
            # Crossing a tag boundary on undo. The queue search filter
            # is a per-tag-walk view; it must not bleed across tags.
            # Clear it so the restored tag shows its full queue and the
            # queue panel's search box (which syncs to this on
            # tag_selected) resets too.
            self._queue_search = ""
            self._current_tag = prev_tag
            self._current_queue = prev_queue
            self._walk_index = prev_index
            self._emit(StateChange("tag_selected", tag=prev_tag))
        else:
            self._walk_index = prev_index
            self._emit(StateChange("walk_advanced"))

    def record_yes(self) -> None:
        """User: current image SHOULD have the current tag."""
        self._record_decision(Decision.YES)

    def record_no(self) -> None:
        """User: current image should NOT have the current tag."""
        self._record_decision(Decision.NO)

    def record_skip_image(self) -> None:
        """Skip the current image for the current tag. No disk write."""
        img = self.current_image
        tag = self._current_tag
        if img is None or tag is None:
            return

        path = img.image_path
        prev_decision = self._decisions.get((path, tag), Decision.UNPROCESSED)
        prev_ctx = self._capture_walk_context()
        prev_tag_status = self._tag_status.get(tag, TagStatus.PENDING)

        # If already skipped, just advance — no state change.
        if prev_decision == Decision.SKIPPED:
            self._advance_walk()
            return

        self._decisions[(path, tag)] = Decision.SKIPPED

        def revert() -> None:
            if prev_decision == Decision.UNPROCESSED:
                self._decisions.pop((path, tag), None)
            else:
                self._decisions[(path, tag)] = prev_decision
            self._tag_status[tag] = prev_tag_status
            # extra="undo" so the log uses the explicit entry below, not
            # a state-inferred (and wrong) Yes/No.
            self._emit(StateChange(
                "image_changed", image_path=path, extra="undo"))
            self._emit(StateChange("tag_changed", tag=tag))
            self._emit(StateChange(
                "action_logged", tag=tag,
                extra=f"Undone: Skip on {path.stem} (tag '{tag}')"))
            # Restore the full walk context. If this skip completed the
            # tag and auto-advanced, this jumps the walk back to the
            # previous tag; otherwise it just moves to the undone image.
            self._restore_walk_context(*prev_ctx)

        self._push_undo(UndoEntry(f"Skip image {path.name}", revert))
        # extra="skip" so the action log does NOT read the resulting tag
        # state and mislabel this as a Yes/No — a skip changes no tags,
        # so state-inference would be meaningless here. The explicit
        # action_logged entry names it correctly.
        self._emit(StateChange(
            "image_changed", image_path=path, extra="skip"))
        self._emit(StateChange(
            "action_logged", tag=tag,
            extra=f"Skipped {path.stem} (tag '{tag}')"))
        self._update_tag_status(tag)
        self._advance_walk()

    def record_batch_decisions(
        self,
        image_paths: list[Path],
        decision: Decision,
        undo_context: Optional[dict] = None,
    ) -> int:
        """Apply Yes or No to multiple images at once for the current tag.

        Used by the queue panel's multi-select batch operations (B6).
        Performance design mirrors auto-yes: one disk pass, one status
        recompute, one undo entry, one event emission. NOT N times each.

        For YES: add the current tag to each image that lacks it.
        For NO:  remove the current tag from each image that has it.
        Images already in the desired state get only a decision recorded
        (no disk write needed).

        Returns the number of images successfully processed (disk write
        succeeded; for already-correct images, that's a no-op success).

        Walk does NOT auto-advance after a batch. The user explicitly
        selected the rows and pressed the button; they probably want to
        review the result before moving on. (Single-image Yes/No still
        advances as always.)

        SKIPPED and orphan images: orphans get the .txt created on YES
        (mirrors single-image YES); on NO the .txt becomes an empty file.

        Disk write failures: any single image's write failure aborts
        that image's contribution to the batch (it stays at its previous
        state). Other images proceed. The returned count reflects only
        successes.
        """
        tag = self._current_tag
        if tag is None or not image_paths or decision not in (Decision.YES, Decision.NO):
            return 0

        # Collect what each image's prev state was, plan its new state.
        plans: list[dict] = []
        for path in image_paths:
            img = self._images_by_path.get(path)
            if img is None:
                continue
            prev_tags = list(self._image_tags.get(path, []))
            prev_decision = self._decisions.get((path, tag), Decision.UNPROCESSED)
            prev_has_txt = self._has_txt.get(path, False)
            if decision == Decision.YES:
                new_tags = list(prev_tags) if tag in prev_tags else prev_tags + [tag]
            else:
                new_tags = [t for t in prev_tags if t != tag]
            needs_disk = (new_tags != prev_tags) or (not prev_has_txt)
            plans.append({
                "img": img,
                "path": path,
                "prev_tags": prev_tags,
                "new_tags": new_tags,
                "prev_decision": prev_decision,
                "prev_has_txt": prev_has_txt,
                "needs_disk": needs_disk,
            })

        if not plans:
            return 0

        # Capture full walk context for undo (same pattern as single Yes/No).
        prev_ctx = self._capture_walk_context()
        prev_tag_status = self._tag_status.get(tag, TagStatus.PENDING)

        succeeded: list[dict] = []
        introduced_overall: set[str] = set()
        disappeared_overall: set[str] = set()

        for p in plans:
            if p["needs_disk"]:
                p["new_tags"] = self._locked_order(p["new_tags"])
                try:
                    tag_io.write_tags(p["img"].txt_path, p["new_tags"])
                except OSError:
                    continue  # skip this image
                self._set_image_tags(p["path"], p["new_tags"])
                self._has_txt[p["path"]] = True
                removed = [t for t in p["prev_tags"] if t not in p["new_tags"]]
                added = [t for t in p["new_tags"] if t not in p["prev_tags"]]
                introduced, disappeared = _adjust_counts(
                    self._tag_counts, self._tag_counts_by_folder,
                    p["img"].subfolder, removed, added,
                )
                for t in introduced:
                    self._tag_status.setdefault(t, TagStatus.PENDING)
                    introduced_overall.add(t)
                for t in disappeared:
                    self._tag_status.pop(t, None)
                    disappeared_overall.add(t)
                p["removed"] = removed
                p["added"] = added
            self._decisions[(p["path"], tag)] = decision
            succeeded.append(p)

        if not succeeded:
            return 0

        # Single status recompute for the affected tag.
        self._update_tag_status(tag)

        # Single combined undo entry reverts the whole batch.
        def revert() -> None:
            for p in succeeded:
                # Restore decision.
                if p["prev_decision"] == Decision.UNPROCESSED:
                    self._decisions.pop((p["path"], tag), None)
                else:
                    self._decisions[(p["path"], tag)] = p["prev_decision"]
                # Restore disk if it was written.
                if p["needs_disk"]:
                    if not p["prev_has_txt"]:
                        if p["img"].txt_path.exists():
                            try:
                                p["img"].txt_path.unlink()
                            except OSError:
                                pass
                        self._has_txt[p["path"]] = False
                        self._image_tags.pop(p["path"], None)
                    else:
                        try:
                            tag_io.write_tags(p["img"].txt_path, p["prev_tags"])
                        except OSError:
                            pass
                        self._set_image_tags(p["path"], p["prev_tags"])
                        self._has_txt[p["path"]] = True
                    # Inverse count diffs.
                    _adjust_counts(
                        self._tag_counts, self._tag_counts_by_folder,
                        p["img"].subfolder,
                        p.get("added", []), p.get("removed", []),
                    )
            self._tag_status[tag] = prev_tag_status
            self._update_tag_status(tag)
            if introduced_overall or disappeared_overall:
                self._rebuild_tree_order()
                self._emit(StateChange("tree_rebuilt"))
            # The same re-filter the forward path needs. Undoing a
            # batch puts those images back into a membership filter's
            # scope, and without this they came back on disk but never
            # returned to the queue.
            if self._filter_mode in (FilterMode.HAS_TAG,
                                     FilterMode.MISSING_TAG,
                                     FilterMode.SKIPPED_ONLY):
                self._rebuild_queue()
                self._emit(StateChange("queue_rebuilt"))
            self._emit(StateChange("tag_changed", tag=tag))
            self._restore_walk_context(*prev_ctx)
            self._emit(StateChange(
                "action_logged", tag=tag,
                extra=f"Undone: batch {action_label_lower} on "
                      f"{len(succeeded)} image(s) for '{tag}'",
            ))

        action_label = "Yes" if decision == Decision.YES else "No"
        action_label_lower = action_label.lower()
        self._push_undo(UndoEntry(
            f"Batch {action_label}: {len(succeeded)} image(s) for '{tag}'",
            revert,
            context=undo_context,
        ))

        # Single event emission.
        if introduced_overall or disappeared_overall:
            self._rebuild_tree_order()
            self._emit(StateChange("tree_rebuilt"))

        # FIELD BUG: the queue itself was left stale. Batch-answering
        # Yes while filtered to "images WITHOUT this tag" makes every
        # one of them stop matching, so they should leave the queue at
        # once — but nothing rebuilt it, and the rows sat there
        # unstyled and unticked. The captions were written correctly,
        # which made it look like the operation had silently failed.
        #
        # A single decision never showed this because the walk moves
        # past the image anyway; a batch changes them all at once.
        if self._filter_mode in (FilterMode.HAS_TAG,
                                 FilterMode.MISSING_TAG,
                                 FilterMode.SKIPPED_ONLY):
            self._rebuild_queue()
            self._emit(StateChange("queue_rebuilt"))
        self._emit(StateChange("tag_changed", tag=tag))
        self._emit(StateChange(
            "action_logged", tag=tag,
            extra=f"Batch {action_label_lower} on {len(succeeded)} image(s) for '{tag}'",
        ))
        # Walk doesn't advance after batch (see docstring). Emit
        # walk_advanced so the queue panel restyles all affected rows
        # in one pass — the position itself didn't change but the row
        # decoration did.
        self._emit(StateChange("walk_advanced"))
        return len(succeeded)

    def record_batch_skip_image(
        self,
        image_paths: list[Path],
        undo_context: Optional[dict] = None,
    ) -> int:
        """Mark multiple images as SKIPPED for the current tag.

        Same batch design as record_batch_decisions, but skip never
        touches disk — it's a pure in-memory decision record. Single
        undo entry reverts the whole batch. Walk position is unchanged
        (same rationale as Yes/No batch: the user explicitly selected
        rows, let them review the result).

        Returns the number of images whose decision was changed (images
        already in SKIPPED state are no-ops and don't count).
        """
        tag = self._current_tag
        if tag is None or not image_paths:
            return 0

        # Plan + capture context.
        affected: list[tuple[Path, Decision]] = []
        for path in image_paths:
            if path not in self._images_by_path:
                continue
            prev_decision = self._decisions.get((path, tag), Decision.UNPROCESSED)
            if prev_decision == Decision.SKIPPED:
                continue  # already skipped, no-op
            affected.append((path, prev_decision))

        if not affected:
            return 0

        prev_ctx = self._capture_walk_context()
        prev_tag_status = self._tag_status.get(tag, TagStatus.PENDING)

        for path, _ in affected:
            self._decisions[(path, tag)] = Decision.SKIPPED

        # Single status recompute.
        self._update_tag_status(tag)

        def revert() -> None:
            for path, prev_decision in affected:
                if prev_decision == Decision.UNPROCESSED:
                    self._decisions.pop((path, tag), None)
                else:
                    self._decisions[(path, tag)] = prev_decision
            self._tag_status[tag] = prev_tag_status
            self._update_tag_status(tag)
            self._emit(StateChange("tag_changed", tag=tag))
            self._restore_walk_context(*prev_ctx)
            self._emit(StateChange(
                "action_logged", tag=tag,
                extra=f"Undone: batch skip on {len(affected)} image(s) for '{tag}'",
            ))

        self._push_undo(UndoEntry(
            f"Batch Skip: {len(affected)} image(s) for '{tag}'",
            revert,
            context=undo_context,
        ))
        self._emit(StateChange("tag_changed", tag=tag))
        self._emit(StateChange(
            "action_logged", tag=tag,
            extra=f"Batch skip on {len(affected)} image(s) for '{tag}'",
        ))
        self._emit(StateChange("walk_advanced"))
        return len(affected)

    def record_skip_tag(self, tag: Optional[str] = None) -> None:
        """Skip a tag entirely. Walk ends; advance to next tag.

        If `tag` is None, skips the currently-walked tag (used by the
        keyboard shortcut and the old button path). If an explicit tag
        is given (used by the tag tree's right-click menu), that tag is
        skipped regardless of which tag is currently being walked. When
        the skipped tag IS the current tag, the walk advances to the
        next pending tag; otherwise the walk is left where it is and
        only the skipped tag's status changes.
        """
        if tag is None:
            tag = self._current_tag
        if tag is None:
            return
        # Only meaningful for a real, known tag.
        if tag not in self._tag_counts and tag not in self._tag_status:
            return

        is_current_tag = (tag == self._current_tag)
        prev_status = self._tag_status.get(tag, TagStatus.PENDING)
        prev_tag = self._current_tag
        prev_index = self._walk_index
        prev_queue = list(self._current_queue)

        self._tag_status[tag] = TagStatus.SKIPPED

        def revert() -> None:
            self._tag_status[tag] = prev_status
            if is_current_tag:
                self._current_tag = prev_tag
                self._current_queue = prev_queue
                self._walk_index = prev_index
                self._emit(StateChange("tag_selected", tag=tag))
            self._emit(StateChange("tag_changed", tag=tag))

        self._push_undo(UndoEntry(f"Skip tag {tag}", revert))
        self._emit(StateChange("tag_changed", tag=tag))
        # Only advance the walk if we skipped the tag we're currently on.
        if is_current_tag:
            self._advance_to_next_tag()

    def mark_tag_complete(self, tag: str) -> bool:
        """Manually mark a tag as completed.

        Sets status to COMPLETED and records the tag in the
        manually-completed set (sticky — _update_tag_status will not
        flip it back to PENDING). Per the Pass D completion model,
        manually-completed tags have ALL their (image, tag) jobs
        counted as decided in project-wide completion, even if some
        images were never individually walked.

        Use case: the user audited the tag via filters and is
        confident it's done without walking every image in ALL mode.

        Does NOT modify the walk position — marking complete is a
        bookkeeping change, not a navigation command. (Earlier this
        advanced the walk, but that surprised users mid-review; the
        shipped behavior leaves the walk where it is.)

        Undoable: revert restores the previous status and removes the
        manual-completion marker. No-op (returns True) if already
        manually complete; returns False for an unknown tag.
        """
        if tag not in self._tag_counts and tag not in self._tag_status:
            return False
        # Avoid wasted work and double-undo entries if already marked.
        if tag in self._manually_completed:
            return True

        prev_status = self._tag_status.get(tag, TagStatus.PENDING)

        self._manually_completed.add(tag)
        self._tag_status[tag] = TagStatus.COMPLETED

        def revert() -> None:
            self._manually_completed.discard(tag)
            self._tag_status[tag] = prev_status
            self._emit(StateChange("tag_changed", tag=tag))

        self._push_undo(UndoEntry(f"Mark tag '{tag}' complete", revert))
        self._emit(StateChange("tag_changed", tag=tag))
        return True

    def _record_decision(self, decision: Decision) -> None:
        """Shared core of record_yes / record_no.

        Branches:
        1. Determines new tag list based on decision.
        2. If disk content changes OR file didn't exist before (orphan
           being initialized), writes to disk and updates memory.
        3. Always records the decision so that subsequent calls to
           get_decision return the correct value.
        4. Builds an undo closure that captures pre-action state for
           full revert.

        A disk write may raise OSError (file locked by another process,
        permission denied, disk full). On such failure, in-memory state
        is unchanged; the exception propagates to the caller, which
        should surface it to the user.
        """
        img = self.current_image
        tag = self._current_tag
        if img is None or tag is None:
            return

        path = img.image_path
        txt_path = img.txt_path
        prev_tags = list(self._image_tags.get(path, []))
        prev_decision = self._decisions.get((path, tag), Decision.UNPROCESSED)
        prev_has_txt = self._has_txt.get(path, False)
        prev_ctx = self._capture_walk_context()
        prev_tag_status = self._tag_status.get(tag, TagStatus.PENDING)

        # Compute new tag list. Order-preserving: appending YES tags to
        # the end matches kohya_ss convention; NO removes in place.
        if decision == Decision.YES:
            new_tags = list(prev_tags) if tag in prev_tags else prev_tags + [tag]
        else:  # NO
            new_tags = [t for t in prev_tags if t != tag]

        # Detect whether the file actually needs to be written. Two cases
        # force a write: tag list changed, OR file did not exist before
        # (orphan being initialized — even an unchanged-content "No"
        # must materialize the empty .txt so the image stops being orphan).
        needs_disk_write = (new_tags != prev_tags) or (not prev_has_txt)

        # Track diffs for count adjustment and undo.
        removed_tags: list[str] = []
        added_tags: list[str] = []
        introduced: list[str] = []
        disappeared: list[str] = []

        if needs_disk_write:
            new_tags = self._locked_order(new_tags)
            # Disk first. If this raises, state is untouched.
            tag_io.write_tags(txt_path, new_tags)

            # Memory mirrors disk.
            self._set_image_tags(path, new_tags)
            self._has_txt[path] = True

            if new_tags != prev_tags:
                removed_tags = [t for t in prev_tags if t not in new_tags]
                added_tags = [t for t in new_tags if t not in prev_tags]
                introduced, disappeared = _adjust_counts(
                    self._tag_counts, self._tag_counts_by_folder,
                    img.subfolder, removed_tags, added_tags,
                )
                for t in introduced:
                    self._tag_status.setdefault(t, TagStatus.PENDING)
                for t in disappeared:
                    self._tag_status.pop(t, None)

        # Decision is always recorded (even if disk didn't change, we
        # record that the user confirmed).
        self._decisions[(path, tag)] = decision

        # ---- Undo closure ----
        # Captures the precise pre-action snapshot needed to fully
        # revert: previous tags, previous decision, previous has_txt,
        # previous walk index, and the inverse tag diffs.
        def revert() -> None:
            if needs_disk_write:
                # Restore disk content.
                if not prev_has_txt:
                    # File didn't exist before; delete it to restore
                    # orphan state. unlink may fail if user deleted it
                    # externally in the meantime — fine, we're back to
                    # the desired state anyway.
                    if txt_path.exists():
                        try:
                            txt_path.unlink()
                        except OSError:
                            pass
                else:
                    try:
                        tag_io.write_tags(txt_path, prev_tags)
                    except OSError:
                        # Best-effort revert. Memory still rolls back.
                        pass
                self._set_image_tags(path, prev_tags)
                self._has_txt[path] = prev_has_txt
                # Apply inverse tag diff. Note arguments swapped:
                # what was removed gets added back, vice versa.
                if removed_tags or added_tags:
                    _adjust_counts(
                        self._tag_counts, self._tag_counts_by_folder,
                        img.subfolder, added_tags, removed_tags,
                    )
                    for t in introduced:
                        # Tags introduced during the action and now reverted
                        # need their status entry cleaned if they have no
                        # remaining count (they shouldn't, since we just
                        # subtracted; but defensive).
                        if t not in self._tag_counts:
                            self._tag_status.pop(t, None)
                    for t in disappeared:
                        # Tags that disappeared during the action are now
                        # back; restore PENDING status.
                        self._tag_status.setdefault(t, TagStatus.PENDING)

            # Restore decision.
            if prev_decision == Decision.UNPROCESSED:
                self._decisions.pop((path, tag), None)
            else:
                self._decisions[(path, tag)] = prev_decision

            # Restore tag status.
            self._tag_status[tag] = prev_tag_status

            # Re-emit notifications so UI repaints.
            if introduced or disappeared:
                self._rebuild_tree_order()
                self._emit(StateChange("tree_rebuilt"))
            # Mark this image_changed as an undo so the action log does
            # NOT mis-infer a fresh Yes/No/Skip from the RESULTING tag
            # state (an undo that restores "tag present" would otherwise
            # log as a green YES). The log skips extra=="undo" and relies
            # on the explicit action_logged entry below instead.
            self._emit(StateChange(
                "image_changed", image_path=path, extra="undo"))
            self._emit(StateChange("tag_changed", tag=tag))
            # A clear, action-naming log entry for the undo — states what
            # was reversed, not what the file now contains.
            verb = {
                Decision.YES: "Yes",
                Decision.NO: "No",
                Decision.SKIPPED: "Skip",
            }.get(decision, "decision")
            self._emit(StateChange(
                "action_logged", tag=tag,
                extra=f"Undone: {verb} on {img.image_path.stem} "
                      f"(tag '{tag}')"))
            # Restore the full walk context. If this decision completed
            # the tag and auto-advanced to the next one, this jumps the
            # walk back to the previous tag (rebuilding its queue and
            # moving the tag-tree highlight); otherwise it just moves
            # back to the undone image within the same tag.
            self._restore_walk_context(*prev_ctx)

        action_label = "Yes" if decision == Decision.YES else "No"
        # The tag travels WITH the entry. Reading the current selection
        # at undo time is wrong: walk 1girl, switch to 1boy, undo —
        # the right decision is reverted, but any UI asking "which tag
        # was that?" would be told 1boy.
        self._push_undo(UndoEntry(
            f"{action_label} on {path.name}", revert,
            context={"tag": tag, "decision": action_label}))

        # Emit notifications.
        self._emit(StateChange("image_changed", image_path=path))
        if introduced or disappeared:
            self._rebuild_tree_order()
            self._emit(StateChange("tree_rebuilt"))
        self._update_tag_status(tag)
        self._advance_walk()

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def undo(self) -> bool:
        """Revert the last action.

        Returns True if an action was reverted cleanly. Returns False
        if there was nothing to undo OR the revert raised.

        On a revert failure (e.g. a granular tag-edit undo whose disk
        rewrite fails because the .txt is locked or read-only), we do
        NOT silently pretend success: the entry is consumed, the error
        is recorded in last_undo_error, and we return False so the
        caller can warn the user that state and disk may have diverged.
        We still avoid crashing the app mid-audit — a recorded,
        surfaced failure is far better than either a hard crash or a
        false "undone" with no indication anything went wrong.
        """
        self._last_undo_error: Optional[str] = getattr(
            self, "_last_undo_error", None
        )
        self._last_undo_error = None
        # Reset the context each undo; set it to the reverted entry's
        # context on success so the UI can act on it (e.g. re-select the
        # group that a reverted group batch op belonged to).
        self._last_undo_context: Optional[dict] = None
        if not self._undo_stack:
            return False
        entry = self._undo_stack.pop()
        # Keep the multi-select pass tally in sync: if the user reverses
        # this entry individually (Ctrl+Z), it's no longer part of any
        # pending "Undo all".
        if self._multi_pass_entries:
            self._multi_pass_entries = [
                e for e in self._multi_pass_entries if e is not entry
            ]
        try:
            entry.revert_callable()
        except Exception as e:  # noqa: BLE001 - we genuinely want any failure
            # Record the failure so the UI can warn. State may now be
            # inconsistent with disk; the user should re-check the
            # affected file. We don't re-raise (no crash) and we don't
            # return True (no false success).
            self._last_undo_error = str(e) or e.__class__.__name__
            return False
        self._last_undo_context = entry.context
        # In multi-select, a reverted tag edit can make an image match the
        # finder again (e.g. undoing an Add in the MISSING finder). The
        # live-shrink that drops images when they STOP matching has no
        # mirror that re-adds them when they START matching, so without
        # this the finder would silently under-count after an undo.
        # Rebuild the multi queue and, if the affected image is back in
        # it, land the cursor there so the user can re-check it.
        if self._multi_select_mode:
            self._rebuild_queue_multi()
            ctx = entry.context or {}
            path = ctx.get("image_path")
            if path is None:
                paths = ctx.get("image_paths")
                if paths:
                    path = paths[0]
            if path is not None:
                for i, e in enumerate(self._current_queue):
                    if e.image_path == path:
                        self._walk_index = i
                        break
            self._emit(StateChange("filter_changed", tag=None))
        return True

    @property
    def last_undo_context(self) -> Optional[dict]:
        """The `context` dict of the most recently reverted undo entry, or
        None if the last undo had no context / failed / there was none.
        Lets the UI recover per-action metadata (e.g. a group key) without
        the state layer knowing what it means."""
        return getattr(self, "_last_undo_context", None)

    @property
    def last_undo_error(self) -> Optional[str]:
        """The error message from the most recent failed undo revert,
        or None if the last undo succeeded (or there was none). Lets the
        UI surface a warning without the state layer needing a dialog.
        """
        return getattr(self, "_last_undo_error", None)

    @property
    def last_write_failures(self) -> list[str]:
        """Caption-file names the most recent global tag mutation
        (rename/delete/split) could not write even after retries. Empty
        if everything wrote. Lets the UI report a partial failure instead
        of silently dropping a tag in a locked file.
        """
        return list(getattr(self, "_last_write_failures", []))

    def _push_undo(self, entry: UndoEntry) -> None:
        # deque(maxlen=UNDO_LIMIT) automatically discards the oldest
        # entry when full. No explicit length check needed.
        self._undo_stack.append(entry)
        # Count pushes made inside an open coalesce block so _end_coalesce
        # knows exactly how many of the rightmost entries to fold together
        # (robust even when the deque discarded older entries meanwhile).
        if self._coalesce_depth > 0:
            self._coalesce_pushed += 1

    def _begin_coalesce(self) -> None:
        """Open a coalesce block. Undo entries pushed until the matching
        _end_coalesce are folded into one combined entry. Nestable — only
        the OUTERMOST begin/end pair coalesces, so a coalescing method can
        freely call other coalescing methods."""
        if self._coalesce_depth == 0:
            self._coalesce_pushed = 0
        self._coalesce_depth += 1

    def _end_coalesce(
        self, description: str, context: Optional[dict] = None
    ) -> Optional[UndoEntry]:
        """Close a coalesce block opened by _begin_coalesce.

        At the outermost level, pop the entries pushed since the block
        opened (counted by _push_undo) and replace them with a SINGLE
        combined UndoEntry whose revert runs the collected reverts
        newest-first (the correct inverse order). Returns that combined
        entry, or None if nothing was pushed inside the block (so the
        caller can skip tracking it).

        A block pushes only a bounded handful of entries (one Add click =
        at most a few ticked tags), well under the deque cap, so the
        rightmost `pushed` entries are exactly the in-block ones even when
        the deque discarded older entries to stay bounded.
        """
        self._coalesce_depth = max(0, self._coalesce_depth - 1)
        if self._coalesce_depth > 0:
            return None
        take = min(self._coalesce_pushed, len(self._undo_stack))
        self._coalesce_pushed = 0
        if take == 0:
            return None
        # Pop newest-first; reverting in this order undoes the most recent
        # sub-action first.
        collected = [self._undo_stack.pop() for _ in range(take)]
        reverts = [e.revert_callable for e in collected]

        def _combined_revert() -> None:
            for r in reverts:
                r()

        combined = UndoEntry(
            description=description,
            revert_callable=_combined_revert,
            context=context,
        )
        self._undo_stack.append(combined)
        return combined

    def clear_undo(self) -> None:
        """Discard undo history. Used after major operations like
        full reset or session load, where old closures would refer
        to stale objects."""
        self._undo_stack.clear()

    # ------------------------------------------------------------------
    # Tag status maintenance
    # ------------------------------------------------------------------

    def _update_tag_status(self, tag: str) -> None:
        """Recompute COMPLETED/PENDING status for a single tag.

        A tag is COMPLETED iff every non-orphan image in the dataset
        has a YES or NO decision recorded for it. A SKIPPED image does
        NOT count toward completion — it's an explicit deferral ("review
        later"), so a tag with skipped-but-not-decided images stays
        PENDING. This matches get_project_completion and get_tag_completion
        (both YES/NO only), keeping the sidebar, swirl, and stats
        consistent. Orphan images (those without a caption file) are
        excluded from the completion check because they're not part of
        the default walk queue — they only appear when the user opts in
        via the "show orphans" filter, and even then they're a separate
        "needs attention" category rather than part of the standard
        review flow. If there are zero non-orphan images, the tag stays
        PENDING rather than vacuously COMPLETED.

        SKIPPED tag-status is *automatically cleared* the moment any
        decision is recorded on this tag. Skip-tag means "review
        later"; once the user starts deciding on this tag they're
        no longer postponing it, so the SKIPPED override is
        meaningless and must give way to the derived PENDING/
        COMPLETED status. Without this auto-clear the user has no
        way to escape the SKIPPED state short of undoing the original
        skip — confusing.

        Manually-completed tags are still left alone (the user
        explicitly marked them done, that's a different intent).

        Cost: O(N_images) per call. At 10k images this is ~0.5 ms,
        invisible to user. If profiling ever shows this as a hotspot,
        replace with a maintained decision-count-per-tag counter that
        also tracks orphan exclusions.
        """
        if tag in self._manually_completed:
            return
        if tag not in self._tag_counts and tag not in self._tag_status:
            return

        # Check if any decision exists for this tag. If so, the user
        # is actively reviewing — clear any SKIPPED override.
        has_any_decision = any(
            self._decisions.get((img.image_path, tag), Decision.UNPROCESSED)
                != Decision.UNPROCESSED
            for img in self._images
            if self._has_txt.get(img.image_path, False)
        )
        if (self._tag_status.get(tag) == TagStatus.SKIPPED
                and has_any_decision):
            # Auto-revert: drop the SKIPPED override; fall through to
            # derive the real status from decisions.
            pass  # don't return early; recompute below clears it
        elif self._tag_status.get(tag) == TagStatus.SKIPPED:
            # Still skipped and no decisions exist → respect the
            # user's choice.
            return

        all_processed = True
        saw_non_orphan = False
        for img in self._images:
            # Orphans are excluded from completion check (see docstring).
            if not self._has_txt.get(img.image_path, False):
                continue
            saw_non_orphan = True
            d = self._decisions.get(
                (img.image_path, tag), Decision.UNPROCESSED
            )
            # A tag is COMPLETED only when every non-orphan image has a
            # YES or NO. A SKIPPED image is explicitly deferred ("review
            # later") and does NOT count as done — counting it would
            # make a fully-skipped tag flip to COMPLETED and end the
            # walk, contradicting both the project-completion math
            # (get_project_completion counts only YES/NO) and the user's
            # intent that skips aren't completion. So treat SKIPPED the
            # same as UNPROCESSED for the completion test.
            if d != Decision.YES and d != Decision.NO:
                all_processed = False
                break
        # Guard against vacuous completion: if there are zero non-orphan
        # images (e.g. a dataset of only orphans, or a lingering tag
        # whose images were all removed), the loop never runs and
        # all_processed stays True — which would flip the tag to
        # COMPLETED with no real work done. A tag with nothing to decide
        # is not "completed"; keep it PENDING.
        if not saw_non_orphan:
            all_processed = False
        new_status = TagStatus.COMPLETED if all_processed else TagStatus.PENDING
        if self._tag_status.get(tag) != new_status:
            self._tag_status[tag] = new_status
            self._emit(StateChange("tag_changed", tag=tag))

    # ------------------------------------------------------------------
    # Walk navigation (auto-advance fix lives here)
    # ------------------------------------------------------------------

    def _advance_walk(self) -> None:
        """Move forward in the current queue, or to the next tag."""
        if self._walk_index < len(self._current_queue) - 1:
            self._walk_index += 1
            # extra="auto_advance" marks this as a DECISION-driven advance
            # (vs a manual jump/nav, which also emit walk_advanced). The
            # group-boundary stop in the queue panel fires only for these,
            # so manual navigation across a boundary is never blocked.
            self._emit(StateChange("walk_advanced", extra="auto_advance"))
            self._run_auto_yes()
        else:
            # End of this tag's queue.
            #
            # Special case: if the queue is a SUBSET of the tag's images
            # — because either a queue search is active OR a filter mode
            # (Has tag / Missing tag) is restricting the queue — then
            # reaching the end of that subset is NOT the same as
            # completing the whole tag. There may be images outside the
            # filter that were never shown and never decided. Advancing
            # to the next tag here would (a) feel like the program
            # declared the tag complete when it isn't, and (b) strand
            # the user's position. So we stop at the last visible image
            # and emit a plain walk_advanced — no walk_ended, no tag
            # advance. The user clears the filter/search to continue
            # with the rest of the tag, or uses Jump-to-pending.
            #
            # We still recompute the tag's status (over the FULL dataset,
            # which _update_tag_status always does) so the sidebar/swirl
            # stay accurate — if the user happens to have decided every
            # image in the whole dataset, the tag legitimately shows
            # complete; we just don't auto-jump away from a subset view.
            queue_is_subset = (
                bool(self._queue_search)
                or self._filter_mode != FilterMode.ALL
            )
            if queue_is_subset:
                if self._current_tag is not None:
                    self._update_tag_status(self._current_tag)
                # Stay on the last visible image; re-emit walk_advanced
                # so the UI settles on it (idempotent).
                self._emit(StateChange("walk_advanced"))
                return
            # Unfiltered (ALL mode, no search): reaching the end means
            # every image of the tag was visited. Mark complete if it is,
            # then move on to the next tag.
            if self._current_tag is not None:
                self._update_tag_status(self._current_tag)
            self._emit(StateChange("walk_ended", tag=self._current_tag))
            self._advance_to_next_tag()

    def _run_auto_yes(self) -> None:
        """Batch-confirm consecutive images that already have the current
        tag, stopping on the first image that lacks it (or queue/tree end).

        Performance design
        ------------------
        Auto-yes only ever fires on images that ALREADY contain the tag,
        so "confirm YES" is a pure in-memory bookkeeping change — the
        caption file already has the tag, so there is NOTHING to write to
        disk. This method therefore:

        - marks all eligible decisions in one pass (zero disk writes),
        - recomputes tag status ONCE at the end (not per image),
        - emits UI events ONCE at the end (not per image),
        - moves the walk index to the stopping point in a single jump
          (so the image panel decodes one image, not the whole run).

        This is the difference between a multi-second UI freeze on large
        datasets and a sub-second in-memory update. The earlier
        implementation called record_decision() per image, which wrote
        the disk, recomputed O(N) status, and refreshed the UI on every
        single step — O(N^2) work plus N pointless disk writes and N
        image decodes. This version is O(N) in memory with no I/O.

        Undo
        ----
        The entire burst is a SINGLE undo entry, so one Back reverts the
        whole run at once. This also avoids exhausting the capped undo
        stack on large bursts.

        Gates
        -----
        - Only runs under the ALL filter (the only mode with a mix of
          has-tag and missing-tag images; see set_filter_mode).
        - Orphan images are never auto-confirmed (they can't already have
          the tag), so the run stops on the first orphan.
        - The re-entry guard is retained for safety, though this batch
          version no longer calls back into _advance_walk.
        """
        if not self._auto_yes or self._in_auto_yes:
            return
        if self._filter_mode != FilterMode.ALL:
            return
        # Auto-yes is suppressed while a queue search is active. The
        # filtered subset is a deliberate narrow view; bursting through
        # it risks confirming images the user wanted to review one by
        # one. The preference itself is preserved (see set_auto_yes) —
        # it resumes automatically when the search clears.
        if self._queue_search:
            return
        tag = self._current_tag
        if tag is None:
            return

        self._in_auto_yes = True
        try:
            queue = self._current_queue
            affected: list[Path] = []
            idx = self._walk_index
            # Scan forward from the current position, collecting the run
            # of already-tagged, undecided, non-orphan images.
            while idx < len(queue):
                path = queue[idx].image_path
                if not self._has_txt.get(path, False):
                    break
                if not self.is_tag_in_image(path, tag):
                    break
                if self._decisions.get((path, tag), Decision.UNPROCESSED) \
                        != Decision.UNPROCESSED:
                    break
                affected.append(path)
                idx += 1

            if not affected:
                return  # nothing to auto-confirm; walk stays put

            # Apply all decisions in memory. No disk writes: every one of
            # these images already contains the tag.
            for path in affected:
                self._decisions[(path, tag)] = Decision.YES

            prev_ctx = self._capture_walk_context()
            prev_tag_status = self._tag_status.get(tag, TagStatus.PENDING)
            self._walk_index = idx  # may equal len(queue) if run hit the end

            # Single combined undo entry reverts the whole burst.
            def revert() -> None:
                for p in affected:
                    self._decisions.pop((p, tag), None)
                self._tag_status[tag] = prev_tag_status
                # One status recompute + minimal UI refresh on undo too.
                self._update_tag_status(tag)
                self._emit(StateChange("tag_changed", tag=tag))
                # Restore the full walk context. If the burst completed
                # the tag and auto-advanced, this jumps back to the tag
                # the burst belonged to; otherwise it restores the index.
                self._restore_walk_context(*prev_ctx)

            self._push_undo(UndoEntry(
                f"Auto-confirm {len(affected)} image(s) for '{tag}'", revert,
            ))

            # One status recompute for the whole burst.
            self._update_tag_status(tag)
            # Record the burst in the action log. Without this, an
            # auto-yes run is invisible in the log unless it happens to
            # complete the tag — the user would see N decisions vanish
            # into the data with no audit trail. We mirror the batch-op
            # style: a single summary line for the whole run.
            self._emit(StateChange(
                "action_logged", tag=tag,
                extra=f"Auto-confirmed {len(affected)} image(s) for '{tag}'",
            ))
            # Tag tree: status / counts may have changed.
            self._emit(StateChange("tag_changed", tag=tag))

            # If the run consumed the rest of the queue, the tag's walk
            # is finished — end it and advance to the next tag, mirroring
            # _advance_walk's end-of-queue branch.
            if self._walk_index >= len(queue):
                self._emit(StateChange("walk_ended", tag=tag))
                self._advance_to_next_tag()
            else:
                # Land on the stopping image: one walk_advanced refreshes
                # the queue styling/selection and decodes the one image.
                self._emit(StateChange("walk_advanced"))
        finally:
            self._in_auto_yes = False

    def _advance_to_next_tag(self) -> None:
        """Find the next PENDING tag in tree order, starting from the
        position AFTER the current one.

        This is the fix for the original beta's bug where completing a
        tag would jump back to the earliest pending tag (it scanned from
        index 0 of master_tag_list). We instead continue forward from
        the current visible tree position.

        If no pending tag exists ahead of the current one, the walk
        simply ends. We do NOT silently wrap to earlier pending tags —
        that would confuse the user (matches the "less nagging behavior"
        principle). The UI can show "Tree walk complete" and the user
        manually selects an earlier tag if they want to continue.
        """
        cur = self._current_tag
        # Find current position in tree order. If somehow not found
        # (e.g. tag was just deleted), treat as -1 so we search from 0.
        found_index = -1
        if cur is not None:
            for i, (_, t) in enumerate(self._tree_order):
                if t == cur:
                    found_index = i
                    break

        # Is there any pending tag ahead of the current one?
        next_pending: Optional[str] = None
        for i in range(found_index + 1, len(self._tree_order)):
            _, tag = self._tree_order[i]
            if self._tag_status.get(tag, TagStatus.PENDING) == TagStatus.PENDING:
                next_pending = tag
                break

        if next_pending is None:
            # No more pending tags ahead. End of tree regardless of the
            # complete-behavior setting.
            self._queue_search = ""
            self._current_tag = None
            self._current_queue = []
            self._walk_index = 0
            self._emit(StateChange("walk_ended", extra="end_of_tree"))
            return

        if self._tag_complete_behavior == "advance":
            # Jump straight to the next pending tag.
            self.select_tag(next_pending)
            return

        # "stop": end the walk here and signal that the tag is complete.
        # The UI shows a completion message; the user picks the next tag
        # manually. We keep current_tag = None so action buttons disable
        # (which physically prevents accidentally tagging the next set),
        # but carry the just-completed tag name in `tag` for the message.
        completed = cur
        self._queue_search = ""
        self._current_tag = None
        self._current_queue = []
        self._walk_index = 0
        self._emit(StateChange(
            "walk_ended", tag=completed, extra="tag_complete",
        ))

    def _recount_tag(self, tag: str) -> None:
        """Recompute global and per-folder counts for one tag from the
        current in-memory image tags.

        Used after operations that change a tag's presence on images in
        ways too complex to adjust incrementally (e.g. splitting one tag
        into several with per-image dedupe). Walks every image once,
        O(N), and rewrites this tag's entries in _tag_counts and
        _tag_counts_by_folder. If the tag ends up on no images, it's
        removed from both maps (and the caller decides about status).
        """
        global_count = 0
        # Clear this tag from all folder buckets first.
        for sub_map in self._tag_counts_by_folder.values():
            sub_map.pop(tag, None)
        for img in self._images:
            tags = self._image_tags.get(img.image_path, [])
            if tag in tags:
                global_count += 1
                sub = self._tag_counts_by_folder.setdefault(img.subfolder, {})
                sub[tag] = sub.get(tag, 0) + 1
        if global_count <= 0:
            self._tag_counts.pop(tag, None)
        else:
            self._tag_counts[tag] = global_count

    def _rebuild_tree_order(self) -> None:
        """Recompute the canonical iteration order of tags in the tree.

        Order:
        - Subfolders sorted alphabetically (empty "" first = root images).
        - Tags within each subfolder ordered per self._tag_sort_mode
          (A->Z, Z->A, most images, or fewest images).
        - Each tag appears exactly ONCE globally, at its first subfolder
          of appearance in the iteration. This realizes the "global tags
          visually grouped" design: the tag is one audit job across the
          whole dataset; the folder grouping is purely visual.

        The chosen sort applies to BOTH the visual tree and the
        auto-advance walk order, since both read self._tree_order.
        """
        def sort_tags(tags: list[str]) -> list[str]:
            mode = self._tag_sort_mode
            if mode == TagSortMode.ALPHA_ASC:
                return sorted(tags, key=natural_key)
            if mode == TagSortMode.ALPHA_DESC:
                return sorted(tags, key=natural_key, reverse=True)
            if mode == TagSortMode.COUNT_DESC:
                # Most images first; ties broken alphabetically (natural)
                # for determinism.
                return sorted(
                    tags,
                    key=lambda t: (-self.get_tag_count_global(t),
                                   natural_key(t)),
                )
            if mode == TagSortMode.COUNT_ASC:
                return sorted(
                    tags,
                    key=lambda t: (self.get_tag_count_global(t),
                                   natural_key(t)),
                )
            return sorted(tags, key=natural_key)

        seen: set[str] = set()
        order: list[tuple[str, str]] = []
        for sf in sorted(self._tag_counts_by_folder.keys(), key=natural_key):
            for tag in sort_tags(list(self._tag_counts_by_folder[sf].keys())):
                if tag not in seen:
                    seen.add(tag)
                    order.append((sf, tag))
        # Any tags that still have status but no counts (e.g. globally
        # deleted with decisions remaining) get appended at the end so
        # they're at least reachable. In practice this should be empty
        # because delete_tag_globally cleans status as well.
        for tag in self._tag_status:
            if tag not in seen:
                seen.add(tag)
                order.append(("", tag))
        self._tree_order = order

    # ------------------------------------------------------------------
    # External edit handling (called by watcher)
    # ------------------------------------------------------------------

    def apply_external_change(
        self,
        image_path: Path,
        new_tags: list[str],
    ) -> None:
        """The .txt file for `image_path` was modified outside the app.

        Reconciles in-memory tags with the new disk content. Updates tag
        counts via the same diff machinery as user actions. Does NOT
        push to undo (external changes are not user actions) and does
        NOT modify decision state (the user's review history is about
        intent, independent of current file contents).

        If the new disk content matches our in-memory state, this is a
        no-op — important because our own writes also trigger watcher
        events, and we don't want to double-count them.
        """
        img = self._images_by_path.get(image_path)
        if img is None:
            return
        prev_tags = self._image_tags.get(image_path, [])
        if prev_tags == new_tags:
            return  # Echo of our own write — no work.

        self._set_image_tags(image_path, list(new_tags))
        self._has_txt[image_path] = True

        removed = [t for t in prev_tags if t not in new_tags]
        added = [t for t in new_tags if t not in prev_tags]
        introduced, disappeared = _adjust_counts(
            self._tag_counts, self._tag_counts_by_folder,
            img.subfolder, removed, added,
        )
        for t in introduced:
            self._tag_status.setdefault(t, TagStatus.PENDING)
        for t in disappeared:
            self._tag_status.pop(t, None)

        if introduced or disappeared:
            self._rebuild_tree_order()
            self._emit(StateChange("tree_rebuilt"))

        # Log the external change DISTINCTLY so a caption edited outside the
        # app (another tool, a script, a find/replace over the folder) can
        # never vanish silently. This is the one class of change the app
        # doesn't originate, so historically it left no trace — which is
        # exactly how a tag could seem to "disappear" with nothing in the
        # log. Naming what changed makes it visible and attributable. The
        # description rides in `extra` (the action_logged convention); the
        # separate image_changed is marked extra="external" so the log
        # skips it rather than double-logging an inferred decision.
        removed_sorted = sorted(removed)
        added_sorted = sorted(added)
        bits = ([f"\u2212{t}" for t in removed_sorted]
                + [f"+{t}" for t in added_sorted])
        summary = ", ".join(bits) if bits else "no tag change"
        self._emit(StateChange(
            "action_logged",
            image_path=image_path,
            extra=f"Reloaded {image_path.stem} from disk: {summary}",
        ))
        self._emit(StateChange("image_changed", image_path=image_path,
                               extra="external"))

    def apply_external_delete(self, image_path: Path) -> None:
        """The .txt file for `image_path` was deleted externally."""
        img = self._images_by_path.get(image_path)
        if img is None:
            return
        prev_tags = self._image_tags.get(image_path, [])
        if prev_tags:
            introduced, disappeared = _adjust_counts(
                self._tag_counts, self._tag_counts_by_folder,
                img.subfolder, prev_tags, [],
            )
            for t in disappeared:
                self._tag_status.pop(t, None)
            if disappeared:
                self._rebuild_tree_order()
                self._emit(StateChange("tree_rebuilt"))
        self._set_image_tags(image_path, [])
        self._has_txt[image_path] = False
        self._emit(StateChange("image_changed", image_path=image_path))

    def apply_external_create(
        self,
        image_path: Path,
        tags: list[str],
    ) -> None:
        """A .txt file appeared for an image that was previously orphan.

        Echo guard: our OWN write of a first tag also *creates* the .txt
        (it didn't exist before), so the watcher fires apply_external_create
        for it — an echo, not a genuine external create. When adding a
        first tag we already set _image_tags and _has_txt before the
        watcher settles, so if the disk content already matches memory this
        is that echo and we must NOT re-run _adjust_counts (which would
        double-count the first tag: 1 from our write, +1 from the echo).
        apply_external_change has the analogous guard; create needs it too.
        On a real create the image was orphan (memory empty []) while the
        new disk tags are non-empty — memory and disk differ; on an echo
        memory already equals disk.
        """
        img = self._images_by_path.get(image_path)
        if img is None:
            return
        prev_tags = self._image_tags.get(image_path, [])
        if prev_tags == list(tags) and self._has_txt.get(image_path, False):
            # Echo of our own first-tag write — memory already reflects it
            # and _has_txt is set. Skip the count adjustment so the first
            # tag isn't counted twice.
            return
        self._has_txt[image_path] = True
        introduced: list[str] = []
        if tags:
            introduced, _ = _adjust_counts(
                self._tag_counts, self._tag_counts_by_folder,
                img.subfolder, [], tags,
            )
            for t in introduced:
                self._tag_status.setdefault(t, TagStatus.PENDING)
        self._set_image_tags(image_path, list(tags))
        if introduced:
            self._rebuild_tree_order()
            self._emit(StateChange("tree_rebuilt"))
        self._emit(StateChange("image_changed", image_path=image_path))

    # ------------------------------------------------------------------
    # Granular per-image tag editing (B4)
    # ------------------------------------------------------------------

    def _apply_image_tag_change(
        self,
        image_path: Path,
        new_tags: list[str],
        action_label: str,
        decision_migrations: Optional[dict[str, str]] = None,
        quiet: bool = False,
    ) -> bool:
        """Shared core of add_tag_to_image / remove_tag_from_image /
        rename_tag_on_image. Writes new_tags to the image's caption
        file, updates all derived state, pushes one undo entry.

        Returns True if the change was applied, False if a no-op (e.g.
        adding an already-present tag) or if the image isn't known.

        `quiet` suppresses the trailing notifications (image_changed /
        action_logged / tree_rebuilt) and therefore the live-shrink they
        drive. A batch caller sets it so N edits don't fan out into N UI
        refreshes + N action-log lines; the caller emits ONE refresh and
        ONE summary afterwards. All disk/state/undo work still happens.

        decision_migrations: optional mapping {old_tag: new_tag} for
        decisions that should follow a tag rename (vs. just being
        cleared when the old tag disappears from the image). Used by
        rename_tag_on_image so a Yes on (img, old) becomes a Yes on
        (img, new). If a decision already exists on (img, new), the
        existing one wins (the user's most-recent intent on the
        target name takes precedence).

        Disk write may raise OSError on failure; caller surfaces it.
        """
        img = self._images_by_path.get(image_path)
        if img is None:
            return False

        prev_tags = list(self._image_tags.get(image_path, []))
        prev_has_txt = self._has_txt.get(image_path, False)
        new_tags = self._locked_order(new_tags)
        if new_tags == prev_tags and prev_has_txt:
            return False  # no-op

        txt_path = img.txt_path
        # Disk first, with a brief retry to ride over a transient lock
        # (antivirus / indexer / cloud-sync momentarily holding the file).
        # If it still fails, raise so the caller can surface it; in-memory
        # state is left untouched either way.
        if not self._write_tags_with_retry(txt_path, new_tags):
            raise OSError(
                f"Could not write {txt_path.name} (locked or read-only) "
                "after several attempts"
            )

        # Memory mirrors disk.
        self._set_image_tags(image_path, new_tags)
        self._has_txt[image_path] = True

        # Adjust counts.
        removed_tags = [t for t in prev_tags if t not in new_tags]
        added_tags = [t for t in new_tags if t not in prev_tags]
        introduced, disappeared = _adjust_counts(
            self._tag_counts, self._tag_counts_by_folder,
            img.subfolder, removed_tags, added_tags,
        )
        for t in introduced:
            self._tag_status.setdefault(t, TagStatus.PENDING)
        for t in disappeared:
            self._tag_status.pop(t, None)

        # Clean up stale decisions on tags removed from this image.
        # If the user previously pressed Yes on (image, tag) and now
        # removes that tag from the image's caption, the Yes decision
        # is no longer meaningful — the image doesn't have the tag
        # anymore. Without this cleanup the queue panel keeps showing
        # a green check on that row for the (no-longer-related) tag.
        #
        # Exception: if decision_migrations names a target for a
        # removed tag (i.e. this was a rename), the decision MOVES to
        # the new tag instead of being cleared. A pre-existing
        # decision on the target wins (it's what the user said about
        # the target name) — we don't clobber it.
        #
        # We remember every decision-state change so undo can fully
        # restore. cleared_decisions stores entries indexed by
        # (target_tag, original_tag, prev_target_decision,
        # prev_source_decision). On undo we reverse this.
        migrations = decision_migrations or {}
        decision_changes: list[tuple[str, str, Decision, Decision]] = []
        # entries: (src_tag, dst_tag_or_empty, prev_src, prev_dst)
        for t in removed_tags:
            key = (image_path, t)
            prev_src = self._decisions.get(key, Decision.UNPROCESSED)
            dst_tag = migrations.get(t, "")
            if dst_tag:
                # Rename: migrate prev_src to (image, dst_tag) unless
                # a decision already lives there.
                prev_dst = self._decisions.get(
                    (image_path, dst_tag), Decision.UNPROCESSED,
                )
                # Record both for undo restoration regardless of
                # whether we actually change them (cheap, complete).
                decision_changes.append((t, dst_tag, prev_src, prev_dst))
                if prev_src != Decision.UNPROCESSED:
                    del self._decisions[key]
                if (prev_dst == Decision.UNPROCESSED
                        and prev_src != Decision.UNPROCESSED):
                    self._decisions[(image_path, dst_tag)] = prev_src
            else:
                # Pure removal: clear the decision (no destination).
                if prev_src != Decision.UNPROCESSED:
                    decision_changes.append((t, "", prev_src, Decision.UNPROCESSED))
                    del self._decisions[key]

        # Status of changed tags may have shifted.
        prev_tag_statuses = {
            t: self._tag_status.get(t, TagStatus.PENDING)
            for t in set(removed_tags) | set(added_tags)
        }
        for t in set(removed_tags) | set(added_tags):
            self._update_tag_status(t)

        def revert() -> None:
            # Restore disk and memory.
            if not prev_has_txt:
                if txt_path.exists():
                    try:
                        txt_path.unlink()
                    except OSError:
                        pass
                self._has_txt[image_path] = False
                # Remove from memory entirely on revert-to-orphan.
                self._image_tags.pop(image_path, None)
            else:
                tag_io.write_tags(txt_path, prev_tags)
                self._set_image_tags(image_path, prev_tags)
                self._has_txt[image_path] = True

            # Restore counts via inverse diff.
            re_added = removed_tags
            re_removed = added_tags
            re_intro, re_disap = _adjust_counts(
                self._tag_counts, self._tag_counts_by_folder,
                img.subfolder, re_removed, re_added,
            )
            for t in re_intro:
                self._tag_status.setdefault(t, TagStatus.PENDING)
            for t in re_disap:
                self._tag_status.pop(t, None)
            # Restore decision states. For each change recorded during
            # apply: put the source's decision back, and (for renames)
            # restore the destination's prior decision too. This
            # cleanly handles both pure-removal (dst="") and renames.
            for src_tag, dst_tag, prev_src, prev_dst in decision_changes:
                if prev_src == Decision.UNPROCESSED:
                    self._decisions.pop((image_path, src_tag), None)
                else:
                    self._decisions[(image_path, src_tag)] = prev_src
                if dst_tag:
                    if prev_dst == Decision.UNPROCESSED:
                        self._decisions.pop((image_path, dst_tag), None)
                    else:
                        self._decisions[(image_path, dst_tag)] = prev_dst
            for t, status in prev_tag_statuses.items():
                if t in self._tag_status:
                    self._tag_status[t] = status

            if re_intro or re_disap or introduced or disappeared:
                self._rebuild_tree_order()
                self._emit(StateChange("tree_rebuilt"))
            # Mark the image_changed as a granular edit so the action
            # log doesn't double-log it as a yes/no decision on the
            # current walking tag (that misleading entry was bug #1 of
            # the post-Pass-B fixes). The action_logged event below
            # carries the precise description.
            self._emit(StateChange(
                "image_changed", image_path=image_path,
                extra="granular_edit",
            ))
            # Action log entry for the undo. action_label was set by the
            # caller and reads "Add 'foo' to file.png" / "Remove 'foo' ...".
            self._emit(StateChange(
                "action_logged", image_path=image_path,
                extra=f"Undone: {action_label}",
            ))

        # Carry the affected image path so undo can re-find it. In
        # multi-select, reverting a tag edit may make this image match the
        # finder again; undo uses this to rebuild the queue and land the
        # cursor back on the restored image.
        self._push_undo(UndoEntry(
            action_label, revert, context={"image_path": image_path},
        ))

        # Re-emit notifications so UI repaints. Same granular_edit
        # marker as the undo path above. Skipped in quiet (batch) mode —
        # the batch caller emits one consolidated refresh instead.
        if not quiet:
            if introduced or disappeared:
                self._rebuild_tree_order()
                self._emit(StateChange("tree_rebuilt"))
            self._emit(StateChange(
                "image_changed", image_path=image_path,
                extra="granular_edit",
            ))
            # Action log entry for the apply. Always carries enough detail
            # to be self-explanatory ("Add 'foo' to file.png").
            self._emit(StateChange(
                "action_logged", image_path=image_path, extra=action_label,
            ))
        return True

    def add_tag_to_image(
        self, image_path: Path, tag: str, quiet: bool = False,
    ) -> bool:
        """Add a single tag to an image's caption file. Returns True if
        added, False if the tag was already present or the image isn't
        known. Atomic disk write, undoable.

        `quiet` defers UI notification to a batch caller (see
        _apply_image_tag_change). New tags are appended at the end
        (kohya_ss convention).
        """
        tag = tag.strip()
        if not tag_io.is_valid_tag(tag):
            # Reject empty and any tag containing a format-breaking
            # character (comma, newline, carriage return) that would
            # corrupt the caption file on the next read.
            return False
        prev_tags = self._image_tags.get(image_path, [])
        if tag in prev_tags:
            return False
        new_tags = list(prev_tags) + [tag]
        return self._apply_image_tag_change(
            image_path, new_tags, f"Add '{tag}' to {image_path.name}",
            quiet=quiet,
        )

    def remove_tag_from_image(
        self, image_path: Path, tag: str, quiet: bool = False,
    ) -> bool:
        """Remove a single tag from an image's caption file. Returns
        True if removed, False if the tag wasn't present. `quiet` defers
        UI notification to a batch caller (see _apply_image_tag_change).
        """
        prev_tags = self._image_tags.get(image_path, [])
        if tag not in prev_tags:
            return False
        new_tags = [t for t in prev_tags if t != tag]
        return self._apply_image_tag_change(
            image_path, new_tags, f"Remove '{tag}' from {image_path.name}",
            quiet=quiet,
        )

    def rename_tag_on_image(
        self, image_path: Path, old_tag: str, new_tag: str,
    ) -> bool:
        """Rename a single tag on a single image's caption file.

        If new_tag is already present on this image, the rename collapses
        (old_tag is removed, no duplicate). Returns True on success.
        """
        new_tag = new_tag.strip()
        if not tag_io.is_valid_tag(new_tag) or new_tag == old_tag:
            return False
        prev_tags = self._image_tags.get(image_path, [])
        if old_tag not in prev_tags:
            return False
        # Build new list preserving order: replace first occurrence of
        # old_tag with new_tag; remove any later duplicate of new_tag.
        seen_new = False
        new_tags: list[str] = []
        for t in prev_tags:
            if t == old_tag:
                if not seen_new:
                    new_tags.append(new_tag)
                    seen_new = True
                # else: collapse, drop the old_tag occurrence
            elif t == new_tag:
                if not seen_new:
                    new_tags.append(new_tag)
                    seen_new = True
                # else: dedupe
            else:
                new_tags.append(t)
        return self._apply_image_tag_change(
            image_path, new_tags,
            f"Rename '{old_tag}' \u2192 '{new_tag}' on {image_path.name}",
            decision_migrations={old_tag: new_tag},
        )

    def split_tag_on_image(
        self, image_path: Path, old_tag: str, new_tags: list[str],
    ) -> bool:
        """Replace `old_tag` with SEVERAL tags on a SINGLE image.

        The per-image counterpart of split_tag_globally — used by the
        file-state panel when the user renames a malformed tag like
        "1girl solo" into "1girl, solo" on just the image they're
        viewing. old_tag is replaced in place by new_tags (deduped
        against tags already on this image); any extra repeats of
        old_tag are dropped.

        Returns True on success, False if new_tags is empty/invalid, the
        parsed result is just old_tag again, or old_tag isn't on the
        image. Atomic disk write, undoable. The old tag's decision is
        NOT migrated to the new tags (splitting changes meaning — the
        merged tag wasn't a real concept), consistent with
        split_tag_globally.
        """
        # Clean and validate target tags.
        cleaned: list[str] = []
        seen: set[str] = set()
        for t in new_tags:
            t = t.strip()
            if not t:
                continue
            if not tag_io.is_valid_tag(t):
                return False
            if t not in seen:
                seen.add(t)
                cleaned.append(t)
        if not cleaned or cleaned == [old_tag]:
            return False
        prev_tags = self._image_tags.get(image_path, [])
        if old_tag not in prev_tags:
            return False

        existing = set(prev_tags)
        new_list: list[str] = []
        inserted = False
        for t in prev_tags:
            if t == old_tag:
                if not inserted:
                    for nt in cleaned:
                        # Insert each new tag, skipping any already on the
                        # image (elsewhere) to avoid duplicates.
                        if (nt not in existing or nt == old_tag) \
                                and nt not in new_list:
                            new_list.append(nt)
                    inserted = True
                # drop further old_tag repeats
            else:
                if t not in new_list:
                    new_list.append(t)

        label = (f"Split '{old_tag}' \u2192 {', '.join(cleaned)} "
                 f"on {image_path.name}")
        # No decision migration: the new tags start fresh. _apply_image_
        # tag_change with no migrations drops old_tag's pair naturally as
        # the tag leaves the image.
        return self._apply_image_tag_change(image_path, new_list, label)

    # ------------------------------------------------------------------
    # Destructive: delete tag globally
    # ------------------------------------------------------------------

    def delete_tag_globally(self, tag: str) -> int:
        """Remove `tag` from every image's caption file.

        Returns the number of files modified. Each file write goes
        through tag_io.write_tags for atomicity. Files that fail to
        write (locked, permission) are skipped — the operation is
        best-effort and the count reflects only successful writes.

        Undo restores every affected file in one undo action.

        This is the only destructive operation the user can trigger
        in normal use. UI must double-confirm before calling it.
        """
        self._last_write_failures = []
        self._consec_write_failures = 0
        affected: list[tuple[Path, list[str], list[str]]] = []
        for img in self._images:
            tags = self._image_tags.get(img.image_path, [])
            if tag in tags:
                new_tags = [t for t in tags if t != tag]
                affected.append((img.image_path, list(tags), new_tags))

        if not affected:
            return 0

        prev_status = self._tag_status.get(tag, TagStatus.PENDING)
        modified = 0
        actually_applied: list[tuple[Path, list[str], list[str]]] = []

        for path, before, after in affected:
            img = self._images_by_path[path]
            after = self._locked_order(after)
            if not self._write_tags_with_retry(img.txt_path, after):
                # Locked/permission error even after retries — record the
                # file and skip it (memory stays at the previous tags).
                self._last_write_failures.append(img.txt_path.name)
                continue
            self._set_image_tags(path, after)
            sub = self._tag_counts_by_folder.setdefault(img.subfolder, {})
            sub_new = sub.get(tag, 0) - 1
            if sub_new <= 0:
                sub.pop(tag, None)
            else:
                sub[tag] = sub_new
            modified += 1
            actually_applied.append((path, before, after))

        if modified == 0:
            return 0

        # Clean up stale decisions on (any_image, deleted_tag). Once
        # the tag is gone from those images, prior Yes/No decisions
        # are meaningless. Remember for undo.
        cleared_decisions: dict[Path, Decision] = {}
        for path, _, _ in actually_applied:
            key = (path, tag)
            prev_decision = self._decisions.get(key, Decision.UNPROCESSED)
            if prev_decision != Decision.UNPROCESSED:
                cleared_decisions[path] = prev_decision
                del self._decisions[key]

        # Global tag count: subtract the number of files we actually
        # cleared. May still be > 0 if some writes failed.
        new_global = self._tag_counts.get(tag, 0) - modified
        if new_global <= 0:
            self._tag_counts.pop(tag, None)
            self._tag_status.pop(tag, None)
            # If this is the currently-walked tag, end the walk.
            if self._current_tag == tag:
                self._current_tag = None
                self._current_queue = []
                self._walk_index = 0
        else:
            self._tag_counts[tag] = new_global

        self._rebuild_tree_order()

        def revert() -> None:
            for path, before, _ in actually_applied:
                img = self._images_by_path[path]
                try:
                    tag_io.write_tags(img.txt_path, before)
                except OSError:
                    pass
                self._set_image_tags(path, before)
                sub = self._tag_counts_by_folder.setdefault(img.subfolder, {})
                sub[tag] = sub.get(tag, 0) + 1
            self._tag_counts[tag] = self._tag_counts.get(tag, 0) + modified
            self._tag_status[tag] = prev_status
            # Restore cleared decisions.
            for path, prev_decision in cleared_decisions.items():
                self._decisions[(path, tag)] = prev_decision
            self._rebuild_tree_order()
            self._emit(StateChange("tree_rebuilt"))
            self._emit(StateChange(
                "action_logged", tag=tag,
                extra=f"Undone: delete tag '{tag}' ({modified} files)",
            ))

        self._push_undo(UndoEntry(
            f"Delete tag '{tag}' ({modified} files)", revert,
        ))
        self._emit(StateChange("tree_rebuilt"))
        self._emit(StateChange(
            "action_logged", tag=tag,
            extra=f"Deleted tag '{tag}' globally ({modified} files)",
        ))
        return modified

    # ------------------------------------------------------------------
    # Duplicate-tag maintenance (dataset-wide cleanup)
    #
    # Background: read_tags deduplicates on load, so in-memory tag lists
    # are always clean and the app never displays a duplicate. But the
    # raw bytes on disk may contain repeats (sloppy auto-tagger merges,
    # manual copy/paste). Those repeats only get cleaned incidentally —
    # when some unrelated edit happens to rewrite that file from the
    # deduped memory view — which is unpredictable and never touches
    # files the user only views. This pair of methods turns that into a
    # deliberate, whole-dataset, reported operation.
    # ------------------------------------------------------------------

    def scan_tag_audit(self, scope_key: str) -> list[dict]:
        """Cross-reference every distinct tag in the dataset against the
        bundled Danbooru tag database (feature F). Read-only.

        scope_key selects which Danbooru categories count as "valid"
        (see core.tag_database.SCOPES). Returns a list of per-tag result
        dicts, one for each distinct dataset tag that is NOT a clean,
        in-scope match (valid tags are omitted — they need no action):

            {
              "tag":      the dataset tag as it appears,
              "verdict":  one of VERDICT_OUT_OF_SCOPE / VERDICT_ALIAS /
                          VERDICT_UNKNOWN,
              "suggestion": canonical fix target (only for VERDICT_ALIAS),
              "category":   Danbooru category id (None for unknown),
              "count":      number of images carrying this tag,
            }

        Sorted by image count descending then tag name, so the most
        impactful tags surface first. Returns an empty list if the
        database can't load (caller checks db.load_error).
        """
        from core import tag_database as tdb
        db = tdb.get_database()
        if not db.ensure_loaded():
            return []
        scope = tdb.SCOPES.get(scope_key, tdb.SCOPES[tdb.SCOPE_DEFAULT])
        valid_categories = scope[1]

        results: list[dict] = []
        for tag, count in self._tag_counts.items():
            verdict, detail, category = db.classify(tag, valid_categories)
            if verdict == tdb.VERDICT_VALID:
                continue  # correct and in scope — nothing to do
            results.append({
                "tag": tag,
                "verdict": verdict,
                "suggestion": detail,
                "category": category,
                "count": count,
            })
        results.sort(key=lambda r: (-r["count"], r["tag"]))
        return results

    def scan_conflicts(self, rules) -> list:
        """Scan every image's tags against the given conflict rules
        (features C + D). Read-only — makes NO changes.

        Detection is alias-aware: rules are written in canonical terms,
        but the image's tags are normalized through the Danbooru alias
        map before comparison, so a rule on "indoors" also catches an
        image tagged with an alias of it. If the database can't load we
        fall back to identity normalization (exact-spelling matching),
        so the feature still works without the CSV — just without alias
        folding.

        Returns a list of core.conflict_rules.Violation, in (image,
        rule) order. The caller (the scan dialog) renders and resolves
        them.
        """
        from core import conflict_rules as cr
        from core import tag_database as tdb

        # Build an alias-aware normalizer if the DB is available.
        db = tdb.get_database()
        if db.ensure_loaded():
            def normalize(tag: str) -> str:
                n = tdb._normalize(tag)
                # Fold aliases to canonical; leave unknowns as their
                # normalized (lowercased, unescaped) form so spelling
                # variants still compare equal where it matters.
                target = db.alias_target(n)
                return target if target else n
        else:
            normalize = None  # detect_violations falls back to identity

        images = [e.image_path for e in self._images]
        return cr.detect_violations(
            images,
            lambda p: self._image_tags.get(p, []),
            rules,
            normalize=normalize,
        )

    def scan_duplicate_tags(self) -> tuple[int, int]:
        """Scan every caption file on disk for duplicate tags.

        Read-only: makes NO changes. Returns (files_with_duplicates,
        total_duplicate_occurrences) so the UI can show the user the
        scope before they confirm a cleanup. A "duplicate occurrence"
        is any repeat of a tag beyond its first appearance in a file
        (so a tag appearing 3 times contributes 2).

        Reads raw (non-deduplicated) on-disk content via
        tag_io.read_tags_raw, since the in-memory view is already
        clean. Files that can't be read (deleted/locked since scan)
        are skipped silently — this is a diagnostic pass, not an edit.

        Orphan images (no caption file) are skipped.
        """
        files_with_dupes = 0
        total_dupes = 0
        for img in self._images:
            if not self._has_txt.get(img.image_path, False):
                continue
            try:
                raw = tag_io.read_tags_raw(img.txt_path)
            except OSError:
                continue
            n_dupes = len(raw) - len(dict.fromkeys(raw))
            if n_dupes > 0:
                files_with_dupes += 1
                total_dupes += n_dupes
        return (files_with_dupes, total_dupes)

    def clean_duplicate_tags(self) -> tuple[int, int]:
        """Collapse duplicate tags to first-occurrence across the dataset.

        For every caption file whose raw on-disk content contains a
        repeated tag, rewrite it with duplicates removed (first
        occurrence kept, order preserved). Files without duplicates are
        left completely untouched — not even rewritten — so this never
        changes the modification time of a clean file.

        Returns (files_modified, total_duplicates_removed).

        Properties:
        - Atomic per file, with a brief retry on a transient lock. A file
          that still can't be written is recorded in last_write_failures
          (so the UI can name it) and left as-is; the count reflects only
          successful rewrites, and the user can re-run later.
        - Does NOT change which tags exist, any tag's count, the tree,
          decisions, or the walk — collapsing a duplicate removes only
          redundant repeats, and the deduped result equals what memory
          already held. So tag counts and statuses are unaffected by
          definition, and no recompute is needed.
        - NOT undoable (see note at the write site). The UI must warn
          the user before they confirm.
        - Logged to the action log.

        Because the in-memory model is already deduplicated, this method
        reconciles ONLY the disk representation with what the app has
        always believed the data to be. It is purely a disk-tidying
        operation.
        """
        # Plan: collect (txt_path, raw_before, deduped_after) for files
        # that actually have duplicates. raw_before is captured for undo.
        self._last_write_failures = []
        self._consec_write_failures = 0
        planned: list[tuple[Path, list[str], list[str]]] = []
        for img in self._images:
            if not self._has_txt.get(img.image_path, False):
                continue
            try:
                raw = tag_io.read_tags_raw(img.txt_path)
            except OSError:
                continue
            deduped = list(dict.fromkeys(raw))
            if len(deduped) != len(raw):
                planned.append((img.txt_path, raw, deduped))

        if not planned:
            return (0, 0)

        files_modified = 0
        dupes_removed = 0
        for txt_path, raw_before, deduped_after in planned:
            deduped_after = self._locked_order(deduped_after)
            if not self._write_tags_with_retry(txt_path, deduped_after):
                # Locked/permission error even after retries — record the
                # file and leave it as-is (the user can re-run later).
                self._last_write_failures.append(txt_path.name)
                continue
            files_modified += 1
            dupes_removed += (len(raw_before) - len(deduped_after))

        if files_modified == 0:
            return (0, 0)

        # NOT undoable. Undo would mean writing the duplicate bytes back,
        # but write_tags deduplicates on write (the app's model can't
        # represent a duplicate), so a revert through it would be a
        # silent no-op — a lying "undo". Rather than pretend, we make
        # this operation explicitly non-reversible: the UI warns the
        # user before they confirm. Removing accidental duplicate cruft
        # is not something a user meaningfully wants to "put back", and
        # the deduped result is exactly what the app already believed
        # the data to be, so nothing the user can see actually changed.
        self._emit(StateChange(
            "action_logged",
            extra=(
                f"Cleaned duplicate tags: {dupes_removed} removed "
                f"across {files_modified} file(s)"
            ),
        ))
        return (files_modified, dupes_removed)

    # ------------------------------------------------------------------
    # Rename tag globally (B5) — with auto-merge if target exists
    # ------------------------------------------------------------------

    def rename_tag_globally(
        self, old_tag: str, new_tag: str, quiet: bool = False,
    ) -> int:
        """Rename `old_tag` to `new_tag` across every image's caption file.

        Returns the number of files modified. If `new_tag` already exists
        in the dataset, this becomes a MERGE — every image with old_tag
        gets old_tag replaced by new_tag, with any pre-existing new_tag
        deduplicated. Either way, the result is that old_tag no longer
        appears anywhere and new_tag's count grows.

        `quiet` suppresses the trailing tree rebuild + notifications so a
        batch caller (apply_tag_rename_map) can run many renames and emit
        ONE consolidated refresh. All disk/state/decision/undo work still
        happens; the per-rename undo entry is still pushed (the batch
        coalesces them into one).

        Returns 0 (no-op) if:
          - new_tag is empty or contains a format-breaking character
            (comma, newline, carriage return — would corrupt the file)
          - old_tag == new_tag
          - old_tag doesn't exist in the dataset

        Disk writes that fail (locked, permission) are skipped; the
        count reflects only successful writes.

        Undo restores every affected file in one undo action.
        """
        self._last_write_failures = []
        self._consec_write_failures = 0
        new_tag = new_tag.strip()
        if not tag_io.is_valid_tag(new_tag) or new_tag == old_tag:
            return 0
        if old_tag not in self._tag_counts:
            return 0

        # Plan the change per image. For each image containing old_tag,
        # compute new tag list with old_tag → new_tag, deduping if
        # new_tag was already present.
        affected: list[tuple[Path, list[str], list[str]]] = []
        for img in self._images:
            tags = self._image_tags.get(img.image_path, [])
            if old_tag not in tags:
                continue
            seen_new = False
            new_tags: list[str] = []
            for t in tags:
                if t == old_tag:
                    if not seen_new:
                        new_tags.append(new_tag)
                        seen_new = True
                    # else: drop the old_tag occurrence (collapse)
                elif t == new_tag:
                    if not seen_new:
                        new_tags.append(new_tag)
                        seen_new = True
                    # else: dedupe
                else:
                    new_tags.append(t)
            affected.append((img.image_path, list(tags), new_tags))

        if not affected:
            return 0

        prev_old_status = self._tag_status.get(old_tag, TagStatus.PENDING)
        prev_new_status = self._tag_status.get(new_tag, None)
        modified = 0
        actually_applied: list[tuple[Path, list[str], list[str]]] = []

        for path, before, after in affected:
            img = self._images_by_path[path]
            if not self._write_tags_with_retry(img.txt_path, after):
                # Locked/permission error even after retries — record the
                # file and skip it (memory stays at the previous tags).
                self._last_write_failures.append(img.txt_path.name)
                continue
            self._set_image_tags(path, after)

            # Adjust per-folder counts: old_tag drops by 1; new_tag
            # gains 1 IF it wasn't already on this image.
            sub = self._tag_counts_by_folder.setdefault(img.subfolder, {})
            sub_old = sub.get(old_tag, 0) - 1
            if sub_old <= 0:
                sub.pop(old_tag, None)
            else:
                sub[old_tag] = sub_old
            if new_tag not in before:
                sub[new_tag] = sub.get(new_tag, 0) + 1

            modified += 1
            actually_applied.append((path, before, after))

        if modified == 0:
            return 0

        # Migrate decisions from old_tag to new_tag. Rename means "this
        # is the same concept under a new name" — the user's prior
        # Yes/No decisions should follow. If a decision already exists
        # on (image, new_tag), the existing decision wins (it's the
        # one the user made under the target name). Remember everything
        # we change for undo.
        decision_changes: list[tuple[Path, Decision, Decision]] = []
        # tuple: (image_path, prev_old_decision, prev_new_decision)
        for path, _, _ in actually_applied:
            prev_old = self._decisions.get((path, old_tag), Decision.UNPROCESSED)
            prev_new = self._decisions.get((path, new_tag), Decision.UNPROCESSED)
            decision_changes.append((path, prev_old, prev_new))
            # Clear the old key.
            if prev_old != Decision.UNPROCESSED:
                del self._decisions[(path, old_tag)]
            # Set the new key, but don't clobber an existing one.
            if prev_new == Decision.UNPROCESSED and prev_old != Decision.UNPROCESSED:
                self._decisions[(path, new_tag)] = prev_old

        # Adjust global counts. old_tag loses `modified`; new_tag
        # gains the number of images where it wasn't already present.
        new_tag_gains = sum(
            1 for _, before, _ in actually_applied if new_tag not in before
        )
        new_global_old = self._tag_counts.get(old_tag, 0) - modified
        if new_global_old <= 0:
            self._tag_counts.pop(old_tag, None)
            self._tag_status.pop(old_tag, None)
            # If this is the currently-walked tag, the walk must end.
            if self._current_tag == old_tag:
                self._current_tag = None
                self._current_queue = []
                self._walk_index = 0
        else:
            self._tag_counts[old_tag] = new_global_old
        if new_tag_gains > 0:
            self._tag_counts[new_tag] = (
                self._tag_counts.get(new_tag, 0) + new_tag_gains
            )
            # Initialize tag status if newly introduced.
            self._tag_status.setdefault(new_tag, TagStatus.PENDING)

        if not quiet:
            self._rebuild_tree_order()

        def revert() -> None:
            for path, before, _ in actually_applied:
                img = self._images_by_path[path]
                try:
                    tag_io.write_tags(img.txt_path, before)
                except OSError:
                    pass
                self._set_image_tags(path, before)
                # Restore counts: add 1 to old_tag's folder count,
                # subtract 1 from new_tag's IF new_tag wasn't in before.
                sub = self._tag_counts_by_folder.setdefault(img.subfolder, {})
                sub[old_tag] = sub.get(old_tag, 0) + 1
                if new_tag not in before:
                    sub_new = sub.get(new_tag, 0) - 1
                    if sub_new <= 0:
                        sub.pop(new_tag, None)
                    else:
                        sub[new_tag] = sub_new
            # Restore globals.
            self._tag_counts[old_tag] = (
                self._tag_counts.get(old_tag, 0) + modified
            )
            if new_tag_gains > 0:
                new_global_now = self._tag_counts.get(new_tag, 0) - new_tag_gains
                if new_global_now <= 0:
                    self._tag_counts.pop(new_tag, None)
                else:
                    self._tag_counts[new_tag] = new_global_now
            # Restore statuses.
            self._tag_status[old_tag] = prev_old_status
            if prev_new_status is None:
                self._tag_status.pop(new_tag, None)
            else:
                self._tag_status[new_tag] = prev_new_status
            # Restore decisions. For each affected image, restore
            # whatever was on (image, old_tag) and (image, new_tag)
            # before the rename. The current state had old's decision
            # migrated to new (or new's own decision preserved); undo
            # puts each key back.
            for path, prev_old, prev_new in decision_changes:
                if prev_old == Decision.UNPROCESSED:
                    self._decisions.pop((path, old_tag), None)
                else:
                    self._decisions[(path, old_tag)] = prev_old
                if prev_new == Decision.UNPROCESSED:
                    self._decisions.pop((path, new_tag), None)
                else:
                    self._decisions[(path, new_tag)] = prev_new
            self._rebuild_tree_order()
            self._emit(StateChange("tree_rebuilt"))
            self._emit(StateChange(
                "action_logged", tag=old_tag,
                extra=f"Undone: rename '{old_tag}' \u2192 '{new_tag}' ({modified} files)",
            ))

        self._push_undo(UndoEntry(
            f"Rename '{old_tag}' \u2192 '{new_tag}' ({modified} files)",
            revert,
        ))
        if not quiet:
            self._emit(StateChange("tree_rebuilt"))
            self._emit(StateChange(
                "action_logged", tag=new_tag,
                extra=f"Renamed '{old_tag}' \u2192 '{new_tag}' ({modified} files)",
            ))
        return modified

    # ------------------------------------------------------------------
    # Bulk tag reformatting (underscore <-> space, find/replace)
    # ------------------------------------------------------------------

    def build_reformat_map(self, to_spaces: bool) -> dict:
        """Build the rename map for an underscore<->space reformat over
        EVERY tag currently in the dataset.

        to_spaces=True  -> each tag's underscores become spaces
                           ("black_elbow_gloves" -> "black elbow gloves").
        to_spaces=False -> each tag's spaces become underscores
                           ("black elbow gloves" -> "black_elbow_gloves").

        The transform is per-tag, so it never touches the comma+space
        separators between tags — only the characters inside each tag.
        Tags already in the target form (and tags that would transform to
        themselves) are omitted. Returns {old_tag: new_tag}.
        """
        out: dict = {}
        for tag in list(self._tag_counts.keys()):
            if to_spaces:
                new = tag.replace("_", " ")
            else:
                new = tag.replace(" ", "_")
            new = new.strip()
            if new and new != tag:
                out[tag] = new
        return out

    def _clean_rename_pairs(self, rename_map: dict) -> list:
        """Filter a rename map to the (old, new) pairs that are actually
        applicable: new is a valid tag, new differs from old, and old
        exists in the dataset. Shared by preview + apply so they agree."""
        pairs = []
        for old, new in rename_map.items():
            new2 = (new or "").strip()
            if not tag_io.is_valid_tag(new2):
                continue
            if new2 == old:
                continue
            if old not in self._tag_counts:
                continue
            pairs.append((old, new2))
        return pairs

    def preview_tag_rename_map(self, rename_map: dict, max_examples: int = 12) -> dict:
        """Dry-run summary of applying `rename_map`, for a confirmation UI.

        Returns {"tags": int, "images": int, "examples": [(old, new), ...]}
        where `tags` is how many tags would change, `images` is how many
        DISTINCT caption files would be rewritten, and `examples` is a
        small sample of the (old -> new) pairs. Touches no disk or state.
        """
        pairs = self._clean_rename_pairs(rename_map)
        sources = {old for old, _ in pairs}
        affected: set = set()
        if sources:
            for img in self._images:
                tags = self._image_tags.get(img.image_path, [])
                if any(t in sources for t in tags):
                    affected.add(img.image_path)
        return {
            "tags": len(pairs),
            "images": len(affected),
            "examples": pairs[:max_examples],
        }

    # After this many consecutive persistent write failures, stop
    # sleeping between retry attempts — the cause is clearly not a
    # momentary lock (see _consec_write_failures in __init__).
    _WRITE_RETRY_GIVEUP_STREAK = 3

    def _write_tags_with_retry(
        self, txt_path: "Path", tags: list[str], attempts: int = 3,
        delay: float = 0.12,
    ) -> bool:
        """Write a caption file, retrying briefly on transient OS errors.

        On Windows an antivirus / search-indexer / cloud-sync client can
        hold a just-touched file open for a moment, making the write fail
        with a 'file in use' error. A short retry rides over that. Returns
        True if written, False if it still failed after all attempts.

        Adaptive: after _WRITE_RETRY_GIVEUP_STREAK consecutive files fail
        persistently, further failures skip the retry sleeps (single
        attempt). A momentary lock never affects many files in a row, so
        a long streak means something systemic (read-only files, dead
        drive) where sleeping 0.24s per file would freeze the UI for
        minutes on a big batch — while changing nothing. Every file is
        still attempted once and still reported by name; any success
        resets the streak.
        """
        import time
        if self._consec_write_failures >= self._WRITE_RETRY_GIVEUP_STREAK:
            attempts = 1
        for i in range(attempts):
            try:
                tag_io.write_tags(txt_path, tags)
                self._consec_write_failures = 0
                return True
            except OSError:
                if i < attempts - 1:
                    time.sleep(delay)
        self._consec_write_failures += 1
        return False

    def apply_tag_rename_map(self, rename_map: dict, delete_tags=None) -> dict:
        """Apply a whole map of tag renames as ONE undoable operation,
        rewriting each affected caption file EXACTLY ONCE.

        All renames for a given file are applied to its tag list in memory
        first, then that file is written a single time. This is both far
        faster than renaming tag-by-tag (a file with N converting tags was
        previously rewritten N times) and far more reliable: hammering a
        file with many writes in quick succession is what provoked
        transient Windows file-locks that silently dropped a tag. Writes
        now retry on failure, and any file that still can't be written is
        reported rather than skipped silently.

        Merges + dedup are preserved (a rename whose target already exists
        in a file collapses to one tag), decisions follow their tag, and
        per-folder + global counts stay correct. The whole batch is a
        single undo entry. This is the engine behind the underscore<->space
        converter and any find/replace-in-tags tool.

        `delete_tags` (optional) is an iterable of tags to remove outright
        in the same single-write pass; a tag listed for deletion is dropped
        even if it is also a rename source. This is the engine behind the
        underscore<->space converter, find/replace-in-tags, and the Audit
        "apply fixes" action (renames + deletes together).

        Returns {"tags": int, "deleted": int, "images": int,
        "failed": [str, ...]}: tags renamed, tags deleted, distinct caption
        files rewritten, and the names of any files that could not be
        written (left unchanged).
        """
        pairs = self._clean_rename_pairs(rename_map)
        ren = {old: new for old, new in pairs}
        # Validate deletes: must currently exist. A tag can't be both
        # renamed and deleted — deletion wins, so drop it from the renames.
        dele: set = set()
        if delete_tags:
            for t in delete_tags:
                if t in self._tag_counts:
                    dele.add(t)
        for t in dele:
            ren.pop(t, None)
        if not ren and not dele:
            return {"tags": 0, "deleted": 0, "images": 0, "failed": []}

        # Plan: only files that actually contain a source tag are touched.
        # For each, build the new tag list (apply renames; drop a value
        # that a rename collapses onto one already present).
        plans: list[tuple] = []  # (path, before, after)
        for img in self._images:
            before = self._image_tags.get(img.image_path, [])
            if not any((t in ren) or (t in dele) for t in before):
                continue
            after: list[str] = []
            for t in before:
                if t in dele:
                    continue  # delete: drop it from the file
                nt = ren.get(t, t)
                if nt not in after:
                    after.append(nt)
            if after != before:
                plans.append((img.image_path, list(before), after))

        if not plans:
            return {"tags": 0, "deleted": 0, "images": 0, "failed": []}

        # Snapshots for a clean, total undo (cheap, in-memory).
        snap_counts = dict(self._tag_counts)
        snap_folder = {k: dict(v) for k, v in self._tag_counts_by_folder.items()}
        snap_dec = dict(self._decisions)
        snap_status = dict(self._tag_status)
        snap_cur = (self._current_tag, list(self._current_queue), self._walk_index)

        applied: list[tuple] = []   # (path, before, after) actually written
        failed: list[str] = []
        introduced_all: set = set()
        disappeared_all: set = set()
        renamed_tags: set = set()
        deleted_tags: set = set()

        # Fresh failure streak for this batch (see _write_tags_with_retry;
        # this method reports failures via its own result["failed"], not
        # last_write_failures, so only the streak needs resetting here).
        self._consec_write_failures = 0
        for path, before, after in plans:
            img = self._images_by_path[path]
            if not self._write_tags_with_retry(img.txt_path, after):
                failed.append(img.txt_path.name)
                continue
            self._set_image_tags(path, after)
            removed = [t for t in before if t not in after]
            added = [t for t in after if t not in before]
            intro, disap = _adjust_counts(
                self._tag_counts, self._tag_counts_by_folder,
                img.subfolder, removed, added,
            )
            introduced_all |= set(intro)
            disappeared_all |= set(disap)
            # A removed tag's prior Yes/No is cleared; a renamed (not
            # deleted) tag carries its decision to the target unless the
            # target already has one.
            for t in removed:
                prev = self._decisions.pop((path, t), Decision.UNPROCESSED)
                if t in dele:
                    deleted_tags.add(t)
                elif t in ren:
                    renamed_tags.add(t)
                    if prev != Decision.UNPROCESSED:
                        tgt = ren[t]
                        if self._decisions.get((path, tgt), Decision.UNPROCESSED) == Decision.UNPROCESSED:
                            self._decisions[(path, tgt)] = prev
            applied.append((path, before, after))

        if not applied:
            return {"tags": 0, "deleted": 0, "images": 0, "failed": failed}

        # Statuses: new tags default to PENDING; a clean 1:1 rename carries
        # the source's review state forward; vanished tags drop out.
        for t in introduced_all:
            self._tag_status.setdefault(t, TagStatus.PENDING)
        for old, new in pairs:
            if old in disappeared_all and new in self._tag_counts:
                st = snap_status.get(old)
                if st is not None:
                    self._tag_status[new] = st
        for t in disappeared_all:
            self._tag_status.pop(t, None)
        # If the walked tag was renamed or deleted away, end the walk.
        if self._current_tag in disappeared_all:
            self._current_tag = None
            self._current_queue = []
            self._walk_index = 0

        self._rebuild_tree_order()

        def revert() -> None:
            for path, before, after in applied:
                img = self._images_by_path[path]
                try:
                    tag_io.write_tags(img.txt_path, before)
                except OSError:
                    pass
                self._set_image_tags(path, before)
            # Restore all derived state wholesale from the snapshots.
            self._tag_counts.clear(); self._tag_counts.update(snap_counts)
            self._tag_counts_by_folder.clear()
            for k, v in snap_folder.items():
                self._tag_counts_by_folder[k] = dict(v)
            self._decisions.clear(); self._decisions.update(snap_dec)
            self._tag_status.clear(); self._tag_status.update(snap_status)
            self._current_tag = snap_cur[0]
            self._current_queue = list(snap_cur[1])
            self._walk_index = snap_cur[2]
            self._rebuild_tree_order()
            self._emit(StateChange("tree_rebuilt"))
            self._emit(StateChange("tag_changed", tag=None))
            if self._multi_select_mode:
                self._rebuild_queue_multi()
            self._emit(StateChange("filter_changed", tag=None))

        nr, nd = len(renamed_tags), len(deleted_tags)
        parts = []
        if nr:
            parts.append(f"{nr} tag(s) renamed")
        if nd:
            parts.append(f"{nd} tag(s) deleted")
        detail = " and ".join(parts) if parts else "tags edited"
        summary = f"{detail} across {len(applied)} image(s)"
        self._push_undo(UndoEntry(summary, revert))

        # One consolidated refresh + log line for the whole batch.
        self._rebuild_tree_order()
        self._emit(StateChange("tree_rebuilt"))
        self._emit(StateChange(
            "action_logged",
            extra=summary
                  + (f"; {len(failed)} file(s) could not be written"
                     if failed else ""),
        ))
        if self._multi_select_mode:
            self._rebuild_queue_multi()
        self._emit(StateChange("filter_changed", tag=None))
        return {"tags": nr, "deleted": nd, "images": len(applied),
                "failed": failed}

    # ------------------------------------------------------------------
    # Direct walk-position control (for queue-row clicks)
    # ------------------------------------------------------------------

    def split_tag_globally(self, old_tag: str, new_tags: list[str]) -> int:
        """Replace `old_tag` with SEVERAL tags across every caption file.

        This is the "fix a malformed tag" operation: a tag that should
        have been multiple tags got merged into one (e.g. "1girl solo"
        instead of "1girl, solo"). The user renames it with commas; the
        UI parses the input into new_tags=["1girl","solo"] and calls
        this. Every image that had old_tag gets old_tag removed and all
        of new_tags inserted at that position (deduping any that are
        already present elsewhere on the image).

        Returns the number of files modified. No-op (returns 0) if
        new_tags is empty, any new tag is malformed, the parsed result
        is just the single old_tag again, or old_tag doesn't exist.

        Decisions: old_tag's recorded YES/NO decisions do NOT migrate to
        the new tags. Splitting changes meaning — "1girl solo" was never
        a real concept, so its prior adjudication doesn't transfer to
        "1girl" or "solo" (those must be reviewed on their own terms).
        The old decisions are dropped (and restored on undo). This is
        deliberate and the UI explains it.

        Atomic per file; failed writes skipped; one combined undo entry.
        """
        # Clean and validate the target tags.
        self._last_write_failures = []
        self._consec_write_failures = 0
        cleaned: list[str] = []
        seen: set[str] = set()
        for t in new_tags:
            t = t.strip()
            if not t:
                continue
            if not tag_io.is_valid_tag(t):
                # A target tag itself is malformed (e.g. still contains a
                # newline). Reject the whole operation rather than write
                # a partially-valid result.
                return 0
            if t not in seen:
                seen.add(t)
                cleaned.append(t)
        if not cleaned:
            return 0
        # If it parsed down to just the original tag, nothing to do.
        if cleaned == [old_tag]:
            return 0
        if old_tag not in self._tag_counts:
            return 0

        # Plan per-image: replace old_tag (first occurrence position)
        # with the cleaned list, dropping any of the new tags already
        # present elsewhere on that image, and dropping extra old_tag
        # repeats.
        affected: list[tuple[Path, list[str], list[str]]] = []
        for img in self._images:
            tags = self._image_tags.get(img.image_path, [])
            if old_tag not in tags:
                continue
            existing = set(tags)
            new_list: list[str] = []
            inserted = False
            for t in tags:
                if t == old_tag:
                    if not inserted:
                        # Insert all new tags here, skipping any that
                        # already exist elsewhere on this image.
                        for nt in cleaned:
                            if nt not in existing or nt == old_tag:
                                # nt==old_tag can't happen (cleaned!=[old]),
                                # but guard anyway; skip dupes already on img
                                if nt not in new_list:
                                    new_list.append(nt)
                        inserted = True
                    # subsequent old_tag occurrences are dropped
                else:
                    if t not in new_list:
                        new_list.append(t)
            affected.append((img.image_path, list(tags), new_list))

        if not affected:
            return 0

        prev_old_status = self._tag_status.get(old_tag, TagStatus.PENDING)
        prev_new_statuses = {
            nt: self._tag_status.get(nt, None) for nt in cleaned
        }
        modified = 0
        applied: list[tuple[Path, list[str], list[str]]] = []
        for path, before, after in affected:
            img = self._images_by_path[path]
            if not self._write_tags_with_retry(img.txt_path, after):
                self._last_write_failures.append(img.txt_path.name)
                continue
            self._set_image_tags(path, after)
            modified += 1
            applied.append((path, before, after))

        if modified == 0:
            return 0

        # Capture old_tag decisions for undo, then drop them (no migrate).
        dropped_decisions: list[tuple[Path, Decision]] = []
        for path, _, _ in applied:
            prev = self._decisions.get((path, old_tag), Decision.UNPROCESSED)
            if prev != Decision.UNPROCESSED:
                dropped_decisions.append((path, prev))
                del self._decisions[(path, old_tag)]

        # Recompute counts from scratch for the affected tags — simplest
        # correct approach given multiple targets with per-image dedupe.
        self._recount_tag(old_tag)
        for nt in cleaned:
            self._recount_tag(nt)
            self._tag_status.setdefault(nt, TagStatus.PENDING)

        # If old_tag is gone and was the active walk, end the walk.
        if old_tag not in self._tag_counts and self._current_tag == old_tag:
            self._current_tag = None
            self._current_queue = []
            self._walk_index = 0

        self._rebuild_tree_order()

        def revert() -> None:
            for path, before, _ in applied:
                img = self._images_by_path[path]
                try:
                    tag_io.write_tags(img.txt_path, before)
                except OSError:
                    pass
                self._set_image_tags(path, before)
            for path, prev in dropped_decisions:
                self._decisions[(path, old_tag)] = prev
            self._recount_tag(old_tag)
            for nt in cleaned:
                self._recount_tag(nt)
            self._tag_status[old_tag] = prev_old_status
            for nt, st in prev_new_statuses.items():
                if st is None:
                    # Only remove if it has no count now.
                    if nt not in self._tag_counts:
                        self._tag_status.pop(nt, None)
                else:
                    self._tag_status[nt] = st
            self._rebuild_tree_order()
            self._emit(StateChange("tree_rebuilt"))
            self._emit(StateChange(
                "action_logged", tag=old_tag,
                extra=(f"Undone: split '{old_tag}' \u2192 "
                       f"{', '.join(cleaned)} ({modified} files)"),
            ))

        self._push_undo(UndoEntry(
            f"Split '{old_tag}' \u2192 {', '.join(cleaned)} ({modified} files)",
            revert,
        ))
        self._rebuild_tree_order()
        self._emit(StateChange("tree_rebuilt"))
        self._emit(StateChange(
            "action_logged", tag=old_tag,
            extra=(f"Split '{old_tag}' \u2192 {', '.join(cleaned)} "
                   f"({modified} files)"),
        ))
        return modified

    # ------------------------------------------------------------------
    # Direct walk-position control (for queue-row clicks)
    # ------------------------------------------------------------------

    def jump_to_queue_index(self, index: int) -> None:
        """Move the walk index to a specific position. Does not modify
        decisions or write anything. Used by the queue panel when the
        user clicks a specific row.
        """
        if 0 <= index < len(self._current_queue):
            self._walk_index = index
            self._emit(StateChange("walk_advanced"))

    # ------------------------------------------------------------------
    # Iteration helpers (used by persistence)
    # ------------------------------------------------------------------

    def iter_decisions(self):
        """Yield (image_path, tag, decision) for every recorded decision.

        Used by persistence to serialize the user's review history.
        Does not include UNPROCESSED — those are absent from the dict.
        """
        for (path, tag), decision in self._decisions.items():
            yield (path, tag, decision)

    def iter_tag_status_overrides(self):
        """Yield (tag, override_value) for tags whose status is set
        explicitly by the user (not derived from decisions).

        override_value is a string token used by persistence:
        - "skipped"             : TagStatus.SKIPPED
        - "manually_completed"  : in self._manually_completed

        Used by persistence to serialize these states. Excluding
        decision-derived statuses (PENDING / auto-COMPLETED) keeps
        the saved file small and unambiguous on load.
        """
        for tag, status in self._tag_status.items():
            if status == TagStatus.SKIPPED:
                yield (tag, "skipped")
        for tag in self._manually_completed:
            # If a tag is both manually completed AND skipped (shouldn't
            # happen in practice, but defensive), the skipped one was
            # already emitted above. We don't double-emit.
            if self._tag_status.get(tag) != TagStatus.SKIPPED:
                yield (tag, "manually_completed")

    # Backwards-compatible alias retained so anything elsewhere still
    # calling iter_skipped_tags continues to work. Remove after the
    # next persistence-format version bump.
    def iter_skipped_tags(self):
        for tag, value in self.iter_tag_status_overrides():
            if value == "skipped":
                yield tag

    # ------------------------------------------------------------------
    # Bulk restoration (used by persistence on session load)
    #
    # These methods bypass the normal record_* path because:
    # 1. The disk is already in the desired state (we're reloading
    #    the user's prior decisions, not making new edits).
    # 2. We don't want to push to the undo stack — a load is not a
    #    reversible action in the action-history sense.
    # 3. We want to defer status recalculation until all decisions
    #    are loaded, then do it once via recalculate_all_tag_statuses.
    # ------------------------------------------------------------------

    def restore_decision(
        self,
        image_path: Path,
        tag: str,
        decision: Decision,
    ) -> None:
        """Restore a single (image, tag, decision) record.

        Does not modify disk content (assumes the .txt file already
        reflects the post-decision state from when the user originally
        made it). Does not push to undo. Does not recalculate tag
        status — caller must invoke recalculate_all_tag_statuses
        afterward.

        Silently ignored if image_path is not known to the session.
        This is how we handle "image deleted since save."
        """
        if image_path not in self._images_by_path:
            return
        if decision == Decision.UNPROCESSED:
            self._decisions.pop((image_path, tag), None)
        else:
            self._decisions[(image_path, tag)] = decision

    def restore_tag_status(self, tag: str, status: TagStatus) -> None:
        """Restore a tag's aggregate status. Used by persistence to
        re-apply user-skipped tags. PENDING and COMPLETED are derived
        from decisions, so normally only SKIPPED needs explicit
        restoration; the rest is recomputed by
        recalculate_all_tag_statuses.
        """
        if tag in self._tag_counts or tag in self._tag_status:
            self._tag_status[tag] = status

    def restore_manual_completion(self, tag: str) -> None:
        """Restore a tag's manual-completion marker. Used by
        persistence. After restoration, recalculate_all_tag_statuses
        will leave this tag at COMPLETED regardless of decision state.
        """
        if tag in self._tag_counts or tag in self._tag_status:
            self._manually_completed.add(tag)
            self._tag_status[tag] = TagStatus.COMPLETED

    def clear_review_state(self) -> None:
        """Wipe all review state that a session load restores.

        Clears decisions, manual completions, skipped/completed tag
        statuses (back to PENDING), the walk position, and the queue
        search — everything persistence.apply_to_state is responsible
        for re-establishing. Does NOT touch the dataset itself (images,
        tag counts, tree order) or app-wide preferences sourced from
        settings (auto-yes, thresholds, tag-complete behavior).

        This exists because loading a session must REPLACE the current
        review state, not merge into it. Without a clear step, decisions
        and manual completions from the user's current unsaved work
        would survive the load and contaminate the loaded session
        (e.g. load a 1-decision file into a 10-decision working state
        and end up with 11). apply_to_state calls this first.

        Does not push to undo (a load is not an undoable edit) and does
        not emit per-change events — apply_to_state emits a single
        refresh at the end via notify_session_reloaded.
        """
        self._decisions.clear()
        self._manually_completed.clear()
        # Reset every tag's status to PENDING. recalculate_all_tag_statuses
        # (called later in apply_to_state) and the restored overrides will
        # re-establish COMPLETED/SKIPPED/manual where appropriate.
        for tag in self._tag_status:
            self._tag_status[tag] = TagStatus.PENDING
        # Drop the walk and any transient search filter.
        self._current_tag = None
        self._current_queue = []
        self._walk_index = 0
        self._queue_search = ""
        # The undo stack refers to the pre-load edits, which no longer
        # make sense against the freshly-loaded state. Clear it so the
        # user can't "undo" their way into a corrupt mix of old and new.
        self._undo_stack.clear()

    def restore_walk_position(
        self,
        tag: str,
        image_path: Path,
    ) -> bool:
        """Restore the walk to a specific (tag, image) pair.

        Returns True if the position was successfully restored, False
        if the tag no longer exists or the image is not in the queue
        for that tag under the current filter/sort settings (in which
        case the walk simply isn't restored — user will start at the
        beginning when they next select a tag).
        """
        if tag not in self._tag_counts and tag not in self._tag_status:
            return False
        # select_tag rebuilds the queue based on current filter / sort
        # / orphan settings, which must be restored BEFORE this method
        # is called by the loader. run_auto_yes=False: restore must
        # reconstruct the saved position and decisions exactly — letting
        # auto-yes scan from index 0 would inject YES decisions on early
        # already-tagged images that weren't in the session file.
        self.select_tag(tag, run_auto_yes=False)
        for i, img in enumerate(self._current_queue):
            if img.image_path == image_path:
                self._walk_index = i
                self._emit(StateChange("walk_advanced"))
                return True
        # Image not in current queue (could be deleted, filtered out,
        # or its file content no longer matches the filter). Leave
        # walk at index 0.
        return False

    def recalculate_all_tag_statuses(self) -> None:
        """Recompute COMPLETED/PENDING for every tag in one pass.

        Used after bulk decision restoration during session load. More
        efficient than calling _update_tag_status per tag, because we
        only iterate self._decisions once instead of self._images per
        tag.

        SKIPPED tags and manually-completed tags are preserved — these
        are explicit user choices that auto-recompute should never
        override. They're restored separately by the persistence load
        process.

        Completion rule: a tag is COMPLETED only when every non-orphan
        image has a YES or NO decision. SKIPPED image-decisions do NOT
        count toward completion (matching _update_tag_status and
        get_project_completion — a deferred image isn't a done image),
        and decisions on orphan images don't count either (orphans are
        outside the standard walk). This keeps the sidebar/walk's notion
        of "done" consistent with the swirl and stats.

        Cost: O(N_decisions + N_tags). Sub-millisecond at typical scale.

        Emits a single tree_rebuilt event at the end if anything
        changed, rather than per-tag events.
        """
        # Count only YES/NO decisions on non-orphan images, per tag.
        decision_counts: dict[str, int] = {}
        for (image_path, tag), decision in self._decisions.items():
            if decision != Decision.YES and decision != Decision.NO:
                continue  # SKIPPED / UNPROCESSED don't count as done
            if not self._has_txt.get(image_path, False):
                continue  # orphan image: outside the completion universe
            decision_counts[tag] = decision_counts.get(tag, 0) + 1

        # Total non-orphan images is the denominator for completion.
        non_orphan_total = sum(
            1 for img in self._images
            if self._has_txt.get(img.image_path, False)
        )
        changed = False
        for tag in list(self._tag_status.keys()):
            if self._tag_status[tag] == TagStatus.SKIPPED:
                continue
            if tag in self._manually_completed:
                continue
            decided = decision_counts.get(tag, 0)
            # When there are no non-orphan images, completion is vacuous
            # (0 >= 0 would mark every tag COMPLETED with no work). A tag
            # with nothing to decide stays PENDING — mirrors the
            # saw_non_orphan guard in _update_tag_status.
            if non_orphan_total == 0:
                new_status = TagStatus.PENDING
            else:
                new_status = (
                    TagStatus.COMPLETED if decided >= non_orphan_total
                    else TagStatus.PENDING
                )
            if self._tag_status.get(tag) != new_status:
                self._tag_status[tag] = new_status
                changed = True
        if changed:
            self._emit(StateChange("tree_rebuilt"))

    def notify_session_reloaded(self) -> None:
        """Emit a tree_rebuilt event unconditionally.

        Called by persistence.apply_to_state at the end of a session
        load to force every attached widget to refresh from the
        freshly-restored decisions — even when no tag status changed
        (recalculate_all_tag_statuses is conditional and would stay
        silent in that case). Treated by listeners exactly like any
        other structural refresh: the stats window recomputes its
        numbers/charts, the swirl recomputes completion, the tag tree
        and queue rebuild, etc.
        """
        self._emit(StateChange("tree_rebuilt"))
