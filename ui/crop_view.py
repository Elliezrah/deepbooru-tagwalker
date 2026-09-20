"""
ui/crop_view.py

The crop rectangle you drag and size with the wheel.

This is the part of the editor that earns its place. An ordinary image
editor makes cropping to a fixed training size slow: you type numbers,
or you drag handles and fight the aspect ratio. Here the box already
has the right shape, the wheel decides how much of the picture it
covers, and the cursor decides where. Nothing else to get right.

Two modes, and the difference is exactly what the user is allowed to
change:

  LOCKED   the box is the bucket's exact pixel size. It can be moved
           and nothing else. Used when the output must land on a known
           bucket without resampling more than necessary.
  FREE     the wheel resizes the box, keeping the bucket's aspect
           ratio, and the selection is scaled to the bucket on export.

The box is always constrained inside the image. A crop that runs off
the edge would be filled with something invented, which is the one
thing this tool exists to avoid.
"""
from __future__ import annotations

import math

from PySide6.QtCore import (QPoint, QPointF, QRect, QRectF, QSize, Qt,
                            Signal)
from PySide6.QtGui import (QColor, QImage, QPainter, QPainterPath,
                           QPolygonF, QPen, QPixmap, QTransform)
from PySide6.QtWidgets import QSizePolicy, QWidget

from config.theme import Colors

MODE_LOCKED = "locked"
MODE_FREE = "free"

# Tilt runs the full circle each way, so the picture can be levelled,
# turned on its side, or flipped fully upside down (+/-180). Small
# angles are the common case (straightening a crooked shot); the wide
# range is there so a mis-oriented source can be righted without
# leaving the tool.
MAX_TILT = 180.0
# The tilt gesture is an ORBIT, not a sideways drag: the picture tracks
# the angle of the cursor AROUND the crop centre, like turning a dial.
# Angular tracking (rather than drag distance) reaches the whole range
# in one motion and gives a natural coarse/fine feel — close to the
# pivot a small move turns fast, far from it the same move turns slowly
# and precisely. No pixels-per-degree constant is needed.

# Wheel step for sizing the crop box. Kept small for fine control;
# holding the wheel still crosses the range quickly enough.
ZOOM_STEP = 0.04

# The dimmed area outside the crop box.
SHADE_ALPHA = 150

# How far past the picture the box may be pulled, as a multiple of the
# longest side. Only reached when the safety lock is off.
MAX_OVERSHOOT = 3.0

# Display zoom: how far the picture itself can be scaled in the view,
# independently of the crop box. Needed because a picture fitted
# exactly to the panel leaves no room on screen to pull the box
# outside it — the very gesture that produces bars.
MIN_VIEW_ZOOM = 0.25
MAX_VIEW_ZOOM = 4.0
VIEW_ZOOM_STEP = 0.08

# Axis constraint while dragging.
AXIS_FREE = "free"
AXIS_X = "x"
AXIS_Y = "y"

BORDER_NORMAL = "normal"     # blue \u2014 nothing wrong
BORDER_WARNING = "warning"   # amber \u2014 confirmation pending
BORDER_ERROR = "error"       # red \u2014 last export failed

# Fraction of the box that must still cover the picture on each axis.
MIN_OVERLAP = 0.25


def _wrap_deg(delta: float) -> float:
    """Fold an angle difference into -180..180.

    The orbit gesture measures the cursor's absolute angle each step and
    subtracts the last; when the cursor passes the +/-180 seam that raw
    difference can be nearly a full turn, which would jerk the tilt.
    Wrapping keeps the step equal to the SHORT way round, so a smooth
    orbit gives a smooth tilt.
    """
    return (delta + 180.0) % 360.0 - 180.0


