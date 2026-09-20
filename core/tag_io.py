"""
core/tag_io.py

Safe read and write of caption files (.txt) for the TagWalker dataset
model.

This module is the *only* place in the codebase that touches caption
files on disk. Everything else — state tracking, undo history, the
file watcher — goes through these two functions:

    read_tags(path)        ->  list[str]
    write_tags(path, tags) ->  None

Why a single layer:
- One place to enforce the on-disk format (UTF-8, comma+space
  separator, single LF line ending).
- One place to enforce atomic writes. A crash mid-write will never
  leave a half-written caption file: callers either see the old
  content or the new content, never garbage.
- One place to handle BOM / encoding quirks so we don't have two
  subtly different parsers in different modules.

Design notes
------------
Atomic write pattern:
    1. Write the new content to a hidden tempfile in the same
       directory as the target. Same-directory matters: cross-filesystem
       rename is NOT atomic on Windows, same-filesystem rename IS atomic.
    2. os.replace(tempfile, target). This is the only "real" mutation
       and it is atomic at the OS level.
    3. If anything fails, clean up the tempfile and re-raise. The
       original target file is untouched.

Sanitization on write:
    Tags are stripped of leading/trailing whitespace, empty tags are
    filtered, and duplicates are removed (first-occurrence kept). This
    is symmetric with read_tags so a write-then-read roundtrip is
    idempotent.

Concurrency:
    The OS handles same-file write serialization. If two threads call
    write_tags on the same path simultaneously, the last call's
    os.replace wins; neither produces a partially-written file. The
    state model is responsible for ordering edits at a higher level —
    this module makes no attempt to lock.

Error contract:
    read_tags raises FileNotFoundError / PermissionError / OSError on
    failure. The scanner wraps these to tolerate transient errors.
    write_tags raises OSError variants on failure; callers should
    surface these to the user since a failed write is unrecoverable
    silently.
"""

from __future__ import annotations

import os
import re as _re_module
from pathlib import Path
from typing import Sequence


# On-disk format constants. Kept here so the canonical format is
# documented in code, and any future change is a one-line edit.
TAG_SEPARATOR: str = ", "
LINE_ENDING: str = "\n"
ENCODING_READ: str = "utf-8-sig"   # transparently strips UTF-8 BOM
ENCODING_WRITE: str = "utf-8"      # we never emit a BOM ourselves

# Tried in order, strictly, before giving up and replacing bad bytes.
#
# Reading with errors="replace" alone was silent data loss: a caption
# saved by Windows Notepad in its ANSI mode is CP932 on a Japanese
# system, decoded to U+FFFD here, and then written BACK as UTF-8 the
# next time a tag was edited — destroying the original Japanese with
# no warning and no way to recover it.
#
# Valid UTF-8 always wins because it is tried first; CP932 only gets a
# look at bytes UTF-8 has already rejected.
CAPTION_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp932")

# Produced by a failed decode. A tag containing one cannot have come
# from the user, and writing it would make the damage permanent.
REPLACEMENT_CHAR = "\ufffd"


_SEPARATORS = _re_module.compile(r"[,\r\n]")


def decode_caption(path: Path) -> tuple[str, str]:
    """(text, encoding used). The encoding is "utf-8 (lossy)" when
    nothing decoded cleanly and bytes had to be replaced."""
    data = path.read_bytes()
    # A byte-order mark is unambiguous, and must be checked before the
    # fallback chain: CP932 will happily decode UTF-16 bytes into
    # private-use gibberish rather than failing, which would then be
    # written back as UTF-8 and destroy the file.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        # The plain "utf-16" codec reads the mark for endianness and
        # consumes it; utf-16-le would leave U+FEFF glued to the first
        # tag.
        return data.decode("utf-16", errors="replace"), "utf-16"
    for encoding in CAPTION_ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8 (lossy)"


# Characters a single tag value may never contain, because they would
# break the on-disk caption format and corrupt the next read:
# - comma separates tags, so a comma inside a value splits it in two;
# - newline / carriage return are the line terminator, so a value
#   containing one would span lines and read back as a mangled tag.
# Booru-style tags never contain these. The state model validates
# user-supplied tag content against this set (via is_valid_tag) before
# calling write_tags, so the format stays parseable round-trip.
_FORMAT_BREAKING_CHARS: frozenset[str] = frozenset({",", "\n", "\r"})


def is_valid_tag(tag: str) -> bool:
    """True if `tag` is non-empty (after stripping) and contains no
    character that would break the caption file format on write/read.

    This is the single source of truth for "may this string be written
    as a tag". Callers in the state model use it to reject malformed
    user input (e.g. a tag typed with a comma or pasted with a newline)
    before it can corrupt a caption file. tag_io itself trusts its
    input on the write path — validation is the caller's job, done
    through this function.
    """
    stripped = tag.strip()
    if not stripped:
        return False
    return not any(c in stripped for c in _FORMAT_BREAKING_CHARS)


