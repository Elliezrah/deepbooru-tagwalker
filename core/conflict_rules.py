"""Conflict rules — detect tag combinations that shouldn't co-occur, and
requirement rules where one tag implies another (Features C + D).

A *rule* encodes domain knowledge the dataset doesn't enforce. Two types:

  EXCLUSION  — a set of tags of which AT MOST ONE should appear on any
               image. If an image carries two or more, that's a
               violation. (e.g. 1girl / 2girls / 3girls — an image has
               exactly one girl-count tag.)

  REQUIREMENT — a trigger tag implies a required tag. If the trigger is
               present but the required tag is missing, that's a
               violation. (e.g. cat_ears implies animal_ears.)

Alias-awareness: rules are written in canonical terms, but datasets may
use aliased spellings. Detection normalizes BOTH the rule tags and each
image's tags through the Danbooru alias map before comparing, so a rule
written against "indoors" still catches an image tagged "indoor". The
normalizer is injected (a callable) so this module stays free of a hard
dependency on the tag database and is trivial to test.

This module is pure data + logic: no Qt, no disk, no global state. The
editor dialog and the scan dialog build on top of it; persistence lives
in conflict_rules_io.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


RULE_EXCLUSION = "exclusion"
RULE_REQUIREMENT = "requirement"


@dataclass
class ConflictRule:
    """One conflict rule.

    Attributes
    ----------
    rule_type : RULE_EXCLUSION or RULE_REQUIREMENT.
    name      : a short human label. If empty, the UI derives one from
                the tags.
    tags      : for EXCLUSION, the mutually-exclusive set (2+ tags).
                for REQUIREMENT, exactly [trigger] (the implied tag goes
                in `requires`).
    requires  : for REQUIREMENT only, the tag that must be present when
                the trigger is. Empty/ignored for EXCLUSION.
    enabled   : rules can be toggled off without deleting them.
    seeded    : True for built-in starter rules (lets the UI mark them
                and lets us avoid re-seeding a rule the user deleted).
    """
    rule_type: str
    tags: list[str] = field(default_factory=list)
    requires: str = ""
    name: str = ""
    enabled: bool = True
    seeded: bool = False

    def display_name(self) -> str:
        """A label for the rule, derived from tags when name is blank."""
        if self.name:
            return self.name
        if self.rule_type == RULE_REQUIREMENT:
            trig = self.tags[0] if self.tags else "?"
            return f"{trig} \u2192 {self.requires}"
        return " / ".join(self.tags) if self.tags else "(empty rule)"

    def to_dict(self) -> dict:
        return {
            "rule_type": self.rule_type,
            "tags": list(self.tags),
            "requires": self.requires,
            "name": self.name,
            "enabled": self.enabled,
            "seeded": self.seeded,
        }

    @staticmethod
    def from_dict(d: dict) -> "ConflictRule":
        return ConflictRule(
            rule_type=d.get("rule_type", RULE_EXCLUSION),
            tags=list(d.get("tags", [])),
            requires=d.get("requires", ""),
            name=d.get("name", ""),
            enabled=bool(d.get("enabled", True)),
            seeded=bool(d.get("seeded", False)),
        )


@dataclass
class Violation:
    """One detected rule violation on one image.

    image_path : the offending image.
    rule       : the rule it broke.
    present    : for EXCLUSION, the 2+ conflicting tags found (in their
                 ORIGINAL dataset spelling, so the UI can offer to remove
                 the exact tag the file contains).
    missing    : for REQUIREMENT, the required tag that's absent.
    trigger    : for REQUIREMENT, the trigger tag found (original
                 spelling).
    """
    image_path: object
    rule: ConflictRule
    present: list[str] = field(default_factory=list)
    missing: str = ""
    trigger: str = ""


def _identity(tag: str) -> str:
    return tag


def detect_violations(
    images: Iterable,
    tags_of: Callable[[object], list[str]],
    rules: Iterable[ConflictRule],
    normalize: Optional[Callable[[str], str]] = None,
) -> list[Violation]:
    """Scan `images` against `rules`, returning a flat list of violations.

    Parameters
    ----------
    images    : iterable of image identifiers (e.g. Paths).
    tags_of   : callable mapping an image to its list of tag strings
                (original dataset spelling).
    rules     : the rules to check (only enabled ones are applied; the
                caller may pre-filter, but we also skip disabled here).
    normalize : optional alias-normalizer; maps any tag spelling to its
                canonical form. Defaults to identity (no alias folding)
                so the module works without the database in tests.

    Returns
    -------
    list of Violation, in (image order, rule order). One row per broken
    rule per image. An exclusion rule broken on an image yields one
    violation listing all the conflicting tags; a requirement rule yields
    one violation per missing requirement.
    """
    norm = normalize or _identity
    active = [r for r in rules if r.enabled]
    out: list[Violation] = []

    for img in images:
        raw_tags = tags_of(img)
        # Map normalized form -> original spelling (first occurrence).
        norm_to_orig: dict[str, str] = {}
        for tg in raw_tags:
            n = norm(tg)
            if n not in norm_to_orig:
                norm_to_orig[n] = tg
        present_norms = set(norm_to_orig.keys())

        for rule in active:
            if rule.rule_type == RULE_EXCLUSION:
                rule_norms = {norm(t) for t in rule.tags}
                hit_norms = present_norms & rule_norms
                if len(hit_norms) >= 2:
                    # Conflicting tags in original spelling, stable order
                    # following the rule's tag order then any extras.
                    ordered = [
                        norm_to_orig[n]
                        for n in (norm(t) for t in rule.tags)
                        if n in hit_norms
                    ]
                    out.append(Violation(
                        image_path=img, rule=rule, present=ordered,
                    ))
            elif rule.rule_type == RULE_REQUIREMENT:
                if not rule.tags or not rule.requires:
                    continue
                trigger_norm = norm(rule.tags[0])
                required_norm = norm(rule.requires)
                if (trigger_norm in present_norms
                        and required_norm not in present_norms):
                    out.append(Violation(
                        image_path=img, rule=rule,
                        missing=rule.requires,
                        trigger=norm_to_orig.get(trigger_norm, rule.tags[0]),
                    ))
    return out


# ---------------------------------------------------------------------------
# Seed rules — ONLY 100%-certain logical contradictions. Every tag here
# was validated as a real canonical Danbooru tag. Users build the rest.
# ---------------------------------------------------------------------------

def default_seed_rules() -> list[ConflictRule]:
    """The conservative built-in starter ruleset.

    An exclusion set means EVERY PAIR within it conflicts. We only seed
    sets that genuinely satisfy that — relationships that are logical
    contradictions, not merely "usually separate". Everything subtler is
    left to the user.

    Deliberately NOT seeded, and why:
      * solo vs counts — solo legitimately co-occurs with 1girl/1boy
        (the one person), so {solo, 1girl, ...} is not pairwise
        exclusive. The real relationship (solo conflicts with 2+ counts)
        isn't expressible as one simple exclusion set.
      * no_humans vs people — similar: it's a relationship against many
        tags, and mixing it with the count tags breaks the
        "every pair conflicts" invariant.
      * multiple_girls is intentionally excluded from the girl-count set
        because Danbooru tags it ALONGSIDE 2girls/3girls/..., so it
        legitimately co-occurs with them.
    Users can add solo/no_humans rules themselves if their dataset
    convention warrants it.
    """
    return [
        # Girl-count tags are pairwise mutually exclusive: an image has
        # exactly one (1girl XOR 2girls XOR ...).
        ConflictRule(
            rule_type=RULE_EXCLUSION,
            tags=["1girl", "2girls", "3girls", "4girls", "5girls",
                  "6+girls"],
            name="Girl count", seeded=True,
        ),
        # Boy-count tags, same logic.
        ConflictRule(
            rule_type=RULE_EXCLUSION,
            tags=["1boy", "2boys", "3boys", "4boys", "5boys", "6+boys"],
            name="Boy count", seeded=True,
        ),
        # A scene is indoors or outdoors, not both.
        ConflictRule(
            rule_type=RULE_EXCLUSION,
            tags=["indoors", "outdoors"],
            name="indoors vs outdoors", seeded=True,
        ),
        # Time of day: an image is day or night, not both. (Only the two
        # clear opposites are seeded; ambiguous times like sunset/dusk
        # are left out so they don't trip false conflicts.)
        ConflictRule(
            rule_type=RULE_EXCLUSION,
            tags=["day", "night"],
            name="day vs night", seeded=True,
        ),
    ]