class CropView(QWidget):
    """Shows one image with a draggable, wheel-sized crop rectangle."""

    changed = Signal()
    # Emitted on Shift+wheel: +1 for wheel-down, -1 for wheel-up, so
    # the editor can step through the image list the same way the main
    # queue panel does.
    step_requested = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._pixmap: QPixmap | None = None
        # A copy of the source pre-scaled to the on-screen view rect,
        # rebuilt only when that rect changes (resize, zoom, pan to a
        # new size). Painting draws THIS at 1:1 instead of rescaling
        # the full-resolution source on every mouse-move repaint, which
        # on a large image was the whole cost of a drag.
        self._scaled_cache: QPixmap | None = None
        self._scaled_for: QSize | None = None
        self._scaled_src: int = 0
        self._image_size = QSize(0, 0)
        self._bucket = (1024, 1024)
        self._mode = MODE_LOCKED
        # Crop rectangle in IMAGE pixel coordinates, never widget
        # ones: the widget can be resized at any moment and the
        # selection must survive it unchanged.
        self._crop = QRect()
        self._dragging = False
        self._grab_offset = QPoint()
        self._view_zoom = 1.0
        # Pan offset of the picture within the panel, in widget pixels.
        # Separate from the crop box: right-click drag slides what you are
        # looking at, the plain left drag moves the selection.
        self._pan = QPoint(0, 0)
        self._panning = False
        self._pan_from = QPoint()
        # Tilt of the picture, in degrees, positive = clockwise. An
        # image-side transform like zoom and pan: the crop box does not
        # turn, the picture under it does. Zero until the user tilts.
        self._image_angle = 0.0
        self._tilting = False
        # Orbit-gesture state. The gesture accumulates: each mouse move
        # adds the SHORT-way change in the cursor's angle since the last
        # move to the tilt. Storing the last cursor angle (not the grab
        # angle) is what makes it continuous — a full orbit racks up past
        # 180 without the total-delta wrap that used to snap the picture
        # when the gesture began from an already-tilted state.
        self._tilt_last_cursor = 0.0
        self._axis = AXIS_FREE
        self._preview: QPixmap | None = None
        # Point the wheel resizes about. None means the image centre;
        # a drag moves it to where the box was left, so zooming happens
        # where the user is looking rather than snapping elsewhere.
        self._anchor = None
        # Border colour communicates export state at a glance:
        # blue = normal, amber = a warning is pending (too small,
        # awaiting confirmation), red = the last export errored.
        self._border = BORDER_NORMAL
        # Fit mode for reset_crop: False = contain (crop inside, no
        # bars), True = cover (short side to frame, overhang padded).
        self._fit_short = False

    # ------------------------------------------------------------------
    def set_bucket(self, bucket: tuple[int, int]) -> None:
        """Change the target shape without reloading the picture.

        Keeps the box centred where it is and its area roughly the
        same, so switching between square and rectangle feels like
        turning the frame rather than starting again.
        """
        if bucket == self._bucket or not self._image_size.width():
            self._bucket = bucket
            return
        old = QRect(self._crop)
        self._bucket = bucket
        if old.isEmpty():
            self.reset_crop()
            return
        area = old.width() * old.height()
        ratio = bucket[0] / bucket[1]
        new_h = max(32, int((area / ratio) ** 0.5))
        new_w = max(32, int(new_h * ratio))
        rect = QRect(0, 0, new_w, new_h)
        rect.moveCenter(old.center())
        self._crop = rect
        self._clamp()
        self.update()
        self.changed.emit()

    def load(self, path, bucket: tuple[int, int],
             mode: str = MODE_LOCKED) -> bool:
        image = QImage(str(path))
        if image.isNull():
            self._pixmap = None
            self._image_size = QSize(0, 0)
            self.update()
            return False
        self._pixmap = QPixmap.fromImage(image)
        self._scaled_cache = None       # new source, drop the cache
        self._scaled_for = None
        self._image_size = image.size()
        self._bucket = bucket
        self._mode = mode
        # A new picture starts level: a tilt found for the last image
        # would otherwise carry over and silently rotate this one. The
        # view zoom/pan follow the same "fresh per image" logic already
        # via reset_crop's callers; the angle is reset here explicitly.
        self._image_angle = 0.0
        self.reset_crop()
        return True

    def reset_crop(self) -> None:
        """Largest box of the bucket's shape that fits, centred.

        Starting at maximum coverage is deliberate: the common case is
        keeping nearly all of the picture, so the first thing shown
        should be the answer most people want, with the wheel there
        for when it is not.
        """
        if not self._image_size.width():
            return
        bucket_w, bucket_h = self._bucket
        image_w = self._image_size.width()
        image_h = self._image_size.height()

        if self._mode == MODE_LOCKED:
            width = min(bucket_w, image_w)
            height = min(bucket_h, image_h)
        elif self._fit_short:
            # Cover fit: the box is scaled so the SHORTEST side of the
            # image reaches the frame, which pushes the box past the
            # other pair of edges. That overhang becomes padding on
            # export. It saves manually pulling the box out to pad,
            # which is the common case for pillar/letterboxing.
            scale = max(image_w / bucket_w, image_h / bucket_h)
            width = max(1, int(bucket_w * scale))
            height = max(1, int(bucket_h * scale))
        else:
            # Contain fit: the largest crop of the frame's shape that
            # fits wholly inside the image. Nothing is padded.
            scale = min(image_w / bucket_w, image_h / bucket_h)
            width = max(1, int(bucket_w * scale))
            height = max(1, int(bucket_h * scale))
        self._crop = QRect((image_w - width) // 2,
                           (image_h - height) // 2, width, height)
        self._anchor = self._crop.center()
        self._clamp()
        self.update()
        self.changed.emit()

    # ------------------------------------------------------------------
    @property
    def crop_rect(self) -> QRect:
        return QRect(self._crop)

    def coverage(self) -> float:
        """Fraction of the image inside the box."""
        if not self._image_size.width() or self._crop.isEmpty():
            return 0.0
        total = self._image_size.width() * self._image_size.height()
        return (self._crop.width() * self._crop.height()) / total

    def can_resize(self) -> bool:
        return self._mode == MODE_FREE

    # ------------------------------------------------------------------
    def _clamp(self) -> None:
        """Keep the box somewhere useful, without forcing it inside.

        It used to be confined to the image. That made padding
        impossible from this mode: to letterbox a picture you have to
        pull the box out past the edges, and the space outside becomes
        the bars.

        What is still enforced is that the box overlaps the picture at
        all, and does not run away to an absurd size — a selection
        containing almost no image is never what anyone meant.
        """
        if not self._image_size.width():
            return
        image_w = self._image_size.width()
        image_h = self._image_size.height()
        rect = self._crop

        # The box may extend past the picture in any direction so that
        # padding can be added by hand — the space outside becomes the
        # bars. Only an absurd size is refused, so a selection can
        # never grow until it swallows the picture whole.
        limit = int(max(image_w, image_h) * MAX_OVERSHOOT)
        if rect.width() > limit or rect.height() > limit:
            scale = min(limit / rect.width(), limit / rect.height())
            rect.setWidth(max(1, int(rect.width() * scale)))
            rect.setHeight(max(1, int(rect.height() * scale)))

        # At least a quarter of the box must be over the picture,
        # measured on each axis, so it can never be dragged into empty
        # space.
        min_x = -int(rect.width() * (1 - MIN_OVERLAP))
        max_x = image_w - int(rect.width() * MIN_OVERLAP)
        min_y = -int(rect.height() * (1 - MIN_OVERLAP))
        max_y = image_h - int(rect.height() * MIN_OVERLAP)
        rect.moveLeft(max(min_x, min(rect.left(), max_x)))
        rect.moveTop(max(min_y, min(rect.top(), max_y)))
        self._crop = rect

    def overflow(self) -> bool:
        """True when part of the box falls outside the picture, so the
        export will have to pad."""
        if not self._image_size.width():
            return False
        return not QRect(0, 0, self._image_size.width(),
                         self._image_size.height()).contains(self._crop)

    def _view_rect(self) -> QRect:
        """Where the image sits inside this widget, letterboxed."""
        if not self._image_size.width():
            return QRect()
        scale = min(self.width() / self._image_size.width(),
                    self.height() / self._image_size.height())
        # Leave a margin at 1.0 so the edges are reachable: fitted
        # exactly, there is no room to drag the box past them.
        scale *= 0.88 * self._view_zoom
        width = max(1, int(self._image_size.width() * scale))
        height = max(1, int(self._image_size.height() * scale))
        return QRect((self.width() - width) // 2 + self._pan.x(),
                     (self.height() - height) // 2 + self._pan.y(),
                     width, height)

    def _to_widget(self, rect: QRect) -> QRect:
        view = self._view_rect()
        if view.isEmpty() or not self._image_size.width():
            return QRect()
        scale = view.width() / self._image_size.width()
        return QRect(view.left() + int(rect.left() * scale),
                     view.top() + int(rect.top() * scale),
                     max(1, int(rect.width() * scale)),
                     max(1, int(rect.height() * scale)))

    def _to_image(self, point: QPoint) -> QPoint:
        view = self._view_rect()
        if view.isEmpty() or not view.width():
            return QPoint()
        scale = self._image_size.width() / view.width()
        return QPoint(int((point.x() - view.left()) * scale),
                      int((point.y() - view.top()) * scale))

    def _cursor_to_image(self, point: QPoint) -> QPoint:
        """Cursor (widget) point to image pixel.

        The crop box is drawn UPRIGHT and moves in screen space — the
        picture's tilt turns only the picture behind it, never the box —
        so placing the box from the cursor is a plain widget->image
        mapping with no rotation. (An earlier version inverse-rotated
        here to 'follow the tilted picture', which was wrong: it turned
        the drag direction by the tilt angle, so the box crept off-axis
        as soon as any tilt was applied. The box is not tilted, so its
        drag must not be either.) Kept as a named seam in case placement
        ever needs to change, but today it is exactly _to_image.
        """
        return self._to_image(point)

    def _cursor_angle(self, point: QPoint) -> float:
        """Angle in degrees from the crop-box centre to a widget point.

        The pivot for the orbit gesture is the crop box's centre in
        WIDGET coordinates — the same point the picture tilts about — so
        the cursor's angle here and the picture's rotation share an
        origin and turn together. Uses screen convention (y grows
        downward), which makes a positive/clockwise cursor sweep match
        the clockwise-positive tilt.
        """
        centre = self._to_widget(self._crop).center()
        return math.degrees(math.atan2(point.y() - centre.y(),
                                       point.x() - centre.x()))

    # ------------------------------------------------------------------
    def set_view_zoom(self, zoom: float) -> None:
        self._view_zoom = max(MIN_VIEW_ZOOM,
                              min(MAX_VIEW_ZOOM, float(zoom)))
        self.update()
        self.changed.emit()

    def set_image_angle(self, angle: float) -> None:
        """Tilt the picture to an exact angle (the number box's entry point).

        Positive is clockwise. Clamped to +/-MAX_TILT (a full half-turn
        each way), so any orientation is reachable — level, on its side,
        or fully upside down — while a value past a full turn is folded
        back to the range. The crop box is left untouched: only the
        picture beneath it turns.
        """
        angle = max(-MAX_TILT, min(MAX_TILT, float(angle)))
        if angle == self._image_angle:
            return
        self._image_angle = angle
        self.update()
        self.changed.emit()

    def reset_view_zoom(self) -> None:
        """Reset zoom, pan AND tilt together — the whole view, back to fit.

        Tilt belongs to the view (an image-side transform), so the
        gesture and shortcut that restore the view restore the tilt as
        well; a levelled picture left tilted after a "reset view" would
        be a surprise.
        """
        self._view_zoom = 1.0
        self._pan = QPoint(0, 0)
        self._image_angle = 0.0
        self.update()
        self.changed.emit()

    def reset_all(self) -> None:
        """Everything: the view (zoom, pan, tilt) AND the crop box."""
        self._view_zoom = 1.0
        self._pan = QPoint(0, 0)
        self._image_angle = 0.0
        self.reset_crop()

    @property
    def view_zoom(self) -> float:
        return self._view_zoom

    @property
    def image_angle(self) -> float:
        """The tilt in degrees (clockwise positive), for the exporter.

        The export rotates the full-resolution source by this same
        angle about the crop centre, then lifts the box — so the file
        matches what the preview showed. Zero means no rotation and the
        exporter can skip the work entirely.
        """
        return self._image_angle

    def set_axis(self, axis: str) -> None:
        """Constrain dragging to one axis, and recentre on the other.

        The point of a horizontal lock is to move the crop left and
        right along the picture's centre line. If the box is already
        above or below centre when the lock goes on, dragging holds
        that wrong offset for ever — so switching a lock on snaps the
        free axis back to the middle, giving a true centre line to
        slide along.
        """
        self._axis = axis
        if axis == AXIS_X and self._image_size.height():
            # Sliding horizontally: centre vertically.
            rect = QRect(self._crop)
            rect.moveTop((self._image_size.height() - rect.height())
                         // 2)
            self._crop = rect
            self._clamp()
            self._anchor = self._crop.center()
            self.update()
            self.changed.emit()
        elif axis == AXIS_Y and self._image_size.width():
            rect = QRect(self._crop)
            rect.moveLeft((self._image_size.width() - rect.width())
                          // 2)
            self._crop = rect
            self._clamp()
            self._anchor = self._crop.center()
            self.update()
            self.changed.emit()

    def set_fit_short(self, short: bool) -> None:
        """Choose how reset_crop frames the picture, then re-fit."""
        self._fit_short = bool(short)
        self.reset_crop()

    @property
    def fit_short(self) -> bool:
        return self._fit_short

    def set_border(self, state: str) -> None:
        """Colour the crop outline to signal export state."""
        self._border = state
        self.update()

    def set_preview(self, pixmap) -> None:
        """Show a temporary pixmap (e.g. a jitter preview) in place of
        the source, or None to restore the source."""
        self._preview = pixmap
        # The preview replaces what is drawn, so the scaled cache no
        # longer matches — drop it and let paint rebuild from whichever
        # pixmap is now active.
        self._scaled_cache = None
        self._scaled_for = None
        self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._pixmap is None:
            event.ignore()
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            # Shift+wheel cycles images, matching the main queue panel.
            notches = event.angleDelta().y() / 120.0
            if notches:
                self.step_requested.emit(-1 if notches > 0 else 1)
            event.accept()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Zooms the VIEW, not the selection. A picture fitted
            # exactly to the panel leaves nowhere on screen to pull
            # the box outside it, so the gesture that produces bars
            # was impossible to perform.
            notches = event.angleDelta().y() / 120.0
            self.set_view_zoom(
                self._view_zoom * (1.0 + VIEW_ZOOM_STEP * notches))
            event.accept()
            return
        if self._mode != MODE_FREE:
            event.ignore()
            return
        notches = event.angleDelta().y() / 120.0
        if not notches:
            return
        # Resize about a FIXED anchor, rebuilding the rectangle each
        # time.
        #
        # The drift bug read rect.center() AFTER the previous frame's
        # clamp had nudged the box off centre, then re-centred on that
        # moved point. On a square image the box cannot grow at all
        # under the one-pair-of-bars rule, so clamp slid it sideways
        # instead, and every notch repeated the same nudge — the box
        # crawled off the top-left corner for ever. Anchoring to a
        # fixed point and recomputing left/top from the new size gives
        # clamp nothing to accumulate: the same input always yields
        # the same rectangle.
        factor = 1.0 - ZOOM_STEP * notches   # wheel up = tighter crop
        bucket_w, bucket_h = self._bucket
        new_w = max(32, int(self._crop.width() * factor))
        # Height follows from the bucket's aspect, so the selection
        # keeps its shape however far it is zoomed.
        new_h = max(32, int(new_w * bucket_h / bucket_w))

        # Resize about a fixed anchor and rebuild the rectangle, so the
        # position cannot accumulate drift across notches.
        anchor = self._anchor if self._anchor is not None \
            else self._crop.center()
        rect = QRect(0, 0, new_w, new_h)
        rect.moveCenter(anchor)
        self._crop = rect
        self._clamp()
        self.update()
        self.changed.emit()
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._pixmap is None:
            return
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if (event.button() == Qt.MouseButton.LeftButton
                and ctrl and shift):
            # Ctrl+Shift+left drag TILTS the picture, as an ORBIT around
            # the crop centre: the picture tracks the cursor turning
            # about that pivot, like a dial. It sits in the image-side
            # family with pan (Ctrl+drag) and zoom (Ctrl+wheel); Shift
            # picks it out from a plain pan. The gesture is INCREMENTAL —
            # each move adds the small change in cursor angle to the
            # tilt — so it picks up smoothly from whatever tilt the
            # picture already holds, with no snap. Checked BEFORE the pan
            # case, or the pan branch (Ctrl alone) would swallow it.
            self._tilting = True
            self._tilt_last_cursor = self._cursor_angle(
                event.position().toPoint())
            return
        if event.button() == Qt.MouseButton.RightButton:
            # Right-click drag PANS the picture within the panel. Distinct
            # from dragging the crop box (left drag): this slides what you
            # are looking at so a box pulled off the edge can be brought
            # back into view. Right-click is used because nothing else is
            # bound to it here and it frees the left button entirely for
            # the crop box. (Zoom stays on Ctrl+wheel; tilt on
            # Ctrl+Shift+left drag.)
            self._panning = True
            self._pan_from = event.position().toPoint() - self._pan
            return
        if event.button() == Qt.MouseButton.LeftButton:
            point = self._cursor_to_image(event.position().toPoint())
            if self._crop.contains(point):
                self._dragging = True
                self._grab_offset = point - self._crop.topLeft()
            else:
                # Click outside jumps the box there \u2014 but under an
                # axis lock only along the free axis, or the click would
                # silently break the lock the user set. This was the
                # bug: moveCenter(point) ignored the constraint, so one
                # click anywhere threw the box off its line.
                rect = QRect(self._crop)
                new_centre = QPoint(point)
                if self._axis == AXIS_X:
                    new_centre.setY(rect.center().y())
                elif self._axis == AXIS_Y:
                    new_centre.setX(rect.center().x())
                rect.moveCenter(new_centre)
                self._crop = rect
                self._clamp()
                self._anchor = self._crop.center()
                self._dragging = True
                self._grab_offset = (self._cursor_to_image(
                    event.position().toPoint()) - self._crop.topLeft())
                self.update()
                self.changed.emit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._tilting:
            # Orbit, accumulated step by step: add the SHORT-way change
            # in the cursor's angle since the last move to the current
            # tilt, then remember this cursor angle for the next step.
            # Wrapping each small step (not the total) means a continuous
            # orbit stays continuous even past 180 and even when the
            # gesture started from an already-tilted picture — the seam
            # is only ever crossed one tiny step at a time. Routing
            # through set_image_angle keeps the clamp and the change
            # signal in one place, so the number box and the drag never
            # disagree.
            now = self._cursor_angle(event.position().toPoint())
            step = _wrap_deg(now - self._tilt_last_cursor)
            self._tilt_last_cursor = now
            self.set_image_angle(self._image_angle + step)
            return
        if self._panning:
            self._pan = event.position().toPoint() - self._pan_from
            self.update()
            return
        if not self._dragging or self._pixmap is None:
            return
        point = self._cursor_to_image(event.position().toPoint())
        rect = QRect(self._crop)
        target = point - self._grab_offset
        if self._axis == AXIS_X:
            target.setY(rect.top())      # horizontal only
        elif self._axis == AXIS_Y:
            target.setX(rect.left())     # vertical only
        rect.moveTopLeft(target)
        self._crop = rect
        self._clamp()
        self._anchor = self._crop.center()
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = False
        self._panning = False
        self._tilting = False

    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(Colors.BG_BASE))
        if self._pixmap is None:
            painter.setPen(QColor(Colors.TEXT_SECONDARY))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "no image")
            painter.end()
            return

        view = self._view_rect()
        source = self._preview or self._pixmap
        # Rebuild the scaled copy only when the view SIZE or the source
        # changes; otherwise reuse it, and just blit it at the current
        # position. Keying on size rather than the full rect is what
        # lets panning reuse the cache — a pan only shifts where the
        # picture is drawn, not how big it is, so rebuilding the
        # scaled bitmap every pan frame (as keying on the whole rect
        # did) would have reintroduced the very cost this cache exists
        # to remove.
        src_id = id(source)
        size = view.size()
        if (self._scaled_cache is None
                or self._scaled_for != size
                or self._scaled_src != src_id):
            self._scaled_cache = source.scaled(
                size,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._scaled_for = QSize(size)
            self._scaled_src = src_id

        # The crop box in widget coordinates. Computed before the image
        # is drawn because it is also the PIVOT the picture tilts about:
        # rotating around the frame's centre keeps the subject in place
        # and swings the edges, which is what levelling a shot means.
        box = self._to_widget(self._crop)

        if self._image_angle:
            # Tilt: rotate the picture about the IMAGE centre, which is
            # fixed while the crop box is dragged. Pivoting on the box
            # centre (as an earlier version did) welded the picture to
            # the box — moving the box swung the picture around with it,
            # worse the more it was tilted. The image centre is the
            # view rect's centre and does not move when the box moves,
            # so dragging the box now slides the box over a stationary
            # tilted picture. Save/rotate/draw/restore so everything
            # after — shade, border, thirds — is drawn in the UNROTATED
            # frame (the box itself never turns). Corners the rotation
            # opens up show the panel background here; on export they
            # become padding.
            pivot = QPointF(view.center())
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                                  True)
            xform = QTransform()
            xform.translate(pivot.x(), pivot.y())
            xform.rotate(self._image_angle)
            xform.translate(-pivot.x(), -pivot.y())
            painter.setTransform(xform, True)
            painter.drawPixmap(view.topLeft(), self._scaled_cache)
            painter.restore()
        else:
            painter.drawPixmap(view.topLeft(), self._scaled_cache)
        # Everything outside the selection is dimmed rather than
        # hidden: the discarded part is what the user is deciding
        # about, so it has to stay visible. The shaded region is the
        # IMAGE minus the crop box — so when the picture is tilted, the
        # shade follows the picture's rotated edges instead of a square
        # that no longer matches it. Built as (image area) subtract
        # (crop box) and filled once: a path handles the tilted case
        # and the plain case with the same code.
        shade = QColor(0, 0, 0, SHADE_ALPHA)
        if self._image_angle:
            # The image occupies a rotated quad — the view rect turned
            # about the IMAGE centre by the same transform used to draw
            # it (must match the draw pivot exactly, or the shade would
            # sit off the picture). Shade that quad, punched through by
            # the crop box.
            pivot = QPointF(view.center())
            xform = QTransform()
            xform.translate(pivot.x(), pivot.y())
            xform.rotate(self._image_angle)
            xform.translate(-pivot.x(), -pivot.y())
            image_area = QPainterPath()
            image_area.addPolygon(xform.map(QPolygonF(QRectF(view))))
            image_area.closeSubpath()
        else:
            image_area = QPainterPath()
            image_area.addRect(QRectF(view))
        hole = QPainterPath()
        hole.addRect(QRectF(box))
        painter.fillPath(image_area.subtracted(hole), shade)

        border_colour = {
            BORDER_WARNING: QColor(Colors.WARNING_AMBER),
            BORDER_ERROR: QColor(Colors.DANGER_RED),
        }.get(self._border, QColor(Colors.ACCENT_BAR))
        pen = QPen(border_colour)
        pen.setWidth(3 if self._border != BORDER_NORMAL else 2)
        painter.setPen(pen)
        painter.drawRect(box)

        # Thirds, which is how people actually judge a crop.
        thin = QPen(QColor(255, 255, 255, 70))
        thin.setWidth(1)
        painter.setPen(thin)
        for i in (1, 2):
            x = box.left() + box.width() * i // 3
            y = box.top() + box.height() * i // 3
            painter.drawLine(x, box.top(), x, box.bottom())
            painter.drawLine(box.left(), y, box.right(), y)
        painter.end()
