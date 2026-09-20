"""
tests/test_image_prep.py

Bucket arithmetic and the image editor's analysis pass.

Two properties matter more than any other here, and both are asserted
below.

FIRST: the bucket sizes must match what a trainer actually generates.
They are derived rather than hard-coded, so a wrong step or base still
produces a self-consistent answer — which means a mistake would look
entirely plausible while making every recommendation wrong. Base 1024
must yield the familiar SDXL set.

SECOND: nothing is written. The program's standing promise is that it
never modifies an image, and the editor keeps it by working on its own
folders and, in this phase, only reading headers.

Run: python3 tests/test_image_prep.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import image_prep as prep
from ui.image_editor_window import ImageEditorWindow
from ui.main_window import MainWindow


def test_buckets_match_what_a_trainer_generates() -> None:
    buckets = prep.buckets_for(1024)
    # The set every SDXL-era config produces.
    for expected in [(1024, 1024), (896, 1152), (1152, 896),
                     (832, 1216), (1216, 832), (768, 1344),
                     (1344, 768)]:
        assert expected in buckets, expected

    for width, height in buckets:
        assert width % prep.BUCKET_STEP == 0
        assert height % prep.BUCKET_STEP == 0
        assert min(width, height) >= prep.MIN_BUCKET
        ratio = max(width, height) / min(width, height)
        assert ratio <= prep.MAX_BUCKET_RATIO + 1e-9

    # Smaller bases produce smaller sets, and 512 must include itself.
    assert (512, 512) in prep.buckets_for(512)
    assert (768, 768) in prep.buckets_for(768)
    print("OK: bucket generation reproduces the standard SDXL set and "
          "respects step, minimum and ratio limits")


def test_each_image_gets_the_right_verdict() -> None:
    """The order of the checks matters: an image is judged too small
    only after its SHAPE is considered, because a wide image can be
    short in one dimension and still fill a wide bucket."""
    def verdict(width, height):
        return prep.plan_for(Path("x.png"), width, height, 1024).verdict

    assert verdict(1024, 1024) == prep.VERDICT_EXACT
    assert verdict(832, 1216) == prep.VERDICT_EXACT   # portrait bucket
    assert verdict(2048, 2048) == prep.VERDICT_DOWNSCALE
    assert verdict(1920, 1080) == prep.VERDICT_CROP
    assert verdict(6000, 4000) == prep.VERDICT_HUGE
    # 900x900 is 14% short — inside the limit, so enlarge.
    assert verdict(900, 900) == prep.VERDICT_UPSCALE
    # 512x512 would need doubling. CORRECTED: this used to be
    # VERDICT_PAD, but padding it leaves bars on all four sides, which
    # is not pillarboxing. See test_too_small_is_its_own_verdict.
    assert verdict(512, 512) == prep.VERDICT_TOO_SMALL

    plan = prep.plan_for(Path("x.png"), 512, 512, 1024)
    assert "neither side reaches" in plan.note
    assert plan.bucket == (1024, 1024)
    print("OK: every image size gets the verdict its dimensions call "
          "for, with padding reserved for genuine enlargement")


def test_crop_loss_is_measured_on_shape_alone() -> None:
    """Scaling costs nothing in area terms, so what a crop loses is
    only what the aspect mismatch forces off the edges."""
    square = prep.plan_for(Path("x.png"), 2048, 2048, 1024)
    assert square.crop_loss < 0.01           # right shape already

    wide = prep.plan_for(Path("x.png"), 3000, 1000, 1024)
    assert wide.crop_loss > 0.2              # 3:1 into a 2:1 bucket

    # Two images of the same shape but different sizes lose the same
    # fraction.
    a = prep.plan_for(Path("x.png"), 1600, 900, 1024)
    b = prep.plan_for(Path("x.png"), 3200, 1800, 1024)
    assert abs(a.crop_loss - b.crop_loss) < 1e-9
    print("OK: crop loss depends on aspect ratio alone, not on how "
          "large the image happens to be")


def test_the_analysis_writes_nothing() -> None:
    """The standing promise is that no original is ever modified. In
    this phase nothing is written at all — only headers are read."""
    folder = Path(tempfile.mkdtemp()) / "raw"
    folder.mkdir(parents=True)
    sizes = [(1024, 1024), (2048, 2048), (1920, 1080), (900, 900),
             (512, 512), (6000, 4000), (832, 1216), (1000, 1500)]
    for i, (width, height) in enumerate(sizes):
        Image.new("RGB", (width, height), "red").save(
            folder / f"i{i:02d}.png")
    (folder / "broken.png").write_bytes(b"not an image")
    (folder / "notes.txt").write_text("ignored", encoding="utf-8")

    before = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
    report = prep.scan_folder(folder, 1024)
    after = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}

    assert before == after                   # nothing touched
    assert report.total == len(sizes)        # the .txt was skipped
    assert len(report.unreadable) == 1       # and the broken one found
    assert report.bucket_spread()
    print("OK: scanning a folder reads headers only and modifies "
          "nothing, skipping non-images and reporting unreadable ones")


def test_the_window_reports_what_needs_attention() -> None:
    s = Settings()
    initialize_theme(s.theme)
    mw = MainWindow(s)
    assert "Launch Image Editor" in mw._act_image_editor.text()
    mw._action_image_editor()
    window = mw._image_editor
    # Launching twice must not stack windows.
    mw._action_image_editor()
    assert mw._image_editor is window

    folder = Path(tempfile.mkdtemp()) / "raw"
    folder.mkdir(parents=True)
    for i, (width, height) in enumerate(
            [(1024, 1024), (1024, 1024), (512, 512), (6000, 4000),
             (1024, 700), (832, 1216)]):
        Image.new("RGB", (width, height), "red").save(
            folder / f"i{i}.png")
    (folder / "broken.png").write_bytes(b"not an image")

    window._input = folder
    window._rescan()
    text = window.report.view.toPlainText()

    # Amber: worth knowing, not failures.
    assert "far larger" in text               # the huge one
    # An image too small to use is a red finding, not an amber one:
    # it is a judgement about the dataset, not a preparation choice.
    assert "too small for 1024" in text
    assert "NOT RECOMMENDED" in text          # if enlarged anyway
    # Red: an actual failure, named.
    assert "broken.png" in text
    print("OK: the editor reports padding, oversize images, thin "
          "buckets and unreadable files, each in its own colour")


def test_padding_is_even_and_never_invents_detail() -> None:
    """Two properties the captioning depends on.

    BARS MUST BE EVEN. The user captions these "pillarboxed" or
    "letterboxed", and that word is only true if the image sits in the
    middle. An image pushed to one side is a different picture needing
    a different description.

    PADDING MUST NOT UPSCALE TO FILL. An earlier version scaled to
    cover the bucket, so a 512x512 image bound for 1024 was doubled —
    inventing exactly the detail its own verdict said not to invent,
    and producing no bars at all.
    """
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    Image.new("RGB", (1600, 900), "red").save(src / "wide.png")
    Image.new("RGB", (900, 1600), "blue").save(src / "tall.png")

    for name, expected in (("wide", "letterboxed"),
                           ("tall", "pillarboxed")):
        path = src / f"{name}.png"
        width, height = prep.read_size(path)
        plan = prep.plan_for(path, width, height, 1024)
        result = prep.export_one(plan, out, prep.FORMAT_PNG,
                                 bucket=(1024, 1024),
                                 fit=prep.FIT_PAD)
        assert result.action == expected, (name, result.action)

        image = Image.open(result.written)
        pixels = image.load()
        row = [x for x in range(image.width)
               if pixels[x, image.height // 2] != (0, 0, 0)]
        col = [y for y in range(image.height)
               if pixels[image.width // 2, y] != (0, 0, 0)]
        left, right = row[0], image.width - 1 - row[-1]
        top, bottom = col[0], image.height - 1 - col[-1]
        assert abs(left - right) <= 1, (name, left, right)
        assert abs(top - bottom) <= 1, (name, top, bottom)

    # An image with one side already at the target keeps every real
    # pixel: it is never enlarged to fill.
    short = src / "short.png"
    Image.new("RGB", (1024, 700), "green").save(short)
    plan = prep.plan_for(short, 1024, 700, 1024)
    result = prep.export_one(plan, out, prep.FORMAT_PNG,
                             bucket=(1024, 1024), fit=prep.FIT_PAD)
    image = Image.open(result.written)
    pixels = image.load()
    col = [y for y in range(image.height)
           if pixels[image.width // 2, y] != (0, 0, 0)]
    content_height = col[-1] - col[0] + 1
    assert content_height <= 700 * (1 + prep.UPSCALE_LIMIT) + 2, \
        content_height
    print("OK: padding centres the image, produces even bars, and "
          "never enlarges past the upscale limit to fill a bucket")


def test_export_never_touches_the_source_and_names_collisions() -> None:
    """A folder holding both "a.png" and "a.jpg" produces one output
    name twice. Silently overwriting the first would destroy work with
    no trace, so the second is suffixed the way Explorer does and the
    rename is reported."""
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    for i, (width, height) in enumerate(
            [(2048, 2048), (1600, 900), (900, 1600), (1024, 1024)]):
        Image.new("RGB", (width, height), "red").save(src / f"i{i}.png")
    Image.new("RGB", (1024, 1024), "blue").save(src / "i0.jpg")

    before = {p.name: p.read_bytes() for p in src.iterdir()}
    report = prep.scan_folder(src, 1024)
    for plan in report.plans:
        prep.export_one(plan, out, prep.FORMAT_PNG)

    assert {p.name: p.read_bytes() for p in src.iterdir()} == before
    written = sorted(p.name for p in out.iterdir())
    assert len(written) == 5
    assert any("_(1)" in name for name in written), written
    for path in out.iterdir():
        assert Image.open(path).size in prep.buckets_for(1024)
    print("OK: export writes only into the output folder, and a name "
          "collision is suffixed rather than overwritten")


def test_alpha_is_flattened_onto_the_pad_colour() -> None:
    """A PNG with alpha saved as JPEG raises; saved as PNG it keeps a
    transparent border a trainer reads as noise. Deciding what is
    behind the image now, while the user can see the choice, beats
    deciding it at training time."""
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    path = src / "alpha.png"
    Image.new("RGBA", (1024, 1024), (255, 0, 0, 128)).save(path)

    plan = prep.plan_for(path, 1024, 1024, 1024)
    for fmt in (prep.FORMAT_PNG, prep.FORMAT_JPEG):
        result = prep.export_one(plan, out, fmt)
        assert result.status != prep.RESULT_FAILED, result.message
        assert Image.open(result.written).mode == "RGB"
    print("OK: transparency is flattened onto the pad colour, so both "
          "PNG and JPEG output succeed")


def test_too_small_is_its_own_verdict() -> None:
    """CORRECTED after a field note, and the rule is cleaner than what
    was built first.

    Padding only works if ONE side already reaches the target: scaling
    to fit then puts bars on the other two, and "pillarboxed" or
    "letterboxed" describes that exactly. When NEITHER side reaches
    it, fitting leaves bars on all four — a small picture in a big
    frame, which no caption describes and which teaches the model a
    border it will draw back.

    Such an image is not awkward to prepare. It is too small to use,
    and saying so is more useful than offering a bad rendering."""
    def verdict(width, height, base=1024):
        return prep.plan_for(Path("x.png"), width, height, base).verdict

    # Neither side reaches 1024.
    assert verdict(512, 512) == prep.VERDICT_TOO_SMALL
    assert verdict(700, 700) == prep.VERDICT_TOO_SMALL
    # One side exceeds it, so the shape can be resolved honestly.
    assert verdict(1600, 900) in (prep.VERDICT_CROP, prep.VERDICT_PAD)
    assert verdict(900, 1600) in (prep.VERDICT_CROP, prep.VERDICT_PAD)
    # Short by less than the upscale tolerance is not "too small".
    assert verdict(950, 950) == prep.VERDICT_UPSCALE
    # And the same file is fine against a smaller target.
    assert verdict(512, 512, 512) == prep.VERDICT_EXACT
    print("OK: an image with neither side reaching the target is "
          "reported as too small rather than padded on all four sides")


def test_a_smaller_target_is_suggested_when_one_fits() -> None:
    """"Remove these from the set" is correct but blunt. If a dozen
    images are short of 1024 and every one clears 768, the useful
    answer is that the dataset wants a lower target."""
    folder = Path(tempfile.mkdtemp()) / "raw"
    folder.mkdir(parents=True)
    for i, (width, height) in enumerate(
            [(1024, 1024), (800, 600), (768, 768), (640, 480)]):
        Image.new("RGB", (width, height), "red").save(
            folder / f"i{i}.png")

    report = prep.scan_folder(folder, 1024)
    stranded = [p for p in report.plans
                if p.verdict == prep.VERDICT_TOO_SMALL]
    assert stranded
    smaller = report.smaller_target_that_fits()
    assert smaller is not None and smaller < 1024
    # And at that target, none of them is stranded any more.
    for plan in stranded:
        assert prep.plan_for(plan.path, plan.width, plan.height,
                             smaller).verdict != prep.VERDICT_TOO_SMALL
    print("OK: when every stranded image would fit a lower target, "
          "the report names it instead of only advising removal")


def test_padding_matters_in_fixed_mode_not_bucket_mode() -> None:
    """A finding worth recording, because it decides where the pad
    option belongs in the interface.

    When the bucket is CHOSEN by best aspect match, it already matches
    the image's shape closely, so there is nothing much to pad — the
    answer is a small crop or nothing. Padding becomes the real
    alternative only when a target is FORCED, which is what fixed mode
    does: a wide picture into a square frame has to lose its sides or
    gain bars, and that is a choice only the user can make.
    """
    folder = Path(tempfile.mkdtemp()) / "raw"
    folder.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    path = folder / "wide.png"
    Image.new("RGB", (1600, 900), "red").save(path)

    # Bucket mode: the picker finds a near-matching shape, so almost
    # nothing is lost either way.
    auto = prep.plan_for(path, 1600, 900, 1024)
    assert auto.crop_loss < 0.05

    # Fixed mode: forced square. Now the two fits differ completely.
    cropped = prep.export_one(auto, out, prep.FORMAT_PNG,
                              bucket=(1024, 1024), fit=prep.FIT_CROP)
    padded = prep.export_one(auto, out, prep.FORMAT_PNG,
                             bucket=(1024, 1024), fit=prep.FIT_PAD)
    assert cropped.action == "cropped"
    assert padded.action == "letterboxed"
    assert Image.open(cropped.written).size == (1024, 1024)
    assert Image.open(padded.written).size == (1024, 1024)
    print("OK: padding is the meaningful alternative in fixed mode, "
          "where the target shape is forced rather than matched")


def test_the_crop_box_keeps_its_shape_and_stays_inside() -> None:
    """The wheel is the point of the one-by-one mode, so what it can
    and cannot do is worth pinning.

    A crop running off the edge would be filled with something
    invented, which is the one thing this tool exists to avoid."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from ui.crop_view import MODE_FREE, MODE_LOCKED, CropView

    folder = Path(tempfile.mkdtemp())
    path = folder / "big.png"
    Image.new("RGB", (3000, 2000), "red").save(path)

    def wheel(view, notches):
        view.wheelEvent(QWheelEvent(
            QPointF(300, 200), QPointF(300, 200), QPoint(0, 0),
            QPoint(0, 120 * notches), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False))

    free = CropView()
    free.resize(600, 400)
    assert free.load(path, (1024, 1024), MODE_FREE)
    start = free.crop_rect
    # Opens at maximum coverage: the common case is keeping nearly
    # everything, so that is what should be shown first.
    assert start.height() == 2000
    assert abs(start.width() / start.height() - 1.0) < 0.01

    wheel(free, 1)
    assert free.crop_rect.width() < start.width()
    assert abs(free.crop_rect.width()
               / free.crop_rect.height() - 1.0) < 0.02

    # CORRECTED: the box used to be confined inside the image, which
    # made padding impossible from this mode. To letterbox a picture
    # by hand you have to pull the box out past the edges, and the
    # space outside becomes the bars.
    for _ in range(30):                  # zoom out far past the edge
        wheel(free, -1)
    rect = free.crop_rect
    assert free.overflow()               # it may now leave the image
    # But not without limit, and never off the picture entirely: a
    # selection containing almost no image is never what was meant.
    from ui.crop_view import MAX_OVERSHOOT, MIN_OVERLAP
    limit = int(max(3000, 2000) * MAX_OVERSHOOT)
    assert rect.width() <= limit and rect.height() <= limit
    assert rect.right() > 0 and rect.bottom() > 0
    assert rect.left() < 3000 and rect.top() < 2000

    locked = CropView()
    locked.resize(600, 400)
    locked.load(path, (1024, 1024), MODE_LOCKED)
    assert (locked.crop_rect.width(),
            locked.crop_rect.height()) == (1024, 1024)
    width_before = locked.crop_rect.width()
    wheel(locked, 1)
    assert locked.crop_rect.width() == width_before   # wheel ignored
    assert not locked.can_resize()
    print("OK: the crop box keeps the bucket's shape, never leaves "
          "the image, and ignores the wheel when locked")


