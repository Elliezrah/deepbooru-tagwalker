"""
core/color_jitter.py

Randomised hue, brightness and contrast shifts applied at export.

WHAT THIS IS FOR. Near-identical images — the same pose shot twice,
a sequence of frames, a variant set — invite the model to memorise
pixels rather than learn a concept. Shifting the colour of the
duplicates breaks that: the shapes repeat, the exact values do not.

Which means MOST images should not be jittered at all. The first of a
variant group is the reference and belongs in the training set in its
original colour; it is the second, third and fourth that need to
differ from it. That is a judgement about which images are near-copies
of which, and nothing here can make it — so the one-by-one tab lets
the choice be made per image, and batch exists only to save time when
a whole folder genuinely is variants.

THE DEAD ZONE follows from that. Applied by hand to a known duplicate,
excluding the middle is right: a shift of +1 has not broken anything,
and the whole point of choosing that image was to move it. Applied
across a folder it is wrong: every file is forced away from neutral
whether it needed it or not, and a set with no unshifted images has
simply traded one uniform cast for a uniformly disturbed one. So the
minimums default to zero in batch and are offered per image.

Qt-free: this is arithmetic on pixels and belongs in tests without a
window.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

# Everything is expressed on the 0-255 scale people already use for
# digital colour, then converted to whatever Pillow wants.
SCALE = 255

# Ceilings on the controls. Past roughly a fifth of the range the
# result stops reading as the same photograph.
MAX_HUE_SWING = 64
MAX_LEVEL_SWING = 64

# Enough to defeat pixel memorisation without changing what the image
# depicts. Offered as the starting point for a hand-applied shift.
SUGGESTED_SWING = 12
SUGGESTED_MIN = 4


@dataclass
class JitterSettings:
    """How far each channel may move, and how far it must."""

    hue_swing: int = 0
    hue_min: int = 0
    saturation_swing: int = 0
    saturation_min: int = 0
    brightness_swing: int = 0
    brightness_min: int = 0

    def active(self) -> bool:
        return any((self.hue_swing, self.saturation_swing,
                    self.brightness_swing))

    def clamped(self) -> "JitterSettings":
        """A copy with every value inside its legal range.

        A minimum larger than its swing would leave no legal draw at
        all, so it is pulled down rather than allowed to produce an
        empty range at export time.
        """
        def fix(swing: int, floor: int, ceiling: int) -> tuple[int, int]:
            swing = max(0, min(int(swing), ceiling))
            floor = max(0, min(int(floor), swing))
            return (swing, floor)

        hue, hue_min = fix(self.hue_swing, self.hue_min, MAX_HUE_SWING)
        sat, sat_min = fix(self.saturation_swing,
                           self.saturation_min, MAX_LEVEL_SWING)
        bright, bright_min = fix(self.brightness_swing,
                                 self.brightness_min, MAX_LEVEL_SWING)
        return JitterSettings(hue, hue_min, sat, sat_min,
                              bright, bright_min)


def draw(swing: int, minimum: int, rng: random.Random) -> int:
    """One offset in [-swing, -minimum] or [+minimum, +swing].

    The sign is chosen first and the magnitude second, so both
    directions stay equally likely however narrow the band is.
    """
    if swing <= 0:
        return 0
    minimum = max(0, min(minimum, swing))
    if minimum == 0:
        return rng.randint(-swing, swing)
    magnitude = rng.randint(minimum, swing)
    return magnitude if rng.random() < 0.5 else -magnitude


@dataclass
class JitterResult:
    hue: int = 0
    saturation: int = 0
    brightness: int = 0

    def describe(self) -> str:
        parts = []
        if self.hue:
            parts.append(f"hue {self.hue:+d}")
        if self.saturation:
            parts.append(f"saturation {self.saturation:+d}")
        if self.brightness:
            parts.append(f"brightness {self.brightness:+d}")
        return ", ".join(parts)


def roll(settings: JitterSettings,
         rng: random.Random | None = None) -> JitterResult:
    """Draw one shift without touching an image.

    Separated so a caller can show the numbers, let the user reject
    them, and then apply exactly those — rather than drawing
    invisibly at the moment of export, where a shift cannot be judged.
    """
    settings = settings.clamped()
    rng = rng or random.Random()
    return JitterResult(
        hue=draw(settings.hue_swing, settings.hue_min, rng),
        saturation=draw(settings.saturation_swing,
                        settings.saturation_min, rng),
        brightness=draw(settings.brightness_swing,
                        settings.brightness_min, rng))


def apply_result(image, result: JitterResult):
    """Apply an already-drawn shift.

    Hue is rotated in HSV, where it wraps: a shift past the end of the
    circle comes back round, which is correct and why hue is handled
    separately from the two levels.
    """
    from PIL import Image as PILImage
    from PIL import ImageEnhance

    if not (result.hue or result.saturation or result.brightness):
        return image
    out = image if image.mode == "RGB" else image.convert("RGB")

    if result.hue:
        hsv = out.convert("HSV")
        channels = list(hsv.split())
        # Point over the hue channel: the byte wraps by construction,
        # which is exactly the behaviour a colour wheel needs.
        shift = result.hue
        channels[0] = channels[0].point(
            lambda value: (value + shift) % 256)
        out = PILImage.merge("HSV", channels).convert("RGB")

    # Saturation: Color enhancer, where 1.0 is unchanged, 0 is
    # greyscale. A shift on the 0-255 scale maps to a proportional
    # nudge around 1.0, same convention as brightness.
    if result.saturation:
        out = ImageEnhance.Color(out).enhance(
            1.0 + (result.saturation / SCALE))
    if result.brightness:
        out = ImageEnhance.Brightness(out).enhance(
            1.0 + (result.brightness / SCALE))
    return out


def apply(image, settings: JitterSettings,
          rng: random.Random | None = None):
    """Draw a shift and apply it. Returns (image, what was applied)."""
    settings = settings.clamped()
    if not settings.active():
        return (image, JitterResult())
    result = roll(settings, rng)
    return (apply_result(image, result), result)
