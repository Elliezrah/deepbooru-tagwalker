"""
config/theme.py

Visual theme: color palette, font choices, spacing constants, status
icons, and the Qt stylesheet that ties them together.

This file is intentionally the only place colors and font sizes are
defined. UI modules import the constants here rather than embedding
hex codes inline, so a future theme change is a single-file edit.

The stylesheet is a Qt QSS string applied to the QApplication. QSS
is a CSS-derivative — most familiar CSS rules work but some don't
(no flexbox, no transitions, no animations). Where Qt offers
multiple ways to style a widget (QSS vs setPalette vs setProperty),
we prefer QSS for consistency.

Custom button variants
----------------------
The Yes / No / Delete-tag buttons need distinct colors. Rather than
applying inline stylesheets to each, we set a Qt dynamic property
on the button (e.g. button.setProperty("role", "success")) and
match on it in the QSS. The pattern is:

    btn = QPushButton("Yes (Y)")
    btn.setProperty("role", "success")
    repolish(btn)  # see helper at bottom of file

This is the standard Qt idiom for variant-styled widgets.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------


class Colors:
    """Named color constants for the active theme.

    Values are populated from a palette dict by ``initialize_theme(name)``
    at app startup. UI modules read these as ``Colors.BG_BASE`` etc.
    The class is mutable: switching themes means reassigning each
    attribute. Switching is restart-required at the user level (Pass C
    decision) — within a single run the values are constant after
    initialize_theme is called once.

    Themes are restart-required so we never have to deal with widgets
    that still hold the previous palette's hex strings in a cached
    stylesheet attribute. Simpler and more reliable than live theming.

    Three families:
    - BG_*    : background surfaces, deepest to most elevated
    - TEXT_*  : foreground text in three muting levels
    - Semantic: ACCENT_BLUE, SUCCESS_GREEN, DANGER_RED, WARNING_AMBER
                each paired with a _BG variant — a tinted background
                used for full-row highlighting (queue items, etc.)
    """

    # All attributes here are populated by initialize_theme(). They're
    # declared with placeholder strings so static type checkers see
    # them as class members. The values shown are the Dark defaults,
    # used only if initialize_theme is never called (defensive).
    BG_DEEPEST: str   = "#0d0d0d"
    BG_BASE: str      = "#1e1e1e"
    BG_PANEL: str     = "#252525"
    BG_ELEVATED: str  = "#2d2d2d"
    BG_INPUT: str     = "#1a1a1a"
    BG_HOVER: str     = "#333333"
    BG_PRESSED: str   = "#3f3f3f"

    TEXT_PRIMARY: str   = "#e6e6e6"
    TEXT_SECONDARY: str = "#a8a8a8"
    TEXT_TERTIARY: str  = "#6e6e6e"
    TEXT_DISABLED: str  = "#555555"

    BORDER_SUBTLE: str = "#3a3a3a"
    BORDER_FOCUS: str  = "#4a8eff"

    ACCENT_BLUE: str    = "#4a8eff"
    ACCENT_BLUE_BG: str = "#1e3a66"

    # Derived at theme load, not written per palette: it depends on
    # the row colours, so hand-picking it seven times would drift.
    ACCENT_BAR: str = "#7daeff"
    SUCCESS_GREEN: str = "#5eb55e"
    SUCCESS_BG: str    = "#2d5a2d"

    DANGER_RED: str = "#e06868"
    DANGER_BG: str  = "#5a2828"

    WARNING_AMBER: str = "#e0a440"
    WARNING_BG: str    = "#5a3f1a"

    STATE_BORDER_HAS_TAG: str     = "#5eb55e"
    STATE_BORDER_MISSING_TAG: str = "#e06868"
    STATE_BORDER_ORPHAN: str      = "#e0a440"


# ---------------------------------------------------------------------------
# Theme palettes
# ---------------------------------------------------------------------------
#
# Each theme is a dict mapping every Colors attribute name to its hex
# value for that theme. To add a theme: add a new dict, add it to
# THEME_PALETTES, and add its name to THEME_NAMES (for the Settings
# dropdown order).
#
# All palettes MUST define every key. If a future theme wants to
# experiment with NOT overriding some default, define a small helper
# that fills in missing keys from the Dark default — but for now,
# explicit is better: easier to audit, no surprise color leaks.


_DARK_PALETTE: dict = {
    "BG_DEEPEST":   "#0d0d0d",
    "BG_BASE":      "#1e1e1e",
    "BG_PANEL":     "#252525",
    "BG_ELEVATED":  "#2d2d2d",
    "BG_INPUT":     "#1a1a1a",
    "BG_HOVER":     "#333333",
    "BG_PRESSED":   "#3f3f3f",

    "TEXT_PRIMARY":   "#e6e6e6",
    "TEXT_SECONDARY": "#a8a8a8",
    "TEXT_TERTIARY":  "#6e6e6e",
    "TEXT_DISABLED":  "#555555",

    "BORDER_SUBTLE": "#3a3a3a",
    "BORDER_FOCUS":  "#4a8eff",

    "ACCENT_BLUE":    "#4a8eff",
    "ACCENT_BLUE_BG": "#1e3a66",

    "SUCCESS_GREEN": "#5eb55e",
    "SUCCESS_BG":    "#2d5a2d",

    "DANGER_RED": "#e06868",
    "DANGER_BG":  "#5a2828",

    "WARNING_AMBER": "#e0a440",
    "WARNING_BG":    "#5a3f1a",

    "STATE_BORDER_HAS_TAG":     "#5eb55e",
    "STATE_BORDER_MISSING_TAG": "#e06868",
    "STATE_BORDER_ORPHAN":      "#e0a440",
}


# Light theme: high-contrast, clean, no flashy accents. Designed to be
# the obvious default for users who prefer light interfaces (often for
# eye comfort in bright rooms). Background uses soft warm white (not
# pure #FFFFFF — easier on the eyes). Text is near-black. Accent blue
# is darker than Dark theme's so it stays distinguishable against the
# pale background. Semantic colors are darker (forest green, brick red,
# amber-bronze) rather than the lighter shades the Dark theme uses on
# its black background — same idea, inverted for legibility.
_LIGHT_PALETTE: dict = {
    "BG_DEEPEST":   "#d4d4d4",   # window chrome (slightly darker than panel)
    "BG_BASE":      "#f5f3ef",   # main window — soft warm white
    "BG_PANEL":     "#e8e6e1",   # sidebars
    "BG_ELEVATED":  "#dcdad5",   # buttons, cards
    "BG_INPUT":     "#fdfbf7",   # inputs — slightly brighter than base
    "BG_HOVER":     "#d0cec8",   # hover
    "BG_PRESSED":   "#c0beb8",   # pressed

    "TEXT_PRIMARY":   "#1c1c1c",   # near-black, not pure black (better antialiasing)
    "TEXT_SECONDARY": "#555555",   # subtitles
    "TEXT_TERTIARY":  "#888888",   # hints, disabled
    "TEXT_DISABLED":  "#a8a8a8",   # fully disabled

    "BORDER_SUBTLE": "#c0bdb6",
    "BORDER_FOCUS":  "#2563c8",   # focused input border

    # Accents shifted darker than Dark theme so they read against light
    # backgrounds. Same families, more saturated, lower luminance.
    "ACCENT_BLUE":    "#2563c8",   # darker steel blue
    "ACCENT_BLUE_BG": "#cfdcf2",   # pale blue tint for queue-current rows

    "SUCCESS_GREEN": "#2b8a2b",   # forest green
    "SUCCESS_BG":    "#cfe6cf",   # pale green tint

    "DANGER_RED": "#b8302d",   # brick red
    "DANGER_BG":  "#f0cccc",   # pale red tint

    "WARNING_AMBER": "#a16410",   # amber-bronze
    "WARNING_BG":    "#f0dcb6",   # pale amber tint

    # State borders: explicitly punchier than the in-text semantic
    # colors above. A border has less visual weight than text-bg pairs,
    # so the saturation+darkness combo helps it pop against the pale
    # canvas. (This is exactly why STATE_BORDER_* exists separately
    # from SUCCESS/DANGER/WARNING.)
    "STATE_BORDER_HAS_TAG":     "#1c8a1c",
    "STATE_BORDER_MISSING_TAG": "#cc2828",
    "STATE_BORDER_ORPHAN":      "#cc8a14",
}


# ---------------------------------------------------------------------------
# Stage 2 themes
# ---------------------------------------------------------------------------
# Five additional palettes. Each follows the same key set as Dark/Light.
# Designed to be visually distinct while keeping text/background contrast
# readable (WCAG-ish; primary text always clears ~7:1 on its background).
# Semantic colors stay recognizably green/red/amber within each theme's
# mood so the decision feedback never becomes ambiguous.


# Nord: cool, muted arctic palette (https://www.nordtheme.com). Polar
# Night backgrounds, Snow Storm text, frost/aurora accents. Calm and
# low-contrast-feeling without sacrificing legibility.
_NORD_PALETTE: dict = {
    "BG_DEEPEST":   "#242933",
    "BG_BASE":      "#2e3440",
    "BG_PANEL":     "#343c4c",
    "BG_ELEVATED":  "#3b4252",
    "BG_INPUT":     "#2a2f3a",
    "BG_HOVER":     "#434c5e",
    "BG_PRESSED":   "#4c566a",

    "TEXT_PRIMARY":   "#eceff4",
    "TEXT_SECONDARY": "#d8dee9",
    "TEXT_TERTIARY":  "#8893a6",
    "TEXT_DISABLED":  "#5c677d",

    "BORDER_SUBTLE": "#434c5e",
    "BORDER_FOCUS":  "#88c0d0",

    "ACCENT_BLUE":    "#88c0d0",   # frost cyan
    "ACCENT_BLUE_BG": "#3b4a52",

    "SUCCESS_GREEN": "#a3be8c",   # aurora green
    "SUCCESS_BG":    "#3a4636",

    "DANGER_RED": "#bf616a",      # aurora red
    "DANGER_BG":  "#4a3236",

    "WARNING_AMBER": "#ebcb8b",   # aurora yellow
    "WARNING_BG":    "#4a4232",

    "STATE_BORDER_HAS_TAG":     "#a3be8c",
    "STATE_BORDER_MISSING_TAG": "#bf616a",
    "STATE_BORDER_ORPHAN":      "#ebcb8b",
}


# Dracula: the popular dark purple palette (https://draculatheme.com).
# Dark blue-gray backgrounds, soft foreground, vivid pastel accents.
_DRACULA_PALETTE: dict = {
    "BG_DEEPEST":   "#191a21",
    "BG_BASE":      "#282a36",
    "BG_PANEL":     "#2e303e",
    "BG_ELEVATED":  "#383a4a",
    "BG_INPUT":     "#21222c",
    "BG_HOVER":     "#44475a",
    "BG_PRESSED":   "#565872",

    "TEXT_PRIMARY":   "#f8f8f2",
    "TEXT_SECONDARY": "#c9c9d8",
    "TEXT_TERTIARY":  "#8a8ca6",
    "TEXT_DISABLED":  "#5a5c75",

    "BORDER_SUBTLE": "#44475a",
    "BORDER_FOCUS":  "#bd93f9",

    "ACCENT_BLUE":    "#bd93f9",   # purple (Dracula's signature accent)
    "ACCENT_BLUE_BG": "#3b3055",

    "SUCCESS_GREEN": "#50fa7b",
    "SUCCESS_BG":    "#1f4a2c",

    "DANGER_RED": "#ff5555",
    "DANGER_BG":  "#4a2230",

    "WARNING_AMBER": "#f1fa8c",   # Dracula yellow (greenish)
    "WARNING_BG":    "#46472a",

    "STATE_BORDER_HAS_TAG":     "#50fa7b",
    "STATE_BORDER_MISSING_TAG": "#ff5555",
    "STATE_BORDER_ORPHAN":      "#ffb86c",   # Dracula orange (clearer than yellow on border)
}


# Solarized Light: Ethan Schoonover's classic low-contrast warm light
# palette (https://ethanschoonover.com/solarized). Cream base, muted
# blue-gray text, accent colors from the fixed Solarized set.
_SOLARIZED_LIGHT_PALETTE: dict = {
    "BG_DEEPEST":   "#d5cdb6",
    "BG_BASE":      "#fdf6e3",   # base3
    "BG_PANEL":     "#eee8d5",   # base2
    "BG_ELEVATED":  "#e3ddc8",
    "BG_INPUT":     "#fdf6e3",
    "BG_HOVER":     "#e0dac4",
    "BG_PRESSED":   "#d0c9b0",

    "TEXT_PRIMARY":   "#586e75",   # base01 (body text)
    "TEXT_SECONDARY": "#657b83",   # base00
    "TEXT_TERTIARY":  "#93a1a1",   # base1
    "TEXT_DISABLED":  "#aab7b7",

    "BORDER_SUBTLE": "#ccc6b0",
    "BORDER_FOCUS":  "#268bd2",

    "ACCENT_BLUE":    "#268bd2",   # solarized blue
    "ACCENT_BLUE_BG": "#cfe3ef",

    "SUCCESS_GREEN": "#859900",   # solarized green
    "SUCCESS_BG":    "#e4e8c0",

    "DANGER_RED": "#dc322f",      # solarized red
    "DANGER_BG":  "#f3d6cd",

    "WARNING_AMBER": "#b58900",   # solarized yellow
    "WARNING_BG":    "#ece4c2",

    "STATE_BORDER_HAS_TAG":     "#5a7000",   # darker green for border pop
    "STATE_BORDER_MISSING_TAG": "#c12522",
    "STATE_BORDER_ORPHAN":      "#9a7400",
}


# Cyberpunk: near-black base with electric yellow as the primary accent
# and neon cyan/magenta secondaries. High-energy. Yellow is the accent
# (replacing the usual blue) per the project plan.
_CYBERPUNK_PALETTE: dict = {
    "BG_DEEPEST":   "#050507",
    "BG_BASE":      "#0d0d12",
    "BG_PANEL":     "#14141c",
    "BG_ELEVATED":  "#1c1c28",
    "BG_INPUT":     "#0a0a0f",
    "BG_HOVER":     "#24243200",  # corrected below — placeholder guard
    "BG_PRESSED":   "#2c2c3e",

    "TEXT_PRIMARY":   "#f0f0f5",
    "TEXT_SECONDARY": "#b8b8d0",
    "TEXT_TERTIARY":  "#7878a0",
    "TEXT_DISABLED":  "#505068",

    "BORDER_SUBTLE": "#2a2a3a",
    "BORDER_FOCUS":  "#fcee0a",

    "ACCENT_BLUE":    "#fcee0a",   # electric yellow (the signature accent)
    "ACCENT_BLUE_BG": "#3a3608",

    "SUCCESS_GREEN": "#00f0a0",   # neon mint
    "SUCCESS_BG":    "#0a3a2a",

    "DANGER_RED": "#ff2e63",      # neon magenta-red
    "DANGER_BG":  "#3e0f20",

    "WARNING_AMBER": "#ff9f1c",   # neon orange
    "WARNING_BG":    "#3a2408",

    "STATE_BORDER_HAS_TAG":     "#00f0a0",
    "STATE_BORDER_MISSING_TAG": "#ff2e63",
    "STATE_BORDER_ORPHAN":      "#ff9f1c",
}
# Fix the placeholder hover value (kept the dict literal clean above).
_CYBERPUNK_PALETTE["BG_HOVER"] = "#242432"


# Green (forest/sage): muted natural greens. Dark mossy backgrounds,
# warm off-white text, sage and bark accents. Easy on the eyes, organic.
_GREEN_PALETTE: dict = {
    "BG_DEEPEST":   "#10160f",
    "BG_BASE":      "#1a231a",
    "BG_PANEL":     "#212c20",
    "BG_ELEVATED":  "#293529",
    "BG_INPUT":     "#161e15",
    "BG_HOVER":     "#324132",
    "BG_PRESSED":   "#3c4d3c",

    "TEXT_PRIMARY":   "#e8ecdf",
    "TEXT_SECONDARY": "#bcc7b0",
    "TEXT_TERTIARY":  "#849078",
    "TEXT_DISABLED":  "#5c6655",

    "BORDER_SUBTLE": "#324132",
    "BORDER_FOCUS":  "#8fbf6f",

    "ACCENT_BLUE":    "#8fbf6f",   # sage green accent (replaces blue)
    "ACCENT_BLUE_BG": "#2e3f24",

    "SUCCESS_GREEN": "#a3d977",   # bright leaf — distinct from sage accent
    "SUCCESS_BG":    "#2c4420",

    "DANGER_RED": "#d97b6c",      # clay red — warm, fits the organic mood
    "DANGER_BG":  "#4a2c26",

    "WARNING_AMBER": "#d9b860",   # wheat
    "WARNING_BG":    "#473a1e",

    "STATE_BORDER_HAS_TAG":     "#a3d977",
    "STATE_BORDER_MISSING_TAG": "#d97b6c",
    "STATE_BORDER_ORPHAN":      "#d9b860",
}


# Registry maps user-visible theme name → palette dict.
# Minimum contrast the current-row accent bar must reach against
# every background it can sit on. WCAG asks 3.0:1 for graphical
# elements; 3.5 leaves margin for the varying displays this runs on.
ACCENT_BAR_MIN_CONTRAST = 3.5

# Width of that bar, in pixels.
ACCENT_BAR_WIDTH = 4


def _relative_luminance(colour: str) -> float:
    """WCAG relative luminance of a #rrggbb colour."""
    raw = colour.lstrip("#")
    channels = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928
              else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return (0.2126 * linear[0] + 0.7152 * linear[1]
            + 0.0722 * linear[2])


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast between two colours, 1.0 (identical) to 21.0."""
    a, b = _relative_luminance(first), _relative_luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _with_lightness(colour: str, lightness: float) -> str:
    import colorsys

    raw = colour.lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, _old, sat = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(
        hue, max(0.0, min(1.0, lightness)), sat)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255),
                              round(b * 255))


def accent_bar_colour(palette: dict) -> str:
    """A bar colour that stays legible on every row state.

    The current row can be plain, green (yes), red (no) or amber
    (skipped), and each theme colours those differently — so a single
    fixed accent cannot work. Two themes fail outright with their own
    accent: Dark manages only 2.5:1 against its green rows, Solarized
    Light 2.7:1.

    So the theme's accent hue is kept and only its LIGHTNESS is
    adjusted, by the smallest amount that clears the threshold against
    every row background. Maximising contrast instead would push every
    theme to near-white and throw away the theme's identity, which is
    the opposite of what this is for.
    """
    import colorsys

    accent = palette.get("ACCENT_BLUE", "#4a8eff")
    backgrounds = [palette.get(key) for key in
                   ("SUCCESS_BG", "DANGER_BG", "WARNING_BG",
                    "BG_ELEVATED", "BG_BASE")]
    backgrounds = [b for b in backgrounds if b]
    if not backgrounds:
        return accent

    def worst(colour: str) -> float:
        return min(contrast_ratio(colour, bg) for bg in backgrounds)

    if worst(accent) >= ACCENT_BAR_MIN_CONTRAST:
        return accent

    raw = accent.lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))
    base = colorsys.rgb_to_hls(r, g, b)[1]
    for step in range(1, 101):
        for candidate_l in (base + step / 100, base - step / 100):
            if not 0.0 <= candidate_l <= 1.0:
                continue
            candidate = _with_lightness(accent, candidate_l)
            if worst(candidate) >= ACCENT_BAR_MIN_CONTRAST:
                return candidate
    # Nothing in this hue works: fall back to whichever extreme is
    # further from the row colours rather than returning something
    # invisible.
    white, black = "#ffffff", "#000000"
    return white if worst(white) >= worst(black) else black


THEME_PALETTES: dict = {
    "Dark":            _DARK_PALETTE,
    "Light":           _LIGHT_PALETTE,
    "Nord":            _NORD_PALETTE,
    "Solarized Light": _SOLARIZED_LIGHT_PALETTE,
    "Dracula":         _DRACULA_PALETTE,
    "Cyberpunk":       _CYBERPUNK_PALETTE,
    "Green":           _GREEN_PALETTE,
}

# Ordered list for the Settings dropdown. Dark first (default), Light
# next (most common alternative), then the rest grouped loosely by mood:
# cool dark (Nord, Dracula), light (Solarized Light), vivid (Cyberpunk,
# Green).
THEME_NAMES: list = [
    "Dark", "Light", "Nord", "Solarized Light", "Dracula",
    "Cyberpunk", "Green",
]


# Tracks which theme is currently active. Read-only from outside.
_active_theme_name: str = "Dark"


def get_active_theme_name() -> str:
    """Return the name of the currently-applied theme."""
    return _active_theme_name


def initialize_theme(name: str) -> None:
    """Populate the Colors class with the values from the named theme.

    Call this exactly once at app startup, BEFORE any UI module is
    constructed (so they all read the right palette during init).
    Subsequent calls are no-ops in practice because Qt widgets already
    captured the old values into their stylesheets — but the function
    will still update Colors if called again, just won't repaint
    existing widgets. (Restart-required theme switching is the design
    decision; that constraint is what keeps this simple.)

    Unknown theme names fall back to "Dark" without raising — defensive,
    so a stale settings file pointing to a removed theme doesn't crash.
    """
    global _active_theme_name
    palette = THEME_PALETTES.get(name)
    if palette is None:
        palette = THEME_PALETTES["Dark"]
        name = "Dark"
    _active_theme_name = name
    for attr, value in palette.items():
        setattr(Colors, attr, value)
    # Derived after the palette is in place, since it is computed from
    # the row colours this theme just set.
    Colors.ACCENT_BAR = accent_bar_colour(palette)


# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------


class Fonts:
    """Font family stacks and sizes.

    Sizes in points, not pixels — Windows DPI scaling adjusts them
    automatically. Segoe UI is the Windows default since Vista;
    Helvetica Neue and Arial cover the fallbacks for non-Windows
    users.
    """
    FAMILY      = '"Segoe UI", "Helvetica Neue", Arial, sans-serif'
    FAMILY_MONO = '"Consolas", "Courier New", monospace'

    SIZE_SMALL   = 9    # queue rows, action log, status bar
    SIZE_NORMAL  = 10   # default body text
    SIZE_LARGE   = 11   # primary buttons
    SIZE_HEADING = 14   # current tag name, dialog titles


# ---------------------------------------------------------------------------
# Spacing
# ---------------------------------------------------------------------------


class Spacing:
    """Common spacing values in pixels. Use these instead of magic
    numbers so the rhythm stays consistent across panels."""
    TIGHT   = 4
    NORMAL  = 8
    LOOSE   = 12
    SECTION = 16


class Radius:
    """Border radius values in pixels."""
    SMALL  = 3
    NORMAL = 4
    LARGE  = 6


# ---------------------------------------------------------------------------
# Status icons
# ---------------------------------------------------------------------------


class Icons:
    """Status icons used throughout the UI.

    All entries are plain Unicode strings that render with the default
    Windows fonts (Segoe UI + Segoe UI Emoji). No image files needed.

    If we ever want crisper icons, a Tabler-style SVG icon font is the
    natural upgrade path — the UI references icons through this class,
    so a swap is a one-file change.
    """
    CHECK       = "\u2713"      # ✓
    CROSS       = "\u2717"      # ✗
    HOURGLASS   = "\u23F3"      # ⏳ (pending)
    SKIP        = "\u21B7"      # ↷  (skipped)
    EYE         = "\U0001F441"  # 👁  (currently viewing)
    SEARCH      = "\U0001F50D"  # 🔍 (view/find images for a tag)
    PIN         = "\U0001F4CD"  # 📍 (position)
    FOLDER      = "\U0001F4C1"  # 📁
    TAG         = "\U0001F3F7"  # 🏷
    DISK        = "\U0001F4BE"  # 💾 (saved)
    WARN        = "\u26A0"      # ⚠
    DONE        = "\U0001F389"  # 🎉
    NO_FILE     = "\u2205"      # ∅ (orphan / no caption file)
    PLUS        = "\u002B"      # + (add tag)


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------


# Built lazily by build_stylesheet() so the active palette is reflected
# even if initialize_theme is called after this module is imported.
# Sections are separated by comment blocks so adding or tweaking rules
# stays readable. Each rule has a brief comment on what it controls.
def build_stylesheet() -> str:
    """Render the Qt stylesheet using the currently active palette.

    Called by apply_theme(). Re-rendered each call so adjusting Colors
    between calls (theoretically) takes effect. In practice this is
    invoked once at startup since theming is restart-required.
    """
    return f"""