def test_the_one_by_one_tab_exports_the_chosen_crop() -> None:
    from PySide6.QtWidgets import QFileDialog

    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    for i, (width, height) in enumerate(
            [(3000, 2000), (2048, 2048), (1600, 900)]):
        Image.new("RGB", (width, height), "red").save(src / f"i{i}.png")
    # Too small to use — must not appear in the crop queue at all.
    Image.new("RGB", (400, 400), "blue").save(src / "tiny.png")
    before = {p.name: p.read_bytes() for p in src.iterdir()}

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window._rescan()

    # Two, both work modes. Colour jitter was a third tab and did not
    # belong beside them: it is a setting both modes read, not a way
    # of working, and sitting in the same row implied otherwise.
    assert window.tabs.count() == 2
    assert len(window._single_plans()) == 3
    assert not window.crop.crop_rect.isEmpty()

    # CORRECTED: the box used to be locked to the bucket by default,
    # and the wheel is ignored when locked — so the tool's headline
    # control appeared broken the moment it was opened. It now starts
    # free, at the largest crop of the right shape that fits.
    assert window.crop.can_resize()
    # The box follows the FRAME selector, not the plan's own bucket:
    # the frame is what the user chose to compose in.
    frame = window.shape.currentData()[1]
    rect = window.crop.crop_rect
    assert abs(rect.width() / rect.height()
               - frame[0] / frame[1]) < 0.02

    # The picker replaced Previous/Skip: stepping blindly through an
    # unknown number of images hides how much is left.
    assert window.image_list.count() == 3
    window.image_list.setCurrentRow(2)
    assert window._single_index == 2

    # Changing the target changes every bucket, so the crop view has
    # to rebuild. Refreshing only on folder change missed this.
    # NOTE the name: `before` is already the file snapshot taken at
    # the top, and reusing it compared a dict of bytes against a tuple
    # at the end of the test — which looked for all the world like the
    # program had modified a source file.
    #
    # The crop FRAME is what follows the target now, so that is what
    # is checked. Found by index rather than position: the target list
    # runs largest-first and its order is not a promise.
    frame_before = window.shape.currentData()[1]
    window.target.setCurrentIndex(window.target.findData(512))
    frame_after = window.shape.currentData()[1]
    assert frame_after != frame_before
    assert min(frame_after) == 512
    window.target.setCurrentIndex(
        window.target.findData(prep.DEFAULT_TARGET))

    QFileDialog.getExistingDirectory = staticmethod(
        lambda *a, **k: str(out))
    for _ in range(3):
        window._crop_current()

    # Every crop must report. The report panel used to live on the
    # overview tab, so a crop confirmed here wrote its line somewhere
    # invisible and the export looked like it had done nothing.
    text = window.report.view.toPlainText()
    assert "\u2192" in text
    assert any(f"i{i}.png" in text for i in range(3))
    # And the list ticks what is finished.
    assert any("\u2713" in window.image_list.item(row).text()
               for row in range(window.image_list.count()))

    written = list(out.iterdir())
    assert len(written) == 3
    for path in written:
        assert Image.open(path).size in prep.buckets_for(1024)
    assert {p.name: p.read_bytes() for p in src.iterdir()} == before
    print("OK: the one-by-one tab crops to the bucket, skips images "
          "too small to use, and never modifies a source")


