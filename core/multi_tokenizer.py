"""
core/multi_tokenizer.py

Multi-tokenizer token counting for different trainer targets.

Background: caption token *counts* are tokenizer-specific. SDXL uses CLIP
BPE (~49k vocab, counted by core/clip_token_counter.py). Flux models use
DIFFERENT text encoders with DIFFERENT tokenizers, so the same caption
tokenizes to a different number of tokens:

  - SDXL              -> CLIP BPE            (existing counter; 75/150/225)
  - Flux.1            -> T5-XXL SentencePiece (spiece.model; 512)
  - Flux.2 [klein]    -> Qwen3 byte-level BPE (tokenizer.json; 512)
  - Flux.2 [dev]      -> Mistral-Small-3.2    (tokenizer.json; 512)

Counting a Flux budget with the CLIP tokenizer is measuring with the wrong
ruler — a wrong count is worse than none (it gives false confidence when
pruning). This module routes each target to the correct tokenizer.

Design constraints (deliberate):
  * LAZY. Nothing here imports a tokenizer library or reads a tokenizer
    file at program startup. The heavy `tokenizers` / `sentencepiece`
    imports happen INSIDE the load path, the first time a Flux target is
    actually counted. Program launch cost is unchanged; users who never
    open a Flux mode pay nothing. (The tokenizer DATA files are bundled
    but inert on disk until loaded.)
  * CACHED. A loaded tokenizer object is memoized; subsequent counts for
    the same target are instant.
  * GRACEFUL. A missing library or missing/ío-failed resource file yields
    a Tokenizer that reports unavailable (ensure_loaded() -> False, with
    an error string) rather than crashing. Callers treat "unavailable"
    the same way the CLIP counter's RuntimeError is treated.

The CLIP target delegates to the existing, exact clip_token_counter so the
SDXL path is byte-for-byte unchanged. Only the Flux targets are new.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Target registry
# ---------------------------------------------------------------------------

# Stable string keys persisted in settings. Do NOT rename without a
# migration — they are stored in the user's config.
TARGET_SDXL = "sdxl"          # CLIP BPE
TARGET_FLUX1 = "flux1"        # T5-XXL SentencePiece
TARGET_FLUX2_KLEIN = "flux2_klein"   # Qwen3 byte-level BPE
TARGET_FLUX2_DEV = "flux2_dev"       # Mistral-Small-3.2


@dataclass(frozen=True)
class TokenizerTarget:
    """Static description of one trainer target's tokenizer + limits."""
    key: str                 # stable settings key
    label: str               # human label for the UI selector
    kind: str                # 'clip' | 'sentencepiece' | 'hf_tokenizers'
    resource_subdir: str     # under resources/tokenizers/, "" for CLIP
    resource_file: str       # main tokenizer file, "" for CLIP
    limits: tuple[int, ...]  # selectable token limits for this target
    default_limit: int       # default limit for this target
    blurb: str               # short description for tooltips/help
    also_covers: tuple[str, ...] = ()  # other model families that share
    #   this exact tokenizer, so this target counts them correctly too
    #   (e.g. Krea 2 and Anima use the same Qwen3 tokenizer as Flux.2
    #   Klein). Surfaced as a hover tooltip only — never in the visible
    #   label — so the selector stays uncluttered.


