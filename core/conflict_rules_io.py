"""Persistence for conflict rules — a small JSON file alongside settings.

Rules persist across sessions AND across datasets (your tagging knowledge
accumulates), so they live next to the app settings rather than in any
per-project session file. On first run the file doesn't exist; we seed it
with the conservative default ruleset.

The format is a simple JSON object:

    {
      "version": 1,
      "rules": [ { ...ConflictRule.to_dict()... }, ... ]
    }

Writes are atomic (temp file + os.replace) and tolerant of a corrupt or
missing file (fall back to seeds), so a bad write can never wedge the
app.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List

from core.conflict_rules import ConflictRule, default_seed_rules


FORMAT_VERSION = 1


def _default_path() -> Path:
    """Where the rules file lives — next to the app config. Mirrors how
    settings pick their location (platform config dir)."""
    # Reuse the same base dir logic as settings if available; fall back
    # to a sensible per-user location.
    base = os.environ.get("TAGWALKER_CONFIG_DIR")
    if base:
        return Path(base) / "conflict_rules.json"
    # Default: user home config.
    home = Path.home()
    return home / ".tagwalker" / "conflict_rules.json"


def load_rules(path: Path = None) -> List[ConflictRule]:
    """Load rules from disk. On a missing or corrupt file, return the
    seed ruleset (and the caller may choose to save it back)."""
    p = path or _default_path()
    try:
        if not p.exists():
            return default_seed_rules()
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("rules", [])
        rules = [ConflictRule.from_dict(d) for d in raw]
        # An empty file is a legitimate "user deleted everything" state —
        # respect it rather than re-seeding.
        return rules
    except (OSError, ValueError, json.JSONDecodeError):
        # Corrupt/unreadable — don't wedge the app; fall back to seeds.
        return default_seed_rules()


def save_rules(rules: List[ConflictRule], path: Path = None) -> bool:
    """Write rules atomically. Returns True on success, False on error
    (caller can surface a non-fatal warning; rules stay in memory)."""
    p = path or _default_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": FORMAT_VERSION,
            "rules": [r.to_dict() for r in rules],
        }
        # Atomic write: temp file in same dir, then os.replace.
        fd, tmp = tempfile.mkstemp(
            dir=str(p.parent), prefix=".conflict_rules_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # fsync best-effort
            os.replace(tmp, p)
        finally:
            # If replace failed, clean up the temp file.
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return True
    except OSError:
        return False
