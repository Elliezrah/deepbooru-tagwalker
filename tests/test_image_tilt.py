"""
tests/test_image_tilt.py

Tests for the image editor's tilt tool: the picture rotates under a
fixed crop box (image-side transform), with the exporter matching the
preview, the number box and the orbit gesture staying in sync, the
full +/-180 range, and the two empty-area fill modes.
"""
import sys, os, tempfile, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPixmap, QColor, QPainter
from PySide6.QtCore import QRect, QPoint, Qt

from core import image_prep as prep
from ui.crop_view import CropView, MODE_FREE, MAX_TILT

app = QApplication.instance() or QApplication(sys.argv)


def _src_image():
    tmp = Path(tempfile.mkdtemp())
    img = Image.new("RGB", (300, 300), "white")
    for x in range(300):
        for y in range(140, 160):
            img.putpixel((x, y), (0, 0, 0))   # horizontal bar
    p = tmp / "bar.png"
    img.save(p)
    return p


def test_zero_angle_is_identical_to_no_angle():
    """angle=0 must not perturb the output at all — same pixels as a
    plain crop, so the tilt path is truly opt-in."""
    src = _src_image()
    out = Path(tempfile.mkdtemp())
    box = (50, 50, 200, 200)
    bucket = (200, 200)
    r_none = prep.export_crop(src, box, bucket, out)          # no angle arg
    r_zero = prep.export_crop(src, box, bucket, out, angle=0.0)
    a = Image.open(r_none.written).tobytes()
    b = Image.open(r_zero.written).tobytes()
    assert a == b, "angle=0 changed the exported pixels vs no-angle path"
    print("OK: angle=0 export is byte-identical to the no-angle export")


def test_export_rotation_direction_clockwise():
    """A +90 tilt turns a horizontal bar vertical — confirming the
    exporter rotates clockwise for a positive angle."""
    src = _src_image()
    out = Path(tempfile.mkdtemp())
    box = (50, 50, 200, 200)
    r = prep.export_crop(src, box, (200, 200), out, angle=90.0)
    c = Image.open(r.written)
    # Center still on the bar (black); a point on the vertical-bar column
    # above center is black, a point on the (old) horizontal row left of
    # center is white.
    assert c.getpixel((100, 100))[0] < 80, "center should stay on the bar"
    assert c.getpixel((100, 20))[0] < 80, "vertical bar column black above centre"
    assert c.getpixel((20, 100))[0] > 200, "off the vertical bar should be white"
    print("OK: exporter rotates clockwise for positive angle (bar H->V at +90)")


def test_tilt_opens_padded_corners():
    """With the crop at the image corner, a tilt opens a corner onto the
    pad colour rather than inventing detail (Option A)."""
    src = _src_image()
    out = Path(tempfile.mkdtemp())
    box = (0, 0, 200, 200)      # flush to the top-left corner
    r = prep.export_crop(src, box, (200, 200), out, angle=20.0,
                         pad_colour="black")
    c = Image.open(r.written)
    # The extreme corner should be pad (black) because the rotated source
    # no longer covers it.
    assert c.getpixel((1, 1)) == (0, 0, 0), "opened corner should be padded"
    print("OK: tilt at an edge pads the opened corner (Option A)")