# The full registry. Order is the order shown in selectors.
#
# SDXL limits are the CLIP content-token chunk boundaries (75 per chunk,
# +2 BOS/EOS handled by the trainer): 75 / 150 / 225 = one / two / three
# chunks. 225 is the global default (see default_target()/default_limit).
#
# Flux targets are all capped at 512 (FLUX MAX_LENGTH = 512 truncates
# longer prompts). 256 is offered too as a tighter self-imposed budget
# some setups prefer; 512 is the real ceiling and the per-target default.
_TARGETS: tuple[TokenizerTarget, ...] = (
    TokenizerTarget(
        key=TARGET_SDXL,
        label="SDXL (CLIP)",
        kind="clip",
        resource_subdir="",
        resource_file="",
        limits=(75, 150, 225),
        default_limit=225,
        blurb=("SDXL / SD-family. CLIP BPE content tokens, fed to the "
               "text encoder in chunks of 75 (150 / 225 = two / three "
               "chunks). 225 packs the most tags while staying clean."),
    ),
    TokenizerTarget(
        key=TARGET_FLUX1,
        label="Flux.1 (T5-XXL)",
        kind="sentencepiece",
        resource_subdir="t5xxl",
        resource_file="spiece.model",
        limits=(256, 512),
        default_limit=512,
        blurb=("Flux.1. T5-XXL SentencePiece tokens. Flux caps the "
               "sequence at 512 (longer prompts are truncated); 256 is a "
               "tighter optional budget."),
    ),
    TokenizerTarget(
        key=TARGET_FLUX2_KLEIN,
        label="Flux.2 Klein (Qwen3)",
        kind="hf_tokenizers",
        resource_subdir="qwen3",
        resource_file="tokenizer.json",
        limits=(256, 512),
        default_limit=512,
        blurb=("Flux.2 Klein (4B/8B). Qwen3 byte-level BPE tokens. Also "
               "correct for Krea 2, Anima, and other Qwen3 / Qwen3-VL "
               "models, which share this exact tokenizer — select this "
               "target to count their captions. Flux caps the sequence at "
               "512; 256 is a tighter optional budget."),
        also_covers=("Krea 2", "Anima"),
    ),
    TokenizerTarget(
        key=TARGET_FLUX2_DEV,
        label="Flux.2 Dev (Mistral)",
        kind="mistral_tekken",
        resource_subdir="mistral",
        resource_file="tekken.json",
        limits=(256, 512),
        default_limit=512,
        blurb=("Flux.2 Dev. Mistral-Small-3.2 tokens, counted with "
               "Mistral's Tekken tokenizer (pure-Python, no heavy deps). Flux "
               "caps "
               "the sequence at 512; 256 is a tighter optional budget."),
    ),
)

_TARGETS_BY_KEY: dict[str, TokenizerTarget] = {t.key: t for t in _TARGETS}


def all_targets() -> tuple[TokenizerTarget, ...]:
    """Every tokenizer target, in selector order."""
    return _TARGETS


def get_target(key: str) -> TokenizerTarget:
    """Target for a settings key, falling back to SDXL for an unknown or
    corrupt key so a bad stored value never yields a nonsensical mode."""
    return _TARGETS_BY_KEY.get(key, _TARGETS_BY_KEY[TARGET_SDXL])


def default_target_key() -> str:
    """The global default target: SDXL."""
    return TARGET_SDXL


def default_limit() -> int:
    """The global default limit: 225 (SDXL three-chunk ceiling)."""
    return 225


def is_valid_limit(target_key: str, limit: int) -> bool:
    """True if `limit` is offered for `target_key`."""
    return limit in get_target(target_key).limits


def coerce_limit(target_key: str, limit: int) -> int:
    """Return `limit` if valid for the target, else the target's default.
    Used when a stored (target, limit) pair is inconsistent — e.g. the
    target changed and the old limit no longer applies."""
    t = get_target(target_key)
    return limit if limit in t.limits else t.default_limit


# ---------------------------------------------------------------------------
# Resource path resolution (PyInstaller-aware)
# ---------------------------------------------------------------------------

