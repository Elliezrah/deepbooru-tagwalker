"""
core/scanner.py

Root-only directory scanner for the TagWalker dataset model.

Enumerates the image files sitting DIRECTLY in a root directory (it does
NOT descend into subfolders), pairs each image with its sibling .txt
caption file (if one exists), reads tags from each .txt, and produces a
ScanResult that the UI layer can render directly.

This module has no GUI dependencies. It is safe to call from a worker
thread; the returned ScanResult is a plain dataclass containing immutable
or copy-safe types and can be marshalled across a Qt signal.

Design notes
------------
- Uses os.scandir for enumeration. On large datasets (10k+ files) this is
  several times faster than glob.glob or Path.rglob.
- Tolerates per-file and per-directory errors. A corrupt or unreadable
  caption file becomes an empty tag list; an unreadable subdirectory is
  skipped rather than aborting the entire scan.
- Supports cooperative cancellation via a callback. Callers should pass
  cancel_check = lambda: self._cancel_flag.is_set() (or equivalent) so the
  user can interrupt a long scan by selecting a different directory.
- Progress callback fires at low frequency (every 32 files in the read
  phase) to keep the inter-thread signal queue empty. Floods of signals
  can themselves cause UI lag.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core import tag_io


# Image extensions we recognize. Matches the original TagWalker beta to
# avoid surprising users with files appearing or disappearing from scans.
# Lowercase — comparisons are always done against the lowercased filename.
IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    '.png', '.jpg', '.jpeg', '.bmp', '.webp',
})


@dataclass(frozen=True)
class ImageEntry:
    """One image and its (possibly nonexistent) paired .txt caption file.

    Frozen so instances can be hashed, cached, and passed across threads
    without aliasing concerns.

    Attributes
    ----------
    image_path : absolute path to the image file
    txt_path   : absolute path the caption file *should* live at; this is
                 always image_path with its suffix replaced by '.txt',
                 regardless of whether the file actually exists
    has_txt    : True if txt_path existed at scan time
    subfolder  : POSIX-style relative path from the dataset root. Empty
                 string for images directly inside root. Always uses '/'
                 as the separator so it matches between Windows and Linux
                 and can be used as a stable dict key.
    """
    image_path: Path
    txt_path: Path
    has_txt: bool
    subfolder: str


@dataclass
class ScanResult:
    """Outcome of a directory scan.

    All collections are owned by this instance and can be moved into the
    state model without copying. The producer thread should not retain
    references after returning the result.
    """
    root: Path
    images: list[ImageEntry] = field(default_factory=list)
    subfolders: list[str] = field(default_factory=list)
    images_by_subfolder: dict[str, list[ImageEntry]] = field(default_factory=dict)
    # initial_tags seeds the tag cache used by the state model. Every image
    # has an entry — orphans (no .txt) get an empty list.
    initial_tags: dict[Path, list[str]] = field(default_factory=dict)
    tag_counts: dict[str, int] = field(default_factory=dict)
    tag_counts_by_subfolder: dict[str, dict[str, int]] = field(default_factory=dict)
    orphan_count: int = 0
    duration_seconds: float = 0.0
    cancelled: bool = False
    # Groups of image filenames that resolve to the SAME caption file
    # (e.g. foo.png and foo.jpg both pair with foo.txt). The one-caption-
    # per-image-stem model can't disambiguate these: editing one would
    # leave the other's view stale and a later edit would clobber it on
    # disk. Surfaced so the UI can warn rather than silently lose tags.
    # Each element is the sorted list of image filenames sharing one .txt.
    caption_collisions: list[list[str]] = field(default_factory=list)
    # Leftover atomic-write temp files (.tw_tmp_*.txt) removed during
    # this scan. tag_io's writes clean these on any failure, but a hard
    # kill (crash, power loss) between the temp write and os.replace can
    # orphan one. They're unambiguously ours by prefix; the scanner
    # removes any older than a minute so they don't accumulate as junk
    # inside the user's dataset folder. Young ones are left alone in
    # case another process is mid-write.
    stale_tempfiles_removed: int = 0
    # Number of immediate subdirectories skipped under the root-only rule
    # (their images/captions are intentionally not loaded). Surfaced so
    # the UI can note "N subfolders ignored" to avoid confusion about
    # missing images.
    ignored_subfolders: int = 0
    # Multi-folder load (field feature): every root that contributed to
    # this result. Single-folder scans set [root]; scan_many() sets the
    # full list. `root` stays the FIRST root so all single-root
    # consumers (titles, legacy sessions) keep working unchanged.
    roots: list[Path] = field(default_factory=list)

    @property
    def total_images(self) -> int:
        return len(self.images)

    @property
    def total_tags(self) -> int:
        return len(self.tag_counts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_tags(txt_path: Path) -> list[str]:
    """Tolerant wrapper around tag_io.read_tags for use during scanning.

    The scanner is expected to gracefully handle any single-file error
    (file vanished between is_file() and read, permission denied,
    corruption) without aborting the entire scan. tag_io.read_tags
    raises on these conditions; here we catch and return [] so the
    scan can continue.
    """
    try:
        return tag_io.read_tags(txt_path)
    except OSError:
        return []


# Native separator, resolved once.
_SEP = os.sep


def _subfolder_of(image_path: Path, root: Path) -> str:
    """Compute the POSIX-style relative subfolder of an image.

    Images directly inside root get an empty string. Nested images get
    e.g. 'train' or 'train/subset_a'. Forward slash is used universally,
    never the native os.sep, so the result is a stable dict key across
    platforms and can be saved to JSON and reloaded on a different OS.
    """
    # MEASURED: Path.relative_to was about 40% of the time to open a
    # folder — 2,400 calls building 24,000 intermediate Path objects on
    # a 2,400-image dataset. The parent directory is already a Path we
    # hold, and the answer is a prefix strip, so string work does the
    # same job without constructing anything.
    parent = str(image_path.parent)
    base = str(root)
    if parent == base:
        return ""
    prefix = base if base.endswith(_SEP) else base + _SEP
    if not parent.startswith(prefix):
        # Should not happen — the image was discovered under root — but
        # a symlink leading outside is treated as living at the root,
        # exactly as the previous relative_to/ValueError path did.
        return ""
    return parent[len(prefix):].replace("\\", "/").replace(_SEP, "/")


def _walk_images(
    root: Path,
    cancel_check: Optional[Callable[[], bool]],
):
    """Yield image file paths located directly in root (non-recursive).

    Root-only rule: images (and their captions) inside subdirectories are
    intentionally NOT loaded. Only files sitting directly in the root of
    the loaded directory are returned.

    Implementation notes:
    - Uses an explicit stack of directories instead of recursion to avoid
      hitting Python's recursion limit on deeply nested datasets and to
      give us a single place to handle per-directory errors.
    - os.scandir returns DirEntry objects that cache stat info, so
      entry.is_file() and entry.is_dir() are essentially free until we
      actually need a Path.
    - Symlinks are NOT followed. This prevents infinite loops if a user
      has set up odd symlink topologies and matches the behavior of most
      file managers' "show in folder" view.
    - The cancel check uses bitwise AND on a counter rather than modulo.
      It's a hair faster on the hot path and the throttle (every 256
      entries) is plenty responsive to user cancellation.
    """
    stack: list[str] = [os.fspath(root)]
    counter = 0
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    counter += 1
                    if (counter & 0xFF) == 0 and cancel_check and cancel_check():
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            # Root-only rule: subfolders are intentionally
                            # ignored. Do NOT descend — images and captions
                            # inside subdirectories are not loaded.
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        # Extension test on the raw string, before we
                        # spend allocations constructing a Path.
                        name_lower = entry.name.lower()
                        for ext in IMAGE_EXTENSIONS:
                            if name_lower.endswith(ext):
                                yield Path(entry.path)
                                break
                    except OSError:
                        # Permission denied on a single entry — skip it,
                        # continue with the rest of the directory.
                        continue
        except (PermissionError, OSError):
            # Cannot enter this directory. Continue with siblings.
            continue


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scan(
    root: Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    """Scan a directory (root level only) for image/caption pairs.

    Parameters
    ----------
    root
        Absolute path of the dataset root.
    progress_callback
        Optional callable invoked as ``progress_callback(processed, total)``
        during the tag-reading phase. ``total`` is -1 during the
        enumeration phase if you want to surface it before counts are
        known. The callback is invoked from the calling thread (i.e. the
        worker thread); UI code receiving it must marshal to the main
        thread via a Qt signal.
    cancel_check
        Optional callable returning True if the caller wants the scan to
        abort. Checked periodically; the returned ScanResult will have
        ``cancelled = True`` and may contain partial data.

    Returns
    -------
    ScanResult
        See dataclass docstring. Always non-None, even on empty or
        nonexistent root (you get an empty result rather than an
        exception).
    """
    started = time.monotonic()
    result = ScanResult(root=root, roots=[root])

    # Defensive: caller should have validated, but a None or missing root
    # must not crash. Return empty result quietly.
    if root is None or not root.exists() or not root.is_dir():
        result.duration_seconds = time.monotonic() - started
        return result

    # Tidy up orphaned atomic-write temp files from a previous hard kill
    # (see ScanResult.stale_tempfiles_removed). Conservative on purpose:
    # exact ".tw_tmp_*.txt" pattern only, and only if the file is over a
    # minute old — a live writer's temp exists for milliseconds, so age
    # is a bulletproof discriminator. Every step best-effort; a locked
    # temp is simply left for next time.
    _now = time.time()
    for _tmp in root.glob(".tw_tmp_*.txt"):
        try:
            if _now - _tmp.stat().st_mtime > 60:
                _tmp.unlink()
                result.stale_tempfiles_removed += 1
        except OSError:
            pass

    # Count (but never enter) immediate subdirectories, so the UI can
    # tell the user their subfolder contents were intentionally skipped
    # under the root-only rule. Cheap: one scandir of the root level.
    try:
        with os.scandir(root) as _it:
            result.ignored_subfolders = sum(
                1 for _e in _it if _e.is_dir(follow_symlinks=False)
            )
    except OSError:
        result.ignored_subfolders = 0

    # ---- Phase 1: enumerate image files. -----------------------------
    image_paths: list[Path] = []
    for image_path in _walk_images(root, cancel_check):
        image_paths.append(image_path)
        if cancel_check and (len(image_paths) & 0x3FF) == 0 and cancel_check():
            result.cancelled = True
            result.duration_seconds = time.monotonic() - started
            return result

    # The enumeration generator stops itself when cancelled (it returns
    # mid-walk on its own throttle, which can fall BETWEEN the outer
    # throttle boundaries above). In that case the loop ends normally
    # without our flag being set, and phase 2 would run on a partial,
    # arbitrarily-truncated file list — a silent half-scan that looks
    # complete. So we re-check cancellation once here, after phase 1
    # and before any phase-2 work begins. This also covers a cancel
    # that arrives in the gap between the two phases.
    if cancel_check and cancel_check():
        result.cancelled = True
        result.duration_seconds = time.monotonic() - started
        return result

    # Deterministic ordering. Sorting on (subfolder, lowercased name)
    # gives the user a tree that matches Windows Explorer's display.
    # We compute the sort key once per element rather than constructing
    # it inside the comparator on every comparison.
    image_paths.sort(key=lambda p: (str(p.parent).lower(), p.name.lower()))

    total = len(image_paths)
    if total == 0:
        result.duration_seconds = time.monotonic() - started
        return result

    # ---- Phase 2: build entries, read caption files. -----------------
    images: list[ImageEntry] = []
    initial_tags: dict[Path, list[str]] = {}
    tag_counts: dict[str, int] = {}
    tag_counts_by_subfolder: dict[str, dict[str, int]] = {}
    images_by_subfolder: dict[str, list[ImageEntry]] = {}
    orphan_count = 0

    for index, image_path in enumerate(image_paths):
        # Cancellation check, throttled. Index 0 is also checked because
        # the user may have cancelled between phase 1 and phase 2.
        if (index & 0x3F) == 0 and cancel_check and cancel_check():
            result.cancelled = True
            break

        txt_path = image_path.with_suffix('.txt')
        # is_file() does a stat call; we accept that cost because we
        # need to know orphan status to drive the UI.
        try:
            has_txt = txt_path.is_file()
        except OSError:
            has_txt = False

        subfolder = _subfolder_of(image_path, root)

        entry = ImageEntry(
            image_path=image_path,
            txt_path=txt_path,
            has_txt=has_txt,
            subfolder=subfolder,
        )
        images.append(entry)
        images_by_subfolder.setdefault(subfolder, []).append(entry)

        if has_txt:
            tags = _read_tags(txt_path)
            initial_tags[image_path] = tags
            if tags:
                sub_counts = tag_counts_by_subfolder.setdefault(subfolder, {})
                for tag in tags:
                    # Two dict ops total per tag — increment global and
                    # per-subfolder. Both are O(1).
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    sub_counts[tag] = sub_counts.get(tag, 0) + 1
        else:
            initial_tags[image_path] = []
            orphan_count += 1

        # Throttled progress emission. Every 32 files is plenty for a
        # progress bar to feel smooth without flooding the signal queue.
        if progress_callback and (index & 0x1F) == 0:
            progress_callback(index + 1, total)

    # One final progress emission so the UI lands on 100%, not 96%.
    if progress_callback:
        progress_callback(total, total)

    # Sort subfolders lexicographically. Empty string ("" = root images)
    # naturally sorts first, which is the correct visual order.
    subfolders = sorted(images_by_subfolder.keys())

    # Detect images that resolve to the SAME caption file (same stem,
    # different image extension). These can't be edited independently —
    # warn rather than silently double-count and clobber.
    by_txt: dict[Path, list[str]] = {}
    for e in images:
        by_txt.setdefault(e.txt_path, []).append(e.image_path.name)
    caption_collisions = [
        sorted(names) for names in by_txt.values() if len(names) > 1
    ]

    result.images = images
    result.subfolders = subfolders
    result.images_by_subfolder = images_by_subfolder
    result.initial_tags = initial_tags
    result.tag_counts = tag_counts
    result.tag_counts_by_subfolder = tag_counts_by_subfolder
    result.orphan_count = orphan_count
    result.caption_collisions = caption_collisions
    result.duration_seconds = time.monotonic() - started
    return result


def scan_many(
    roots: list[Path],
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    """Scan several dataset folders (each root-only, exactly like
    scan()) and merge them into one combined ScanResult — the field
    feature: multiple load directories, all images in one list.

    Merge rules: image paths are unique across distinct folders, so the
    path-keyed maps union cleanly; per-tag counts sum; orphans, stale
    temp files, ignored subfolders and caption collisions accumulate.
    Duplicate root entries are dropped (first occurrence wins).
    `root` is the first root; `roots` carries the full opened order.
    If any sub-scan is cancelled the merged result is marked cancelled.
    """
    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        key = str(Path(r).resolve())
        if key not in seen:
            seen.add(key)
            unique.append(Path(r))
    if not unique:
        raise ValueError("scan_many needs at least one root")
    if len(unique) == 1:
        return scan(unique[0], progress_callback, cancel_check)

    merged: Optional[ScanResult] = None
    for r in unique:
        part = scan(r, progress_callback, cancel_check)
        if merged is None:
            merged = part
            continue
        merged.images.extend(part.images)
        for sf in part.subfolders:
            if sf not in merged.subfolders:
                merged.subfolders.append(sf)
        for sf, imgs in part.images_by_subfolder.items():
            merged.images_by_subfolder.setdefault(sf, []).extend(imgs)
        merged.initial_tags.update(part.initial_tags)
        for t, n in part.tag_counts.items():
            merged.tag_counts[t] = merged.tag_counts.get(t, 0) + n
        for sf, counts in part.tag_counts_by_subfolder.items():
            dst = merged.tag_counts_by_subfolder.setdefault(sf, {})
            for t, n in counts.items():
                dst[t] = dst.get(t, 0) + n
        merged.orphan_count += part.orphan_count
        merged.duration_seconds += part.duration_seconds
        merged.cancelled = merged.cancelled or part.cancelled
        merged.caption_collisions.extend(part.caption_collisions)
        merged.stale_tempfiles_removed += part.stale_tempfiles_removed
        merged.ignored_subfolders += part.ignored_subfolders
    assert merged is not None
    merged.roots = unique
    merged.root = unique[0]
    return merged