def _sanitize_tags(tags: Sequence[str]) -> list[str]:
    """Normalize a caller-supplied tag sequence for writing.

    - Strips leading and trailing whitespace from each tag.
    - Drops tags that become empty after stripping.
    - Deduplicates while preserving first-occurrence order.

    This is the inverse of the parse in read_tags; running
    read_tags(write_tags(x)) yields the same sequence in the same
    order after one round.

    Note on format-breaking characters inside tag values: booru-style
    tagging does not use comma/newline as internal characters. We do
    NOT scrub them here, because doing so silently would munge data the
    caller may have provided intentionally. If a caller passes such a
    tag, the file format becomes ambiguous on the next read. The state
    model is responsible for validating tag content (via is_valid_tag)
    before calling write_tags; tag_io trusts its input.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = raw.strip()
        if tag and tag not in seen:
            seen.add(tag)
            cleaned.append(tag)
    return cleaned


def read_tags(path: Path) -> list[str]:
    """Read a caption file and return its tag list.

    Parameters
    ----------
    path
        Absolute path to a .txt caption file.

    Returns
    -------
    list[str]
        Tags in file order, with duplicates removed and whitespace
        stripped. Empty list if the file is empty or contains only
        separators / whitespace.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    PermissionError
        If the file exists but cannot be read.
    OSError
        For other I/O failures.

    Notes
    -----
    - UTF-8 BOM is stripped transparently (some editors save with one).
    - Invalid byte sequences are replaced with U+FFFD rather than
      raising. A single corrupt file should not crash an audit session;
      the user will see the replacement character in the UI and can
      investigate.
    """
    text, _encoding = decode_caption(path)
    text = text.strip()
    if not text:
        return []
    seen: set[str] = set()
    result: list[str] = []
    # Newlines separate too. Splitting on commas alone left a caption
    # wrapped across lines as one tag containing a line break —
    # "solo\nsmile" instead of "solo" and "smile".
    for raw in _SEPARATORS.split(text):
        # U+FEFF is not whitespace to str.strip, so a stray byte-order
        # mark would otherwise ride along on a tag.
        tag = raw.strip().strip("\ufeff").strip()
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def read_tags_raw(path: Path) -> list[str]:
    """Read a caption file WITHOUT deduplicating.

    Same parsing as read_tags (UTF-8/BOM handling, whitespace strip,
    empty-tag drop) but preserves repeated tags in file order. This is
    the only way to observe duplicate tags as they actually exist on
    disk, because read_tags collapses them on load — so the rest of the
    app never sees a duplicate, and any normal rewrite silently drops
    them. The de-duplication maintenance tool uses this to detect which
    files genuinely contain duplicates before rewriting anything.

    Returns tags in file order with repeats intact. Same Raises contract
    as read_tags.
    """
    text, _encoding = decode_caption(path)
    text = text.strip()
    if not text:
        return []
    result: list[str] = []
    for raw in _SEPARATORS.split(text):
        tag = raw.strip()
        if tag:
            result.append(tag)
    return result


def write_tags(path: Path, tags: Sequence[str]) -> None:
    """Atomically write a tag list to a caption file.

    Parameters
    ----------
    path
        Absolute path of the .txt caption file to write. If it does
        not exist it will be created. The parent directory must
        exist; this function does not create directories.
    tags
        Iterable of tag strings. Sanitized before writing per
        _sanitize_tags. An empty (or all-empty-strings) sequence
        produces a zero-length file, not a deleted file — this is
        intentional. An empty caption file signals "reviewed, no
        applicable tags," distinct from an orphan image with no
        caption file at all.

    Raises
    ------
    FileNotFoundError
        If the parent directory does not exist.
    PermissionError
        If the file or its parent is not writable.
    OSError
        For other I/O failures (disk full, etc.).

    Atomicity
    ---------
    Implementation writes to a tempfile in the same directory, then
    uses os.replace to swap it into place. os.replace is atomic on
    Windows NTFS and POSIX filesystems for same-directory paths.
    A reader who opens `path` either sees the complete previous
    content or the complete new content, never a mix. The tempfile
    is removed on any failure.
    """
    target = Path(path)
    parent = target.parent

    # Parent must exist. Creating directories silently from a write
    # function would hide real bugs (e.g. user pointed the app at the
    # wrong drive and we'd quietly start scattering files).
    if not parent.exists():
        raise FileNotFoundError(
            f"Parent directory does not exist: {parent}"
        )

    cleaned = _sanitize_tags(tags)
    content = TAG_SEPARATOR.join(cleaned) + LINE_ENDING if cleaned else ""

    # Tempfile name lives in the same directory so os.replace is atomic.
    # Random suffix avoids collisions between concurrent writes from
    # different threads, and the ".tw_tmp_" prefix means partial files
    # are visually identifiable if something ever goes wrong.
    tmp_name = f".tw_tmp_{os.urandom(6).hex()}.txt"
    tmp_path = parent / tmp_name

    if any(REPLACEMENT_CHAR in tag for tag in tags):
        # These bytes could not be decoded when the file was read.
        # Writing them would replace the user's original text with
        # question marks, permanently. Refuse instead.
        raise ValueError(
            f"refusing to write {path.name}: its existing contents "
            "could not be decoded, so saving would destroy them. "
            "Re-save the file as UTF-8 and try again.")
    try:
        # newline="" disables Python's universal-newline translation
        # on Windows. We want the exact bytes we asked for ("\n"),
        # not "\r\n" — keeps caption files diff-clean and consistent
        # across platforms.
        with open(tmp_path, "w", encoding=ENCODING_WRITE, newline="") as f:
            f.write(content)
            # Flush Python's buffer to the OS, then ask the OS to flush
            # to physical storage before we rename. os.replace below is
            # atomic regardless (a reader sees old or new, never torn),
            # but without fsync a power loss right after the rename can
            # leave the durably-renamed name pointing at not-yet-flushed
            # (zero/stale) data on some filesystems. fsync closes that
            # window for the caption data — the user's actual work.
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # Some filesystems / handles don't support fsync. The
                # atomicity guarantee from os.replace still holds, so a
                # missing fsync only forgoes the extra power-loss
                # durability — never a reason to fail the write.
                pass
        # The single atomic step. After this line, target reflects
        # the new content; before it, target reflects the old content.
        os.replace(tmp_path, target)
    except BaseException:
        # Best-effort cleanup of the tempfile. We catch BaseException
        # (not just Exception) so KeyboardInterrupt mid-write also
        # cleans up. The except re-raises after cleanup.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
