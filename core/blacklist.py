"""
core/blacklist.py

Content blocking for images fetched FROM DANBOORU.

Danbooru ships every new account with a default blacklist —
`guro scat furry -rating:g` — because the site carries content most
people never want to meet by accident. TagWalker fetches Danbooru
images now (wiki examples, and post browsing), so it needs the same
protection: without it, a wiki page's curated examples can put gore on
screen with no warning.

SCOPE, and it matters: these rules apply ONLY to images fetched from
Danbooru. A user's own dataset is never filtered — this is a caption
auditing tool, and hiding the user's own images would break the thing
it exists to do.

Rule syntax is a compatible subset of Danbooru's own, so a user can
paste their site blacklist straight in:

    guro                  every post tagged guro
    scat                  every post tagged scat
    furry -rating:g       furry posts that are NOT rated general
    loli rating:e         posts tagged loli AND rated explicit

Each line is a set of terms that must ALL hold for the post to be
blocked (AND, not OR). `-term` inverts a term. `rating:x` tests the
post's rating, with both letters (g/s/q/e) and full names accepted.

The list ships pre-filled (DEFAULT_BLACKLIST) with Danbooru's own
defaults plus the close relatives of `guro`, and is fully editable —
the same arrangement Danbooru gives a new account. "Reset to
defaults" in Preferences restores it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Shipped as the starting contents of the editable blacklist, the way
# Danbooru pre-fills a new account's. `guro` is Danbooru's umbrella tag
# for gore; implications are not shipped with the app, so the closest
# severe relatives are listed explicitly rather than assumed to imply
# it. Everything here is editable — the list is visible and the user
# decides.
DEFAULT_BLACKLIST = """\
# Blocked content. One rule per line; delete a line to allow it.
# All terms on a line must match (AND). "-" inverts a term.
# Examples:  scat        furry -rating:g        loli rating:e
guro
mutilation
decapitation
disembowelment
necrophilia
scat
furry -rating:g
"""

# Danbooru metatag names. A term is only treated as a metatag when the
# part before its colon is one of these.
#
# BUG this fixes: any colon used to mean "metatag", so `:d` and `:o` —
# real, common tags — parsed as a metatag with an empty name, were
# dropped, and left an empty rule that was discarded. A blacklist line
# naming one silently did nothing. `re:zero_kara_hajimeru_isekai_
# seikatsu` went the same way. That is precisely the failure this
# design set out to rule out: the list is meant to be the whole truth,
# so a line must either work or be visibly wrong.
METATAG_NAMES = frozenset({
    "rating", "score", "favcount", "id", "md5", "width", "height",
    "mpixels", "ratio", "filesize", "date", "age", "order", "limit",
    "user", "approver", "commenter", "noter", "fav", "ordfav", "pool",
    "ordpool", "parent", "child", "source", "status", "tagcount",
    "gentags", "arttags", "chartags", "copytags", "metatags", "is",
    "has", "filetype", "duration", "embedded", "search", "random",
    "disapproved", "note", "comment", "commentary", "delreason",
})

_RATING_ALIASES = {
    "g": "g", "general": "g", "safe": "g", "s": "s", "sensitive": "s",
    "q": "q", "questionable": "q", "e": "e", "explicit": "e",
}


def _fold(term: str) -> str:
    return term.strip().lower().replace(" ", "_")


@dataclass
class Rule:
    """One blacklist line, pre-split for matching."""

    source: str
    tags: list[str] = field(default_factory=list)
    not_tags: list[str] = field(default_factory=list)
    ratings: list[str] = field(default_factory=list)
    not_ratings: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.tags or self.not_tags
                    or self.ratings or self.not_ratings)

    def matches(self, tags: set[str], rating: str) -> bool:
        """True when EVERY term holds — a line is an AND."""
        if self.is_empty():
            return False
        rating = _RATING_ALIASES.get(_fold(rating), _fold(rating))
        for t in self.tags:
            if t not in tags:
                return False
        for t in self.not_tags:
            if t in tags:
                return False
        if self.ratings and rating not in self.ratings:
            return False
        if rating in self.not_ratings:
            return False
        return True


def parse_rule(line: str) -> Rule:
    rule = Rule(source=line.strip())
    for raw in line.split():
        negated = raw.startswith("-")
        term = raw[1:] if negated else raw
        if not term:
            continue
        key, sep, value = term.partition(":")
        if sep and _fold(key) in METATAG_NAMES:
            if _fold(key) == "rating":
                value = _RATING_ALIASES.get(_fold(value), _fold(value))
                (rule.not_ratings if negated
                 else rule.ratings).append(value)
                continue
            # Any other metatag (user:, pool:, score:) is not
            # something we can evaluate from post data alone. Ignoring
            # the whole LINE would silently under-block, so the term
            # is dropped and the rest of the line still applies.
            continue
        # Anything else containing a colon is an ordinary tag — `:d`,
        # `:o`, `re:zero_...` — and must be matched literally.
        (rule.not_tags if negated else rule.tags).append(_fold(term))
    return rule


def parse_rules(text: str) -> list[Rule]:
    """Parse user-entered lines. Blank lines and `#` comments skipped."""
    rules: list[Rule] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rule = parse_rule(line)
        if not rule.is_empty():
            rules.append(rule)
    return rules


def build_rules(custom: str = "") -> list[Rule]:
    """The active rule set — simply whatever the blacklist says.

    An earlier design kept some rules permanently on and offered the
    rest as switches. One editable list is better: the user can see
    exactly what is filtered, paste a blacklist straight from
    Danbooru, and nothing is hidden from them.
    """
    return parse_rules(custom)


def blocked_tag_reason(tag: str, rules: list[Rule]) -> str | None:
    """Whether the TAG ITSELF is off limits, as opposed to a post.

    A line naming a single tag and nothing else ("guro") means "I do
    not want this subject at all", so its wiki page should not open
    either. A conditional line ("furry -rating:g", "loli rating:e")
    describes which POSTS to hide and says nothing about whether the
    tag may be read about, so it does not block the page.
    """
    folded = _fold(tag)
    for rule in rules:
        if (rule.tags == [folded] and not rule.not_tags
                and not rule.ratings and not rule.not_ratings):
            return rule.source
    return None


def rules_for(settings) -> list[Rule]:
    """Active rules for a Settings object. Both the Tag Referencer and
    the post browser go through here, so neither can drift from the
    other's idea of what is blocked."""
    return build_rules(
        getattr(settings, "danbooru_block_custom", "") or "")


def apply_to_posts(posts, rules: list[Rule]):
    """Mark blocked posts as skipped, with the reason.

    Duck-typed rather than importing PostInfo: this keeps the blocking
    logic free of any dependency on the API layer, and it runs on post
    METADATA, which always arrives before any image request.
    """
    for post in posts:
        if getattr(post, "skip", False):
            continue
        reason = blocked_reason(getattr(post, "tag_string", ""),
                                getattr(post, "rating", ""), rules)
        if reason:
            post.skip = True
            post.skip_reason = f"blocked: {reason}"
    return posts


def blocked_reason(tag_string: str, rating: str,
                   rules: list[Rule]) -> str | None:
    """The first rule this post trips, or None. Returning the rule
    means the UI can say WHY something is hidden instead of showing an
    unexplained gap."""
    tags = {_fold(t) for t in (tag_string or "").split()}
    for rule in rules:
        if rule.matches(tags, rating or ""):
            return rule.source
    return None