/* ---------- Base: applies to everything by default ---------- */

QWidget {{
    background-color: {Colors.BG_BASE};
    color: {Colors.TEXT_PRIMARY};
    font-family: {Fonts.FAMILY};
    font-size: {Fonts.SIZE_NORMAL}pt;
    selection-background-color: {Colors.ACCENT_BLUE};
    selection-color: {Colors.BG_BASE};
}}

QMainWindow, QDialog {{
    background-color: {Colors.BG_BASE};
}}

/* ---------- Panels and frames ---------- */

QFrame[role="panel"] {{
    background-color: {Colors.BG_PANEL};
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: {Radius.NORMAL}px;
}}

QFrame[role="elevated"] {{
    background-color: {Colors.BG_ELEVATED};
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: {Radius.NORMAL}px;
}}

QSplitter::handle {{
    background-color: {Colors.BORDER_SUBTLE};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical   {{ height: 1px; }}

/* ---------- Menus and menu bar ---------- */

QMenuBar {{
    background-color: {Colors.BG_BASE};
    color: {Colors.TEXT_PRIMARY};
    border-bottom: 1px solid {Colors.BORDER_SUBTLE};
    padding: {Spacing.TIGHT}px;
}}
QMenuBar::item {{
    background: transparent;
    padding: {Spacing.TIGHT}px {Spacing.LOOSE}px;
    border-radius: {Radius.SMALL}px;
}}
QMenuBar::item:selected {{
    background-color: {Colors.BG_HOVER};
}}

QMenu {{
    background-color: {Colors.BG_ELEVATED};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER_SUBTLE};
    padding: {Spacing.TIGHT}px;
}}
QMenu::item {{
    padding: {Spacing.TIGHT}px {Spacing.LOOSE}px;
    border-radius: {Radius.SMALL}px;
}}
QMenu::item:selected {{
    background-color: {Colors.ACCENT_BLUE};
    color: {Colors.BG_BASE};
}}
QMenu::separator {{
    height: 1px;
    background-color: {Colors.BORDER_SUBTLE};
    margin: {Spacing.TIGHT}px {Spacing.NORMAL}px;
}}

/* ---------- Buttons: default ---------- */

QPushButton {{
    background-color: {Colors.BG_ELEVATED};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: {Radius.NORMAL}px;
    padding: {Spacing.TIGHT}px {Spacing.LOOSE}px;
    min-height: 22px;
}}
QPushButton:hover {{
    background-color: {Colors.BG_HOVER};
    border-color: {Colors.BORDER_FOCUS};
}}
QPushButton:pressed {{
    background-color: {Colors.BG_PRESSED};
}}
QPushButton:disabled {{
    background-color: {Colors.BG_PANEL};
    color: {Colors.TEXT_DISABLED};
    border-color: {Colors.BORDER_SUBTLE};
}}

/* ---------- Buttons: semantic variants via [role] property ---------- */
/* Set with: btn.setProperty("role", "success") then repolish(btn).    */

QPushButton[role="success"] {{
    background-color: {Colors.SUCCESS_BG};
    color: {Colors.SUCCESS_GREEN};
    border: 1px solid {Colors.SUCCESS_GREEN};
    font-size: {Fonts.SIZE_LARGE}pt;
    font-weight: bold;
    padding: {Spacing.NORMAL}px {Spacing.SECTION}px;
}}
QPushButton[role="success"]:hover {{
    background-color: {Colors.SUCCESS_GREEN};
    color: {Colors.BG_BASE};
}}
QPushButton[role="success"]:pressed {{
    background-color: {Colors.SUCCESS_GREEN};
    color: {Colors.BG_DEEPEST};
}}

QPushButton[role="danger"] {{
    background-color: {Colors.DANGER_BG};
    color: {Colors.DANGER_RED};
    border: 1px solid {Colors.DANGER_RED};
    font-size: {Fonts.SIZE_LARGE}pt;
    font-weight: bold;
    padding: {Spacing.NORMAL}px {Spacing.SECTION}px;
}}
QPushButton[role="danger"]:hover {{
    background-color: {Colors.DANGER_RED};
    color: {Colors.BG_BASE};
}}
QPushButton[role="danger"]:pressed {{
    background-color: {Colors.DANGER_RED};
    color: {Colors.BG_DEEPEST};
}}

QPushButton[role="secondary"] {{
    background-color: transparent;
    color: {Colors.TEXT_SECONDARY};
    border: 1px solid {Colors.BORDER_SUBTLE};
}}
QPushButton[role="secondary"]:hover {{
    background-color: {Colors.BG_HOVER};
    color: {Colors.TEXT_PRIMARY};
}}

/* ---------- Inputs: line edits and combo boxes ---------- */

QLineEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {Colors.BG_INPUT};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: {Radius.NORMAL}px;
    padding: {Spacing.TIGHT}px {Spacing.NORMAL}px;
    min-height: 22px;
}}
QLineEdit:focus, QSpinBox:focus {{
    border-color: {Colors.BORDER_FOCUS};
}}
QLineEdit:disabled {{
    background-color: {Colors.BG_PANEL};
    color: {Colors.TEXT_DISABLED};
}}

