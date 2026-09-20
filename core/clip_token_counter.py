"""
core/clip_token_counter.py

Exact CLIP BPE token counting for caption-length checks (field
feature: keep captions under trainer chunk limits of 75 / 150 / 225
content tokens).

This is a faithful re-implementation of OpenAI CLIP's
simple_tokenizer, reduced to COUNTING (we never need token ids):
byte-level pre-encoding, the same merge table
(resources/clip_bpe_vocab_16e6.txt.gz, merges[1:48895] exactly as
CLIP slices it), and the same word-splitting pattern. Counts exclude
BOS/EOS — the 75/150/225 limits are content-token chunk sizes.

The word-split pattern uses the stdlib `re` emulation of CLIP's
unicode-category pattern ([^\\W\\d_]+ for \\p{L}+ etc.). The
equivalence was verified against the reference `regex`-based pattern
across a battery of realistic booru captions (underscores, commas,
parentheses, digits, kaomoji, unicode) before shipping — see
tests/test_token_counter.py for pinned exact values.

Differences from CLIP that cannot matter here: no ftfy text fixing
(booru captions are already clean UTF-8; we still html-unescape and
normalize whitespace like CLIP does after ftfy).
"""
from __future__ import annotations

import gzip
import html
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

_RESOURCE = Path(__file__).resolve().parent.parent / "resources" / \
    "clip_bpe_vocab_16e6.txt.gz"

# CLIP's pattern, emulated with stdlib re (UNICODE is default in py3):
#   's 't 're 've 'm 'll 'd   contractions
#   [^\W\d_]+                 letter runs   (= \p{L}+)
#   \d                        single digit  (= \p{N})
#   (?:[^\s\w]|_)+           punct runs INCLUDING underscore, so
#                             mixed runs like ^_^ stay ONE chunk,
#                             matching CLIP's [^\s\p{L}\p{N}]+ class
_PAT = re.compile(
    r"'s|'t|'re|'ve|'m|'ll|'d|[^\W\d_]+|\d|(?:[^\s\w]|_)+",
    re.IGNORECASE,
)

_WHITESPACE = re.compile(r"\s+")


@lru_cache(maxsize=1)
def _bytes_to_unicode() -> dict[int, str]:
    bs = (list(range(ord("!"), ord("~") + 1))
          + list(range(ord("\u00a1"), ord("\u00ac") + 1))
          + list(range(ord("\u00ae"), ord("\u00ff") + 1)))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, (chr(c) for c in cs)))


class _Counter:
    def __init__(self) -> None:
        self._ranks: Optional[dict[tuple[str, str], int]] = None
        self._error: Optional[str] = None

    def ensure_loaded(self) -> bool:
        if self._ranks is not None:
            return True
        if self._error is not None:
            return False
        try:
            with gzip.open(_RESOURCE, "rt", encoding="utf-8") as f:
                merges = f.read().split("\n")
        except OSError as exc:
            self._error = f"CLIP vocab unavailable: {exc}"
            return False
        # Exactly CLIP's slice: skip the version line, keep 48894
        # merge rules (49152 - 256 - 2 + 1).
        merges = merges[1:49152 - 256 - 2 + 1]
        self._ranks = {
            tuple(m.split()): i for i, m in enumerate(merges)
        }
        return True

    @property
    def error(self) -> Optional[str]:
        return self._error

    def _bpe_len(self, token: str) -> int:
        """Number of BPE pieces for one regex chunk."""
        return len(self._bpe_pieces(token))

    @lru_cache(maxsize=65536)
    def _bpe_pieces(self, token: str) -> tuple[str, ...]:
        assert self._ranks is not None
        b2u = _bytes_to_unicode()
        token = "".join(b2u[b] for b in token.encode("utf-8"))
        word: tuple[str, ...] = tuple(token[:-1]) + (token[-1] + "</w>",)
        if len(word) == 1:
            return word
        while True:
            pairs = {(word[i], word[i + 1]) for i in range(len(word) - 1)}
            best = min(
                pairs,
                key=lambda p: self._ranks.get(p, float("inf")),
            )
            if best not in self._ranks:
                break
            first, second = best
            new_word: list[str] = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                except ValueError:
                    new_word.extend(word[i:])
                    break
                new_word.extend(word[i:j])
                if (j < len(word) - 1 and word[j] == first
                        and word[j + 1] == second):
                    new_word.append(first + second)
                    i = j + 2
                else:
                    new_word.append(word[j])
                    i = j + 1
            word = tuple(new_word)
            if len(word) == 1:
                break
        return word

    def count(self, text: str) -> int:
        """Content-token count of `text` under CLIP BPE (no BOS/EOS).

        Raises RuntimeError if the vocab resource is unavailable —
        an approximate count from a limit checker would be a lie.
        """
        if not self.ensure_loaded():
            raise RuntimeError(self._error or "CLIP vocab unavailable")
        text = _WHITESPACE.sub(" ", html.unescape(text)).strip().lower()
        if not text:
            return 0
        return sum(self._bpe_len(chunk)
                   for chunk in _PAT.findall(text))


_COUNTER = _Counter()


def ensure_loaded() -> bool:
    """Load the vocab (once). False + get_error() on failure."""
    return _COUNTER.ensure_loaded()


def get_error() -> Optional[str]:
    return _COUNTER.error


def count_tokens(text: str) -> int:
    """Exact CLIP content-token count for a caption string."""
    return _COUNTER.count(text)
