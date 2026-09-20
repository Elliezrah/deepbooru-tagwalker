"""
core/image_prep.py

Bucket arithmetic and folder analysis for the image editor.

Writes nothing and imports no UI. Everything here is a measurement:
which bucket an image belongs in, how much would have to be cut to
get it there, and what a whole folder looks like against a chosen
target. The editor decides what to DO with those answers.

Kept Qt-free so the arithmetic can be tested without a window, and
because getting bucket sizes wrong would make every downstream
recommendation wrong in a way no amount of UI polish would show.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Trainers round bucket dimensions to a multiple of this. 64 is the
# kohya default and what SDXL-era configs almost always use.
BUCKET_STEP = 64

# Below this, a bucket is too small to be worth training at.
MIN_BUCKET = 256

# Widest bucket allowed, as a ratio of long side to short. Past 2:1 a
# bucket holds so few images that it trains erratically.
MAX_BUCKET_RATIO = 2.0

# Target resolutions offered in the editor. The number is the base
# from which buckets are derived: area stays near base squared.
COMMON_TARGETS: list[tuple[int, str]] = [
    (2048, "2048 \u2014 Flux"),
    (1536, "1536 \u2014 recent Illustrious"),
    (1024, "1024 \u2014 SDXL, Illustrious, NoobAI, Pony"),
    (768, "768 \u2014 SD 2.1, some SD 1.5 setups"),
    (512, "512 \u2014 SD 1.5"),
]
DEFAULT_TARGET = 1024

# How much an image may be enlarged before padding is the better
# answer. At 15% linear, LANCZOS resampling is visually near-lossless;
# beyond it, invented detail starts to show.
UPSCALE_LIMIT = 0.15

VERDICT_EXACT = "exact"          # already a bucket size
VERDICT_DOWNSCALE = "downscale"  # bigger than needed, shrink to fit
VERDICT_CROP = "crop"            # wrong shape, must lose some edge
VERDICT_UPSCALE = "upscale"      # slightly small, enlarge within limit
VERDICT_PAD = "pad"              # one side meets; bars on the other two
VERDICT_TOO_SMALL = "too_small"  # neither side meets; unusable as-is
VERDICT_HUGE = "huge"            # far larger than any bucket
VERDICT_UNREADABLE = "unreadable"

# An image this many times a bucket's area is worth flagging on its
# own: shrinking it that far throws away most of what was captured,
# and it is usually a sign the source was never meant for training.
HUGE_AREA_FACTOR = 6.0


def buckets_for(base: int = DEFAULT_TARGET, step: int = BUCKET_STEP,
                min_side: int = MIN_BUCKET,
                max_ratio: float = MAX_BUCKET_RATIO
                ) -> list[tuple[int, int]]:
    """Every bucket a trainer would generate for this base resolution.

    Derived rather than hard-coded, so a non-standard base or step
    still produces the right answer. For base 1024 this yields the
    familiar set: 1024x1024, 1152x896, 1216x832, 1344x768 and their
    transposes.
    """
    found = set()
    area = base * base
    width = min_side
    while width <= int(base * max_ratio):
        height = (area // width) // step * step
        if height >= min_side:
            long_side, short_side = max(width, height), min(width, height)
            if short_side and long_side / short_side <= max_ratio:
                found.add((width, height))
        width += step
    return sorted(found, key=lambda wh: (-(wh[0] * wh[1]), wh[0]))


def _crop_loss(width: int, height: int, bucket: tuple[int, int]) -> float:
    """Fraction of the image lost cropping it to the bucket's shape.

    Only shape matters, not size: scaling costs nothing in area terms,
    so what is lost is whatever the aspect mismatch forces off the
    edges.
    """
    if not width or not height:
        return 1.0
    source = width / height
    target = bucket[0] / bucket[1]
    if source > target:
        kept = target / source        # too wide: lose left and right
    else:
        kept = source / target        # too tall: lose top and bottom
    return max(0.0, 1.0 - kept)


def best_bucket(width: int, height: int,
                base: int = DEFAULT_TARGET) -> tuple[int, int]:
    """The bucket costing the least crop, preferring the larger of two
    that tie so detail is kept."""
    options = buckets_for(base)
    if not options:
        return (base, base)
    return min(options,
               key=lambda b: (round(_crop_loss(width, height, b), 4),
                              -(b[0] * b[1])))


@dataclass
class ImagePlan:
    """What one image needs, and why."""

    path: Path
    width: int = 0
    height: int = 0
    bucket: tuple[int, int] = (0, 0)
    verdict: str = VERDICT_UNREADABLE
    crop_loss: float = 0.0        # fraction of area lost to cropping
    scale: float = 1.0            # <1 shrink, >1 enlarge
    note: str = ""

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000


def plan_for(path: Path, width: int, height: int,
             base: int = DEFAULT_TARGET,
             upscale_limit: float = UPSCALE_LIMIT) -> ImagePlan:
    """Decide what this image needs to reach a bucket.

    The order matters. An image is judged too small only after its
    shape has been considered, because a wide image can be short in
    one dimension and still fill a wide bucket perfectly.
    """
    plan = ImagePlan(path=path, width=width, height=height)
    if width <= 0 or height <= 0:
        plan.note = "could not be read"
        return plan

    bucket = best_bucket(width, height, base)
    plan.bucket = bucket
    plan.crop_loss = _crop_loss(width, height, bucket)

    # Scale needed to cover the bucket entirely (the larger of the two
    # ratios, since the shorter side is what runs out first).
    scale = max(bucket[0] / width, bucket[1] / height)
    plan.scale = scale

    if width == bucket[0] and height == bucket[1]:
        plan.verdict = VERDICT_EXACT
        plan.note = "already a bucket size"
        return plan

    # Padding is only honest when ONE side already reaches the target.
    # Scaling to fit then puts bars on the other two sides, and
    # "pillarboxed" or "letterboxed" describes the result exactly.
    #
    # When NEITHER side reaches it, fitting would leave bars on all
    # four — a small picture in a big frame, which no caption
    # describes and which teaches the model a border it will draw
    # back. Such an image is not too awkward to prepare; it is too
    # small to use, and saying so is more useful than offering a bad
    # rendering of it.
    fit_scale = min(bucket[0] / width, bucket[1] / height)
    if fit_scale > 1.0 + upscale_limit:
        plan.verdict = VERDICT_TOO_SMALL
        plan.note = (f"{width}x{height} against "
                     f"{bucket[0]}x{bucket[1]} \u2014 neither side "
                     "reaches the target")
        return plan

    if scale > 1.0 + upscale_limit:
        # One side reaches the target, so bars go on the other two.
        plan.verdict = VERDICT_PAD
        plan.note = (f"one side is short of {bucket[0]}x{bucket[1]}; "
                     "even bars on the other two")
        return plan

    if scale > 1.0:
        plan.verdict = VERDICT_UPSCALE
        plan.note = f"slightly small; enlarge {scale - 1:.0%}"
        return plan

    if (width * height) > HUGE_AREA_FACTOR * bucket[0] * bucket[1]:
        plan.verdict = VERDICT_HUGE
        plan.note = (f"{plan.megapixels:.1f} MP against a "
                     f"{bucket[0] * bucket[1] / 1_000_000:.1f} MP "
                     "bucket \u2014 most of it is discarded")
        return plan

    if plan.crop_loss > 0.005:
        plan.verdict = VERDICT_CROP
        plan.note = (f"shape differs; {plan.crop_loss:.0%} would be "
                     "cropped away")
        return plan

    plan.verdict = VERDICT_DOWNSCALE
    plan.note = "right shape, just larger \u2014 shrink to fit"
    return plan


@dataclass
class FolderReport:
    """What a whole folder looks like against one target."""

    base: int = DEFAULT_TARGET
    plans: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.plans)

    def by_verdict(self) -> dict:
        counts: dict = {}
        for plan in self.plans:
            counts[plan.verdict] = counts.get(plan.verdict, 0) + 1
        return counts

    def bucket_spread(self) -> list[tuple[tuple[int, int], int]]:
        """Which buckets the folder would fill, busiest first.

        A bucket holding one or two images is worth seeing: trainers
        pad a partial batch or drop it, so a nearly-empty bucket is
        wasted work either way.
        """
        counts: dict = {}
        for plan in self.plans:
            if plan.bucket != (0, 0):
                counts[plan.bucket] = counts.get(plan.bucket, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])

    def smaller_target_that_fits(self) -> Optional[int]:
        """A lower target at which the too-small images would work.

        "Remove these from the set" is correct but blunt. If a dozen
        images are short of 1024 and every one of them clears 768, the
        useful answer is that the dataset wants a 768 target, not that
        a dozen files should be thrown away.
        """
        stranded = [p for p in self.plans
                    if p.verdict == VERDICT_TOO_SMALL]
        if not stranded:
            return None
        for base, _label in COMMON_TARGETS:
            if base >= self.base:
                continue
            if all(plan_for(p.path, p.width, p.height, base).verdict
                   != VERDICT_TOO_SMALL for p in stranded):
                return base
        return None

    def heaviest_crops(self, limit: int = 10) -> list:
        return sorted(
            (p for p in self.plans if p.verdict == VERDICT_CROP),
            key=lambda p: -p.crop_loss)[:limit]


# Extensions worth offering to a trainer. Anything else in the folder
# is left alone rather than guessed at.
IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"})


# Compiled once, not per call: this key runs on every filename when a
# folder is sorted, and a large NSFW dataset can be well over a
# thousand files.
_NATURAL_SPLIT = re.compile(r"(\d+)")


def natural_name_key(path: Path):
    """Sort filenames the way a person counts.

    Plain alphabetical puts "img10" before "img2", because it compares
    "1" against "2" one character at a time. Splitting the digits out
    and comparing them as numbers gives 1, 2, 10 — which is the order
    the files were almost certainly created in.
    """
    parts = _NATURAL_SPLIT.split(path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def read_size(path: Path) -> tuple[int, int]:
    """Dimensions from the file header, without decoding the picture.

    Pillow reads only what it needs for .size, which is what makes
    scanning a large folder quick. Returns (0, 0) rather than raising:
    an unreadable file is a finding to report, not a crash.
    """
    try:
        from PIL import Image
    except ImportError:
        return (0, 0)
    try:
        with Image.open(path) as im:
            return (im.width, im.height)
    except Exception:
        return (0, 0)


def scan_folder(folder: Path, base: int = DEFAULT_TARGET,
                upscale_limit: float = UPSCALE_LIMIT,
                progress=None) -> FolderReport:
    """Measure every image in a folder against one target.

    Reads nothing but headers and writes nothing at all. `progress` is
    called with (done, total) so a caller can drive a bar without this
    module knowing what a bar is.
    """
    report = FolderReport(base=base)
    try:
        entries = sorted(
            (p for p in Path(folder).iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES),
            key=natural_name_key)
    except OSError:
        return report

    total = len(entries)
    for index, path in enumerate(entries, 1):
        width, height = read_size(path)
        if width <= 0 or height <= 0:
            report.unreadable.append(path)
        else:
            report.plans.append(
                plan_for(path, width, height, base, upscale_limit))
        if progress is not None:
            progress(index, total)
    return report


# ---------------------------------------------------------------------
# Export
#
# Every function below writes, so every one of them obeys the same two
# rules: it writes only inside the chosen output folder, and it never
# opens an original for anything but reading. The program's standing
# promise is that an original is never modified, and the cheapest way
# to keep a promise is to make it structurally difficult to break.
# ---------------------------------------------------------------------

FORMAT_PNG = "png"
FORMAT_JPEG = "jpeg"

# Quality for JPEG output. 95 is where further increases stop being
# visible and start being only larger.
JPEG_QUALITY = 95

PAD_BLACK = "black"
PAD_WHITE = "white"

RESULT_OK = "ok"
RESULT_RENAMED = "renamed"     # written, but under a different name
RESULT_FAILED = "failed"
# The selection is below the upscale threshold. Not a hard failure:
# the UI can offer to proceed anyway after confirming with the user.
RESULT_TOO_SMALL = "too_small"


@dataclass
class ExportResult:
    """What happened to one image."""

    source: Path
    written: Optional[Path] = None
    status: str = RESULT_FAILED
    action: str = ""             # crop / pad / resize, for the report
    message: str = ""
    jitter: str = ""             # what colour shift was applied


def unique_path(folder: Path, stem: str, suffix: str) -> tuple[Path, bool]:
    """A free path in `folder`, suffixed the way Explorer does.

    Returns (path, renamed). Collisions are real: a folder holding
    both "a.png" and "a.jpg" produces one output name twice, and
    silently overwriting the first would destroy work with no trace.
    """
    candidate = folder / f"{stem}{suffix}"
    if not candidate.exists():
        return (candidate, False)
    index = 1
    while True:
        candidate = folder / f"{stem}_({index}){suffix}"
        if not candidate.exists():
            return (candidate, True)
        index += 1


def _flatten(image, pad_colour: str):
    """Drop transparency onto the pad colour.

    A PNG with alpha saved as JPEG raises; saved as PNG it keeps a
    transparent border that a trainer will read as noise. Either way
    the honest answer is to decide what is behind the image now,
    while the user can see the choice, rather than at training time.
    """
    from PIL import Image as PILImage

    if image.mode in ("RGBA", "LA", "P"):
        converted = image.convert("RGBA")
        backdrop = PILImage.new("RGB", converted.size, pad_colour)
        backdrop.paste(converted, mask=converted.split()[-1])
        return backdrop
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


# The three things a batch run can do. These are genuinely different
# operations, not variations on one.
#
#   MODE_BUCKET  no crop, no bars. The shortest side is brought to the
#                target and the picture keeps its shape; the trainer's
#                own bucketing takes it from there. This is the mode
#                for anyone training with bucketing on, and it is the
#                only one that discards nothing.
#   MODE_SQUARE_PAD   a perfectly square image. The LONGEST side goes
#                to the target and the short one gains even bars.
#                Nothing is lost; caption these pillarboxed or
#                letterboxed.
#   MODE_SQUARE_CROP  a perfectly square image the other way: the
#                middle is kept and the edges are lost.
MODE_BUCKET = "bucket"
MODE_SQUARE_PAD = "square_pad"
# MODE_SQUARE_CROP was offered and withdrawn: cropping blind to a
# square, with no one looking, discards the sides of every landscape
# image in the folder and there is no position that is right for all
# of them. Squaring by hand is what the one-by-one tab is for.
MODE_SQUARE_CROP = "square_crop"

# Retained names for the older two-way fit, still used by the
# one-by-one crop path.
FIT_CROP = "crop"
FIT_PAD = "pad"


def render_to_bucket(image, bucket: tuple[int, int], plan: "ImagePlan",
                     pad_colour: str = PAD_BLACK,
                     fit: str = FIT_CROP,
                     upscale_limit: float = UPSCALE_LIMIT):
    """Produce exactly `bucket` pixels from `image`.

    `fit` decides how a shape mismatch is resolved, and it is the
    user's choice rather than something derivable:

      FIT_CROP  cover the bucket, then take the middle. Loses the
                edges; keeps every remaining pixel at full size.
      FIT_PAD   fit inside the bucket, then centre. Keeps the whole
                picture; adds bars.

    Padding is ALWAYS centred, so bars are even on both sides and
    "pillarboxed" or "letterboxed" describes the result honestly. An
    image pushed to one side is a different picture needing a
    different caption.

    Padding also never enlarges past `upscale_limit`. An earlier
    version scaled to fill, which meant a 512x512 image bound for a
    1024 bucket was doubled — inventing exactly the detail its own
    verdict said not to invent, and producing no bars at all.
    """
    from PIL import Image as PILImage

    target_w, target_h = bucket
    if fit == FIT_PAD:
        scale = min(target_w / image.width, target_h / image.height)
        scale = min(scale, 1.0 + upscale_limit)
        new_w = max(1, round(image.width * scale))
        new_h = max(1, round(image.height * scale))
        resized = image.resize(
            (new_w, new_h),
            PILImage.LANCZOS if scale < 1 else PILImage.BICUBIC)
        canvas = PILImage.new("RGB", (target_w, target_h), pad_colour)
        canvas.paste(resized, ((target_w - new_w) // 2,
                               (target_h - new_h) // 2))
        bars = ("pillarboxed" if new_w < target_w and
                new_h >= target_h - 1
                else "letterboxed" if new_h < target_h and
                new_w >= target_w - 1
                else "padded")
        return canvas, bars

    # Cover the bucket, then take the middle. Centre-cropping is the
    # only defensible default without a human looking: it is the one
    # choice that is never systematically wrong, which is why the
    # one-by-one mode exists for the cases where it is.
    scale = max(target_w / image.width, target_h / image.height)
    new_w = max(target_w, round(image.width * scale))
    new_h = max(target_h, round(image.height * scale))
    resized = image.resize((new_w, new_h), PILImage.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    cropped = resized.crop((left, top, left + target_w, top + target_h))
    action = "cropped" if (new_w > target_w or new_h > target_h) \
        else "resized"
    return cropped, action


def export_one(plan: "ImagePlan", out_dir: Path,
               fmt: str = FORMAT_PNG,
               pad_colour: str = PAD_BLACK,
               bucket: Optional[tuple[int, int]] = None,
               fit: str = FIT_CROP) -> ExportResult:
    """Write one prepared image. Never touches the source."""
    from PIL import Image as PILImage

    result = ExportResult(source=plan.path)
    target = bucket or plan.bucket
    if target == (0, 0):
        result.message = "no bucket could be chosen"
        return result
    try:
        with PILImage.open(plan.path) as raw:
            raw.load()
            flat = _flatten(raw, pad_colour)
            rendered, action = render_to_bucket(
                flat, target, plan, pad_colour, fit)
    except Exception as exc:
        result.message = f"{type(exc).__name__}: {exc}"
        return result

    suffix = ".png" if fmt == FORMAT_PNG else ".jpg"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path, renamed = unique_path(out_dir, plan.path.stem, suffix)
        if fmt == FORMAT_PNG:
            rendered.save(path, "PNG")
        else:
            rendered.save(path, "JPEG", quality=JPEG_QUALITY,
                          subsampling=0)
    except Exception as exc:
        result.message = f"{type(exc).__name__}: {exc}"
        return result

    result.written = path
    result.action = action
    result.status = RESULT_RENAMED if renamed else RESULT_OK
    if renamed:
        result.message = f"name taken, written as {path.name}"
    return result


def crop_upscale_ratio(box: tuple[int, int, int, int],
                       bucket: tuple[int, int]) -> float:
    """How much a selection must be enlarged to fill the bucket.

    1.0 means no enlargement; 1.30 means it would be scaled up 30%.
    Exposed so the UI can decide whether to warn before exporting,
    and state the exact figure in that warning.
    """
    _l, _t, width, height = box
    if width <= 0 or height <= 0:
        return 0.0
    return max(bucket[0] / width, bucket[1] / height)


def export_crop(source: Path, box: tuple[int, int, int, int],
                bucket: tuple[int, int], out_dir: Path,
                fmt: str = FORMAT_PNG,
                pad_colour: str = PAD_BLACK,
                upscale_limit: float = UPSCALE_LIMIT,
                jitter=None, rng=None,
                allow_upscale: bool = False,
                angle: float = 0.0,
                flat_fill: bool = False) -> ExportResult:
    """Write one hand-chosen crop, padding wherever the box overhangs.

    `box` is (left, top, width, height) in the SOURCE image's own
    pixels, and it is allowed to extend past the edges — that is how
    pillarboxing is done by hand. Pillow's crop already fills the
    overhang, so the work here is choosing what it is filled WITH and
    refusing the selections that cannot be honoured.

    A box smaller than the bucket in both directions would have to be
    enlarged to fill it. Past the upscale limit that invents detail,
    so it is refused with a message rather than quietly done.

    `angle` tilts the picture under the box before the box is lifted,
    matching the editor's tilt preview. It is in degrees, CLOCKWISE
    positive — the same sense the preview rotates — and the rotation
    is taken about the box's OWN centre, so the framed subject stays
    put and the edges swing.

    The visible content is always laid where the box overlaps the
    picture — the SAME place the preview showed it — and the empty
    space (from a tilt, or a box pulled past an edge, on however many
    sides) is filled with `pad_colour` (the "Bars" choice). Placement
    matches the preview exactly, so a multi-side overhang does not shift
    the content. Balanced pillarbox / letterbox bars come from placing
    the box symmetrically over the picture, which the preview shows
    directly.

    `flat_fill` is accepted for call compatibility; fill placement is
    now preview-matched in every case, so it no longer alters the
    geometry.
    """
    from PIL import Image as PILImage

    result = ExportResult(source=source)
    left, top, width, height = box
    if width <= 0 or height <= 0:
        result.message = "empty selection"
        return result

    scale = max(bucket[0] / width, bucket[1] / height)
    # Past the threshold the export used to be refused outright. Now
    # the UI may confirm it with the user (who sees the exact ratio),
    # so the block only stands when that confirmation was not given.
    if scale > 1.0 + upscale_limit and not allow_upscale:
        result.status = RESULT_TOO_SMALL
        result.message = (
            f"selection is {width}x{height}, too small for "
            f"{bucket[0]}x{bucket[1]} \u2014 enlarging it "
            f"{scale - 1:.0%} would invent detail.")
        return result

    try:
        with PILImage.open(source) as raw:
            raw.load()
            flat = _flatten(raw, pad_colour)
            if angle:
                # Tilt the picture under the box before the box is
                # lifted. The preview turns the picture CLOCKWISE about
                # the crop centre; PIL's rotate is anticlockwise for a
                # positive angle, so it is negated to match. expand is
                # False on purpose: the pixel grid and origin stay put,
                # so the box coordinates computed against the untilted
                # image remain valid against the tilted one. The pivot is
                # the IMAGE centre — matching the preview, which rotates
                # the picture about its own centre so that dragging the
                # crop box slides it over a stationary tilted picture.
                # (Rotating about the box centre instead would make the
                # export disagree with the preview and, in the editor,
                # welded the picture to the box.) Corners this opens up
                # are filled with the pad colour, so nothing is invented.
                cx = flat.width / 2.0
                cy = flat.height / 2.0
                flat = flat.rotate(
                    -angle,
                    resample=PILImage.BICUBIC,
                    expand=False,
                    center=(cx, cy),
                    fillcolor=pad_colour)
            overhangs = (left < 0 or top < 0
                         or left + width > flat.width
                         or top + height > flat.height)
            if overhangs:
                # The box reaches past the picture (a hand-pulled bar, a
                # tilt that opened an edge, or several at once). Fill the
                # frame with the pad colour and lay the visible piece
                # exactly where the box overlaps the picture — the SAME
                # place the preview showed it. Positioning it anywhere
                # else (e.g. re-centring) would make the file disagree
                # with the preview, which is what made a multi-side
                # overhang look wrong. Balanced bars come from placing
                # the box symmetrically, not from the export shifting the
                # content behind the user's back.
                canvas = PILImage.new("RGB", (width, height), pad_colour)
                src_left = max(0, left)
                src_top = max(0, top)
                src_right = min(flat.width, left + width)
                src_bottom = min(flat.height, top + height)
                if src_right > src_left and src_bottom > src_top:
                    piece = flat.crop((src_left, src_top,
                                       src_right, src_bottom))
                    canvas.paste(piece, (src_left - left, src_top - top))
                cropped = canvas
            else:
                cropped = flat.crop(
                    (left, top, left + width, top + height))
            if cropped.size != bucket:
                cropped = cropped.resize(
                    bucket,
                    PILImage.LANCZOS if scale < 1 else PILImage.BICUBIC)
            if jitter is not None:
                from core import color_jitter as cj
                shift = (jitter if isinstance(jitter, cj.JitterResult)
                         else cj.roll(jitter, rng))
                cropped = cj.apply_result(cropped, shift)
                result.jitter = shift.describe()
    except Exception as exc:
        result.message = f"{type(exc).__name__}: {exc}"
        return result

    suffix = ".png" if fmt == FORMAT_PNG else ".jpg"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path, renamed = unique_path(out_dir, source.stem, suffix)
        if fmt == FORMAT_PNG:
            cropped.save(path, "PNG")
        else:
            cropped.save(path, "JPEG", quality=JPEG_QUALITY,
                         subsampling=0)
    except Exception as exc:
        result.message = f"{type(exc).__name__}: {exc}"
        return result

    result.written = path
    result.action = "padded" if overhangs else "cropped"
    result.status = RESULT_RENAMED if renamed else RESULT_OK
    if renamed:
        result.message = f"name taken, written as {path.name}"
    return result


def render_batch(image, mode: str, target: int,
                 pad_colour: str = PAD_BLACK,
                 upscale_limit: float = UPSCALE_LIMIT):
    """Apply one batch mode to one already-flattened image.

    Returns (rendered, action, note). `note` is non-empty when the
    result is worth a line in the report.

    Every mode here is defined by what it does to the SIDES, because
    that is what the user is choosing between:

      bucket       shortest side -> target, shape kept, nothing lost
      square pad   longest side  -> target, short side gains even bars
      square crop  shortest side -> target, long side is trimmed
    """
    from PIL import Image as PILImage

    width, height = image.width, image.height
    short_side = min(width, height)
    long_side = max(width, height)

    if mode == MODE_BUCKET:
        # Bring the shortest side to the target. Never enlarge past
        # the limit: an image whose short side is well under the
        # target cannot be made into one that is.
        scale = target / short_side
        if scale > 1.0 + upscale_limit:
            return (None, "", f"short side is only {short_side}px "
                              f"against a {target} target")
        if scale >= 1.0:
            resample = PILImage.BICUBIC
        else:
            resample = PILImage.LANCZOS
        new_w = max(1, round(width * scale))
        new_h = max(1, round(height * scale))
        # Trainers step buckets by 64; landing on a multiple avoids a
        # second resample at training time.
        new_w = max(BUCKET_STEP, round(new_w / BUCKET_STEP) * BUCKET_STEP)
        new_h = max(BUCKET_STEP, round(new_h / BUCKET_STEP) * BUCKET_STEP)
        return (image.resize((new_w, new_h), resample), "resized", "")

    if mode == MODE_SQUARE_PAD:
        # Longest side to the target, then even bars on the other.
        # This is what makes the result square while keeping every
        # real pixel.
        scale = target / long_side
        if scale > 1.0 + upscale_limit:
            return (None, "", f"longest side is only {long_side}px "
                              f"against a {target} target")
        new_w = max(1, round(width * scale))
        new_h = max(1, round(height * scale))
        resized = image.resize(
            (new_w, new_h),
            PILImage.LANCZOS if scale < 1 else PILImage.BICUBIC)
        canvas = PILImage.new("RGB", (target, target), pad_colour)
        canvas.paste(resized, ((target - new_w) // 2,
                               (target - new_h) // 2))
        if new_w == new_h:
            action = "resized"        # already square, no bars
        elif new_w < target:
            action = "pillarboxed"
        else:
            action = "letterboxed"
        return (canvas, action, "")

    # MODE_SQUARE_CROP: shortest side to the target, centre kept.
    scale = target / short_side
    if scale > 1.0 + upscale_limit:
        return (None, "", f"short side is only {short_side}px against "
                          f"a {target} target")
    new_w = max(target, round(width * scale))
    new_h = max(target, round(height * scale))
    resized = image.resize(
        (new_w, new_h),
        PILImage.LANCZOS if scale < 1 else PILImage.BICUBIC)
    left = (new_w - target) // 2
    top = (new_h - target) // 2
    cropped = resized.crop((left, top, left + target, top + target))
    action = "cropped" if (new_w > target or new_h > target) \
        else "resized"
    return (cropped, action, "")


def export_batch(plan: "ImagePlan", out_dir: Path, mode: str,
                 target: int, fmt: str = FORMAT_PNG,
                 pad_colour: str = PAD_BLACK,
                 upscale_limit: float = UPSCALE_LIMIT,
                 jitter=None, rng=None) -> ExportResult:
    """Write one image under a batch mode. Never touches the source."""
    from PIL import Image as PILImage

    result = ExportResult(source=plan.path)
    try:
        with PILImage.open(plan.path) as raw:
            raw.load()
            flat = _flatten(raw, pad_colour)
            rendered, action, note = render_batch(
                flat, mode, target, pad_colour, upscale_limit)
            if rendered is not None and jitter is not None:
                # Applied AFTER resizing, so the shift is measured on
                # the pixels that will actually be trained on rather
                # than on detail about to be thrown away.
                from core import color_jitter as cj
                shift = (jitter if isinstance(jitter, cj.JitterResult)
                         else cj.roll(jitter, rng))
                rendered = cj.apply_result(rendered, shift)
                result.jitter = shift.describe()
    except Exception as exc:
        result.message = f"{type(exc).__name__}: {exc}"
        return result

    if rendered is None:
        result.message = note or "too small for this target"
        return result

    suffix = ".png" if fmt == FORMAT_PNG else ".jpg"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path, renamed = unique_path(out_dir, plan.path.stem, suffix)
        if fmt == FORMAT_PNG:
            rendered.save(path, "PNG")
        else:
            rendered.save(path, "JPEG", quality=JPEG_QUALITY,
                          subsampling=0)
    except Exception as exc:
        result.message = f"{type(exc).__name__}: {exc}"
        return result

    result.written = path
    result.action = action
    result.status = RESULT_RENAMED if renamed else RESULT_OK
    if renamed:
        result.message = f"name taken, written as {path.name}"
    return result


# The three frames offered in the one-by-one crop, per target.
SHAPE_SQUARE = "square"
SHAPE_PORTRAIT = "portrait"
SHAPE_LANDSCAPE = "landscape"


# Aspect ratios offered as crop frames. Standard photographic shapes
# rather than entries from the bucket table: the bucket list is
# area-constrained, so every rectangle in it is close to square and
# none of them is a usefully different frame to compose in.
PORTRAIT_RATIO = (2, 3)
LANDSCAPE_RATIO = (3, 2)


def shape_buckets(base: int = DEFAULT_TARGET) -> dict:
    """Output size for each crop frame.

    The SHORTEST side is the target in every case. That is what a
    trainer wants from a bucketed image, and it is the rule that makes
    a portrait and a landscape crop of the same picture carry the same
    amount of detail.

    Sizes are rounded to the bucket step so the trainer does not
    resample a second time on load.
    """
    def rounded(value: int) -> int:
        return max(BUCKET_STEP,
                   round(value / BUCKET_STEP) * BUCKET_STEP)

    short_w, long_h = PORTRAIT_RATIO
    long_w, short_h = LANDSCAPE_RATIO
    return {
        SHAPE_SQUARE: (base, base),
        SHAPE_PORTRAIT: (base, rounded(base * long_h / short_w)),
        SHAPE_LANDSCAPE: (rounded(base * long_w / short_h), base),
    }