QComboBox {{
    background-color: {Colors.BG_ELEVATED};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: {Radius.NORMAL}px;
    padding: {Spacing.TIGHT}px {Spacing.NORMAL}px;
    min-height: 22px;
}}
QComboBox:hover {{
    border-color: {Colors.BORDER_FOCUS};
}}
QComboBox::drop-down {{
    background: transparent;
    border: none;
    width: 18px;
}}
QComboBox QAbstractItemView {{
    background-color: {Colors.BG_ELEVATED};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER_SUBTLE};
    selection-background-color: {Colors.ACCENT_BLUE};
    selection-color: {Colors.BG_BASE};
}}

/* ---------- Checkboxes ---------- */

QCheckBox {{
    color: {Colors.TEXT_PRIMARY};
    spacing: {Spacing.NORMAL}px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: {Radius.SMALL}px;
    background-color: {Colors.BG_INPUT};
}}
QCheckBox::indicator:hover {{
    border-color: {Colors.BORDER_FOCUS};
}}
QCheckBox::indicator:checked {{
    background-color: {Colors.ACCENT_BLUE};
    border-color: {Colors.ACCENT_BLUE};
}}

/* ---------- Radio buttons ---------- */

QRadioButton {{
    color: {Colors.TEXT_PRIMARY};
    spacing: {Spacing.NORMAL}px;
}}
QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: 8px;
    background-color: {Colors.BG_INPUT};
}}
QRadioButton::indicator:hover {{
    border-color: {Colors.BORDER_FOCUS};
}}
QRadioButton::indicator:checked {{
    /* Blue ring with a blue centre dot, so the selected state reads
       clearly instead of the near-invisible default dark dot. The
       radial gradient draws the inner dot inset from the border. */
    border: 1px solid {Colors.ACCENT_BLUE};
    background-color: qradialgradient(
        cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 {Colors.ACCENT_BLUE}, stop:0.55 {Colors.ACCENT_BLUE},
        stop:0.6 {Colors.BG_INPUT}, stop:1 {Colors.BG_INPUT}
    );
}}
QRadioButton::indicator:checked:hover {{
    border-color: {Colors.ACCENT_BLUE};
}}
QRadioButton:disabled {{
    color: {Colors.TEXT_DISABLED};
}}

