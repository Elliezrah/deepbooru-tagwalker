"""
tests/test_color_jitter.py

Randomised colour shifts applied at export.

A trainer's own colour augmentation re-rolls every epoch, so the model
sees one picture under many casts. This bakes ONE shift into each
file, permanently, and that is a different thing worth being clear
about: it decorrelates a SET. Images from one artist, capture or
camera share a cast, and the model will learn that cast as part of the
concept.

The dead zone is the feature that makes it work. A plain draw between
-15 and +15 lands near zero as often as anywhere else, so much of the
set would come out unchanged; excluding the middle guarantees every
image moves.

Run: python3 tests/test_color_jitter.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PIL import Image

from core import color_jitter as jitter


def _pixels(image) -> list:
    return list(image.convert("RGB").tobytes())


def test_the_dead_zone_keeps_every_image_moving() -> None:
    rng = random.Random(7)
    draws = [jitter.draw(15, 3, rng) for _ in range(3000)]

    # Nothing lands in the excluded middle.
    assert not any(0 < abs(value) < 3 for value in draws)
    assert all(abs(value) <= 15 for value in draws)
    # Both directions stay available and roughly equal: the sign is
    # chosen before the magnitude precisely so a narrow band does not
    # skew one way.
    negatives = sum(1 for value in draws if value < 0)
    assert 0.42 < negatives / len(draws) < 0.58

    # A zero minimum is the ordinary case and must still allow zero.
    assert 0 in [jitter.draw(15, 0, rng) for _ in range(300)]
    # A zero swing switches the channel off.
    assert jitter.draw(0, 0, rng) == 0
    print("OK: every draw clears the dead zone, both directions stay "
          "equally likely, and a zero swing does nothing")


def test_a_minimum_above_its_swing_cannot_empty_the_range() -> None:
    """Left alone this would ask for a number between 40 and 5."""
    settings = jitter.JitterSettings(hue_swing=5, hue_min=40).clamped()
    assert settings.hue_min <= settings.hue_swing
    # And the ceilings are respected.
    huge = jitter.JitterSettings(
        hue_swing=9999, brightness_swing=9999,
        contrast_swing=9999).clamped()
    assert huge.hue_swing <= jitter.MAX_HUE_SWING
    assert huge.brightness_swing <= jitter.MAX_LEVEL_SWING
    print("OK: a minimum larger than its swing is pulled down instead "
          "of producing an impossible range")


def test_applying_it_changes_the_picture() -> None:
    source = Image.new("RGB", (64, 64), (120, 80, 200))
    settings = jitter.JitterSettings(
        hue_swing=20, hue_min=5, brightness_swing=20,
        brightness_min=5, contrast_swing=20, contrast_min=5)

    shifted, applied = jitter.apply(source, settings, random.Random(3))
    assert _pixels(shifted) != _pixels(source)
    assert shifted.size == source.size
    assert shifted.mode == "RGB"
    # Each channel cleared its own dead zone.
    assert abs(applied.hue) >= 5
    assert abs(applied.brightness) >= 5
    assert abs(applied.contrast) >= 5
    assert applied.describe()

    # No settings means the file is left exactly as it was.
    same, nothing = jitter.apply(source, jitter.JitterSettings(),
                                 random.Random(3))
    assert _pixels(same) == _pixels(source)
    assert nothing.describe() == ""

    # And two images get different rolls, which is the entire point:
    # a shift applied identically to every file would ADD a cast
    # rather than break one.
    first, _ = jitter.apply(source, settings, random.Random(1))
    second, _ = jitter.apply(source, settings, random.Random(2))
    assert _pixels(first) != _pixels(second)
    print("OK: a jittered image really changes, an unset one does "
          "not, and two images get different shifts")


def test_export_applies_it_and_says_what_it_did() -> None:
    from core import image_prep as prep

    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    path = src / "a.png"
    Image.new("RGB", (2048, 2048), (120, 80, 200)).save(path)
    before = path.read_bytes()

    plan = prep.plan_for(path, 2048, 2048, 1024)
    settings = jitter.JitterSettings(hue_swing=25, hue_min=8,
                                     brightness_swing=20,
                                     brightness_min=6)
    result = prep.export_batch(plan, out, prep.MODE_BUCKET, 1024,
                               jitter=settings, rng=random.Random(5))
    assert result.status == prep.RESULT_OK
    assert result.jitter                    # reported, not silent
    assert "hue" in result.jitter

    # The hand-crop path applies it too.
    cropped = prep.export_crop(path, (0, 0, 2048, 2048), (1024, 1024),
                               out, jitter=settings,
                               rng=random.Random(9))
    assert cropped.jitter

    # With no settings the field stays empty rather than saying
    # something misleading.
    plain = prep.export_batch(plan, out, prep.MODE_BUCKET, 1024)
    assert plain.jitter == ""
    # And the source is untouched throughout.
    assert path.read_bytes() == before
    print("OK: both export paths apply the shift, report what they "
          "applied, and never modify the source")


def test_a_reference_image_can_be_left_alone() -> None:
    """CORRECTED after a field note. The purpose is not to decorrelate
    a whole set — it is to stop the model memorising NEAR-IDENTICAL
    images by pixel.

    Which means most images should not be jittered at all. The first
    of a variant group is the reference and belongs in the set in its
    original colour; it is the copies that must differ from it. So the
    per-image control defaults to OFF, and the shift is rolled and
    SHOWN before export rather than drawn invisibly at write time —
    a shift nobody sees cannot be rejected.
    """
    from PySide6.QtWidgets import QApplication, QFileDialog

    _app = QApplication.instance() or QApplication([])
    from config.settings import Settings
    from config.theme import initialize_theme
    from ui.image_editor_window import ImageEditorWindow

    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    for i in range(3):                       # three near-identical
        Image.new("RGB", (2048, 2048), (120, 80, 200)).save(
            src / f"var{i}.png")

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window._rescan()
    assert not window.chk_jitter_one.isChecked()

    window._jitter_rows["hue"][0].setValue(14)
    window._jitter_rows["hue"][1].setValue(4)
    window._jitter_rows["brightness"][0].setValue(12)
    QFileDialog.getExistingDirectory = staticmethod(
        lambda *a, **k: str(out))
    window._pick_output()

    window._crop_current()                   # reference, untouched
    for _ in range(2):
        window.chk_jitter_one.setChecked(True)
        assert "will apply" in window.lbl_jitter_one.text()
        window._crop_current()

    corners = [Image.open(p).convert("RGB").getpixel((5, 5))
               for p in sorted(out.iterdir())]
    assert len(corners) == 3
    assert corners[0] == (120, 80, 200)      # reference kept
    assert all(c != (120, 80, 200) for c in corners[1:])
    assert corners[1] != corners[2]          # and differ from each other
    print("OK: the reference image exports in its original colour "
          "while the copies each get their own visible shift")


def test_a_drawn_shift_is_applied_exactly() -> None:
    """The roll is shown, so the export must use that roll and not
    draw a fresh one."""
    source = Image.new("RGB", (32, 32), (120, 80, 200))
    fixed = jitter.JitterResult(hue=10, brightness=-6, contrast=0)
    once = jitter.apply_result(source, fixed)
    twice = jitter.apply_result(source, fixed)
    assert _pixels(once) == _pixels(twice)   # deterministic
    assert _pixels(once) != _pixels(source)

    # An empty result is a no-op rather than a subtle re-encode.
    assert _pixels(jitter.apply_result(
        source, jitter.JitterResult())) == _pixels(source)
    print("OK: a drawn shift applies identically every time, so what "
          "was shown is what is written")


def run() -> None:
    test_the_dead_zone_keeps_every_image_moving()
    test_a_minimum_above_its_swing_cannot_empty_the_range()
    test_applying_it_changes_the_picture()
    test_export_applies_it_and_says_what_it_did()
    test_a_drawn_shift_is_applied_exactly()
    test_a_reference_image_can_be_left_alone()
    print("\nALL PASS: colour jitter")


if __name__ == "__main__":
    run()
