"""
ui/swirl_widget.py

The swirl convergence visualization.

A 200x200 widget shown in the main task area's top-right corner. Each
of 100 plus-shaped dots represents 1% of the *current tag's queue*
completion. As the user makes Yes/No decisions (NOT skip-image —
those are postponements, not completions), dots accrete one-by-one
into a logarithmic spiral. At 0% all 100 dots float in chaotic orbits
in a ring outside the spiral's max radius; at 100% all dots are
arranged in a tidy galaxy. Smooth interpolation animates the
transition — no teleporting, even under auto-yes bursts or undos.

Completion semantics (Pass C decisions)
---------------------------------------
- Denominator: current tag's queue size (filter-aware). So if the
  user changes filter from "all" to "missing tag", the swirl rescales.
- Numerator: count of decisions in {YES, NO}. SKIPPED does NOT count
  (skipping is "review later", not completion).
- granular tag edits via file_state_panel do NOT affect the swirl
  (no decision change).
- Walk ended (current_tag is None): swirl freezes in last state.
  When a new tag is selected, animates to that tag's state.

Color scheme
------------
Aesthetic only: stars-in-galaxy palette where each dot is colored by
its position in the spiral (inner = warm gold, outer = cool cyan).
Color carries no decision semantics — purely a visual gradient that
makes the swirl look like a galaxy. (An earlier semantic Scheme B was
removed because mapping decisions to dots by approximate index made
the swirl flash red/green during bursts without conveying actionable
information.)

Subscribed events
-----------------
- tag_selected     : new tag, reset dots
- walk_advanced    : completion may have changed (Yes/No/auto-yes/etc)
- walk_ended       : completion may have changed
- filter_changed   : denominator changed
- tag_changed      : batch decisions emit this — recompute

NOT subscribed:
- image_changed (granular edits don't change decisions)
- tree_rebuilt  (tag set changes don't affect current walk's progress)
"""

from __future__ import annotations

import math
import random
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from config.theme import Colors
from core.state import Decision, SessionState, StateChange


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIDGET_SIZE          = 200          # px (logical; high-DPI handled by Qt)
DOT_COUNT            = 100          # one dot per percent
FPS                  = 60           # animation rate
FRAME_MS             = 1000 // FPS  # ~16ms; pure aesthetic — 60 feels smoother

# Spiral parameters — tuned by hand for visual feel
SPIRAL_MAX_RADIUS    = 70.0         # px from center to outermost spiral dot
SPIRAL_PADDING       = 10.0         # gap between spiral edge and chaos ring
CHAOS_RING_INNER     = SPIRAL_MAX_RADIUS + SPIRAL_PADDING
CHAOS_RING_OUTER     = 95.0         # just inside the 100px canvas half-width
GOLDEN_ANGLE         = math.pi * (3.0 - math.sqrt(5.0))  # ~137.5°

# Plus-shape size (px); also scaled per dot
PLUS_ARM_LENGTH      = 3.5
PLUS_LINE_WIDTH      = 1.5

# Animation tuning
LERP_FACTOR          = 0.06         # fraction of distance moved per frame
                                    # 0.06 at 60 FPS ≈ ~200ms time constant
                                    # (matches old 0.12 at 30 FPS for the same
                                    # visual smoothness; bumped FPS for fluidity)
SPIRAL_SPIN_RATE     = 0.10         # rad/sec, slow global spin
                                    # ~1 rotation per minute
CHAOS_DRIFT_RATE     = 0.05         # rad/sec for chaos orbits

# Per-dot self-spin rates (rad/sec). Outer dots spin faster than inner
# (matches your physics-of-galaxy-arms intuition).
INNER_SELF_SPIN_RATE = 0.6
OUTER_SELF_SPIN_RATE = 2.0

# Rhythmic sparkle: spiraled dots gently pulse in scale and alpha to
# make the completed galaxy feel like it's breathing. Phase per dot is
# tied to spiral position (index * golden_angle), so the pulse appears
# to travel along the spiral arms as time advances. Period is slow
# enough to feel calm, not nervous.
#
# Only applied to spiraled dots. Chaos dots stay constant so the busy
# outer cloud doesn't visually compete with the breathing spiral.
SPARKLE_PERIOD_SEC   = 3.0          # one full pulse cycle
SPARKLE_SCALE_AMP    = 0.15         # scale modulation: 1.0 ± this
SPARKLE_ALPHA_AMP    = 0.25         # alpha modulation: 1.0 ± this
                                    # (clamped to [0, 1] after multiply)