/* ---------- Lists and trees ---------- */

QTreeWidget, QListWidget {{
    background-color: {Colors.BG_PANEL};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: {Radius.NORMAL}px;
    outline: 0;
}}
QTreeWidget::item, QListWidget::item {{
    padding: {Spacing.TIGHT}px {Spacing.NORMAL}px;
    border-radius: {Radius.SMALL}px;
}}
QTreeWidget::item:hover, QListWidget::item:hover {{
    background-color: {Colors.BG_HOVER};
}}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background-color: {Colors.ACCENT_BLUE_BG};
    color: {Colors.TEXT_PRIMARY};
}}

QHeaderView::section {{
    background-color: {Colors.BG_PANEL};
    color: {Colors.TEXT_SECONDARY};
    border: none;
    border-bottom: 1px solid {Colors.BORDER_SUBTLE};
    padding: {Spacing.TIGHT}px {Spacing.NORMAL}px;
}}

/* ---------- Tabs ---------- */
/* Without explicit styling, QTabBar falls back to the native palette,
   which on dark themes renders as a near-white tab with white text —
   effectively invisible. These rules make tabs theme-aware: the panel
   color for the bar, secondary text for inactive tabs, primary text on
   an elevated background for the selected tab, with the accent as the
   active underline. */
QTabWidget::pane {{
    background-color: {Colors.BG_BASE};
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: {Radius.NORMAL}px;
    top: -1px;
}}
QTabBar {{
    background: transparent;
}}
QTabBar::tab {{
    background-color: {Colors.BG_PANEL};
    color: {Colors.TEXT_SECONDARY};
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-bottom: none;
    border-top-left-radius: {Radius.SMALL}px;
    border-top-right-radius: {Radius.SMALL}px;
    padding: {Spacing.TIGHT}px {Spacing.LOOSE}px;
    margin-right: 2px;
}}
QTabBar::tab:hover {{
    background-color: {Colors.BG_HOVER};
    color: {Colors.TEXT_PRIMARY};
}}
QTabBar::tab:selected {{
    background-color: {Colors.BG_ELEVATED};
    color: {Colors.TEXT_PRIMARY};
    border-bottom: 2px solid {Colors.ACCENT_BLUE};
}}

