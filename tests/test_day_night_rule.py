"""Regression test for the seeded 'day vs night' absolute rule.

day and night are mutually exclusive (a scene is one or the other, not
both), seeded alongside the girl/boy counts and indoors/outdoors. This
test confirms the seed is present, that it flags an image carrying both
tags, and that it leaves day-only / night-only / neither images alone.

It also confirms the dialog's "Restore built-in rules" merge would add
day/night to an older saved rule-set that predates it (via signature),
which is how existing projects gain the new rule.

Run: python3 tests/test_day_night_rule.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.conflict_rules import (
    default_seed_rules, detect_violations, RULE_EXCLUSION, ConflictRule,
)


def run() -> None:
    rules = default_seed_rules()
    names = [r.name for r in rules]
    assert "day vs night" in names, names

    imgs = {
        "both": ["1girl", "day", "night", "outdoors"],
        "day": ["1girl", "day", "outdoors"],
        "night": ["1girl", "night", "indoors"],
        "neither": ["1girl", "indoors"],
        "inout": ["1girl", "indoors", "outdoors"],
    }
    viols = detect_violations(list(imgs.keys()), lambda i: imgs[i], rules)
    by_img = {}
    for v in viols:
        by_img.setdefault(v.image_path, []).append(
            (v.rule.name, sorted(v.present))
        )

    # day+night flagged.
    assert any(rn == "day vs night" and set(t) == {"day", "night"}
               for (rn, t) in by_img.get("both", []))
    # day-only / night-only / neither NOT flagged for day vs night.
    for k in ("day", "night", "neither"):
        assert not any(rn == "day vs night" for (rn, _t) in by_img.get(k, []))
    # indoors/outdoors rule still works (unchanged).
    assert any(rn == "indoors vs outdoors"
               for (rn, _t) in by_img.get("inout", []))

    # "Restore built-in rules" merge (same signature logic the dialog
    # uses) would add day/night to an older set that lacks it.
    def sig(r):
        return (r.rule_type, tuple(t.lower() for t in r.tags),
                r.requires.lower())
    older = [r for r in default_seed_rules() if r.name != "day vs night"]
    have = {sig(r) for r in older}
    to_add = [s for s in default_seed_rules() if sig(s) not in have]
    assert any(s.name == "day vs night" for s in to_add), \
        "restore-seeds would add day/night to an older rule-set"

    print("OK: day vs night absolute rule verified "
          "(seeded, flags both, ignores singles, restore-seeds adds it)")


if __name__ == "__main__":
    run()