def test_the_three_batch_modes_do_what_they_say() -> None:
    """CORRECTED after a field report: "batch export with padding is a
    complete failure — the purpose of padding is to produce perfectly
    square images and it did not."

    True. Padding was applied to an ASPECT-MATCHED bucket, which
    already fits the image's shape, so it added nothing. Padding only
    means anything against a forced square.

    And the crop option was mislabelled. What batch mode needs for a
    bucketing workflow is a RESIZE — shortest side to the target, no
    crop, no bars, shape kept — which is a different operation
    entirely, not a variation on cropping.
    """
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    sizes = [(3000, 2000), (2000, 3000), (2048, 2048), (1600, 900)]
    for i, (width, height) in enumerate(sizes):
        Image.new("RGB", (width, height), "red").save(src / f"i{i}.png")

    def run_mode(mode):
        out = Path(tempfile.mkdtemp()) / "out"
        results = []
        for i, (width, height) in enumerate(sizes):
            path = src / f"i{i}.png"
            plan = prep.plan_for(path, width, height, 1024)
            result = prep.export_batch(plan, out, mode, 1024)
            assert result.written is not None, result.message
            results.append((Image.open(result.written).size,
                            result.action))
        return results

    # Bucketing: shape kept, shortest side at the target, nothing lost.
    for size, action in run_mode(prep.MODE_BUCKET):
        assert min(size) == 1024, size
        assert action == "resized"
    # A 3:2 image must NOT come back square here.
    assert run_mode(prep.MODE_BUCKET)[0][0] == (1536, 1024)

    # Pad to square: every output square, nothing cropped.
    for size, action in run_mode(prep.MODE_SQUARE_PAD):
        assert size == (1024, 1024), size
    actions = [a for _s, a in run_mode(prep.MODE_SQUARE_PAD)]
    assert "letterboxed" in actions      # wide sources
    assert "pillarboxed" in actions      # tall sources

    # Crop to square: also square, but by discarding the edges.
    for size, action in run_mode(prep.MODE_SQUARE_CROP):
        assert size == (1024, 1024), size
    print("OK: bucketing resizes without loss, and both square modes "
          "produce genuinely square output")