# Aesthetic palette. Indexed by normalized radius (0=center, 1=edge).
# We sample a small gradient from warm gold → soft purple → cool cyan.
# Stars-in-galaxy feel.
SCHEME_A_PALETTE = [
    "#e6c060",  # warm gold (center)
    "#e08a5a",  # amber
    "#c574c4",  # soft purple
    "#7c8ce6",  # periwinkle
    "#5fc4d6",  # cyan (edge)
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation. t in [0,1]."""
    return a + (b - a) * t


def _sample_scheme_a(t: float) -> QColor:
    """Sample the Scheme A gradient at normalized position t in [0,1]."""
    if t <= 0.0:
        return QColor(SCHEME_A_PALETTE[0])
    if t >= 1.0:
        return QColor(SCHEME_A_PALETTE[-1])
    # Find the two surrounding palette stops and interpolate.
    n_stops = len(SCHEME_A_PALETTE) - 1
    scaled = t * n_stops
    idx = int(scaled)
    frac = scaled - idx
    c1 = QColor(SCHEME_A_PALETTE[idx])
    c2 = QColor(SCHEME_A_PALETTE[idx + 1])
    return QColor(
        int(_lerp(c1.red(),   c2.red(),   frac)),
        int(_lerp(c1.green(), c2.green(), frac)),
        int(_lerp(c1.blue(),  c2.blue(),  frac)),
    )


def _spiral_position(
    index: int, dot_count: int, max_radius: float,
) -> tuple[float, float]:
    """Compute the spiral slot for dot `index` (0..dot_count-1).

    Uses sunflower-seed (golden-angle) distribution: evenly fills a
    disc with no preferential direction. Inner dots are the first
    indices, outermost are the last.

    Returns local (x, y) offsets from the spiral center.
    """
    if index <= 0:
        return (0.0, 0.0)
    # Square-root radius so dots are evenly area-distributed (not
    # bunched near center). Normalize by sqrt(dot_count - 1) so the
    # outermost dot is at max_radius exactly.
    r = max_radius * math.sqrt(index / (dot_count - 1))
    theta = index * GOLDEN_ANGLE
    return (r * math.cos(theta), r * math.sin(theta))


def _chaos_home(
    index: int, ring_inner: float, ring_outer: float,
) -> tuple[float, float, float]:
    """Compute the chaos-ring home for dot `index`.

    Returns (radius, base_angle, drift_amplitude). Position over time
    is the home angle plus a small sinusoidal drift, so the chaos
    cloud looks alive but each dot stays in its own neighborhood.

    Seeded deterministically by index so the chaos pattern is stable
    across redraws and undos.
    """
    rng = random.Random(index * 12345 + 7)
    # Spread dots in the chaos ring randomly but stably.
    radius = rng.uniform(ring_inner, ring_outer)
    angle = rng.uniform(0, 2 * math.pi)
    drift = rng.uniform(0.05, 0.15)  # small radian-amplitude oscillation
    return (radius, angle, drift)


# ---------------------------------------------------------------------------
# Per-dot state
# ---------------------------------------------------------------------------


class _Dot:
    """One of the 100 dots.

    Carries:
    - `index`: its position in the spiral (0=innermost)
    - `vx, vy`: current visual position (px, relative to widget center)
    - `vscale`: current visual scale (1.0 normal; smaller while moving)
    - `self_rotation_phase`: current self-spin angle for the "+" glyph
    - `decision`: the decision that put it in the spiral (only used by
       Scheme B). YES or NO; UNPROCESSED while in chaos.
    - `chaos_seed`: precomputed chaos-home parameters

    All movement is computed in tick(); paint() reads visual state.
    """

    __slots__ = (
        "index",
        "vx",
        "vy",
        "vscale",
        "self_rotation_phase",
        "decision",
        "chaos_radius",
        "chaos_angle",
        "chaos_drift",
        "self_spin_rate",
    )

    def __init__(
        self,
        index: int,
        dot_count: int,
        ring_inner: float,
        ring_outer: float,
    ) -> None:
        self.index = index
        self.decision: Decision = Decision.UNPROCESSED
        # Initial visual position = chaos home (so a fresh swirl shows
        # the chaos field immediately, no warm-up animation needed).
        self.chaos_radius, self.chaos_angle, self.chaos_drift = _chaos_home(
            index, ring_inner, ring_outer,
        )
        self.vx = self.chaos_radius * math.cos(self.chaos_angle)
        self.vy = self.chaos_radius * math.sin(self.chaos_angle)
        self.vscale = 1.0
        # Random initial phase so all dots don't blink in sync.
        rng = random.Random(index * 1009 + 13)
        self.self_rotation_phase = rng.uniform(0, 2 * math.pi)
        # Self-spin rate: inner dots slow, outer dots fast. Linear
        # interpolation by normalized index. The Pass C plan called for
        # ~3:1 ratio; we get that from INNER vs OUTER constants above.
        norm = index / (dot_count - 1) if dot_count > 1 else 0
        self.self_spin_rate = _lerp(
            INNER_SELF_SPIN_RATE, OUTER_SELF_SPIN_RATE, norm,
        )


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class SwirlWidget(QWidget):
    """Convergence visualization. See module docstring.

    Two modes:
    - per-tag (default): completion = current tag's decided fraction.
      Used on the main task page (200px, 100 dots).
    - project-wide: completion = whole-project decided fraction across
      all (image, tag) jobs. Used on the stats page (larger, more dots).
      Set project_mode=True.

    Parameters
    ----------
    size : int
        Square canvas size in px (default 200).
    dot_count : int
        Number of dots (default 100). The stats swirl uses 1000.
    project_mode : bool
        If True, completion reflects whole-project decision coverage,
        recomputed from state.get_project_completion(). If False (the
        default), completion reflects the current tag's walk.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        size: int = WIDGET_SIZE,
        dot_count: int = DOT_COUNT,
        project_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self._size = size
        self._dot_count = dot_count
        self._project_mode = project_mode
        self.setFixedSize(size, size)

        # Geometry scaled to the canvas. The main swirl's tuning was
        # designed for a 200px canvas with a 70px spiral radius (0.35 of
        # size) and chaos ring from 0.40..0.475 of size. Keep those
        # ratios so any size looks proportionally identical.
        half = size / 2.0
        self._spiral_max_radius = size * 0.35
        self._chaos_ring_inner = size * 0.40
        self._chaos_ring_outer = size * 0.475
        # Plus-shape arm length scales with canvas but with a gentle
        # curve — at 1000 dots on a 600px canvas, dots must be smaller
        # relative to canvas so they don't overlap. Scale by sqrt of the
        # density ratio.
        density_scale = math.sqrt(DOT_COUNT / dot_count) if dot_count > 0 else 1.0
        self._plus_arm = PLUS_ARM_LENGTH * (size / WIDGET_SIZE) * density_scale
        self._plus_width = max(1.0, PLUS_LINE_WIDTH * (size / WIDGET_SIZE) * density_scale)

        self._state: Optional[SessionState] = None

        # All dots, indexed 0..dot_count-1.
        self._dots: list[_Dot] = [
            _Dot(i, dot_count, self._chaos_ring_inner, self._chaos_ring_outer)
            for i in range(dot_count)
        ]

        # Global spiral rotation (rad). Increments every frame.
        self._spiral_rotation: float = 0.0

        # Current completion as a dot count (0..dot_count). Recomputed
        # on state events.
        self._completed: int = 0
        # Most recent denominator (total jobs / tag queue size). When 0
        # or no active tag (per-tag mode), the widget freezes.
        self._walk_size: int = 0

        # Animation timer.
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        # Track elapsed seconds for spin calculations. Using a counter
        # rather than QTime so the math stays predictable in tests.
        self._elapsed_seconds: float = 0.0

    # ------------------------------------------------------------------
    # State binding
    # ------------------------------------------------------------------

    def attach(self, state: Optional[SessionState]) -> None:
        if self._state is not None:
            self._state.remove_listener(self._on_state_change)
        self._state = state
        if state is not None:
            state.add_listener(self._on_state_change)
        self._recompute_targets()

    def detach(self) -> None:
        self.attach(None)

    def set_scheme(self, scheme: str) -> None:
        """Deprecated no-op kept for API compatibility.

        Scheme B was removed in a Pass C revision because semantic
        red/green dot coloring produced patterns the user couldn't
        interpret. The swirl now uses Scheme A (aesthetic gradient)
        exclusively.
        """
        # Intentionally empty.
        pass

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _on_state_change(self, change: StateChange) -> None:
        """Recompute on any event that affects completion.

        Per-tag mode reacts to walk/tag events. Project mode reacts to
        any event that could change the global decision count —
        including image_changed (granular edits add/clear decisions)
        and tree_rebuilt (tags added/removed shift the denominator),
        which per-tag mode deliberately ignores.
        """
        kind = change.kind
        if self._project_mode:
            if kind in (
                "tag_selected", "walk_advanced", "walk_ended",
                "filter_changed", "tag_changed", "image_changed",
                "tree_rebuilt",
            ):
                self._recompute_targets()
        else:
            if kind in (
                "tag_selected", "walk_advanced", "walk_ended",
                "filter_changed", "tag_changed",
            ):
                self._recompute_targets()

    def _recompute_targets(self) -> None:
        """Read current state and update completion (the dot count).

        Project mode: completion = whole-project decision coverage from
        state.get_project_completion(). Recomputes freely (no freeze).

        Per-tag mode: completion = current tag's decided fraction,
        ignoring the queue search. Freezes (holds last state) when no
        tag is active, so a finished walk stays as a full spiral.
        """
        n = self._dot_count
        if self._project_mode:
            if self._state is None:
                self._completed = 0
                self._walk_size = 0
                return
            decided, total = self._state.get_project_completion()
            self._walk_size = total
            if total == 0:
                self._completed = 0
                return
            # Floor so the last dot only appears at exact 100%.
            frac = decided / total
            self._completed = max(0, min(n, int(frac * n)))
            return

        # ---- per-tag mode ----
        if self._state is None or self._state.current_tag is None:
            # No active walk. Hold the last state — don't reset dots
            # to chaos. This gives the "completed walk freezes at full
            # spiral" reward.
            return

        tag = self._state.current_tag
        # Full-tag completion, IGNORING the queue search filter.
        decided_count, total = self._state.get_tag_completion(tag)
        self._walk_size = total
        if total == 0:
            self._completed = 0
            for dot in self._dots:
                dot.decision = Decision.UNPROCESSED
            return

        # Floor (not round) so the last dot only appears at exact 100%.
        frac = decided_count / total
        self._completed = max(0, min(n, int(frac * n)))

        for i, dot in enumerate(self._dots):
            dot.decision = (
                Decision.YES if i < self._completed else Decision.UNPROCESSED
            )

    # ------------------------------------------------------------------
    # Animation tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Advance one animation frame.

        - Update global spin and self-spin phases.
        - Move each dot a fraction of the way toward its target.
        - Repaint.
        """
        dt = FRAME_MS / 1000.0
        self._elapsed_seconds += dt
        self._spiral_rotation = (self._spiral_rotation + SPIRAL_SPIN_RATE * dt) % (2 * math.pi)

        # Compute targets for each dot and lerp current → target.
        for i, dot in enumerate(self._dots):
            # Decide target position: spiral slot if i<completed, else chaos.
            if i < self._completed:
                # Spiral slot, rotated by the global spin.
                sx, sy = _spiral_position(
                    i, self._dot_count, self._spiral_max_radius,
                )
                ca = math.cos(self._spiral_rotation)
                sa = math.sin(self._spiral_rotation)
                tx = sx * ca - sy * sa
                ty = sx * sa + sy * ca
            else:
                # Chaos orbit: drift angle around home position.
                cur_angle = dot.chaos_angle + dot.chaos_drift * math.sin(
                    self._elapsed_seconds * CHAOS_DRIFT_RATE * 2 * math.pi
                    + dot.self_rotation_phase
                )
                tx = dot.chaos_radius * math.cos(cur_angle)
                ty = dot.chaos_radius * math.sin(cur_angle)

            # Smooth-lerp toward target.
            dot.vx = _lerp(dot.vx, tx, LERP_FACTOR)
            dot.vy = _lerp(dot.vy, ty, LERP_FACTOR)

            # Self-spin: rotates the "+" glyph.
            dot.self_rotation_phase = (
                dot.self_rotation_phase + dot.self_spin_rate * dt
            ) % (2 * math.pi)

        self.update()  # schedule repaint

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Background. Theme's panel color; the swirl sits "embedded" in
        # the UI rather than as a floating box. Dark themes naturally
        # look galaxy-ish; light themes get a paler backdrop and the
        # dot palette compensates (see _resolve_dot_color).
        painter.fillRect(self.rect(), QColor(Colors.BG_PANEL))

        # Draw a faint outline so the widget reads as its own region
        # even on themes whose panel color matches the surrounding area.
        pen = QPen(QColor(Colors.BORDER_SUBTLE))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawRoundedRect(
            0.5, 0.5, self._size - 1, self._size - 1, 6, 6,
        )

        # Translate to center; from here positions are relative.
        cx = self._size / 2.0
        cy = self._size / 2.0
        painter.translate(cx, cy)

        # Draw each dot. Outer (chaos) dots first so spiraled dots
        # paint over them at any overlap — though the chaos ring is
        # outside the spiral radius, this still helps with the brief
        # animation moments when a dot is mid-transition.
        # Walk indices in reverse: highest index first (outermost),
        # which is the chaos zone, then spiral indices on top.
        for i in range(self._dot_count - 1, -1, -1):
            self._paint_dot(painter, self._dots[i])

    def _paint_dot(self, painter: QPainter, dot: _Dot) -> None:
        """Draw one plus-shaped dot."""
        # Color resolution depends on scheme + whether dot is "spiraled"
        # (i.e. has a decision in Scheme B).
        is_spiraled = dot.index < self._completed
        color = self._resolve_dot_color(dot, is_spiraled)

        # Sparkle / breathing pulse: spiraled dots gently modulate
        # scale and alpha so the completed galaxy looks alive. Phase
        # per dot is tied to its spiral position so the wave appears
        # to travel along the arms rather than blinking randomly.
        # Chaos dots stay constant — they're already a busy field;
        # adding a pulse on top would look noisy.
        if is_spiraled:
            phase = (
                self._elapsed_seconds * (2 * math.pi / SPARKLE_PERIOD_SEC)
                + dot.index * GOLDEN_ANGLE
            )
            wave = math.sin(phase)
            scale_mult = 1.0 + SPARKLE_SCALE_AMP * wave
            alpha_mult = 1.0 + SPARKLE_ALPHA_AMP * wave
            # Clamp alpha. Scale doesn't need clamping (always positive
            # at these amplitudes).
            alpha_mult = max(0.0, min(1.0, alpha_mult))
            color = QColor(color)
            color.setAlphaF(color.alphaF() * alpha_mult)
        else:
            scale_mult = 1.0
            # Dimming: chaos dots are slightly dim, spiral dots are
            # full brightness. Visually separates the two states even
            # with the same colors.
            color = QColor(color)
            color.setAlphaF(0.55)

        pen = QPen(color)
        pen.setWidthF(self._plus_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)

        # The plus is two line segments. Apply per-dot rotation:
        # spiraled dots get spiral_rotation + self_rotation; chaos
        # dots get only self_rotation. The spiral rotation is what
        # makes the "+" stay oriented along the spiral arc instead of
        # always facing up.
        if is_spiraled:
            base_angle = math.atan2(dot.vy, dot.vx) + math.pi / 2.0
            # `base_angle` makes the "+" axis tangent to the radius.
            # Add the self-rotation phase on top.
            angle = base_angle + dot.self_rotation_phase
        else:
            angle = dot.self_rotation_phase

        ca = math.cos(angle)
        sa = math.sin(angle)
        arm = self._plus_arm * dot.vscale * scale_mult
        # Horizontal arm rotated
        x1, y1 = -arm * ca, -arm * sa
        x2, y2 = arm * ca, arm * sa
        # Vertical arm rotated (perpendicular)
        x3, y3 = arm * sa, -arm * ca
        x4, y4 = -arm * sa, arm * ca

        painter.drawLine(
            QPointF(dot.vx + x1, dot.vy + y1),
            QPointF(dot.vx + x2, dot.vy + y2),
        )
        painter.drawLine(
            QPointF(dot.vx + x3, dot.vy + y3),
            QPointF(dot.vx + x4, dot.vy + y4),
        )

    def _resolve_dot_color(self, dot: _Dot, is_spiraled: bool) -> QColor:
        """Map dot → color using the aesthetic gradient (Scheme A).

        Color is determined by the dot's position in the spiral
        (inner = warm gold, outer = cool cyan). Same color whether
        spiraled or in chaos; chaos dots get reduced alpha instead.

        Scheme B (semantic green/red) was removed because mapping
        decisions to dots by approximate index produced patterns the
        user couldn't interpret — the dot colors flickered between
        red and green as the burst happened, without conveying useful
        information.
        """
        norm = (
            dot.index / (self._dot_count - 1) if self._dot_count > 1 else 0.0
        )
        return _sample_scheme_a(norm)
