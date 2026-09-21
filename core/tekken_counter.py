"""
core/tekken_counter.py

Pure-Python token COUNTER for Mistral's Tekken tokenizer (tekken.json),
used by the Flux.2 Dev target. No mistral-common, no tiktoken.

Why this exists: Mistral ships its tokenizer as tekken.json — a
tiktoken-style byte-level BPE. The obvious way to count with it is
Mistral's own `mistral-common` library, but that pulls in ~80 MB of
dependencies (numpy, pycountry, pydantic, tiktoken, …) to use a ~4 MB
tokenizing core — unacceptable bloat for a limit checker in a
size-constrained desktop app. This module reimplements ONLY the counting
path (byte-level BPE merge) directly against tekken.json, so the sole
added dependency is the small `regex` module (~3 MB), needed because the
pre-tokenization pattern uses \\p{...} Unicode classes that stdlib `re`
cannot parse.

Correctness: this implementation was validated to produce token counts
IDENTICAL to mistral-common's Tekkenizer across ASCII, underscores,
digits, kaomoji, Cyrillic, Japanese, and accented-Latin samples. It
counts CONTENT tokens (no BOS/EOS), matching the convention of the other
counters in core.multi_tokenizer.

Algorithm (standard tiktoken byte-level BPE):
  1. Pre-tokenize: split the text into chunks with the Tekken regex.
  2. For each chunk: UTF-8 encode to bytes. If the whole byte string is a
     known vocab token, it's one token. Otherwise start from single bytes
     and repeatedly merge the adjacent pair with the LOWEST rank (rank =
     merge priority) until no adjacent pair is a known token. The number
     of remaining parts is the chunk's token count.
  3. Sum across chunks.

tekken.json layout (the fields this uses):
  config.pattern           - pre-tokenization regex (\\p{...} classes)
  vocab[i].token_bytes      - base64 of the token's raw bytes
  vocab[i].rank             - merge priority (lower merges first)
  (special_tokens and multimodal are irrelevant to content counting.)
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional


class TekkenCounter:
    """Loads a tekken.json once and counts content tokens for text.

    Construction does NOT read the file; call ensure_loaded() (or count(),
    which loads on first use). Kept lazy so nothing happens until a Mistral
    count is actually requested. Not thread-safe for the initial load; the
    app loads it from the UI thread on first counter use.
    """

    def __init__(self, tekken_path: Path) -> None:
        self._path = Path(tekken_path)
        self._ranks: dict[bytes, int] = {}
        self._pat = None
        self._loaded = False
        self._error: Optional[str] = None

    @property
    def error(self) -> Optional[str]:
        return self._error

    def ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        if not self._path.is_file():
            self._error = f"tekken.json missing: {self._path.name}"
            return False
        try:
            import regex  # lazy; the one added dependency
        except Exception as exc:
            self._error = (
                "the 'regex' module is required for Tekken/Flux.2 Dev "
                f"counting but is not available ({exc})")
            return False
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            cfg = data["config"]
            pattern = cfg["pattern"]
            # CRITICAL: the vocab list holds more entries (num_vocab_tokens,
            # e.g. 150000) than the tokenizer actually uses. The real
            # tokenizer truncates to default_vocab_size, minus the special
            # tokens that occupy the low id range, so only ranks below
            #   cutoff = default_vocab_size - default_num_special_tokens
            # are mergeable. Entries at or above the cutoff are NOT loaded —
            # including them would let the merge combine byte pairs the real
            # tokenizer can't, undercounting tokens (e.g. ">_<" would wrongly
            # merge "_<" if its high rank were kept). This cutoff is what
            # makes counts match mistral-common exactly.
            default_vocab = int(cfg["default_vocab_size"])
            default_special = int(cfg["default_num_special_tokens"])
            cutoff = default_vocab - default_special
            ranks: dict[bytes, int] = {}
            for entry in data["vocab"]:
                rank = entry["rank"]
                if rank >= cutoff:
                    continue
                ranks[base64.b64decode(entry["token_bytes"])] = rank
        except Exception as exc:
            self._error = f"could not parse {self._path.name} ({exc})"
            return False
        if not ranks:
            self._error = f"{self._path.name} contained no vocab tokens"
            return False
        try:
            self._pat = regex.compile(pattern)
        except Exception as exc:
            self._error = f"invalid Tekken pattern in {self._path.name} ({exc})"
            return False
        self._ranks = ranks
        self._loaded = True
        self._error = None
        return True

    def _merge_len(self, piece: bytes) -> int:
        """Byte-level BPE: how many tokens `piece` becomes.

        Start with one part per byte, then repeatedly merge the adjacent
        pair whose concatenation has the lowest rank, until no adjacent
        pair is a known token. Returns the final part count.
        """
        ranks = self._ranks
        parts = [piece[i:i + 1] for i in range(len(piece))]
        if len(parts) <= 1:
            return len(parts)
        while len(parts) > 1:
            best_rank: Optional[int] = None
            best_i = -1
            for i in range(len(parts) - 1):
                r = ranks.get(parts[i] + parts[i + 1])
                if r is not None and (best_rank is None or r < best_rank):
                    best_rank = r
                    best_i = i
            if best_i < 0:
                break  # no adjacent pair can merge further
            parts[best_i:best_i + 2] = [parts[best_i] + parts[best_i + 1]]
        return len(parts)

    def count(self, text: str) -> int:
        """Content-token count of `text` under this Tekken tokenizer.

        Raises RuntimeError if the tokenizer could not be loaded (missing
        file or missing `regex`), matching the raise-don't-guess contract
        of the other counters — an approximate count in a limit checker
        would be worse than none.
        """
        if not self.ensure_loaded():
            raise RuntimeError(self._error or "Tekken tokenizer unavailable")
        assert self._pat is not None
        ranks = self._ranks
        total = 0
        for chunk in self._pat.findall(text):
            b = chunk.encode("utf-8")
            if not b:
                continue
            # Whole chunk is a single known token? (the common case for
            # frequent words/subwords) — one token, skip the merge loop.
            total += 1 if b in ranks else self._merge_len(b)
        return total
