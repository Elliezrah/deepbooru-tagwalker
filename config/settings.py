"""
config/settings.py

Persistent user preferences for TagWalker.

Wraps Qt's QSettings to provide typed accessors and a single source
of truth for default values. Settings live in an INI file (not the
Windows registry) so they're portable across machines, human-readable,
and easy to inspect or delete if anything ever goes wrong.

On Windows the file lives at:
    %APPDATA%\\TagWalker\\TagWalker.ini

What's stored
-------------
- Autosave configuration: enabled flag, time interval, action interval.
  Autosave only actually fires when a save path is set (see persistence
  + main_window); this module just stores the user's preference.
- Keyboard shortcuts: each remappable, with sensible defaults matching
  what was agreed in design discussion (Y/N/Space/Backspace/arrows/Esc).
- UI defaults: sort mode, filter mode, show-orphans toggle. These are
  used as the initial state for a fresh session; the live session can
  diverge and is restored separately via persistence.py if loaded.
- Last-used directory: the dataset folder most recently opened, used
  as the default starting point for the File → Browse dialog.
- Window geometry / state: position, size, and splitter positions so
  the layout the user adjusted is remembered.

What is deliberately NOT stored
-------------------------------
- Recent session files. Per the design decision in our chat, sessions
  are loaded manually only — no auto-detection, no recent list. The
  user explicitly opens whichever .json they want via File → Load
  Session. Avoiding even an opt-in recent list keeps the file menu
  clean and matches the "less nagging" principle.

Type-coercion notes
-------------------
QSettings stores everything as strings internally (especially with
INI format), so reading a bool back can give "true" or "false" as
strings rather than True/False. The _get helper centralizes coercion:
it inspects the default's type and converts the stored value to match.
This means callers always get the type they expect, never a string
masquerading as a bool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtGui import QKeySequence


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


# Single source of truth for every setting's default value. The Settings
# dialog's "Reset to defaults" button reads from here. Every defined
# setting MUST have an entry — _get falls back to this dict if QSettings
# has no stored value.
# The shipped blacklist text lives with the blacklist logic;
# settings only needs it as this table's default value.
from core.blacklist import DEFAULT_BLACKLIST as _DEFAULT_BLACKLIST

DEFAULTS: dict[str, Any] = {
    # Runtime-compatibility warning acknowledgment. Stores the exact
    # Python+PySide6 pair string (crashlog.runtime_pair_string) the user
    # chose "don't warn again" for. Empty = never acknowledged. If either
    # component changes (an upgrade), the pair no longer matches and the
    # startup warning returns.
    "runtime/acknowledged_pair": "",
    # Time-boxed variant of the above: the warning is suppressed while
    # the running pair equals snooze_pair AND time.time() is before
    # snooze_until (epoch seconds). Chosen from the warning dialog's
    # "don't remind me for N" dropdown.
    "runtime/snooze_pair": "",
    "runtime/snooze_until": 0.0,

    # EULA acceptance. Stores the version string of the EULA the user
    # last accepted; empty means never accepted. On launch, if this does
    # not match the current EULA_VERSION, the agreement is shown and must
    # be accepted before the app proceeds. Bumping EULA_VERSION re-prompts
    # everyone (use when the terms materially change).
    "eula/accepted_version": "",

    # Autosave behavior. The "enabled" flag is meaningful only once a
    # save path has been set; until then, autosave is silent regardless.
    "autosave/enabled":          True,
    "autosave/interval_seconds": 30,    # 0 means "do not save on time"
    "autosave/interval_actions": 20,    # 0 means "do not save on actions"

    # Tag entry format: "underscores" (default) or "spaces". Controls the
    # format tags are WRITTEN in when entered or autocompleted — e.g.
    # inserting "long_hair" vs "long hair". Matching/lookup works in BOTH
    # formats regardless (core/tag_database._normalize folds spaces), so
    # this affects inserted OUTPUT only, never matching. Underscores keeps
    # the historical behavior; spaces suits space-separated datasets.
    "tags/entry_format": "underscores",

    # Caption token limit (CLIP content tokens). The last value that still
    # trains cleanly: a caption AT the limit fits, strictly OVER it spills
    # past what most trainers keep. Default 225 = three full 75-token
    # chunks (3 x 75), the common practical ceiling. Used by the file-state
    # token badge (the red "over limit" line) and the "over token limit"
    # queue filter. Allowed values are offered in Preferences.
    "tokens/limit": 225,
    # Which tokenizer the count uses: SDXL (CLIP) by default, or a Flux
    # target (T5 / Qwen3 / Mistral). See core.multi_tokenizer. MUST be in
    # DEFAULTS so _get round-trips the stored key rather than returning
    # None (which would make the getter always report the fallback).
    "tokens/target": "sdxl",

    # Keyboard shortcuts. Stored as Qt key-sequence strings — what
    # QKeySequence.toString() produces. Empty string means "no binding."
    "shortcuts/yes":          "Y",
    "shortcuts/no":           "N",
    "shortcuts/skip_image":   "Space",
    "shortcuts/skip_tag":     "",        # button-only by default
    # Queue wheel-navigation modifier: hold this key and scroll the
    # mouse wheel over the queue list to step the selection (and the
    # walk) image by image. Shift by default — Ctrl+wheel is already
    # image zoom. "Disabled" turns the feature off.
    "shortcuts/wheel_nav_modifier": "Shift",
    "shortcuts/back":         "Backspace",
    "shortcuts/nav_prev":     "Left",
    "shortcuts/nav_next":     "Right",
    "shortcuts/close_zoom":   "Escape",
    "shortcuts/undo":         "Ctrl+Z",
    "shortcuts/save_session": "Ctrl+S",

    # UI defaults applied to a fresh session.
    "defaults/sort_mode":    "alpha_asc",   # matches SortMode enum values
    "defaults/filter_mode":  "all",          # matches FilterMode enum values
    "defaults/show_orphans": False,
    # When True, walking a tag auto-confirms (Yes) images that already
    # have the tag and stops only on images that lack it — speeds up
    # audits where you mostly trust existing tags.
    "defaults/auto_yes":     False,
    # What to do when every image for a tag has been decided:
    #   "stop"    -> end the walk and show a completion message (default,
    #                safer: prevents accidentally tagging the next tag's
    #                images without noticing the transition)
    #   "advance" -> immediately jump to the next pending tag
    "defaults/tag_complete_behavior": "stop",
    # Co-occurrence hints: tags appearing on more than this percentage
    # of all images are considered "too common to be meaningful" and
    # excluded from the hints list. Pairs with the lift-based ranking
    # to suppress ubiquitous tags like "1girl" or "sweat" that would
    # otherwise dominate every suggestion. 0..100; 50 means "if a tag
    # is on half or more of the dataset, don't suggest it."
    "defaults/cooccur_too_common_pct": 50,

    # Source for the file-state co-occurrence hints. "danbooru" (default)
    # uses the bundled official Danbooru co-occurrence lookup (authoritative,
    # Ochiai-ranked); "dataset" uses co-occurrence computed from the user's
    # own loaded images (checks internal consistency of their own tagging).
    "defaults/cooccur_source": "danbooru",

    # Master on/off for the file-state co-occurrence hints. True (default)
    # shows the "Often appears with…" panel; False hides it entirely,
    # independent of the source/threshold above. For users who find the
    # hints distracting or who tag without that assist.
    "defaults/show_cooccur_hints": True,

    # Image grouping (Feature B): the dimension of the center-panel
    # thumbnail preview grid shown when a group header is selected.
    # Stored as the column count: 2 (=2x2, default — largest, cleanest
    # square tiles), 3 (=3x3), 4 (=4x4), or 5 (=5x5). Capped at 5;
    # larger grids make thumbnails too small and slow to load.
    "defaults/group_grid_cols": 2,

    # Appearance: theme name and swirl visualization preferences.
    # Theme is restart-required (the stylesheet bakes in at startup).
    # Swirl visibility/scheme take effect on the next state change
    # since the widget is light enough to redraw on demand.
    #
    # Valid theme names are listed in config.theme.THEME_NAMES; if a
    # stale value points at a removed theme, initialize_theme falls
    # back to Dark.
    "appearance/theme": "Dark",
    # Swirl widget: a 200x200 convergence visualization next to the
    # image area on the main task page. Each of 100 plus-shaped dots
    # represents 1% of the current tag's queue; dots accrete into a
    # spiral as the user makes decisions (Yes/No only — skip-image
    # does NOT count, per Pass C design).
    "appearance/show_swirl":   True,

    # Audit: which Danbooru CSV snapshot the "Audit against Danbooru" tool
    # checks captions against. Restart-required (the tag database loads at
    # startup). Value is a preset key from tag_database.CSV_PRESETS
    # ("current"/"mid2024"/"pony"/"ancient") or an absolute path to a custom
    # CSV. Default is the mid-2024 old-rules snapshot, the safest match
    # for the common SDXL/Illustrious/Pony finetuning case.
    "audit/tag_database":      "mid2024",

    # Paths the file dialogs default to. Stored as plain strings;
    # empty string means "use OS default starting location."
    "paths/last_directory":     "",
    "paths/multi_load_folders": "",
    "paths/multi_load_lists":   "",
    "danbooru/lookups_enabled": "0",
    "danbooru/show_images":     "0",
    "danbooru/ref_always_on_top": "1",
    "danbooru/browser_on_top":  "1",
    "editor/fullscreen":        "1",
    "danbooru/browser_cols":    "5",
    "danbooru/export_naming":   "time_id",
    "danbooru/image_cache_mb":  "200",
    "danbooru/discover_scope":  "general",
    "danbooru/discover_min":    "500",
    "danbooru/discover_norepeat": "1",
    "danbooru/discover_skip_owned": "0",
    "danbooru/reveal_by_default": "0",
    "danbooru/export_dir":      "",
    "danbooru/export_original": "0",
    "danbooru/block_custom":    _DEFAULT_BLACKLIST,
    "paths/last_multi_list":    "",
    "paths/last_session_save":  "",
}


# Set of shortcut keys (lookup table for validation and iteration).
SHORTCUT_KEYS: tuple[str, ...] = tuple(
    k for k in DEFAULTS
    if k.startswith("shortcuts/")
    # The wheel-nav modifier is a choice ("Shift"/"Ctrl"/…), not a key
    # sequence — keep it out of the QKeySequence machinery (dialog rows,
    # conflict validation, registration).
    and k != "shortcuts/wheel_nav_modifier"
)


# Human-readable labels for the Settings dialog. Order here defines
# the order they appear in the dialog.
SHORTCUT_LABELS: dict[str, str] = {
    "shortcuts/yes":          "Yes",
    "shortcuts/no":           "No",
    "shortcuts/skip_image":   "Skip image",
    "shortcuts/skip_tag":     "Skip current tag",
    "shortcuts/back":         "Back / Undo",
    "shortcuts/nav_prev":     "Navigate previous (no commit)",
    "shortcuts/nav_next":     "Navigate next (no commit)",
    "shortcuts/close_zoom":   "Close zoom overlay",
    "shortcuts/undo":         "Undo (menu)",
    "shortcuts/save_session": "Save session",
}


# ---------------------------------------------------------------------------
# Settings class
# ---------------------------------------------------------------------------


class Settings:
    """Typed wrapper around QSettings.

    Construct once at app startup (in main.py) and pass the instance
    to widgets that need persistent prefs. All accessors are cheap;
    property reads return cached values, writes hit QSettings
    immediately (and are batched to disk by Qt on sync/destruction).
    """

    ORGANIZATION = "TagWalker"
    APPLICATION = "TagWalker"

    def __init__(self, ini_path: Optional[Path] = None) -> None:
        """Create or open the settings store.

        Parameters
        ----------
        ini_path : optional
            If provided, settings are read/written to this exact file.
            Used by tests to isolate from real user prefs. In normal
            production, leave None and QSettings will pick the
            standard per-user location (%APPDATA%\\TagWalker\\... on
            Windows).
        """
        if ini_path is None:
            # Honor TAGWALKER_CONFIG_DIR (tests + portable installs).
            # Field defect this closes: the whole regression suite
            # constructed bare Settings() believing this env var
            # isolated it — it never did, so test runs were writing
            # into the REAL per-user preference store (and would have
            # done so on a user's machine too). With this, every
            # existing test becomes hermetic with no test edits, and
            # cache_dir() isolation becomes true as documented.
            import os as _os
            env_dir = _os.environ.get("TAGWALKER_CONFIG_DIR")
            if env_dir:
                from pathlib import Path as _Path
                ini_path = _Path(env_dir) / "tagwalker_settings.ini"
        if ini_path is not None:
            self._qs = QSettings(
                str(ini_path),
                QSettings.Format.IniFormat,
            )
        else:
            self._qs = QSettings(
                QSettings.Format.IniFormat,
                QSettings.Scope.UserScope,
                self.ORGANIZATION,
                self.APPLICATION,
            )

    # ------------------------------------------------------------------
    # Generic typed accessor (used internally by every property)
    # ------------------------------------------------------------------

    def _get(self, key: str) -> Any:
        """Return the value stored at `key`, coerced to the type of
        the default.

        QSettings returns strings for most stored values (especially
        with IniFormat). For booleans we'd otherwise see "true" /
        "false" instead of True / False; for ints, "30" instead of 30.
        This method centralizes the coercion so callers don't have
        to think about it.

        If the key has no entry in DEFAULTS the call returns None.
        Callers should treat any new setting as a programming error
        if it isn't in DEFAULTS.
        """
        if key not in DEFAULTS:
            return None
        default = DEFAULTS[key]
        raw = self._qs.value(key, default)

        if isinstance(default, bool):
            # bool first because bool IS-A int in Python; this branch
            # must come before the int check to avoid mis-routing.
            if isinstance(raw, str):
                return raw.strip().lower() in ("true", "1", "yes", "on")
            return bool(raw)
        if isinstance(default, int):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default
        if isinstance(default, str):
            return str(raw) if raw is not None else default
        return raw

    def _set(self, key: str, value: Any) -> None:
        """Write a value. No type checking — relies on the caller
        having gone through a typed property setter.
        """
        self._qs.setValue(key, value)

    def sync(self) -> None:
        """Force pending writes to disk.

        Call after a batch of changes (e.g. after the Settings dialog
        is accepted) and on app shutdown. Not strictly required —
        QSettings auto-syncs on destruction — but explicit sync
        guarantees the file is on disk before the next user action.
        """
        self._qs.sync()

    # ------------------------------------------------------------------
    # Autosave
    # ------------------------------------------------------------------

    @property
    def autosave_enabled(self) -> bool:
        return self._get("autosave/enabled")

    @autosave_enabled.setter
    def autosave_enabled(self, value: bool) -> None:
        self._set("autosave/enabled", bool(value))

    @property
    def runtime_warning_acknowledged_pair(self) -> str:
        """The Python+PySide6 pair the user opted out of warnings for
        (see DEFAULTS entry). Empty string means never acknowledged."""
        return self._get("runtime/acknowledged_pair")

    @runtime_warning_acknowledged_pair.setter
    def runtime_warning_acknowledged_pair(self, value: str) -> None:
        self._set("runtime/acknowledged_pair", str(value))

    @property
    def eula_accepted_version(self) -> str:
        """Version string of the EULA the user last accepted (see
        DEFAULTS entry). Empty string means never accepted."""
        return self._get("eula/accepted_version")

    @eula_accepted_version.setter
    def eula_accepted_version(self, value: str) -> None:
        self._set("eula/accepted_version", str(value))

    @property
    def runtime_warning_snooze_pair(self) -> str:
        """The Python+PySide6 pair a time-boxed snooze applies to."""
        return self._get("runtime/snooze_pair")

    @runtime_warning_snooze_pair.setter
    def runtime_warning_snooze_pair(self, value: str) -> None:
        self._set("runtime/snooze_pair", str(value))

    @property
    def runtime_warning_snooze_until(self) -> float:
        """Epoch seconds until which the warning is snoozed (see
        runtime_warning_snooze_pair; both must match/hold)."""
        return self._get("runtime/snooze_until")

    @runtime_warning_snooze_until.setter
    def runtime_warning_snooze_until(self, value: float) -> None:
        self._set("runtime/snooze_until", float(value))

    _WHEEL_NAV_CHOICES = ("Shift", "Ctrl", "Alt", "Disabled")

    @property
    def wheel_nav_modifier(self) -> str:
        """Modifier for wheel navigation over the queue (see DEFAULTS).
        Unknown stored values fall back to the default rather than
        silently disabling the feature."""
        v = self._get("shortcuts/wheel_nav_modifier")
        return v if v in self._WHEEL_NAV_CHOICES else "Shift"

    @wheel_nav_modifier.setter
    def wheel_nav_modifier(self, value: str) -> None:
        v = str(value)
        if v not in self._WHEEL_NAV_CHOICES:
            v = "Shift"
        self._set("shortcuts/wheel_nav_modifier", v)

    @property
    def autosave_interval_seconds(self) -> int:
        return self._get("autosave/interval_seconds")

    @autosave_interval_seconds.setter
    def autosave_interval_seconds(self, value: int) -> None:
        # Negative values are nonsensical; clamp to 0 (which disables
        # time-based saves while preserving action-based ones).
        self._set("autosave/interval_seconds", max(0, int(value)))

    @property
    def autosave_interval_actions(self) -> int:
        return self._get("autosave/interval_actions")

    @autosave_interval_actions.setter
    def autosave_interval_actions(self, value: int) -> None:
        self._set("autosave/interval_actions", max(0, int(value)))

    @property
    def tag_entry_format(self) -> str:
        """Tag entry format: "underscores" or "spaces" (see DEFAULTS).
        Any unrecognized stored value falls back to "underscores" so a
        corrupt/old value never breaks tag entry."""
        v = self._get("tags/entry_format")
        return v if v in ("underscores", "spaces") else "underscores"

    @tag_entry_format.setter
    def tag_entry_format(self, value: str) -> None:
        # Only the two known values are accepted; anything else is
        # coerced to the safe default rather than stored verbatim.
        v = value if value in ("underscores", "spaces") else "underscores"
        self._set("tags/entry_format", v)

    @property
    def tags_use_spaces(self) -> bool:
        """Convenience: True when tag entry should insert space-formatted
        tags. Derived from tag_entry_format."""
        return self.tag_entry_format == "spaces"

    # Caption token counting is now tokenizer-specific: a (tokenizer
    # target, token limit) pair rather than a bare integer, because
    # different trainer families use different tokenizers (SDXL=CLIP,
    # Flux.1=T5, Flux.2 Klein=Qwen3, Flux.2 Dev=Mistral) and the same
    # caption counts differently under each. The target's allowed limits
    # and default live in core.multi_tokenizer; this class stores the
    # user's chosen pair and validates it against that registry.
    #
    # The global default is SDXL / 225 (three full 75-token CLIP chunks).

    @property
    def tokenizer_target(self) -> str:
        """Chosen tokenizer target key (see core.multi_tokenizer). An
        unknown/corrupt stored value falls back to the SDXL default so a
        bad value never yields a nonsensical mode."""
        from core import multi_tokenizer as mt
        v = self._get("tokens/target")
        if isinstance(v, str) and v in {t.key for t in mt.all_targets()}:
            return v
        return mt.default_target_key()

    @tokenizer_target.setter
    def tokenizer_target(self, value: str) -> None:
        from core import multi_tokenizer as mt
        valid = {t.key for t in mt.all_targets()}
        key = value if value in valid else mt.default_target_key()
        self._set("tokens/target", key)
        # Snap the stored limit to one valid for the new target (the old
        # limit may not apply — e.g. switching SDXL 225 -> Flux, or back).
        cur = self._get("tokens/limit")
        try:
            cur = int(cur)
        except (TypeError, ValueError):
            cur = mt.get_target(key).default_limit
        self._set("tokens/limit", mt.coerce_limit(key, cur))

    @property
    def token_limit(self) -> int:
        """Caption token limit for the current tokenizer target. Validated
        against that target's allowed limits; an out-of-range or corrupt
        stored value falls back to the target's default. Legacy configs
        that stored only a bare limit (75/150/225/256/512) still resolve:
        the target defaults to SDXL, and a legacy 256/512 (Flux-ish) value
        is coerced to SDXL's range, i.e. the 225 default."""
        from core import multi_tokenizer as mt
        target = self.tokenizer_target
        v = self._get("tokens/limit")
        try:
            v = int(v)
        except (TypeError, ValueError):
            return mt.get_target(target).default_limit
        return mt.coerce_limit(target, v)

    @token_limit.setter
    def token_limit(self, value: int) -> None:
        from core import multi_tokenizer as mt
        target = self.tokenizer_target
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = mt.get_target(target).default_limit
        self._set("tokens/limit", mt.coerce_limit(target, v))

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def get_shortcut(self, name: str) -> str:
        """Return the shortcut string for the given setting key.

        Accepts either "shortcuts/yes" or the suffix "yes" — the
        latter is more convenient for callers that don't want to
        repeat the prefix.

        Returns the user-stored value if any, otherwise the default,
        otherwise an empty string.
        """
        key = name if name.startswith("shortcuts/") else f"shortcuts/{name}"
        return self._get(key) or ""

    def set_shortcut(self, name: str, value: str) -> bool:
        """Update a shortcut binding.

        Returns True if the value was stored, False if it was rejected
        as invalid. Empty string is accepted (means "no binding").

        Validation: anything QKeySequence accepts is valid. This
        rejects garbage strings like "asdf!@#" silently — the
        Settings dialog should re-display the previous value if False
        is returned.

        Note on validation strategy: QKeySequence.isEmpty() is too
        permissive — it returns False for inputs that parsed to
        nothing useful. We instead check whether the canonical
        round-tripped form (toString) is non-empty. If parsing
        garbage like "@@@invalid" yields an empty canonical string,
        we reject.
        """
        key = name if name.startswith("shortcuts/") else f"shortcuts/{name}"
        if key not in DEFAULTS:
            return False
        cleaned = value.strip()
        if cleaned:
            seq = QKeySequence.fromString(cleaned)
            canonical = seq.toString()
            if not canonical:
                return False
            cleaned = canonical
        self._set(key, cleaned)
        return True

    def reset_shortcut(self, name: str) -> None:
        """Restore a single shortcut to its default."""
        key = name if name.startswith("shortcuts/") else f"shortcuts/{name}"
        if key in DEFAULTS:
            self._set(key, DEFAULTS[key])

    def all_shortcuts(self) -> dict[str, str]:
        """Return every shortcut as {key: current_value}.

        Used by the Settings dialog to populate its form, and by
        the main window to wire up QShortcut objects.
        """
        return {key: self._get(key) or "" for key in SHORTCUT_KEYS}

    # ------------------------------------------------------------------
    # UI defaults
    # ------------------------------------------------------------------

    @property
    def default_sort_mode(self) -> str:
        return self._get("defaults/sort_mode")

    @default_sort_mode.setter
    def default_sort_mode(self, value: str) -> None:
        self._set("defaults/sort_mode", str(value))

    @property
    def default_filter_mode(self) -> str:
        return self._get("defaults/filter_mode")

    @default_filter_mode.setter
    def default_filter_mode(self, value: str) -> None:
        self._set("defaults/filter_mode", str(value))

    @property
    def default_show_orphans(self) -> bool:
        return self._get("defaults/show_orphans")

    @default_show_orphans.setter
    def default_show_orphans(self, value: bool) -> None:
        self._set("defaults/show_orphans", bool(value))

    @property
    def default_auto_yes(self) -> bool:
        return self._get("defaults/auto_yes")

    @default_auto_yes.setter
    def default_auto_yes(self, value: bool) -> None:
        self._set("defaults/auto_yes", bool(value))

    @property
    def default_tag_complete_behavior(self) -> str:
        return self._get("defaults/tag_complete_behavior")

    @default_tag_complete_behavior.setter
    def default_tag_complete_behavior(self, value: str) -> None:
        self._set("defaults/tag_complete_behavior", str(value))

    @property
    def default_cooccur_too_common_pct(self) -> int:
        return int(self._get("defaults/cooccur_too_common_pct"))

    @default_cooccur_too_common_pct.setter
    def default_cooccur_too_common_pct(self, value: int) -> None:
        # 0..100 inclusive. 0 = filter nothing (all candidates eligible);
        # 100 = filter all (no candidates ever pass) which would make
        # the feature useless but isn't dangerous.
        v = max(0, min(100, int(value)))
        self._set("defaults/cooccur_too_common_pct", v)

    @property
    def default_cooccur_source(self) -> str:
        v = str(self._get("defaults/cooccur_source") or "danbooru").strip()
        return v if v in ("danbooru", "dataset") else "danbooru"

    @default_cooccur_source.setter
    def default_cooccur_source(self, value: str) -> None:
        v = str(value).strip()
        self._set("defaults/cooccur_source",
                  v if v in ("danbooru", "dataset") else "danbooru")

    @property
    def default_show_cooccur_hints(self) -> bool:
        return self._get("defaults/show_cooccur_hints")

    @default_show_cooccur_hints.setter
    def default_show_cooccur_hints(self, value: bool) -> None:
        self._set("defaults/show_cooccur_hints", bool(value))

    @property
    def default_group_grid_cols(self) -> int:
        return int(self._get("defaults/group_grid_cols"))

    @default_group_grid_cols.setter
    def default_group_grid_cols(self, value: int) -> None:
        # Bounded 2..5 (2x2 / 3x3 / 4x4 / 5x5). Anything outside snaps in.
        v = max(2, min(5, int(value)))
        self._set("defaults/group_grid_cols", v)

    # ------------------------------------------------------------------
    # Appearance (Pass C)
    # ------------------------------------------------------------------

    @property
    def theme(self) -> str:
        """Active theme name. See config.theme.THEME_NAMES for valid
        values. Restart-required: the setter persists the new value
        but the running app keeps using the theme it was started with.
        """
        return self._get("appearance/theme")

    @theme.setter
    def theme(self, value: str) -> None:
        # Validate against the known theme list. Unknown values are
        # silently coerced to "Dark" so a typo can't break the dialog.
        # (initialize_theme also has a defensive fallback, but
        # validating here keeps the stored value clean.)
        from config.theme import THEME_NAMES
        s = str(value)
        if s not in THEME_NAMES:
            s = "Dark"
        self._set("appearance/theme", s)

    @property
    def tag_database_choice(self) -> str:
        """Which Danbooru CSV the audit tool checks against.

        A preset key ("current"/"mid2024"/"pony"/"ancient") or an absolute path
        to a custom CSV. Restart-required: the tag database loads at
        startup, so the setter persists the choice but the running app
        keeps using the snapshot it started with.
        """
        return self._get("audit/tag_database")

    @tag_database_choice.setter
    def tag_database_choice(self, value: str) -> None:
        self._set("audit/tag_database", str(value))

    @property
    def show_swirl(self) -> bool:
        return bool(int(self._get("appearance/show_swirl")))

    @show_swirl.setter
    def show_swirl(self, value: bool) -> None:
        self._set("appearance/show_swirl", 1 if value else 0)

    # ------------------------------------------------------------------
    # Last-used paths
    # ------------------------------------------------------------------

    @property
    def last_directory(self) -> str:
        return self._get("paths/last_directory")

    @last_directory.setter
    def last_directory(self, value: str) -> None:
        self._set("paths/last_directory", str(value))

    @property
    def multi_load_folders(self) -> list[str]:
        """Remembered multi-folder load set (File → Open Multiple
        Folders, "remember" checkbox). Stored newline-joined — a
        character that cannot appear in a path on any platform. Empty
        list when nothing is remembered. May contain folders that no
        longer exist; the dialog marks those "(missing)" instead of
        silently dropping them (unplugged drives come back)."""
        raw = self._get("paths/multi_load_folders")
        return [p for p in str(raw).split("\n") if p.strip()]

    @multi_load_folders.setter
    def multi_load_folders(self, folders: list[str]) -> None:
        self._set("paths/multi_load_folders",
                  "\n".join(str(f) for f in folders))

    @property
    def multi_load_lists(self) -> dict[str, list[str]]:
        """Named multi-folder load lists (File \u2192 Open Multiple
        Folders \u2014 the "playlist" model). JSON-encoded in one
        key; insertion order preserved. The legacy single
        multi_load_folders set is migrated into a first list by the
        dialog on first open."""
        import json
        raw = str(self._get("paths/multi_load_lists") or "")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        out: dict[str, list[str]] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, list):
                    out[k] = [str(p) for p in v if str(p).strip()]
        return out

    @multi_load_lists.setter
    def multi_load_lists(self, lists: dict[str, list[str]]) -> None:
        import json
        clean = {str(k): [str(p) for p in v]
                 for k, v in lists.items()}
        self._set("paths/multi_load_lists",
                  json.dumps(clean, ensure_ascii=False))

    @property
    def last_multi_list(self) -> str:
        """Name of the load list used most recently (preselected on
        dialog open, so the routine restart flow stays two clicks)."""
        return str(self._get("paths/last_multi_list") or "")

    @last_multi_list.setter
    def last_multi_list(self, name: str) -> None:
        self._set("paths/last_multi_list", str(name))

    @property
    def last_session_save(self) -> str:
        """Last path used for File → Save Session As..., remembered
        so the dialog defaults to the same folder next time.

        This is NOT used for auto-loading sessions; it's just the
        dialog's starting location.
        """
        return self._get("paths/last_session_save")

    @last_session_save.setter
    def last_session_save(self, value: str) -> None:
        self._set("paths/last_session_save", str(value))

    # ------------------------------------------------------------------
    # Window geometry / state
    # ------------------------------------------------------------------
    #
    # Qt provides QMainWindow.saveGeometry() and saveState() which
    # return QByteArray blobs that QMainWindow.restoreGeometry() /
    # restoreState() can apply. We just store and retrieve these as
    # opaque bytes; this is the standard pattern.
    #
    # An empty QByteArray means "no saved geometry — let Qt pick a
    # sensible default size and position."

    @property
    def tag_reference_always_on_top(self) -> bool:
        """Keep the Tag Reference window above the main window. The
        window is parentless by design (so it can live on a second
        monitor), which means it otherwise falls behind whenever the
        main window is used."""
        return bool(int(self._get("danbooru/ref_always_on_top")))

    @tag_reference_always_on_top.setter
    def tag_reference_always_on_top(self, value: bool) -> None:
        self._set("danbooru/ref_always_on_top", "1" if value else "0")

    @property
    def danbooru_image_cache_mb(self) -> int:
        """Disk limit for cached images, in megabytes. Zero means do
        not write images to disk at all."""
        from core import danbooru_api

        try:
            value = int(self._get("danbooru/image_cache_mb"))
        except (TypeError, ValueError):
            return danbooru_api.DEFAULT_DISK_CACHE_MB
        allowed = {mb for mb, _l, _d in danbooru_api.DISK_CACHE_CHOICES}
        return (value if value in allowed
                else danbooru_api.DEFAULT_DISK_CACHE_MB)

    @danbooru_image_cache_mb.setter
    def danbooru_image_cache_mb(self, value: int) -> None:
        self._set("danbooru/image_cache_mb", str(int(value)))

    @property
    def danbooru_export_naming(self) -> str:
        """How a downloaded post is named. See core/export_naming."""
        from core import export_naming

        value = self._get("danbooru/export_naming") or ""
        return (value if value in
                {k for k, _l, _e in export_naming.MODES}
                else export_naming.DEFAULT_MODE)

    @danbooru_export_naming.setter
    def danbooru_export_naming(self, value: str) -> None:
        self._set("danbooru/export_naming", str(value))

    @property
    def post_browser_columns(self) -> int:
        """Thumbnails per row in the post browser. Five fits the
        default window without scrolling; the page size stays 20."""
        try:
            value = int(self._get("danbooru/browser_cols"))
        except (TypeError, ValueError):
            return 5
        return value if value in (4, 5, 6, 8, 10) else 5

    @post_browser_columns.setter
    def post_browser_columns(self, value: int) -> None:
        self._set("danbooru/browser_cols", str(int(value)))

    @property
    def discovery_scope(self) -> str:
        """"general" or "all". General-only by default: artist and
        character names are not vocabulary you caption WITH."""
        value = self._get("danbooru/discover_scope") or "general"
        return value if value in ("general", "all") else "general"

    @discovery_scope.setter
    def discovery_scope(self, value: str) -> None:
        self._set("danbooru/discover_scope",
                  value if value in ("general", "all") else "general")

    @property
    def discovery_min_count(self) -> int:
        """Posts a tag needs before Discover will offer it. The knob
        that decides whether the button is useful or noise \u2014 the
        long tail of a booru is mostly one-off names."""
        try:
            return max(0, int(self._get("danbooru/discover_min")))
        except (TypeError, ValueError):
            return 500

    @discovery_min_count.setter
    def discovery_min_count(self, value: int) -> None:
        self._set("danbooru/discover_min", str(max(0, int(value))))

    @property
    def discovery_no_repeat(self) -> bool:
        """Do not offer the same tag twice in one session."""
        return bool(int(self._get("danbooru/discover_norepeat")))

    @discovery_no_repeat.setter
    def discovery_no_repeat(self, value: bool) -> None:
        self._set("danbooru/discover_norepeat", "1" if value else "0")

    @property
    def discovery_skip_owned(self) -> bool:
        """Skip tags already in the loaded dataset, turning browsing
        into gap-finding."""
        return bool(int(self._get("danbooru/discover_skip_owned")))

    @discovery_skip_owned.setter
    def discovery_skip_owned(self, value: bool) -> None:
        self._set("danbooru/discover_skip_owned",
                  "1" if value else "0")

    @property
    def post_browser_always_on_top(self) -> bool:
        """Pinned by default, same reasoning as the Tag Referencer:
        the window is parentless and otherwise falls behind the main
        window the moment you touch it."""
        return bool(int(self._get("danbooru/browser_on_top")))

    @post_browser_always_on_top.setter
    def post_browser_always_on_top(self, value: bool) -> None:
        self._set("danbooru/browser_on_top", "1" if value else "0")

    @property
    def image_editor_fullscreen(self) -> bool:
        """Open the image editor maximised. On by default: the editor
        is a place you settle into for a whole batch, not something
        you flick back and forth from, so a full window is the
        cleaner starting view."""
        return bool(int(self._get("editor/fullscreen")))

    @image_editor_fullscreen.setter
    def image_editor_fullscreen(self, value: bool) -> None:
        self._set("editor/fullscreen", "1" if value else "0")

    @property
    def danbooru_block_custom(self) -> str:
        """The blacklist, Danbooru syntax, one rule per line. Ships
        pre-filled; "Reset to defaults" restores the shipped text."""
        return self._get("danbooru/block_custom")

    @danbooru_block_custom.setter
    def danbooru_block_custom(self, value: str) -> None:
        self._set("danbooru/block_custom", value or "")

    @property
    def danbooru_export_original(self) -> bool:
        """Export the untouched upload rather than the shrunk sample
        Danbooru serves for quick viewing. Off by default because the
        originals are much larger; on is what you want if the export
        is destined for training."""
        return bool(int(self._get("danbooru/export_original")))

    @danbooru_export_original.setter
    def danbooru_export_original(self, value: bool) -> None:
        self._set("danbooru/export_original", "1" if value else "0")

    @property
    def danbooru_export_dir(self) -> str:
        """Where "Export image + tags" writes. Empty until the user
        picks one — the app never guesses a location to write into."""
        return self._get("danbooru/export_dir")

    @danbooru_export_dir.setter
    def danbooru_export_dir(self, value: str) -> None:
        self._set("danbooru/export_dir", value or "")

    @property
    def danbooru_reveal_by_default(self) -> bool:
        """Show example images immediately instead of hiding them
        until hover. Off by default: the safe-by-surprise behaviour
        stays the default, but a user working alone can opt into the
        faster view."""
        return bool(int(self._get("danbooru/reveal_by_default")))

    @danbooru_reveal_by_default.setter
    def danbooru_reveal_by_default(self, value: bool) -> None:
        self._set("danbooru/reveal_by_default", "1" if value else "0")

    @property
    def danbooru_lookups_enabled(self) -> bool:
        """Master opt-in for the Tag Reference's Danbooru lookups
        (description + curated examples). Default OFF — the app makes
        no network requests until the user enables this. Only the tag
        name is ever sent."""
        return bool(int(self._get("danbooru/lookups_enabled")))

    @danbooru_lookups_enabled.setter
    def danbooru_lookups_enabled(self, value: bool) -> None:
        self._set("danbooru/lookups_enabled", "1" if value else "0")

    @property
    def danbooru_show_images(self) -> bool:
        """Sub-option: show the staff-curated example images. Default
        OFF; hidden-until-hover in the window regardless."""
        return bool(int(self._get("danbooru/show_images")))

    @danbooru_show_images.setter
    def danbooru_show_images(self, value: bool) -> None:
        self._set("danbooru/show_images", "1" if value else "0")

    @property
    def bookmarks_path(self) -> "Path":
        """Beside the settings file, not inside a cache: if the
        program stops opening, the bookmarks are still a plain text
        file the user can find and read."""
        from pathlib import Path
        return Path(self._qs.fileName()).parent / "bookmarks.json"

    def cache_dir(self, name: str) -> "Path":
        """A named cache directory beside the settings file (so tests
        isolated via ini_path/TAGWALKER_CONFIG_DIR get isolated
        caches too)."""
        from pathlib import Path
        return Path(self._qs.fileName()).parent / "cache" / name

    @property
    def tag_reference_geometry(self) -> QByteArray:
        """Size/position of the Tag Reference window (independent
        top-level; spec: remembers geometry)."""
        value = self._qs.value("window/tag_reference_geometry",
                               QByteArray())
        return value if isinstance(value, QByteArray) else QByteArray()

    @tag_reference_geometry.setter
    def tag_reference_geometry(self, value: QByteArray) -> None:
        self._qs.setValue("window/tag_reference_geometry", value)

    @property
    def window_geometry(self) -> QByteArray:
        value = self._qs.value("window/geometry", QByteArray())
        return value if isinstance(value, QByteArray) else QByteArray()

    @window_geometry.setter
    def window_geometry(self, value: QByteArray) -> None:
        self._qs.setValue("window/geometry", value)

    @property
    def window_state(self) -> QByteArray:
        value = self._qs.value("window/state", QByteArray())
        return value if isinstance(value, QByteArray) else QByteArray()

    @window_state.setter
    def window_state(self, value: QByteArray) -> None:
        self._qs.setValue("window/state", value)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def reset_all_to_defaults(self) -> None:
        """Clear every TagWalker setting back to its DEFAULTS value.

        Used by the Settings dialog's "Reset to defaults" button.
        Window geometry is NOT reset — clobbering the window layout
        would be more surprising than helpful. If the user truly
        wants a clean slate they can delete the INI file.
        """
        for key, default in DEFAULTS.items():
            self._set(key, default)
        self.sync()

    def file_path(self) -> str:
        """Return the path to the INI file in use, mostly for use in
        the Settings dialog (showing the user where their prefs live).
        """
        return self._qs.fileName()