/* ---------- Labels ---------- */

QLabel {{
    background: transparent;
    color: {Colors.TEXT_PRIMARY};
}}
QLabel[role="secondary"] {{
    color: {Colors.TEXT_SECONDARY};
}}
QLabel[role="tertiary"] {{
    color: {Colors.TEXT_TERTIARY};
    font-size: {Fonts.SIZE_SMALL}pt;
}}
QLabel[role="heading"] {{
    color: {Colors.TEXT_PRIMARY};
    font-size: {Fonts.SIZE_HEADING}pt;
    font-weight: bold;
}}
QLabel[role="success"]   {{ color: {Colors.SUCCESS_GREEN};   }}
QLabel[role="danger"]    {{ color: {Colors.DANGER_RED};      }}
QLabel[role="warning"]   {{ color: {Colors.WARNING_AMBER};   }}
QLabel[role="info"]      {{ color: {Colors.ACCENT_BLUE};     }}

QLabel[role="mono"] {{
    font-family: {Fonts.FAMILY_MONO};
    font-size: {Fonts.SIZE_SMALL}pt;
    color: {Colors.TEXT_SECONDARY};
}}

/* ---------- Status bar ---------- */

QStatusBar {{
    background-color: {Colors.BG_BASE};
    color: {Colors.TEXT_SECONDARY};
    border-top: 1px solid {Colors.BORDER_SUBTLE};
    font-size: {Fonts.SIZE_SMALL}pt;
}}
QStatusBar::item {{
    border: none;
}}

