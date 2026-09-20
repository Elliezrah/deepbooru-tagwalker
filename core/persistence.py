"""
core/persistence.py

Save and load TagWalker session state to/from JSON files.

Three public operations:

    save(state, file_path)          ->  None
    load(file_path)                 ->  LoadedSession
    apply_to_state(loaded, state)   ->  ReconciliationReport

Why three functions instead of one "save + load + apply in one":
- Loading is pure parsing of a file (no side effects on running state).
- The UI needs to inspect the loaded data BEFORE applying it — in
  particular to check whether the saved dataset root matches the
  currently-open root, and ask the user how to proceed if they differ.
- Applying loaded data to the running state is a separate concern,
  and it returns a structured report so the UI can show the
  reconciliation summary ("12 new images found, 3 missing...").

File format
-----------
JSON. Schema is documented inline in the save() function and
versioned via a `format_version` field. Image paths inside the file
are stored as POSIX-style relative paths from the saved root, so a
session file is portable: rename or move the dataset folder, load
the same session against the new location, all references still
resolve.

The file contains both a full image inventory and a full tag inventory
at save time. This is what makes the "new images found / images
deleted" reconciliation counts possible — without those snapshots
we could only detect deletions implied by orphaned decisions, not
plain additions to the dataset.

Atomic save
-----------
Same temp-then-replace pattern used by tag_io.py. A crashed or
killed app never leaves a half-written session file. The original
session file is untouched until os.replace succeeds.

Drift reconciliation
--------------------
When loading a session against a dataset that has changed:

- Images deleted since save  -> decisions for those images dropped silently
- Images added since save    -> treated as pending; counted in report
- Tags that disappeared      -> any stored skipped-tag status dropped
- New tags discovered        -> default PENDING; counted in report
- Walk position stale        -> if tag or image gone, position is cleared

The report describes exactly what was reconciled so the UI can show
the user a one-shot summary dialog without asking for confirmation
per item.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.state import (
    Decision, FilterMode, SessionState, SortMode, TagSortMode, TagStatus,
)


# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

CURRENT_FORMAT_VERSION: int = 1
# Version 2 = multi-folder sessions (field feature). Single-folder
# sessions keep writing version 1 byte-compatibly, so older builds
# still open them; only multi-root sessions need the new format.
MULTI_FORMAT_VERSION: int = 2
SUPPORTED_VERSIONS: frozenset[int] = frozenset({1, 2})
SUGGESTED_EXTENSION: str = ".tagwalker.json"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidSessionFileError(ValueError):
    """The file is not a valid TagWalker session file.

    Raised on:
    - Malformed JSON
    - Missing required top-level keys
    - format_version absent or not an integer
    - Unsupported format_version
    - Required field has wrong type (e.g. decisions is a dict, not list)

    Code that catches this should treat the file as unloadable and
    inform the user. There is no automatic repair.
    """


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class LoadedSession:
    """Raw parsed contents of a session file.

    Not yet applied to any running state. The UI inspects fields like
    `saved_root` to decide whether to confirm with the user before
    proceeding to apply_to_state.
    """
    format_version: int
    saved_at: str
    saved_root: Path
    image_inventory: list[str]   # relative POSIX paths at save time
    tag_inventory: list[str]
    decisions: list[tuple[str, str, str]]  # (rel_path, tag, decision_value)
    tag_status_overrides: dict[str, str]   # tag -> status value (e.g. "skipped")
    walk_position: Optional[tuple[str, str]]  # (tag, image_rel_path) or None
    ui_preferences: dict[str, Any]
    front_locked_tokens: list[str] = field(default_factory=list)
    saved_roots: list[Path] = field(default_factory=list)


@dataclass
class ReconciliationReport:
    """Summary of what changed between saved state and current state.

    All counts and lists are non-destructive — they describe what
    apply_to_state already did. The UI presents them in a one-shot
    summary dialog.
    """
    decisions_restored: int = 0
    decisions_discarded_missing_image: int = 0
    decisions_discarded_missing_tag: int = 0
    new_images: list[str] = field(default_factory=list)        # rel paths
    missing_images: list[str] = field(default_factory=list)    # rel paths
    new_tags: list[str] = field(default_factory=list)
    disappeared_tags: list[str] = field(default_factory=list)
    walk_position_restored: bool = False
    walk_position_was_set: bool = False  # whether the saved file had one
    pending_tags_remaining: int = 0


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _rel_path_from(image_path: Path, root: Path) -> str:
    """Convert an absolute image path to a POSIX-style relative path
    rooted at `root`. Always uses '/' regardless of OS so the saved
    file is portable across Windows and POSIX.
    """
    try:
        rel = image_path.relative_to(root)
        return rel.as_posix()
    except ValueError:
        # Image is not under root (unexpected — every image was
        # discovered under root by the scanner). Fall back to the
        # filename only; reconciliation will treat it as missing.
        return image_path.name


def _canonical_roots(roots) -> list[Path]:
    """Roots in a canonical (sorted-by-string) order, so the same set
    of folders encodes identically regardless of the order the user
    opened them in — session files stay matchable across reopens."""
    return sorted((Path(r) for r in roots), key=lambda q: str(q))


def _encode_rel(image_path: Path, roots) -> str:
    """Encode an image path relative to its owning root. Single root:
    the classic v1 rel-posix string (unchanged). Multiple roots:
    "{index}:{rel}" with the index into the CANONICALLY SORTED roots.
    Used by BOTH serialization and apply-side matching, so the two
    sides can never disagree."""
    canon = _canonical_roots(roots)
    if len(canon) == 1:
        return _rel_path_from(image_path, canon[0])
    for i, r in enumerate(canon):
        try:
            return f"{i}:{image_path.relative_to(r).as_posix()}"
        except ValueError:
            continue
    return f"?:{image_path.name}"


def _resolve_rel_path(rel: str, root: Path) -> Path:
    """Inverse of _rel_path_from. Always works regardless of OS, since
    we normalize forward slashes to the OS separator via Path()."""
    return root / rel


def _serialize(state: SessionState) -> dict[str, Any]:
    """Build the JSON-serializable dictionary representing `state`.

    Schema (format_version 1):

        format_version       int, currently 1
        saved_at             ISO 8601 datetime string (informational)
        tagwalker_version    string for forward-compat debugging
        root_directory       absolute path (string, for portability check)
        image_inventory      list of rel-path strings (one per image)
        tag_inventory        list of all known tag names
        decisions            list of [rel_path, tag, decision_value]
        tag_status_overrides {tag: status_value}  (only SKIPPED tags)
        walk_position        {tag, image_relative_path} | null
        ui_preferences       {filter_mode, sort_mode, show_orphans}
    """
    root = state.root
    roots = getattr(state, "roots", None) or [root]
    multi = len(_canonical_roots(roots)) > 1

    image_inventory = [
        _encode_rel(img.image_path, roots)
        for img in state.all_images
    ]
    tag_inventory = state.all_tags

    decisions: list[list[str]] = []
    for image_path, tag, decision in state.iter_decisions():
        decisions.append([
            _encode_rel(image_path, roots),
            tag,
            decision.value,
        ])

    tag_status_overrides: dict[str, str] = {}
    # Persist BOTH skipped and manually-completed tags. Previously only
    # skipped tags were saved (via iter_skipped_tags), so manual
    # completions were silently lost on save/load. iter_tag_status_
    # overrides yields the right token for each ("skipped" or
    # "manually_completed"); apply_to_state restores them on load.
    for tag, override_value in state.iter_tag_status_overrides():
        tag_status_overrides[tag] = override_value

    walk_position: Optional[dict[str, str]] = None
    cur_tag = state.current_tag
    cur_img = state.current_image
    if cur_tag is not None and cur_img is not None:
        walk_position = {
            "tag": cur_tag,
            "image_relative_path": _encode_rel(cur_img.image_path, roots),
        }

    return {
        "format_version": (
            MULTI_FORMAT_VERSION if multi else CURRENT_FORMAT_VERSION),
        **({"root_directories": [str(q) for q in _canonical_roots(roots)]}
           if multi else {}),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "tagwalker_version": "1.0.0",
        "root_directory": str(root),
        "image_inventory": image_inventory,
        "tag_inventory": tag_inventory,
        "decisions": decisions,
        "tag_status_overrides": tag_status_overrides,
        "walk_position": walk_position,
        "front_locked_tokens": list(state.front_locked_tokens),
        "ui_preferences": {
            "filter_mode": state.filter_mode.value,
            "sort_mode": state.sort_mode.value,
            "show_orphans": state.show_orphans,
            # Additive keys (older readers ignore them; older files load
            # with these absent and fall back to defaults). tag_sort_mode
            # matters beyond cosmetics: it sets the order auto-advance
            # follows, so losing it on load silently changed navigation.
            "tag_sort_mode": state._tag_sort_mode.value,
            "search_locked": state.search_locked,
        },
    }


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def save(state: SessionState, file_path: Path) -> None:
    """Atomically serialize `state` to `file_path`.

    Uses the same write-temp-then-os.replace pattern as tag_io.py.
    A crash mid-write never leaves a partially-written session file:
    the original is intact, the temp gets cleaned up. After success,
    the file at file_path is a complete, valid session snapshot.

    Parameters
    ----------
    state : SessionState
        Live session whose state should be captured. Read-only access.
    file_path : Path
        Destination file. Parent directory must exist. Will be
        overwritten if it already exists.

    Raises
    ------
    FileNotFoundError
        Parent directory does not exist.
    PermissionError
        File or parent is not writable.
    OSError
        Other I/O failures (disk full, etc.).
    """
    target = Path(file_path)
    parent = target.parent
    if not parent.exists():
        raise FileNotFoundError(
            f"Parent directory does not exist: {parent}"
        )

    data = _serialize(state)
    # ensure_ascii=False keeps Unicode (e.g. Japanese tag names) as
    # actual characters in the file, not escape sequences. The file
    # is UTF-8, which represents anything.
    json_text = json.dumps(data, ensure_ascii=False, indent=2)
    json_bytes = json_text.encode("utf-8")

    tmp_name = f".tw_session_tmp_{os.urandom(6).hex()}.json"
    tmp_path = parent / tmp_name
    try:
        # Explicit open (rather than write_bytes) so we can fsync the
        # file descriptor before the atomic rename. The session file
        # holds the user's accumulated review work; os.replace already
        # guarantees no torn file, but fsync ensures a save survives a
        # power loss, not just an orderly app crash. Cost is negligible
        # here — the session file is written infrequently (explicit save
        # or the autosave interval), not in any hot loop.
        with open(tmp_path, "wb") as f:
            f.write(json_bytes)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync unsupported on this fs/handle: atomicity from
                # os.replace still holds; only extra durability is lost.
                pass
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Load — pure parsing, no state mutation
# ---------------------------------------------------------------------------


def load(file_path: Path) -> LoadedSession:
    """Parse a session file and return its contents.

    Does NOT modify any state. Does NOT compare against any current
    dataset. Pure deserialization with structural validation.

    Parameters
    ----------
    file_path : Path
        Path to a session JSON file.

    Returns
    -------
    LoadedSession
        Parsed contents. Caller is responsible for next steps (path
        mismatch check, reconciliation via apply_to_state).

    Raises
    ------
    FileNotFoundError
        File does not exist.
    PermissionError
        File exists but cannot be read.
    InvalidSessionFileError
        File is not a valid TagWalker session: malformed JSON,
        missing required fields, wrong types, or unsupported
        format_version.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise InvalidSessionFileError(
            f"File is not valid UTF-8: {e}"
        ) from e

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidSessionFileError(
            f"Malformed JSON: {e}"
        ) from e

    if not isinstance(data, dict):
        raise InvalidSessionFileError(
            "Top-level JSON value must be an object"
        )

    # Version check first. If we don't recognize the version, fail
    # before attempting to interpret any other field.
    fv = data.get("format_version")
    if not isinstance(fv, int):
        raise InvalidSessionFileError(
            "Missing or invalid 'format_version' field"
        )
    if fv not in SUPPORTED_VERSIONS:
        raise InvalidSessionFileError(
            f"Unsupported format_version {fv}. "
            f"This TagWalker supports: {sorted(SUPPORTED_VERSIONS)}"
        )

    # Required string fields. We tolerate older versions missing the
    # informational fields ("saved_at", "tagwalker_version") but
    # require structural ones.
    saved_at = data.get("saved_at", "")
    if not isinstance(saved_at, str):
        saved_at = ""

    root_str = data.get("root_directory")
    if not isinstance(root_str, str):
        raise InvalidSessionFileError(
            "Missing or invalid 'root_directory' field"
        )
    saved_root = Path(root_str)

    image_inventory = data.get("image_inventory", [])
    if not isinstance(image_inventory, list):
        raise InvalidSessionFileError(
            "'image_inventory' must be a list"
        )
    image_inventory = [s for s in image_inventory if isinstance(s, str)]

    tag_inventory = data.get("tag_inventory", [])
    if not isinstance(tag_inventory, list):
        raise InvalidSessionFileError("'tag_inventory' must be a list")
    tag_inventory = [s for s in tag_inventory if isinstance(s, str)]

    # Decisions: list of [rel_path, tag, decision_value] tuples.
    # We validate the shape of each entry and silently drop malformed
    # ones rather than failing the whole load.
    raw_decisions = data.get("decisions", [])
    if not isinstance(raw_decisions, list):
        raise InvalidSessionFileError("'decisions' must be a list")
    decisions: list[tuple[str, str, str]] = []
    for entry in raw_decisions:
        if (isinstance(entry, list) and len(entry) == 3
                and all(isinstance(x, str) for x in entry)):
            decisions.append((entry[0], entry[1], entry[2]))

    # Tag status overrides.
    raw_overrides = data.get("tag_status_overrides", {})
    if not isinstance(raw_overrides, dict):
        raise InvalidSessionFileError(
            "'tag_status_overrides' must be an object"
        )
    overrides: dict[str, str] = {}
    for k, v in raw_overrides.items():
        if isinstance(k, str) and isinstance(v, str):
            overrides[k] = v

    # Walk position is optional.
    walk_position: Optional[tuple[str, str]] = None
    raw_position = data.get("walk_position")
    if isinstance(raw_position, dict):
        tag = raw_position.get("tag")
        rel = raw_position.get("image_relative_path")
        if isinstance(tag, str) and isinstance(rel, str):
            walk_position = (tag, rel)

    # UI preferences are best-effort.
    raw_prefs = data.get("ui_preferences", {})
    if not isinstance(raw_prefs, dict):
        raw_prefs = {}

    return LoadedSession(
        format_version=fv,
        saved_at=saved_at,
        saved_root=saved_root,
        image_inventory=image_inventory,
        tag_inventory=tag_inventory,
        decisions=decisions,
        tag_status_overrides=overrides,
        walk_position=walk_position,
        ui_preferences=raw_prefs,
        saved_roots=[
            Path(q) for q in data.get("root_directories", [])
        ] or [saved_root],
        front_locked_tokens=[
            str(t) for t in data.get("front_locked_tokens", [])
            if isinstance(t, str) and t.strip()
        ],
    )


