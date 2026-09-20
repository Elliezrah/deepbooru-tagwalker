"""
core/dataset_marker.py

Marks over-limit captions by RENAMING their files, so that sorting a
folder by name in the file manager floats every problem image to the
top and the whole set can be judged at a glance.

`image23.png` + `image23.txt`  ->  `!image23.png` + `!image23.txt`

Why "!" and not "*": `*` is a reserved character in Windows filenames
(along with < > : " / \\ | ? *) — a file cannot be named that at all.
`!` is legal on every platform, sorts above letters and digits in
Windows Explorer, and is the long-standing convention for pinning an
entry to the top of a listing. It is a module constant, so a toolchain
that dislikes it can be accommodated by changing one line.

This module renames files on disk — the most destructive thing the
application does — so the work is split in two: `plan_marks` decides
and returns a plan that can be shown to the user and asserted against
in tests, and `apply_plan` performs it. Nothing is written during
planning.

Three hazards are handled explicitly:

- An image and its caption must move TOGETHER or the trainer's
  stem-based pairing breaks. If the caption rename fails, the image
  rename is rolled back and the pair is reported as skipped.
- Two images can resolve to one caption file (foo.png and foo.jpg both
  pair with foo.txt — the scanner reports these as caption_collisions).
  Renaming the caption would orphan the other image, so anything in a
  collision group is refused.
- The operation is a SYNC, not a one-way stamp: a caption pruned back
  under the limit has its mark removed on the next run. Re-running
  after editing therefore leaves the marks true rather than stale.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Legal everywhere, sorts to the top of a Windows Explorer listing.
MARK_PREFIX = "!"


def is_marked(name: str) -> bool:
    return name.startswith(MARK_PREFIX)


def marked_name(name: str) -> str:
    return name if is_marked(name) else MARK_PREFIX + name


def unmarked_name(name: str) -> str:
    """Strips ONE prefix. Repeated marking is prevented by
    `marked_name`, so a run of them would have to be hand-made, and
    guessing how many the user meant to keep would be worse than
    leaving the rest alone."""
    return name[len(MARK_PREFIX):] if is_marked(name) else name


@dataclass
class MarkPlan:
    """What would change. Produced without touching the disk."""

    to_mark: list[tuple[Path, Path]] = field(default_factory=list)
    to_unmark: list[tuple[Path, Path]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.to_mark or self.to_unmark)

    def summary(self) -> str:
        bits = []
        if self.to_mark:
            bits.append(f"{len(self.to_mark)} to mark")
        if self.to_unmark:
            bits.append(f"{len(self.to_unmark)} to unmark")
        if self.skipped:
            bits.append(f"{len(self.skipped)} skipped")
        return ", ".join(bits) if bits else "nothing to do"


@dataclass
class MarkResult:
    marked: int = 0
    unmarked: int = 0
    skipped: list[tuple[Path, str]] = field(default_factory=list)


def plan_marks(
    pairs,
    over_limit,
    collision_names=(),
) -> MarkPlan:
    """Decide the renames.

    pairs           : iterable of (image_path, txt_path)
    over_limit      : container of image_paths whose caption is over
                      the selected threshold
    collision_names : image FILENAMES that share a caption file with
                      another image (scanner's caption_collisions)
    """
    over = set(over_limit or ())
    collisions = set(collision_names or ())
    plan = MarkPlan()
    for image_path, txt_path in pairs:
        image_path = Path(image_path)
        txt_path = Path(txt_path)
        name = image_path.name
        if name in collisions:
            plan.skipped.append(
                (image_path,
                 "shares one caption file with another image"))
            continue
        currently = is_marked(name)
        should = image_path in over
        if should and not currently:
            plan.to_mark.append((image_path, txt_path))
        elif currently and not should:
            plan.to_unmark.append((image_path, txt_path))
    return plan


def _rename_pair(image_path: Path, txt_path: Path,
                 new_image: Path, new_txt: Path) -> str | None:
    """Move an image and its caption together. Returns a reason on
    refusal or failure, None on success."""
    if new_image.exists():
        return f"a file named {new_image.name} already exists"
    had_txt = txt_path.exists()
    if had_txt and new_txt.exists():
        return f"a file named {new_txt.name} already exists"
    try:
        os.rename(image_path, new_image)
    except OSError as exc:
        return f"could not rename the image ({exc.strerror or exc})"
    if not had_txt:
        return None
    try:
        os.rename(txt_path, new_txt)
    except OSError as exc:
        # Put the image back: a renamed image with an unrenamed
        # caption is a broken pair, which is worse than not marking.
        try:
            os.rename(new_image, image_path)
        except OSError:
            return ("the image was renamed but its caption could not "
                    "be, and the image could not be put back — this "
                    "pair needs fixing by hand")
        return f"could not rename the caption ({exc.strerror or exc})"
    return None


def apply_plan(plan: MarkPlan) -> MarkResult:
    """Perform the plan. Per-pair failures are collected rather than
    aborting, so one locked file does not strand the rest."""
    result = MarkResult(skipped=list(plan.skipped))
    for image_path, txt_path in plan.to_mark:
        reason = _rename_pair(
            image_path, txt_path,
            image_path.with_name(marked_name(image_path.name)),
            txt_path.with_name(marked_name(txt_path.name)))
        if reason:
            result.skipped.append((image_path, reason))
        else:
            result.marked += 1
    for image_path, txt_path in plan.to_unmark:
        reason = _rename_pair(
            image_path, txt_path,
            image_path.with_name(unmarked_name(image_path.name)),
            txt_path.with_name(unmarked_name(txt_path.name)))
        if reason:
            result.skipped.append((image_path, reason))
        else:
            result.unmarked += 1
    return result