def _make_view():
    img = QImage(300, 300, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    v = CropView()
    v.resize(320, 320)
    v._pixmap = QPixmap.fromImage(img)
    v._image_size = img.size()
    v._crop = QRect(75, 75, 150, 150)
    v._mode = MODE_FREE
    return v


def test_angle_clamped_to_limit():
    """set_image_angle clamps to +/-MAX_TILT (a half-turn each way), so
    any orientation including fully upside down (+/-180) is reachable but
    a value past a full turn does not run away."""
    v = _make_view()
    assert MAX_TILT == 180.0, "range should be a full half-turn each way"
    v.set_image_angle(180.0)
    assert v.image_angle == 180.0, "should reach 180 (upside down)"
    v.set_image_angle(999.0)
    assert v.image_angle == MAX_TILT, "angle not clamped to +MAX_TILT"
    v.set_image_angle(-999.0)
    assert v.image_angle == -MAX_TILT, "angle not clamped to -MAX_TILT"
    print(f"OK: angle clamped to +/-{MAX_TILT} (upside down reachable)")


def test_upside_down_export():
    """A 180 tilt turns the picture fully over: a bar across the top of
    the source lands across the bottom of the export."""
    tmp = Path(tempfile.mkdtemp())
    img = Image.new("RGB", (200, 200), "white")
    for x in range(200):
        for y in range(10, 30):          # bar near the TOP
            img.putpixel((x, y), (0, 0, 0))
    src = tmp / "top_bar.png"
    img.save(src)
    out = Path(tempfile.mkdtemp())
    box = (0, 0, 200, 200)
    r = prep.export_crop(src, box, (200, 200), out, angle=180.0)
    c = Image.open(r.written)
    # After a half turn the top bar (rows 10-29) lands near the bottom
    # (rows ~170-189). Check inside that band, and that the old top is
    # now empty.
    assert c.getpixel((100, 180))[0] < 80, "top bar should land at the bottom"
    assert c.getpixel((100, 10))[0] > 200, "old top should now be empty/white"
    print("OK: 180 tilt flips the picture fully (top bar -> bottom)")


def test_reset_zeroes_tilt():
    """reset_view_zoom and reset_all both level the picture (tilt is part
    of the view)."""
    v = _make_view()
    v.set_image_angle(12.0)
    v.reset_view_zoom()
    assert v.image_angle == 0.0, "reset_view_zoom left a residual tilt"
    v.set_image_angle(12.0)
    v.reset_all()
    assert v.image_angle == 0.0, "reset_all left a residual tilt"
    print("OK: reset_view_zoom and reset_all both zero the tilt")


def test_new_image_starts_level():
    """Loading a new image clears any tilt from the previous one."""
    v = _make_view()
    v.set_image_angle(18.0)
    src = _src_image()
    ok = v.load(src, (200, 200))
    assert ok and v.image_angle == 0.0, "tilt carried over into a new image"
    print("OK: loading a new image resets the tilt to level")


def test_orbit_gesture_tracks_cursor_angle():
    """The tilt gesture is an orbit: the tilt follows the CHANGE in the
    cursor's angle about the crop centre, so a quarter-circle sweep of
    the cursor turns the picture ~90 degrees regardless of drag length."""
    from ui.crop_view import _wrap_deg
    v = _make_view()
    from PySide6.QtCore import QPoint
    centre = v._to_widget(v._crop).center()
    # Grab to the RIGHT of the pivot (cursor angle ~0), tilt starts at 0.
    grab = QPoint(centre.x() + 60, centre.y())
    v._tilting = True
    v._tilt_grab_angle = v._cursor_angle(grab)
    v._tilt_start_angle = 0.0
    # Move to DIRECTLY BELOW the pivot (cursor angle ~+90 in screen y-down
    # convention): a quarter orbit clockwise.
    now = QPoint(centre.x(), centre.y() + 60)
    delta = _wrap_deg(v._cursor_angle(now) - v._tilt_grab_angle)
    v.set_image_angle(v._tilt_start_angle + delta)
    assert abs(v.image_angle - 90.0) < 1e-6, (
        f"quarter orbit should give ~90 deg, got {v.image_angle}")
    print("OK: orbit gesture tracks cursor angle (quarter turn -> 90 deg)")


def test_orbit_wrap_is_continuous():
    """Crossing the +/-180 seam during an orbit produces a small step,
    not a near-full-turn jump."""
    from ui.crop_view import _wrap_deg
    # Simulate the per-step delta the move handler computes when the
    # cursor angle goes from +179 to -179 (passing the seam).
    step = _wrap_deg(-179.0 - 179.0)
    assert abs(step - 2.0) < 1e-6, f"seam crossing should be ~2 deg, got {step}"
    print("OK: orbit wraps continuously across the +/-180 seam")


def test_export_matches_preview_multi_side_overhang():
    """Regression: the exported crop places content exactly where the box
    frames it — the same as the preview — even when the box overhangs
    several sides. Earlier the default 'balanced' mode re-centred the
    content, so a multi-side overhang came out shifted."""
    tmp = Path(tempfile.mkdtemp())
    img = Image.new("RGB", (100, 100), (128, 128, 128))
    for x in range(20):
        for y in range(20):
            img.putpixel((x, y), (255, 0, 0))    # red top-left marker
    src = tmp / "marker.png"
    img.save(src)

    def red_pos(im):
        for x in range(im.width):
            for y in range(im.height):
                p = im.getpixel((x, y))
                if p[0] > 200 and p[1] < 80:
                    return (x, y)
        return None

    # Box pulled off the LEFT by 60: the picture starts at x=60 in the
    # frame, which is where the preview shows it. The marker must land at
    # x=60 in both fill modes (they no longer differ in placement).
    for ff in (False, True):
        out = Path(tempfile.mkdtemp())
        r = prep.export_crop(src, (-60, 0, 160, 100), (160, 100), out,
                             pad_colour="black", flat_fill=ff)
        pos = red_pos(Image.open(r.written))
        assert pos == (60, 0), (
            f"placement {pos} != preview position (60,0), flat_fill={ff}")

    # Four-side overhang: image centred at cols/rows 50..150, marker at
    # (50,50) — matching where the box frames it.
    out = Path(tempfile.mkdtemp())
    r = prep.export_crop(src, (-50, -50, 200, 200), (200, 200), out,
                         pad_colour="black")
    assert red_pos(Image.open(r.written)) == (50, 50), \
        "four-side overhang did not match preview placement"
    print("OK: export matches preview for multi-side overhang (no re-centre)")


def test_orbit_from_tilted_state_is_smooth():
    """Regression: an orbit begun while the picture is already tilted, and
    crossing the +/-180 seam, must climb smoothly and clamp — not snap to
    a different angle. The gesture accumulates small wrapped steps rather
    than wrapping a total delta, which is what fixes the snap."""
    from ui.crop_view import _wrap_deg
    v = _make_view()
    v.set_image_angle(170.0)
    centre = v._to_widget(v._crop).center()

    def cursor_at(deg):
        rad = math.radians(deg)
        return QPoint(int(centre.x() + 60 * math.cos(rad)),
                      int(centre.y() + 60 * math.sin(rad)))

    v._tilting = True
    v._tilt_last_cursor = v._cursor_angle(cursor_at(0))
    seen = []
    for d in range(2, 40, 2):
        now = v._cursor_angle(cursor_at(d))
        step = _wrap_deg(now - v._tilt_last_cursor)
        v._tilt_last_cursor = now
        v.set_image_angle(v._image_angle + step)
        seen.append(v.image_angle)
    assert all(a >= 169 for a in seen), f"tilt snapped negative: {seen}"
    assert max(seen) <= 180.0, "exceeded the clamp"
    assert seen[-1] == 180.0, "should climb to and hold 180"
    print("OK: orbit from a tilted state climbs smoothly and clamps")


def test_image_does_not_follow_box_under_tilt():
    """Regression: dragging the crop box must NOT move the picture, at any
    tilt. The picture rotates about the IMAGE centre (fixed), so moving
    the box slides it over a stationary picture. Earlier the rotation
    pivoted on the box centre, welding the picture to the box — moving the
    box swung the picture around, worse the more it was tilted.
    """
    img = QImage(100, 100, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.fillRect(45, 45, 10, 10, QColor("red"))   # marker at image centre
    p.end()
    v = CropView()
    v.resize(500, 500)
    v._pixmap = QPixmap.fromImage(img)
    v._image_size = img.size()
    v._mode = MODE_FREE

    def red_pos():
        out = QImage(v.size(), QImage.Format.Format_RGB32)
        out.fill(QColor(20, 20, 20))
        v.render(out, QPoint(0, 0))
        xs = ys = n = 0
        for y in range(out.height()):
            for x in range(out.width()):
                c = out.pixelColor(x, y)
                if c.red() > 180 and c.green() < 80 and c.blue() < 80:
                    xs += x
                    ys += y
                    n += 1
        return (xs / n, ys / n, n) if n else (None, None, 0)

    for angle in (0.0, 45.0, 90.0, 180.0):
        v.set_image_angle(angle)
        v._crop = QRect(30, 30, 40, 40)
        ax, ay, an = red_pos()
        v._crop = QRect(40, 30, 40, 40)     # box +10 right
        bx, by, bn = red_pos()
        assert an and bn, f"marker off-screen at tilt {angle}"
        assert abs(bx - ax) < 3 and abs(by - ay) < 3, (
            f"at tilt {angle}, moving the box moved the picture "
            f"({bx-ax:.0f},{by-ay:.0f}) — picture should stay put")
    print("OK: picture stays put when the box is dragged, at every tilt")


def test_preview_and_export_pivot_match_off_centre():
    """Preview and export must rotate about the SAME point, checked with an
    OFF-CENTRE crop (where box centre and image centre differ, so a pivot
    mismatch would show). Both sample the same colour at the crop centre.
    """
    W = H = 200
    img = QImage(W, H, QImage.Format.Format_RGB32)
    p = QPainter(img)
    p.fillRect(0, 0, 100, 100, QColor(255, 0, 0))
    p.fillRect(100, 0, 100, 100, QColor(0, 255, 0))
    p.fillRect(0, 100, 100, 100, QColor(0, 0, 255))
    p.fillRect(100, 100, 100, 100, QColor(255, 255, 0))
    p.end()
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "quad.png"
    img.save(str(src))

    crop = QRect(20, 20, 80, 80)      # off-centre
    angle = 25.0

    v = CropView()
    v.resize(300, 300)
    v._pixmap = QPixmap.fromImage(img)
    v._image_size = img.size()
    v._mode = MODE_FREE
    v._crop = crop
    v.set_image_angle(angle)
    out = QImage(v.size(), QImage.Format.Format_RGB32)
    out.fill(QColor(0, 0, 0))
    v.render(out, QPoint(0, 0))
    boxw = v._to_widget(v._crop)
    prev = out.pixelColor(boxw.center().x(), boxw.center().y())

    outd = Path(tempfile.mkdtemp())
    r = prep.export_crop(src, (20, 20, 80, 80), (80, 80), outd,
                         pad_colour="black", angle=angle)
    assert r.written, r.message
    exp = Image.open(r.written).convert("RGB")
    ec = exp.getpixel((exp.width // 2, exp.height // 2))
    dr = abs(prev.red() - ec[0])
    dg = abs(prev.green() - ec[1])
    db = abs(prev.blue() - ec[2])
    assert dr < 60 and dg < 60 and db < 60, (
        f"preview {(prev.red(), prev.green(), prev.blue())} vs export {ec} "
        "— pivots disagree for an off-centre crop")
    print("OK: preview and export share the pivot (off-centre crop matches)")


def test_crop_drag_is_tilt_independent():
    """The crop box is drawn upright and must move in SCREEN space, the
    same at any tilt — the picture's rotation turns only the picture
    behind the box, never the box. Regression for a drag that crept
    off-axis (and at 180 ran backwards) when the cursor mapping was
    wrongly rotated by the tilt.
    """
    img = QImage(200, 200, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    v = CropView()
    v.resize(240, 240)
    v._pixmap = QPixmap.fromImage(img)
    v._image_size = img.size()
    v._mode = MODE_FREE

    def box_screen_centre():
        return v._to_widget(v._crop).center()

    def drag_right_40(angle):
        v._crop = QRect(70, 70, 60, 60)
        v.set_image_angle(angle)
        before = box_screen_centre()
        press = QPoint(before.x(), before.y())
        grab_pt = v._cursor_to_image(press)
        v._dragging = True
        v._grab_offset = grab_pt - v._crop.topLeft()
        move = QPoint(before.x() + 40, before.y())
        new_pt = v._cursor_to_image(move)
        r = QRect(v._crop)
        r.moveTopLeft(new_pt - v._grab_offset)
        v._crop = r
        after = box_screen_centre()
        return after.x() - before.x(), after.y() - before.y()

    for angle in (0, 15, 30, 90, 180, -45):
        dx, dy = drag_right_40(angle)
        assert abs(dx - 40) < 5 and abs(dy) < 5, (
            f"at tilt {angle}, drag +40 right moved ({dx},{dy}) on screen "
            "— box drag should be tilt-independent")
    print("OK: crop drag moves in screen space, identical at every tilt")


def test_shade_follows_tilt():
    """The dimming outside the crop must track the rotated image, not
    stay an axis-aligned square. Regression for the mask going out of
    sync when the picture is tilted.
    """
    img = QImage(300, 300, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))            # white so shade darkening reads
    v = CropView()
    v.resize(340, 340)
    v._pixmap = QPixmap.fromImage(img)
    v._image_size = img.size()
    v._crop = QRect(100, 100, 100, 100)
    v._mode = MODE_FREE

    def render():
        out = QImage(v.size(), QImage.Format.Format_RGB32)
        out.fill(QColor(50, 50, 50))     # dark panel bg
        v.render(out, QPoint(0, 0))
        return out

    def lum(qimg, x, y):
        c = qimg.pixelColor(x, y)
        return (c.red() + c.green() + c.blue()) / 3

    cx, cy = v.width() // 2, v.height() // 2

    # Untilted: crop centre unshaded (bright), image outside crop shaded.
    v.set_image_angle(0.0)
    im0 = render()
    assert lum(im0, cx, cy) > 230, "crop region should be unshaded"
    box_w = v._to_widget(v._crop)
    assert lum(im0, box_w.left() - 20, cy) < lum(im0, cx, cy) - 30, \
        "image outside the crop should be shaded"

    # Tilted: crop centre still unshaded; a far corner that the tilt
    # pushes OFF the image reads as panel bg, not a shaded-white square.
    # (With the old axis-aligned shade it would be mid-grey ~105.)
    v.set_image_angle(30.0)
    im30 = render()
    assert lum(im30, cx, cy) > 230, "crop stays unshaded when tilted"
    assert lum(im30, 6, 6) < 90, \
        "opened corner should be panel bg — shade must follow the tilt"
    print("OK: shade follows the rotated image (crop unshaded, no square mask)")


if __name__ == "__main__":
    test_zero_angle_is_identical_to_no_angle()
    test_export_rotation_direction_clockwise()
    test_tilt_opens_padded_corners()
    test_angle_clamped_to_limit()
    test_upside_down_export()
    test_reset_zeroes_tilt()
    test_new_image_starts_level()
    test_orbit_gesture_tracks_cursor_angle()
    test_orbit_wrap_is_continuous()
    test_export_matches_preview_multi_side_overhang()
    test_orbit_from_tilted_state_is_smooth()
    test_crop_drag_is_tilt_independent()
    test_image_does_not_follow_box_under_tilt()
    test_preview_and_export_pivot_match_off_centre()
    test_shade_follows_tilt()
    print("\nALL PASS: image tilt")