/* ---------- Scrollbars ---------- */
/* Default Windows scrollbars look out of place in dark themes; this
   makes them slim and dark to match the rest of the UI.            */

QScrollBar:vertical {{
    background: {Colors.BG_PANEL};
    width: 10px;
    margin: 0;
    border-radius: {Radius.SMALL}px;
}}
QScrollBar::handle:vertical {{
    background: {Colors.BORDER_SUBTLE};
    min-height: 24px;
    border-radius: {Radius.SMALL}px;
}}
QScrollBar::handle:vertical:hover {{
    background: {Colors.TEXT_TERTIARY};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0; background: transparent;
}}

QScrollBar:horizontal {{
    background: {Colors.BG_PANEL};
    height: 10px;
    margin: 0;
    border-radius: {Radius.SMALL}px;
}}
QScrollBar::handle:horizontal {{
    background: {Colors.BORDER_SUBTLE};
    min-width: 24px;
    border-radius: {Radius.SMALL}px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {Colors.TEXT_TERTIARY};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0; background: transparent;
}}

/* ---------- ToolTips ---------- */

QToolTip {{
    background-color: {Colors.BG_ELEVATED};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER_SUBTLE};
    padding: {Spacing.TIGHT}px {Spacing.NORMAL}px;
    border-radius: {Radius.SMALL}px;
}}

