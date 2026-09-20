"""Unit test for the reformat guard classifier.

Pattern rule catches symbol faces; the curated seed catches lettered
emoticons; real word-joins and legit short tags are NOT protected.

Run: python3 tests/test_reformat_guard.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from core import reformat_guard as g


def run() -> None:
    # Symbol faces -> protected by the no-letter pattern rule.
    for t in ["^_^", ">_<", "@_@", "=_=", "+_+", "|_|", "0_0", "._.", ";_;"]:
        assert g.is_protected_tag(t), f"{t} should be protected (pattern)"

    # Lettered emoticons -> protected by the curated seed.
    for t in ["o_o", "u_u", "x_x", ">_o", "o_<", "t_t"]:
        assert g.is_protected_tag(t), f"{t} should be protected (seed)"

    # Real word-joins -> NOT protected (these are the whole point of the tool).
    for t in ["black_elbow_gloves", "blue_eyes", "long_hair", "short_hair"]:
        assert not g.is_protected_tag(t), f"{t} must NOT be protected"

    # Legit short tags that merely lack a 3-letter word -> NOT protected.
    # (weapon/aircraft models, character races, etc.)
    for t in ["bf_109", "cz_75", "au_ra", "d_va", "k_on"]:
        assert not g.is_protected_tag(t), f"{t} must NOT be protected"

    assert g.is_protected_tag("") is False
    assert len(g.protected_seed()) > 0, "curated seed should load from resource"
    print(f"OK: guard protects faces, leaves {len(g.protected_seed())}-tag seed + patterns; spares real tags")
    print("\nALL PASS: reformat guard")


if __name__ == "__main__":
    run()
