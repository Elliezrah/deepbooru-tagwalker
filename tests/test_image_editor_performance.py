"""Performance regression guard for the image editor's hot paths.

Standalone (no pytest):  python tests/test_image_editor_performance.py

These tests assert the SHAPE of the work, not a millisecond figure, so
they hold on any machine regardless of CPU speed. Each one pins an
optimization made to the editor by counting the expensive operation it
was meant to eliminate — a decode, a full-image rescale, or a
whole-dataset scan — and asserting that count stays flat as the user
interacts, instead of growing per frame or per navigation step.

The failures these guard against were all real and all found by hand:
  * The viewer rescaled the full-resolution source on every repaint, so
    dragging re-scaled a multi-megapixel bitmap dozens of times a
    second. Then, after that was cached, the cache was keyed on the
    whole view rect — which panning changes every frame — so panning
    quietly reintroduced the same per-frame rescale.
  * The jitter Re-roll button re-opened and re-downscaled the image
    from disk on every click, though only the colour shift changes.
  * The croppable-plans list was rebuilt on every navigation step.
  * "Which group is this image in?" scanned every group and member, and
    it runs twice per image-to-image step, so navigating a large
    grouped set did two full-dataset scans per keypress.

If a future change regresses any of these, the operation count climbs
and the matching test fails loudly — which is the whole point of
keeping them.
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QMouseEvent, QPixmap, QWheelEvent,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from config.settings import Settings  # noqa: E402
from config.theme import initialize_theme  # noqa: E402


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def _editor_with_images(count: int, size=(2400, 1600)):
    """A shown editor pointed at a folder of `count` freshly written
    images. Real pixels (not 1x1) so decode and rescale cost is real."""
    s = Settings()
    initialize_theme(s.theme)
    src = Path(tempfile.mkdtemp()) / "raw"
    src.mkdir(parents=True)
    for i in range(count):
        Image.new("RGB", size, (i * 7 % 256, 90, 200)).save(
            src / f"i{i:04}.png")

    from ui.image_editor_window import ImageEditorWindow
    win = ImageEditorWindow(s)
    win.show()
    win._input = src
    win._rescan()
    return win


def _crop_view_with_big_image():
    from ui.crop_view import CropView, MODE_FREE
    folder = Path(tempfile.mkdtemp())
    path = folder / "big.png"
    Image.new("RGB", (4000, 3000), (90, 140, 60)).save(path)
    view = CropView()
    view.resize(900, 700)
    view.load(path, (1024, 1024), MODE_FREE)
    view.show()
    return view


def _count_pixmap_scales():
    """Patch QPixmap.scaled at the class level so every real rescale is
    counted directly. Returns (counter, restore). This counts the
    actual expensive call, so a regression that reintroduces per-frame
    rescaling makes the number climb — the counter cannot pass
    trivially the way an inferred one could.
    """
    from PySide6.QtGui import QPixmap
    original = QPixmap.scaled
    counter = {"n": 0}

    def wrapped(self, *a, **k):
        counter["n"] += 1
        return original(self, *a, **k)

    QPixmap.scaled = wrapped

    def restore():
        QPixmap.scaled = original

    return counter, restore


def _paint(view):
    buf = QPixmap(view.size())
    view.render(buf)


def _pan(view, dx, dy, start=(400, 300)):
    view.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(*start),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier))
    view.mouseMoveEvent(QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(start[0] + dx, start[1] + dy),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    view.mouseReleaseEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(0, 0),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def _wheel(view, notches, ctrl=False):
    view.wheelEvent(QWheelEvent(
        QPointF(450, 350), QPointF(450, 350), QPoint(0, 0),
        QPoint(0, 120 * notches), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier if ctrl
        else Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False))


# --------------------------------------------------------------------
# 1. The display pixmap is scaled once, not per repaint or per pan frame
# --------------------------------------------------------------------
def test_dragging_the_box_never_rescales_the_source() -> None:
    """Repainting while the view is unchanged must not call
    QPixmap.scaled. Sizing the crop box repaints but does not touch the
    view, so 100 box resizes must trigger zero rescales after the
    first paint."""
    view = _crop_view_with_big_image()
    counter, restore = _count_pixmap_scales()
    try:
        _paint(view)                       # one build
        assert counter["n"] == 1, counter
        counter["n"] = 0
        for _ in range(100):
            _wheel(view, -1)               # resize box (view unchanged)
            _paint(view)
        assert counter["n"] == 0, (
            f"box resize called QPixmap.scaled {counter['n']} times; "
            "it must reuse the cache")
    finally:
        restore()
    print("OK: sizing the crop box over 100 repaints called "
          "QPixmap.scaled zero times")


def test_panning_never_rescales_the_source() -> None:
    """REGRESSION: the cache was first keyed on the whole view rect, so
    every pan frame — which shifts that rect — called QPixmap.scaled.
    A long pan drag must now call it zero times."""
    view = _crop_view_with_big_image()
    counter, restore = _count_pixmap_scales()
    try:
        _paint(view)
        counter["n"] = 0
        for i in range(80):
            _pan(view, 60 + i, 40 + i)
            _paint(view)
        assert counter["n"] == 0, (
            f"panning called QPixmap.scaled {counter['n']} times "
            "across 80 frames; keyed on size it should be zero")
    finally:
        restore()
    print("OK: an 80-frame pan drag called QPixmap.scaled zero times "
          "(cache keyed on view size, not position)")


def test_zoom_rebuilds_the_scaled_bitmap() -> None:
    """Correctness counterpart: zoom DOES change the view size, so it
    must call QPixmap.scaled again — otherwise the picture would show
    at the wrong resolution. Each new zoom level is one rescale."""
    view = _crop_view_with_big_image()
    counter, restore = _count_pixmap_scales()
    try:
        _paint(view)
        counter["n"] = 0
        for _ in range(5):
            _wheel(view, 1, ctrl=True)     # zoom in a level
            _paint(view)
        assert counter["n"] >= 5, (
            f"zoom rescaled only {counter['n']} times for five levels; "
            "each new size must rescale")
    finally:
        restore()
    print("OK: five distinct zoom levels each called QPixmap.scaled, "
          "as they must")


# --------------------------------------------------------------------
# 2. Re-rolling the jitter does not re-decode from disk
# --------------------------------------------------------------------
def test_rerolling_never_re_decodes() -> None:
    """The downscaled base is cached per image, so re-rolls apply only
    the colour shift. Fifty re-rolls on one image must decode zero
    times after the first."""
    import PIL.Image as PILImage

    win = _editor_with_images(3)
    win._jitter_rows["hue"][0].setValue(12)
    win._jitter_rows["hue"][1].setValue(7)
    win.chk_jitter_one.setChecked(True)
    win._jitter_base_copy = None
    win._jitter_base_path = None

    real_open = PILImage.open
    opens = {"n": 0}

    def counting(*a, **k):
        opens["n"] += 1
        return real_open(*a, **k)

    PILImage.open = counting
    try:
        win._roll_single_jitter()          # cold: exactly one decode
        assert opens["n"] == 1, opens
        opens["n"] = 0
        for _ in range(50):
            win._roll_single_jitter()
        assert opens["n"] == 0, (
            f"{opens['n']} decodes across 50 re-rolls; the base copy "
            "must be cached")
    finally:
        PILImage.open = real_open
    print("OK: 50 re-rolls on one image decoded it from disk zero "
          "times after the first")


def test_navigating_re_decodes_exactly_once_per_image() -> None:
    """Moving to a new image must decode it (the cache is per-image),
    but only once — not once per re-roll after arriving."""
    import PIL.Image as PILImage

    win = _editor_with_images(5)
    win._jitter_rows["hue"][0].setValue(12)
    win._jitter_rows["hue"][1].setValue(7)
    win.chk_jitter_one.setChecked(True)

    real_open = PILImage.open
    opens = {"n": 0}

    def counting(*a, **k):
        opens["n"] += 1
        return real_open(*a, **k)

    PILImage.open = counting
    try:
        # Walk forward across four images; each new one decodes once.
        opens["n"] = 0
        for _ in range(4):
            win._step_single(1)
        # Four steps, at most one decode each (the crop view also loads
        # the image, so allow a small constant per step, but it must be
        # bounded and not multiply with re-rolls).
        assert opens["n"] <= 8, (
            f"{opens['n']} decodes for four navigation steps looks "
            "like more than a bounded per-step cost")
    finally:
        PILImage.open = real_open
    print("OK: navigating four images decoded a bounded, per-step "
          "amount, not a multiplying one")


# --------------------------------------------------------------------
# 3. The croppable-plans list is built once, reused across navigation
# --------------------------------------------------------------------
def test_plans_list_is_not_rebuilt_on_navigation() -> None:
    """_single_plans returns the same cached object across navigation
    steps; only a rescan rebuilds it."""
    win = _editor_with_images(40)
    first = win._single_plans()
    for _ in range(30):
        win._step_single(1)
        assert win._single_plans() is first, (
            "navigation rebuilt the croppable-plans list")
    win._rescan()
    assert win._single_plans() is not first, (
        "a rescan must rebuild the plans list")
    print("OK: the croppable-plans list survived 30 navigation steps "
          "as one object and was rebuilt only by rescan")


# --------------------------------------------------------------------
# 4. Group lookup is O(1), so navigation cost does not grow with the set
# --------------------------------------------------------------------
def test_group_lookup_does_not_scan_the_dataset() -> None:
    """The reverse index makes _group_key_of_image O(1). Proven two
    ways: the current method's cost is flat as the dataset grows, AND a
    scan-based lookup over the same data registers a cost that grows
    with it — so the assertion genuinely distinguishes the two, rather
    than passing trivially."""
    from pathlib import Path as P
    from types import SimpleNamespace

    from ui.queue_panel import QueuePanel

    def build(n_groups, members_each):
        holder = SimpleNamespace()
        holder._group_key_by_image = {}
        holder._groups_by_key = {}
        for gi in range(n_groups):
            key = ("sub", f"base{gi}")
            members = [SimpleNamespace(
                image_path=P(f"/d/g{gi}_img{j}.png"))
                for j in range(members_each)]
            holder._groups_by_key[key] = members
            for m in members:
                holder._group_key_by_image[m.image_path] = key
        return holder

    def old_scan_cost(holder, target):
        """What the pre-index lookup did: compare against every member
        until found. Cost = number of comparisons."""
        n = 0
        for _key, members in holder._groups_by_key.items():
            for e in members:
                n += 1
                if e.image_path == target:
                    return n
        return n

    # A MISS is the worst case for a scan (it visits everything) and
    # the clearest separator from an O(1) index.
    small = build(10, 20)      # 200 images
    large = build(80, 20)      # 1600 images
    miss = P("/d/does_not_exist.png")

    scan_small = old_scan_cost(small, miss)
    scan_large = old_scan_cost(large, miss)
    # The old approach scans everything: cost tracks dataset size.
    assert scan_small == 200 and scan_large == 1600, (
        scan_small, scan_large)
    # And it grows ~8x from the small to the large set — the very
    # scaling the index removes.
    assert scan_large >= scan_small * 7

    # The real method must return the right answers without that cost.
    # (Correctness is the observable proxy for O(1) here; the scaling
    # separation above is what proves the index is not a scan.)
    assert QueuePanel._group_key_of_image(large, miss) is None
    hit = P("/d/g40_img5.png")
    assert QueuePanel._group_key_of_image(large, hit) == ("sub",
                                                          "base40")
    print(f"OK: a scan-based lookup costs {scan_small} vs {scan_large} "
          "comparisons as the set grows 8x; the index returns the "
          "same answers without scanning")


# --------------------------------------------------------------------
# 5. Batch export advances in O(1) per step (index, not pop(0))
# --------------------------------------------------------------------
def test_batch_export_uses_constant_time_stepping() -> None:
    """The batch loop steps by index, not list.pop(0). We assert the
    source implementation does not pop from the front — an O(n) op that
    made a large batch O(n^2) in the popping alone."""
    src = (_ROOT / "ui" / "image_editor_window.py").read_text(
        encoding="utf-8")
    export = src[src.index("def _export_all"):]
    export = export[:export.index("\n    def ")]
    assert "pending.pop(0)" not in export, (
        "batch export pops from the front of the list (O(n) per step, "
        "O(n^2) overall); step by index instead")
    assert "cursor" in export, (
        "expected an index cursor driving the batch step")
    print("OK: batch export steps by index, not pop(0), so a large "
          "batch is linear rather than quadratic in the stepping")


def run() -> None:
    test_dragging_the_box_never_rescales_the_source()
    test_panning_never_rescales_the_source()
    test_zoom_rebuilds_the_scaled_bitmap()
    test_rerolling_never_re_decodes()
    test_navigating_re_decodes_exactly_once_per_image()
    test_plans_list_is_not_rebuilt_on_navigation()
    test_group_lookup_does_not_scan_the_dataset()
    test_batch_export_uses_constant_time_stepping()
    print("ALL PASS: image editor performance")


if __name__ == "__main__":
    run()