# ---------------------------------------------------------------------------
# Apply — reconcile loaded data with running state
# ---------------------------------------------------------------------------


def _parse_decision(value: str) -> Optional[Decision]:
    """Map a string back to a Decision enum, or None if unrecognized."""
    for d in Decision:
        if d.value == value:
            return d
    return None


def _parse_filter_mode(value: str) -> Optional[FilterMode]:
    for m in FilterMode:
        if m.value == value:
            return m
    return None


def _parse_sort_mode(value: str) -> Optional[SortMode]:
    for m in SortMode:
        if m.value == value:
            return m
    return None


def _parse_tag_sort_mode(value: str) -> Optional[TagSortMode]:
    for m in TagSortMode:
        if m.value == value:
            return m
    return None


def apply_to_state(
    loaded: LoadedSession,
    state: SessionState,
) -> ReconciliationReport:
    """Apply a loaded session, REPLACING the target's review state.

    Despite the historical "fresh SessionState" wording, the live app
    loads sessions into the existing state object. So this function
    first clears all review state (decisions, manual completions, tag
    statuses, walk position, undo stack) via state.clear_review_state(),
    then restores from the loaded file. Without that clear step, the
    user's current unsaved decisions would merge into the loaded
    session and contaminate it.

    Reconciliation philosophy: silent, but reported. Per the design
    discussion, the user should not have to confirm each individual
    drift case. We do the best we can — drop stale references, keep
    everything we can match — and emit a single summary report. The
    UI shows that report as a one-shot informational dialog.

    The caller is responsible for the path-mismatch check (comparing
    loaded.saved_root to state.root). If the user has confirmed they
    want to proceed despite a path mismatch, just call this function
    normally — same-folder reconciliation logic still applies and
    handles mismatched references the same way it handles deletions.

    Order of operations matters:
    0. Clear existing review state so the load replaces rather than
       merges.
    1. UI preferences (filter / sort / orphan toggle) — must be set
       before walk position restoration, since the queue depends on
       these settings.
    2. Tag status overrides (SKIPPED) — restored before decisions
       so the recalculate pass at step 4 preserves them.
    3. Per-decision restoration — drops references to missing
       images/tags, counting them for the report.
    4. Recalculate tag completion status.
    5. Walk position restoration — depends on everything above.

    Returns
    -------
    ReconciliationReport
        Counts and lists describing what was reconciled. The UI uses
        this to render the post-load summary dialog.
    """
    report = ReconciliationReport()
    root = state.root

    # ---- 0. Clear existing review state ------------------------------
    # A load REPLACES, never merges. Wipe decisions / manual
    # completions / statuses / walk / undo before restoring, so the
    # user's current unsaved work can't bleed into the loaded session.
    state.clear_review_state()
    state.set_front_locked_tokens(list(loaded.front_locked_tokens))

    # ---- 1. UI preferences --------------------------------------------
    prefs = loaded.ui_preferences
    fm = _parse_filter_mode(prefs.get("filter_mode", ""))
    if fm is not None:
        state.set_filter_mode(fm)
    sm = _parse_sort_mode(prefs.get("sort_mode", ""))
    if sm is not None:
        state.set_sort_mode(sm)
    show_orphans = prefs.get("show_orphans")
    if isinstance(show_orphans, bool):
        state.set_show_orphans(show_orphans)
    # Additive prefs. Absent in older files, so guarded: only applied
    # when present and valid, otherwise the state keeps its default.
    tsm = _parse_tag_sort_mode(prefs.get("tag_sort_mode", ""))
    if tsm is not None:
        state.set_tag_sort_mode(tsm)
    search_locked = prefs.get("search_locked")
    if isinstance(search_locked, bool):
        state.set_search_locked(search_locked)

    # ---- 2. Tag status overrides --------------------------------------
    # We restore SKIPPED tags directly. Other statuses (PENDING,
    # COMPLETED) are derived from decisions and computed in step 4.
    saved_tag_set = set(loaded.tag_inventory)
    current_tag_set = set(state.all_tags)

    report.new_tags = sorted(current_tag_set - saved_tag_set)
    report.disappeared_tags = sorted(saved_tag_set - current_tag_set)

    for tag, status_value in loaded.tag_status_overrides.items():
        if tag not in current_tag_set:
            # Tag no longer exists; drop the override silently.
            continue
        if status_value == TagStatus.SKIPPED.value:
            state.restore_tag_status(tag, TagStatus.SKIPPED)
        elif status_value == "manually_completed":
            # Restore the manual-completion marker so the tag stays
            # COMPLETED and counts as fully decided in project stats.
            state.restore_manual_completion(tag)

    # ---- 3. Decisions ------------------------------------------------
    # Build a set of current image relative paths for fast membership
    # checks. Resolution to absolute Path is done once per saved entry,
    # so this is O(N_decisions) overall, not O(N_decisions x N_images).
    current_rel_paths: dict[str, Path] = {}
    for img in state.all_images:
        current_rel_paths[_encode_rel(
            img.image_path, getattr(state, 'roots', None) or [root]
        )] = img.image_path

    saved_rel_paths = set(loaded.image_inventory)
    report.new_images = sorted(set(current_rel_paths.keys()) - saved_rel_paths)
    report.missing_images = sorted(saved_rel_paths - set(current_rel_paths.keys()))

    for rel, tag, decision_value in loaded.decisions:
        image_path = current_rel_paths.get(rel)
        if image_path is None:
            report.decisions_discarded_missing_image += 1
            continue
        if tag not in current_tag_set:
            report.decisions_discarded_missing_tag += 1
            continue
        decision = _parse_decision(decision_value)
        if decision is None:
            # Unknown decision value (future format?). Skip silently.
            continue
        state.restore_decision(image_path, tag, decision)
        report.decisions_restored += 1

    # ---- 4. Recompute tag statuses (single pass) ---------------------
    state.recalculate_all_tag_statuses()

    # Count pending tags for the report.
    report.pending_tags_remaining = sum(
        1 for t in state.all_tags
        if state.get_tag_status(t) == TagStatus.PENDING
    )

    # ---- 5. Walk position --------------------------------------------
    if loaded.walk_position is not None:
        report.walk_position_was_set = True
        tag, rel = loaded.walk_position
        image_path = current_rel_paths.get(rel)
        if image_path is not None and tag in current_tag_set:
            ok = state.restore_walk_position(tag, image_path)
            report.walk_position_restored = ok
        # else: silently dropped — image or tag no longer exists.

    # ---- 6. Notify listeners -----------------------------------------
    # Always emit a full-refresh signal, even if nothing above happened
    # to change a tag status. recalculate_all_tag_statuses only emits
    # when a status actually flips, so reloading a session whose
    # computed statuses match the current ones (e.g. loading the same
    # file twice, or re-loading after no edits) would otherwise leave
    # every attached widget — the stats window especially — showing
    # stale numbers. This unconditional emit guarantees the UI reflects
    # the freshly-loaded decisions.
    state.notify_session_reloaded()

    return report