def _resource_root() -> Path:
    """Base dir for bundled resources, whether running from source or from
    a PyInstaller-frozen exe (sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "resources" / "tokenizers"
    return Path(__file__).resolve().parent.parent / "resources" / "tokenizers"


def _resource_path(target: TokenizerTarget) -> Path:
    return _resource_root() / target.resource_subdir / target.resource_file


def _resolve_tekken_file(target: TokenizerTarget) -> Optional[Path]:
    """Find the Tekken tokenizer file in the target's folder, tolerant of
    version-suffixed names.

    Mistral names the file `tekken.json` by convention, but it is
    sometimes versioned (e.g. `tekken.json.v7`, `tekken.json.v11`), so an
    exact-name check is fragile — the user may download a valid file under
    a different suffix. Resolution order: the configured exact name first
    (fast path, and lets the user pin one if several are present), then any
    file matching `tekken*` in the folder, preferring the highest version.

    Version ordering is NUMERIC, not lexicographic: a plain string sort
    puts "tekken.json.v11" before "tekken.json.v7" (because '1' < '7' at
    the first differing char), which would wrongly pick the older v7. We
    extract the trailing .vN integer and sort on that; files with no
    version suffix sort as version -1 (so an explicit versioned file wins
    over a bare one only when the bare one isn't the exact configured
    name, which was already handled above).
    """
    folder = _resource_root() / target.resource_subdir
    exact = folder / target.resource_file
    if exact.is_file():
        return exact
    if not folder.is_dir():
        return None
    matches = [p for p in folder.glob("tekken*") if p.is_file()]
    if not matches:
        return None

    import re

    def _version_key(p: Path) -> tuple[int, str]:
        m = re.search(r"\.v(\d+)$", p.name)
        return (int(m.group(1)) if m else -1, p.name)

    return max(matches, key=_version_key)


# ---------------------------------------------------------------------------
# Lazy, cached tokenizer wrappers
# ---------------------------------------------------------------------------

class _BaseTok:
    """Common shape: ensure_loaded() -> bool, error property, count(str)."""

    def __init__(self, target: TokenizerTarget) -> None:
        self._target = target
        self._error: Optional[str] = None
        self._loaded = False

    @property
    def error(self) -> Optional[str]:
        return self._error

    def ensure_loaded(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def count(self, text: str) -> int:  # pragma: no cover - overridden
        raise NotImplementedError


class _ClipTok(_BaseTok):
    """Delegates to the existing exact CLIP counter (SDXL path unchanged)."""

    def ensure_loaded(self) -> bool:
        from core import clip_token_counter as ctc
        ok = ctc.ensure_loaded()
        self._loaded = ok
        self._error = None if ok else (ctc.get_error() or "CLIP vocab unavailable")
        return ok

    def count(self, text: str) -> int:
        from core import clip_token_counter as ctc
        return ctc.count_tokens(text)


class _SentencePieceTok(_BaseTok):
    """T5-XXL via the `sentencepiece` library and a bundled spiece.model.

    Counts CONTENT tokens (excludes the trailing EOS the T5 tokenizer
    appends), matching how the CLIP counter excludes BOS/EOS — the limit
    is a content-token budget in both cases.
    """

    def __init__(self, target: TokenizerTarget) -> None:
        super().__init__(target)
        self._sp = None

    def ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        path = _resource_path(self._target)
        if not path.is_file():
            self._error = f"Tokenizer file missing: {path.name}"
            return False
        try:
            import sentencepiece as spm  # lazy — only when a T5 count runs
        except Exception as exc:  # ImportError or a broken native lib
            self._error = f"sentencepiece not available ({exc})"
            return False
        try:
            self._sp = spm.SentencePieceProcessor(model_file=str(path))
        except Exception as exc:
            self._error = f"Could not load {path.name} ({exc})"
            return False
        self._loaded = True
        self._error = None
        return True

    def count(self, text: str) -> int:
        if not self.ensure_loaded():
            raise RuntimeError(self._error or "T5 tokenizer unavailable")
        # encode() returns content piece ids; SentencePieceProcessor does
        # NOT prepend/append BOS/EOS by default, so len() is the content
        # count. (If add_eos were enabled we'd subtract it here.)
        return len(self._sp.encode(text, out_type=int))


class _HFTokenizersTok(_BaseTok):
    """Qwen3 / Mistral via HuggingFace `tokenizers` (Rust) and a bundled
    tokenizer.json.

    Counts CONTENT tokens: we tokenize WITHOUT special tokens so the count
    is the caption's own tokens, not template/BOS/EOS overhead — this is
    what the user controls when editing a caption, and it matches the
    content-token convention of the other counters. (Flux's own 512 cap
    includes any template wrapping the pipeline adds; that overhead is
    fixed and not something the caption editor changes, so the field
    budget the user prunes against is the content count.)
    """

    def __init__(self, target: TokenizerTarget) -> None:
        super().__init__(target)
        self._tok = None

    def ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        path = _resource_path(self._target)
        if not path.is_file():
            self._error = f"Tokenizer file missing: {path.name}"
            return False
        try:
            from tokenizers import Tokenizer  # lazy — only when needed
        except Exception as exc:
            self._error = f"tokenizers not available ({exc})"
            return False
        try:
            self._tok = Tokenizer.from_file(str(path))
        except Exception as exc:
            self._error = f"Could not load {path.name} ({exc})"
            return False
        self._loaded = True
        self._error = None
        return True

    def count(self, text: str) -> int:
        if not self.ensure_loaded():
            raise RuntimeError(self._error or "tokenizer unavailable")
        enc = self._tok.encode(text, add_special_tokens=False)
        return len(enc.ids)


class _MistralTekkenTok(_BaseTok):
    """Mistral-Small-3.2 via a bundled tekken.json, counted by our own
    pure-Python Tekken reader (core.tekken_counter) — NOT mistral-common.

    Mistral ships its tokenizer as the Tekken format (a tiktoken-style
    byte-level BPE). Using mistral-common to read it would drag in ~80 MB
    of dependencies to use a ~4 MB core, so instead we reimplement just the
    counting path against tekken.json directly. The only added dependency
    is the small `regex` module (the Tekken pre-tokenization pattern uses
    \\p{...} Unicode classes stdlib `re` can't parse). This was validated
    to match mistral-common's counts exactly across ASCII, digits,
    underscores, kaomoji, Cyrillic, Japanese, and accented Latin.

    Counts CONTENT tokens (no BOS/EOS), matching the other counters.
    """

    def __init__(self, target: TokenizerTarget) -> None:
        super().__init__(target)
        self._counter = None

    def ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        path = _resolve_tekken_file(self._target)
        if path is None:
            self._error = (
                "Tekken tokenizer file missing: expected "
                f"{self._target.resource_file} (or any 'tekken*' file) in "
                f"resources/tokenizers/{self._target.resource_subdir}/")
            return False
        # lazy import — the Tekken reader (and its regex dependency) only
        # load when a Mistral count actually runs.
        from core.tekken_counter import TekkenCounter
        counter = TekkenCounter(path)
        if not counter.ensure_loaded():
            self._error = counter.error or "Tekken tokenizer unavailable"
            return False
        self._counter = counter
        self._loaded = True
        self._error = None
        return True

    def count(self, text: str) -> int:
        if not self.ensure_loaded():
            raise RuntimeError(self._error or "Mistral tokenizer unavailable")
        return self._counter.count(text)


def _make(target: TokenizerTarget) -> _BaseTok:
    if target.kind == "clip":
        return _ClipTok(target)
    if target.kind == "sentencepiece":
        return _SentencePieceTok(target)
    if target.kind == "hf_tokenizers":
        return _HFTokenizersTok(target)
    if target.kind == "mistral_tekken":
        return _MistralTekkenTok(target)
    raise ValueError(f"unknown tokenizer kind: {target.kind}")


# One cached wrapper per target key, created on first use.
_CACHE: dict[str, _BaseTok] = {}


def _tok_for(target_key: str) -> _BaseTok:
    t = get_target(target_key)
    inst = _CACHE.get(t.key)
    if inst is None:
        inst = _make(t)
        _CACHE[t.key] = inst
    return inst


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_loaded(target_key: str) -> bool:
    """Load the tokenizer for `target_key` (once). False on failure;
    get_error(target_key) then explains why."""
    return _tok_for(target_key).ensure_loaded()


def get_error(target_key: str) -> Optional[str]:
    """Human-readable reason the target's tokenizer is unavailable, or
    None."""
    return _tok_for(target_key).error


def count_tokens(text: str, target_key: str) -> int:
    """Content-token count of `text` under the tokenizer for `target_key`.

    Raises RuntimeError if that tokenizer is unavailable (missing library
    or file) — an approximate count from a limit checker would be a lie,
    same contract as the CLIP counter.
    """
    return _tok_for(target_key).count(text)


def available(target_key: str) -> bool:
    """Convenience: True if the target can currently count (lib + file
    present). Does the load attempt; result is cached."""
    return _tok_for(target_key).ensure_loaded()