/* ---------- Group boxes (used in settings dialog) ---------- */

QGroupBox {{
    background-color: {Colors.BG_PANEL};
    border: 1px solid {Colors.BORDER_SUBTLE};
    border-radius: {Radius.NORMAL}px;
    margin-top: {Spacing.LOOSE}px;
    padding: {Spacing.LOOSE}px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 {Spacing.NORMAL}px;
    background-color: {Colors.BG_BASE};
    color: {Colors.TEXT_SECONDARY};
}}
"""


# ---------------------------------------------------------------------------
# Helpers used at runtime
# ---------------------------------------------------------------------------


def apply_theme(app) -> None:
    """Apply the TagWalker theme to a QApplication.

    Call this exactly once at startup from main.py, after the
    QApplication is constructed and AFTER initialize_theme(name) has
    set the desired palette, but before any widgets are shown.

    The parameter is left untyped so this module remains importable
    without PySide6 in pure-Python tooling contexts (e.g. printing
    color values from a script).
    """
    app.setStyleSheet(build_stylesheet())


def scrollbar_with_arrows_qss() -> str:
    """Vertical-scrollbar QSS WITH classic up/down arrow buttons.

    The global theme hides scrollbar arrow buttons for a slim modern
    look. Long dialog lists (audit results, conflict scan) are easier
    to nudge with classic click-and-hold arrows, so apply this to a
    specific QScrollArea via ``widget.setStyleSheet(...)``. Scoping it
    to one widget keeps the rest of the app's scrollbars unchanged.

    Arrows use the CSS border-triangle trick (no image assets). The
    track is widened to 16px to leave room for the buttons and to be
    easier to grab. Colors track the active theme via ``Colors``.
    """
    return f"""
    QScrollBar:vertical {{
        background: {Colors.BG_PANEL};
        width: 16px;
        margin: 16px 0 16px 0;
        border-radius: {Radius.SMALL}px;
    }}
    QScrollBar::handle:vertical {{
        background: {Colors.BORDER_SUBTLE};
        min-height: 24px;
        border-radius: {Radius.SMALL}px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {Colors.TEXT_TERTIARY};
    }}
    QScrollBar::sub-line:vertical {{
        background: {Colors.BG_ELEVATED};
        height: 15px;
        subcontrol-position: top;
        subcontrol-origin: margin;
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.SMALL}px;
    }}
    QScrollBar::add-line:vertical {{
        background: {Colors.BG_ELEVATED};
        height: 15px;
        subcontrol-position: bottom;
        subcontrol-origin: margin;
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.SMALL}px;
    }}
    QScrollBar::sub-line:vertical:hover,
    QScrollBar::add-line:vertical:hover {{
        background: {Colors.ACCENT_BLUE};
    }}
    QScrollBar::sub-line:vertical:pressed,
    QScrollBar::add-line:vertical:pressed {{
        background: {Colors.BORDER_FOCUS};
    }}
    QScrollBar::up-arrow:vertical {{
        width: 0; height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-bottom: 6px solid {Colors.TEXT_SECONDARY};
    }}
    QScrollBar::down-arrow:vertical {{
        width: 0; height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 6px solid {Colors.TEXT_SECONDARY};
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}
    """


def repolish(widget) -> None:
    """Force a widget to re-apply stylesheet rules after a property change.

    Use this after calling setProperty("role", ...) so the new
    selector match takes effect immediately. Without it, the
    property change is registered but the stylesheet doesn't
    re-evaluate until the next event-loop refresh.

        btn.setProperty("role", "success")
        repolish(btn)
    """
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