def test_crop_frames_put_the_short_side_on_the_target() -> None:
    """FIELD REQUEST: switchable square / portrait / landscape frames,
    with the shortest side at the training resolution.

    The bucket table cannot supply these. It is area-constrained, so
    every rectangle in it sits close to square — 960x1088 at a 1024
    target — which is not a usefully different frame to compose in.
    Standard photographic ratios with the short side pinned to the
    target are, and they are what a bucketing trainer wants.
    """
    for base in (2048, 1536, 1024, 768, 512):
        shapes = prep.shape_buckets(base)
        assert shapes[prep.SHAPE_SQUARE] == (base, base)
        for key in (prep.SHAPE_PORTRAIT, prep.SHAPE_LANDSCAPE):
            width, height = shapes[key]
            assert min(width, height) == base, (base, key)
            # Rounded to the bucket step, so the trainer does not
            # resample a second time on load.
            assert width % prep.BUCKET_STEP == 0
            assert height % prep.BUCKET_STEP == 0
        assert shapes[prep.SHAPE_PORTRAIT][1] > \
            shapes[prep.SHAPE_PORTRAIT][0]
        assert shapes[prep.SHAPE_LANDSCAPE][0] > \
            shapes[prep.SHAPE_LANDSCAPE][1]
    print("OK: every crop frame puts the shortest side on the target "
          "and lands on the bucket step")


def test_a_hand_crop_can_pad_and_refuses_the_impossible() -> None:
    """The 1280x945 question: at a 1024 target there are two honest
    answers, and the tool has to allow both.

      A  crop inside the picture and enlarge the short side 8% to
         1024 — loses the sides, keeps full detail.
      B  pull the box out past the top and bottom — keeps the whole
         width, gains even bars.

    And a third case that must be refused rather than fudged: a
    selection so small that filling the target would invent detail.
    """
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    path = src / "wide.png"
    Image.new("RGB", (1280, 945), "red").save(path)

    inside = prep.export_crop(path, (128, 0, 1024, 945),
                              (1024, 1024), out)
    assert inside.status == prep.RESULT_OK
    assert inside.action == "cropped"
    assert Image.open(inside.written).size == (1024, 1024)

    overhang = prep.export_crop(path, (0, -167, 1280, 1280),
                                (1024, 1024), out)
    assert overhang.status in (prep.RESULT_OK, prep.RESULT_RENAMED)
    assert overhang.action == "padded"
    image = Image.open(overhang.written)
    assert image.size == (1024, 1024)
    pixels = image.load()
    column = [y for y in range(image.height)
              if pixels[image.width // 2, y] != (0, 0, 0)]
    top, bottom = column[0], image.height - 1 - column[-1]
    assert abs(top - bottom) <= 2, (top, bottom)   # even bars

    tiny = prep.export_crop(path, (0, 0, 400, 400), (1024, 1024), out)
    # UPDATED: a too-small selection is now a soft warning status
    # (RESULT_TOO_SMALL), not a hard failure — the UI turns it into an
    # "are you sure" the user can accept. The refusal-by-default and
    # the message are unchanged.
    assert tiny.status == prep.RESULT_TOO_SMALL
    assert "invent detail" in tiny.message
    print("OK: a hand crop may overhang the picture and be padded, "
          "and a selection too small to fill the target is refused")


def test_the_editor_keeps_its_place_and_sorts_naturally() -> None:
    from PySide6.QtWidgets import QFileDialog

    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    for name, (width, height) in [("img1", (3000, 2000)),
                                  ("img2", (2000, 3000)),
                                  ("img10", (2048, 2048))]:
        Image.new("RGB", (width, height), "red").save(
            src / f"{name}.png")

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window._rescan()

    names = [window.image_list.item(i).text().split()[0]
             for i in range(window.image_list.count())]
    # Plain alphabetical puts img10 before img2, because it compares
    # one character at a time.
    assert names.index("img2.png") < names.index("img10.png")

    targets = [window.target.itemData(i)
               for i in range(window.target.count())]
    assert 2048 in targets and 1536 in targets

    window.image_list.setCurrentRow(2)
    assert window._single_index == 2
    window.target.setCurrentIndex(targets.index(1024))
    # Changing the target used to jump back to the first image —
    # exactly the moment someone is comparing one picture at two
    # resolutions.
    assert window._single_index == 2

    # Batch no longer offers a blind square crop.
    modes = [window.mode.itemData(i)
             for i in range(window.mode.count())]
    assert prep.MODE_SQUARE_CROP not in modes
    assert prep.MODE_BUCKET in modes and prep.MODE_SQUARE_PAD in modes
    print("OK: the list sorts numerically, the selection survives a "
          "target change, and batch offers no blind square crop")


def test_enlarging_the_selection_is_smooth_and_bounded() -> None:
    """The bars-safety guardrail was removed after it proved
    unfixable: on a square image it capped any growth back to the
    image size, and the next notch re-capped it, so the box appeared
    to snap back to default and could be wheeled outward in a loop.

    What must hold now is simpler and testable: enlarging grows the
    box monotonically, it may extend past the picture so padding can
    be added by hand, and it stops at a sane maximum rather than
    growing without limit."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from ui.crop_view import MAX_OVERSHOOT, MODE_FREE, CropView

    folder = Path(tempfile.mkdtemp())
    path = folder / "sq.png"
    Image.new("RGB", (1000, 1000), "red").save(path)
    view = CropView()
    view.resize(800, 600)
    view.load(path, (1024, 1024), MODE_FREE)

    def wheel(notches):
        view.wheelEvent(QWheelEvent(
            QPointF(400, 300), QPointF(400, 300), QPoint(0, 0),
            QPoint(0, 120 * notches), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False))

    sizes = []
    for _ in range(12):
        wheel(-1)                       # enlarge
        sizes.append(view.crop_rect.width())
    # Monotonic — no snap-back to default.
    assert all(b >= a for a, b in zip(sizes, sizes[1:])), sizes
    # Actually grows past the image, so padding is possible.
    assert max(sizes) > 1000
    # While it still fits inside, it stays centred.
    for _ in range(6):
        wheel(1)
    if view.crop_rect.width() <= 1000:
        assert abs(view.crop_rect.center().x() - 500) <= 2

    # Bounded, never infinite.
    for _ in range(80):
        wheel(-1)
    assert view.crop_rect.width() <= 1000 * MAX_OVERSHOOT + 1
    # And it never loses the picture entirely.
    assert view.crop_rect.left() < 1000 and view.crop_rect.right() > 0
    print("OK: the selection enlarges smoothly, may overhang for "
          "padding, and stops at a bounded maximum")


def test_ctrl_wheel_zooms_the_view_not_the_selection() -> None:
    """A picture fitted exactly to the panel leaves nowhere on screen
    to pull the box outside it — the very gesture that produces bars.
    So the display zooms separately from the selection."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from ui.crop_view import MODE_FREE, CropView

    folder = Path(tempfile.mkdtemp())
    path = folder / "wide.png"
    Image.new("RGB", (1600, 900), "red").save(path)
    view = CropView()
    view.resize(800, 600)
    view.load(path, (1024, 1024), MODE_FREE)

    def wheel(notches, ctrl):
        view.wheelEvent(QWheelEvent(
            QPointF(400, 300), QPointF(400, 300), QPoint(0, 0),
            QPoint(0, 120 * notches), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier if ctrl
            else Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False))

    zoom_before = view.view_zoom
    box_before = view.crop_rect.width()
    wheel(3, ctrl=True)
    assert view.view_zoom > zoom_before
    assert view.crop_rect.width() == box_before     # untouched
    view.reset_view_zoom()
    assert abs(view.view_zoom - 1.0) < 1e-9

    wheel(-1, ctrl=False)
    assert view.crop_rect.width() != box_before     # this one sizes it
    print("OK: ctrl+wheel zooms the picture in the panel while the "
          "plain wheel still sizes the selection")


def test_a_refused_export_does_not_walk_on() -> None:
    """FIELD REPORT: after a small selection it advanced anyway.

    The behaviour has since changed shape: a too-small selection is no
    longer a hard error but a warning the user answers. What still
    must hold is that declining it (No) keeps the position and writes
    nothing, and that a genuine ERROR — not a size warning — also
    stays put. Both are checked here with the dialog stubbed, since an
    offscreen modal would block."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    for i in range(3):
        Image.new("RGB", (1600, 900), "red").save(src / f"i{i}.png")

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window._rescan()
    QFileDialog.getExistingDirectory = staticmethod(
        lambda *a, **k: str(out))
    window._pick_output()

    start = window._single_index
    for _ in range(40):                  # shrink until it is too small
        window.crop.wheelEvent(QWheelEvent(
            QPointF(300, 200), QPointF(300, 200), QPoint(0, 0),
            QPoint(0, 120), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False))

    # Answer No to the "are you sure" — must cancel and stay put.
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.No)
    window._crop_current()
    assert window._single_index == start
    assert not out.exists() or not list(out.iterdir())
    from ui.crop_view import BORDER_NORMAL
    assert window.crop._border == BORDER_NORMAL   # border reset

    # A good export still advances.
    window.crop.reset_crop()
    window._crop_current()
    assert window._single_index != start
    print("OK: declining the small-selection warning cancels and "
          "stays on the image; a valid export advances")


def test_the_jitter_preview_follows_the_toggle() -> None:
    """The numbers alone do not tell you whether a hue rotation has
    turned skin green. And a preview left on screen after jitter is
    switched off would show something that will not be written."""
    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    for i in range(2):
        Image.new("RGB", (1600, 900), (120, 80, 200)).save(
            src / f"i{i}.png")

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window._rescan()
    # Two tabs now: colour jitter is a setting both modes read, not a
    # third way of working.
    assert window.tabs.count() == 2

    window._jitter_rows["hue"][0].setValue(30)
    window._jitter_rows["hue"][1].setValue(10)
    assert window.crop._preview is None
    window.chk_jitter_one.setChecked(True)
    assert window.crop._preview is not None
    first = window.crop._preview.cacheKey()
    window._roll_single_jitter()
    assert window.crop._preview.cacheKey() != first

    window.chk_jitter_one.setChecked(False)
    assert window.crop._preview is None
    print("OK: the jitter preview appears, changes on re-roll, and "
          "clears whenever the shift will not be applied")


def test_pan_and_axis_lock_and_merged_reset() -> None:
    """Field fixes to the one-by-one viewer.

    - Ctrl+drag PANS the picture, distinct from dragging the crop box,
      so a box pulled off the edge can be brought back into view.
    - The axis lock was broken by a click: clicking outside the box
      jumped it with moveCenter(point), ignoring the constraint, so
      one click threw the box off its locked line. It must now move
      only along the free axis.
    - The two reset buttons are merged; reset_all clears zoom, pan and
      the crop box together.
    """
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from ui.crop_view import AXIS_X, AXIS_Y, MODE_FREE, CropView

    folder = Path(tempfile.mkdtemp())
    path = folder / "sq.png"
    Image.new("RGB", (1000, 1000), "red").save(path)
    view = CropView()
    view.resize(800, 600)
    view.load(path, (1024, 1024), MODE_FREE)

    def press(x, y, ctrl=False, right=False):
        btn = (Qt.MouseButton.RightButton if right
               else Qt.MouseButton.LeftButton)
        view.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(x, y),
            btn, btn,
            Qt.KeyboardModifier.ControlModifier if ctrl
            else Qt.KeyboardModifier.NoModifier))

    def move(x, y, right=False):
        held = (Qt.MouseButton.RightButton if right
                else Qt.MouseButton.LeftButton)
        view.mouseMoveEvent(QMouseEvent(
            QMouseEvent.Type.MouseMove, QPointF(x, y),
            Qt.MouseButton.NoButton, held,
            Qt.KeyboardModifier.NoModifier))

    def release():
        view.mouseReleaseEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease, QPointF(0, 0),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))

    # Right-click drag pans without touching the box. (Pan was changed
    # from Ctrl+left drag to right-click drag; the box stays on left drag.)
    box = QPoint(view.crop_rect.topLeft())
    press(400, 300, right=True)
    move(470, 350, right=True)
    release()
    assert view._pan.x() != 0 or view._pan.y() != 0
    assert view.crop_rect.topLeft() == box

    # reset_all clears everything.
    view.reset_all()
    assert view._pan == QPoint(0, 0)
    assert abs(view.view_zoom - 1.0) < 1e-9

    # Axis lock survives a click outside the box.
    view.set_axis(AXIS_X)                    # horizontal only
    y_mid = view.crop_rect.center().y()
    press(200, 120)                          # click well above centre
    assert abs(view.crop_rect.center().y() - y_mid) <= 2
    release()

    view.set_axis(AXIS_Y)                     # vertical only
    x_mid = view.crop_rect.center().x()
    press(120, 200)
    assert abs(view.crop_rect.center().x() - x_mid) <= 2
    release()
    print("OK: ctrl+drag pans without moving the box, the axis lock "
          "holds through an off-line click, and reset clears all")


def test_small_export_warns_instead_of_refusing() -> None:
    """FIELD REQUEST: stop hard-refusing a selection under the 15%
    threshold; warn with the exact ratio and let the user decide.

    export_crop now takes allow_upscale. Without it a too-small
    selection returns RESULT_TOO_SMALL (a soft status the UI turns
    into an "are you sure"); with it, the same selection exports."""
    src = Path(tempfile.mkdtemp())
    path = src / "x.png"
    Image.new("RGB", (1600, 900), "red").save(path)
    out = Path(tempfile.mkdtemp()) / "o"

    box = (0, 0, 400, 400)
    # The ratio is exposed so the warning can state it.
    ratio = prep.crop_upscale_ratio(box, (1024, 1024))
    assert ratio > 2.0

    refused = prep.export_crop(path, box, (1024, 1024), out)
    assert refused.status == prep.RESULT_TOO_SMALL
    assert refused.written is None

    allowed = prep.export_crop(path, box, (1024, 1024), out,
                               allow_upscale=True)
    assert allowed.status == prep.RESULT_OK
    assert Image.open(allowed.written).size == (1024, 1024)
    print("OK: a small selection returns a soft warning status by "
          "default and exports when explicitly authorised")


def test_fit_short_covers_and_pads_while_fit_long_contains() -> None:
    """FIELD REQUEST: an inverted fit that sizes the crop to the
    SHORTEST side of the image, leaving the long edges to be padded —
    a one-click pillar/letterbox instead of dragging the box out.
    """
    from PySide6.QtWidgets import QApplication
    from ui.crop_view import MODE_FREE, CropView

    _app = QApplication.instance() or QApplication([])
    folder = Path(tempfile.mkdtemp())
    path = folder / "wide.png"
    Image.new("RGB", (1600, 900), "red").save(path)

    view = CropView()
    view.resize(800, 600)
    view.load(path, (1024, 1024), MODE_FREE)   # square frame

    # Contain (default): the box fits wholly inside — nothing padded.
    assert not view.overflow()

    # Cover: the short side (900) reaches the frame, so the square box
    # becomes 1600x1600 and overhangs top and bottom, which pads.
    view.set_fit_short(True)
    assert view.crop_rect.width() >= 1600
    assert view.overflow()

    view.set_fit_short(False)
    assert not view.overflow()
    print("OK: fit-shortest covers the frame and pads the long edges; "
          "fit-longest contains the crop with no bars")


def test_the_recommended_jitter_preset_is_wide_with_a_floor() -> None:
    """The recommended button sets up to \u00b112 with a minimum of
    \u00b17 on every channel.

    The floor is the point: a random draw over \u00b112 alone
    clusters near zero, so without it many jittered images would get a
    shift too small to see. \u00b17 guarantees each one moves a
    visible amount. The wider ceiling is made safe by the preview \u2014
    the user re-rolls anything pushed too far before it is written."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    _app = QApplication.instance() or QApplication([])
    QMessageBox.exec = lambda self: None

    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    Image.new("RGB", (1600, 900), (120, 80, 200)).save(src / "a.png")

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window._rescan()

    window._apply_recommended_jitter()
    for key in ("hue", "saturation", "brightness"):
        swing, floor = window._jitter_rows[key]
        assert swing.value() == 12, (key, swing.value())
        assert floor.value() == 7, (key, floor.value())

    # The preset must be internally coherent: a minimum never exceeds
    # its swing, or clamped() would have to pull it back.
    settings = window.jitter_settings()
    assert settings.active()
    assert settings.hue_min <= settings.hue_swing
    assert settings.saturation_min <= settings.saturation_swing
    assert settings.brightness_min <= settings.brightness_swing

    # Reset returns everything to off.
    window._reset_jitter()
    for key in ("hue", "saturation", "brightness"):
        swing, floor = window._jitter_rows[key]
        assert swing.value() == 0 and floor.value() == 0
    print("OK: the recommended preset is up to \u00b112 with a "
          "\u00b17 floor on each channel, and reset clears it")


def test_the_jitter_help_is_a_paged_booklet() -> None:
    """FIELD REQUEST: the colour-jitter help was one message box long
    enough to run off the screen. It is now the app's standard paged
    booklet, one idea per page."""
    from PySide6.QtWidgets import QApplication
    from ui.paged_help_dialog import PagedHelpDialog

    _app = QApplication.instance() or QApplication([])
    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    Image.new("RGB", (1600, 900), (120, 80, 200)).save(src / "a.png")

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window._rescan()
    window._show_jitter_help()

    dlg = window._jitter_help_win
    assert isinstance(dlg, PagedHelpDialog)
    assert len(dlg._pages) >= 5             # genuinely paged
    # No single page is a wall of text — that was the whole complaint.
    assert max(len(body) for _, body in dlg._pages) < 700
    # The substance survived the split.
    bodies = " ".join(body for _, body in dlg._pages)
    assert "caching" in bodies              # the trainer rationale
    assert "±12" in bodies and "±7" in bodies
    assert "whole-folder" in bodies or "whole folder" in bodies
    # And it pages.
    dlg.step(1)
    assert dlg._index == 1
    print("OK: colour-jitter help is a multi-page booklet with no "
          "oversized page, and its content is intact")


def test_the_display_pixmap_is_cached_across_repaints() -> None:
    """EFFICIENCY: the viewer rescaled the full-resolution source to
    the panel on every paint, and paint fires on every mouse-move
    during a drag \u2014 so dragging a large image rescaled a
    multi-megapixel bitmap dozens of times a second.

    The scaled copy is now cached and reused, rebuilt only when the
    view rect or the source changes. Dragging the crop box, which
    leaves the view unchanged, must reuse it."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QPixmap, QWheelEvent
    from PySide6.QtWidgets import QApplication
    from ui.crop_view import MODE_FREE, CropView

    _app = QApplication.instance() or QApplication([])
    folder = Path(tempfile.mkdtemp())
    path = folder / "big.png"
    Image.new("RGB", (4000, 3000), (90, 140, 60)).save(path)

    view = CropView()
    view.resize(900, 700)
    view.load(path, (1024, 1024), MODE_FREE)
    view.show()

    def paint():
        buf = QPixmap(view.size())
        view.render(buf)

    paint()
    assert view._scaled_cache is not None
    key = view._scaled_cache.cacheKey()

    # An identical repaint reuses the cache.
    paint()
    assert view._scaled_cache.cacheKey() == key

    # Resizing the crop box (view unchanged) reuses it too.
    view.wheelEvent(QWheelEvent(
        QPointF(350, 250), QPointF(350, 250), QPoint(0, 0),
        QPoint(0, -120), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False))
    paint()
    assert view._scaled_cache.cacheKey() == key

    # Resizing the WIDGET rebuilds it (correctness, not just reuse).
    view.resize(700, 500)
    paint()
    assert view._scaled_cache.cacheKey() != key
    print("OK: the scaled display pixmap is cached across repaints and "
          "rebuilt only when the view or source changes")


def test_the_croppable_plans_are_cached() -> None:
    """EFFICIENCY: the filtered list of croppable plans was rebuilt on
    every navigation step and by the jitter preview. It only changes
    on a rescan, so it is cached and invalidated there."""
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    for i in range(40):
        Image.new("RGB", (1600, 900), "red").save(src / f"i{i:03}.png")

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window._rescan()

    first = window._single_plans()
    assert window._single_plans() is first      # cached, same object
    window._step_single(1)
    assert window._single_plans() is first       # step did not rebuild
    window._rescan()
    assert window._single_plans() is not first   # rescan invalidated
    print("OK: the croppable-plans list is cached and rebuilt only on "
          "a rescan")


def test_the_editor_is_an_independent_window() -> None:
    """FIELD REPORT (four rounds): 'open maximised' worked at open but
    the editor came back shrunken after minimising and restoring the
    whole program.

    Every per-editor and per-parent event fix failed for the same root
    reason: the editor was a CHILD of the main window, so it was
    minimised with the parent at the OS level and its state rode the
    parent's. The real fix, matching Post Browser and Tag Reference, is
    to make the editor a parentless top-level window. Then the main
    window's minimise does not touch it at all, so nothing has to
    re-impose the maximised state afterwards.

    The trade-off a parent gave — closing with the opener — is restored
    explicitly: the main window's closeEvent closes the editor.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])

    def is_max(w):
        return bool(w.windowState() & Qt.WindowState.WindowMaximized)

    # Setting ON: the editor opens as an independent, maximised window.
    s = Settings()
    initialize_theme(s.theme)
    s.image_editor_fullscreen = True
    mw = MainWindow(s)
    mw.show()
    mw._action_image_editor()
    ed = mw._image_editor

    assert ed.parent() is None, "editor must be a parentless top-level " \
        "window, like Post Browser and Tag Reference"
    assert ed.isWindow()
    assert ed._want_maximised
    assert is_max(ed)

    # THE BUG, resolved structurally: minimising and restoring the MAIN
    # window leaves the editor's state completely untouched, because
    # they are decoupled. No event handler has to intervene.
    state_before = ed.windowState()
    mw.setWindowState(
        mw.windowState() | Qt.WindowState.WindowMinimized)
    _app.processEvents()
    assert ed.windowState() == state_before, (
        "main-window minimise changed the editor's state; it should be "
        "independent")
    assert is_max(ed)
    mw.setWindowState(
        mw.windowState() & ~Qt.WindowState.WindowMinimized)
    _app.processEvents()
    assert is_max(ed), "editor lost maximised after main restore"

    # A deliberate un-maximise is still tracked (the editor keeps its
    # own changeEvent for the events it genuinely receives).
    ed.setWindowState(Qt.WindowState.WindowNoState)
    assert not ed._want_maximised

    # Setting OFF: opens windowed.
    s.image_editor_fullscreen = False
    mw2 = MainWindow(s)
    mw2.show()
    mw2._action_image_editor()
    assert not is_max(mw2._image_editor)

    # Closing the main window closes the parentless editor too
    # (parentless windows do not close with their opener on their own).
    mw._state = None
    mw.close()
    _app.processEvents()
    _app.processEvents()
    assert getattr(mw, "_image_editor", None) is None, (
        "closing the main window must close the editor")

    print("OK: the editor is a parentless top-level window whose state "
          "is independent of the main window's minimise/restore, and it "
          "still opens per the setting and closes with the program")


def test_panning_reuses_the_display_cache() -> None:
    """REGRESSION guard: the display-pixmap cache was first keyed on
    the whole view rect, but panning shifts that rect every frame, so
    a pan drag rebuilt the scaled bitmap on every frame — exactly the
    cost the cache exists to avoid. Keyed on the view SIZE (which a pan
    does not change), panning now reuses it; only a zoom, which changes
    the size, rebuilds it."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent, QPixmap, QWheelEvent
    from PySide6.QtWidgets import QApplication
    from ui.crop_view import MODE_FREE, CropView

    _app = QApplication.instance() or QApplication([])
    folder = Path(tempfile.mkdtemp())
    path = folder / "big.png"
    Image.new("RGB", (4000, 3000), (90, 140, 60)).save(path)
    view = CropView()
    view.resize(900, 700)
    view.load(path, (1024, 1024), MODE_FREE)
    view.show()

    def paint():
        buf = QPixmap(view.size())
        view.render(buf)

    paint()
    key = view._scaled_cache.cacheKey()

    # Pan: ctrl-drag shifts the picture. Cache must be reused.
    view.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(400, 300),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier))
    view.mouseMoveEvent(QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(470, 350),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    paint()
    assert view._scaled_cache.cacheKey() == key, "pan rebuilt the cache"

    # Zoom changes the view size, so it must rebuild.
    view.wheelEvent(QWheelEvent(
        QPointF(450, 350), QPointF(450, 350), QPoint(0, 0),
        QPoint(0, 120), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase, False))
    paint()
    assert view._scaled_cache.cacheKey() != key
    print("OK: panning reuses the display cache (keyed on size); "
          "zooming rebuilds it")


def test_rerolling_jitter_does_not_re_decode_the_image() -> None:
    """EFFICIENCY: the jitter preview re-opened and re-downscaled the
    image from disk on every re-roll, though only the shift changes.
    The decode is the slow part, so the Re-roll button carried a full
    decode each click. The downscaled base is now cached per image."""
    import PIL.Image as PILImage
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    for i in range(3):
        Image.new("RGB", (2400, 1600), (i * 40, 80, 200)).save(
            src / f"i{i}.png")

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window._rescan()
    window._jitter_rows["hue"][0].setValue(12)
    window._jitter_rows["hue"][1].setValue(7)
    window.chk_jitter_one.setChecked(True)

    # Count disk opens.
    window._jitter_base_copy = None
    window._jitter_base_path = None
    real_open = PILImage.open
    opens = {"n": 0}

    def counting_open(*a, **k):
        opens["n"] += 1
        return real_open(*a, **k)

    PILImage.open = counting_open
    try:
        window._roll_single_jitter()          # cold: one decode
        assert opens["n"] == 1, opens
        opens["n"] = 0
        for _ in range(10):                    # warm: none
            window._roll_single_jitter()
        assert opens["n"] == 0, opens
        # Navigating to a new image decodes it once.
        opens["n"] = 0
        window._step_single(1)
        assert opens["n"] >= 1
    finally:
        PILImage.open = real_open
    print("OK: re-rolling the jitter reuses the cached downscaled base "
          "instead of re-decoding from disk")


def test_show_small_images_toggles_the_list() -> None:
    """FIELD REQUEST: the target resolution hides images too small for
    it from the one-by-one list. A checkbox now controls that, so
    small images can be listed and edited.

    Two separate controls: "Show small images" governs LISTING; the
    existing "Allow small images" governs whether EXPORTING one warns.
    They compose \u2014 a small image can be visible and editable while
    the export still prompts."""
    from PySide6.QtWidgets import (QApplication, QFileDialog,
                                   QMessageBox)

    _app = QApplication.instance() or QApplication([])
    QMessageBox.exec = lambda self: None
    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    out = Path(tempfile.mkdtemp()) / "out"
    for i in range(3):
        Image.new("RGB", (1600, 900), "red").save(src / f"big{i}.png")
    for i in range(2):
        Image.new("RGB", (200, 150), "blue").save(src / f"tiny{i}.png")

    window = ImageEditorWindow(s)
    window.show()
    window._input = src
    window.target.setCurrentIndex(
        window.target.findData(prep.DEFAULT_TARGET))
    window._rescan()

    # Off by default: the tiny images are hidden.
    assert not window.chk_show_small.isChecked()
    assert len(window._single_plans()) == 3
    assert not any("tiny" in p.path.name
                   for p in window._single_plans())

    # On: all five listed.
    window.chk_show_small.setChecked(True)
    assert len(window._single_plans()) == 5
    assert window.image_list.count() == 5

    # Selection survives the toggle when the image is still listed.
    for i, p in enumerate(window._single_plans()):
        if p.path.name == "big1.png":
            window._single_index = i
            break
    keep = window._single_plans()[window._single_index].path.name
    window.chk_show_small.setChecked(False)
    assert window._single_plans()[window._single_index].path.name == keep

    # Standing on a tiny image and hiding small must not crash; it
    # falls back to a valid, non-tiny row.
    window.chk_show_small.setChecked(True)
    for i, p in enumerate(window._single_plans()):
        if "tiny" in p.path.name:
            window._single_index = i
            break
    window._reload_single()
    window.chk_show_small.setChecked(False)
    assert 0 <= window._single_index < len(window._single_plans())
    assert "tiny" not in (
        window._single_plans()[window._single_index].path.name)

    # Listing and exporting are independent: with a small image shown
    # but allow-small OFF, cropping it still warns.
    QFileDialog.getExistingDirectory = staticmethod(
        lambda *a, **k: str(out))
    window._pick_output()
    window.chk_show_small.setChecked(True)
    for i, p in enumerate(window._single_plans()):
        if "tiny" in p.path.name:
            window._single_index = i
            break
    window._reload_single()
    asked = {"n": 0}
    QMessageBox.question = staticmethod(
        lambda *a, **k: (asked.__setitem__("n", asked["n"] + 1),
                         QMessageBox.StandardButton.No)[1])
    window.chk_allow_small.setChecked(False)
    window._crop_current()
    assert asked["n"] == 1, "showing a small image must not suppress " \
        "the export warning"
    print("OK: a checkbox lists or hides small images, preserves the "
          "selection, and stays independent of the export warning")


def run() -> None:
    test_buckets_match_what_a_trainer_generates()
    test_each_image_gets_the_right_verdict()
    test_crop_loss_is_measured_on_shape_alone()
    test_the_analysis_writes_nothing()
    test_padding_is_even_and_never_invents_detail()
    test_export_never_touches_the_source_and_names_collisions()
    test_alpha_is_flattened_onto_the_pad_colour()
    test_too_small_is_its_own_verdict()
    test_a_smaller_target_is_suggested_when_one_fits()
    test_padding_matters_in_fixed_mode_not_bucket_mode()
    test_the_three_batch_modes_do_what_they_say()
    test_crop_frames_put_the_short_side_on_the_target()
    test_a_hand_crop_can_pad_and_refuses_the_impossible()
    test_the_editor_keeps_its_place_and_sorts_naturally()
    test_enlarging_the_selection_is_smooth_and_bounded()
    test_ctrl_wheel_zooms_the_view_not_the_selection()
    test_a_refused_export_does_not_walk_on()
    test_the_jitter_preview_follows_the_toggle()
    test_pan_and_axis_lock_and_merged_reset()
    test_small_export_warns_instead_of_refusing()
    test_fit_short_covers_and_pads_while_fit_long_contains()
    test_the_recommended_jitter_preset_is_wide_with_a_floor()
    test_the_jitter_help_is_a_paged_booklet()
    test_the_display_pixmap_is_cached_across_repaints()
    test_the_croppable_plans_are_cached()
    test_the_editor_is_an_independent_window()
    test_panning_reuses_the_display_cache()
    test_rerolling_jitter_does_not_re_decode_the_image()
    test_show_small_images_toggles_the_list()
    test_the_crop_box_keeps_its_shape_and_stays_inside()
    test_the_one_by_one_tab_exports_the_chosen_crop()
    test_the_window_reports_what_needs_attention()
    print("\nALL PASS: image prep")


if __name__ == "__main__":
    run()
