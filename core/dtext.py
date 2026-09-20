"""
core/dtext.py

Danbooru DText parser and HTML renderer for the Tag Reference window
(DESIGN_TAG_REFERENCE.md — the wiki `body` field is DText).

Constructs handled, verified against Danbooru's help:dtext and against
real wiki pages reported from the field (high_heels, close-up,
breast_focus):

- headings `h1.`..`h6.` (with optional `#anchor` ids, which we drop)
- post embeds `!post #1234` -> Gallery blocks (the curated examples).
  Matching is deliberately tolerant: case-insensitive, and any
  spacing around `post` / `#` is accepted, because field reports
  showed embeds surviving into the rendered text when the pattern was
  strict.
- bare `post #1234` references. These are NOT always examples — wiki
  prose uses them for counter-examples ("what doesn't belong
  includes post #123"). So a bare reference becomes gallery content
  ONLY when its paragraph contains nothing but post references;
  inside prose it stays inline text.
- wiki links `[[target]]` / `[[target|label]]` -> clickable tag links
  on the app's existing `tag:` href scheme
- search links `{{tag}}` -> clickable tag link; `{{pool:Name}}` and
  other metatag searches -> readable plain text (we cannot navigate a
  post search, and showing raw braces is the bug this fixes)
- bullet lists `* item` (nesting flattened)
- inline `[b]/[i]/[u]/[s]` and container `[quote]/[spoiler]/
  [expand]/[code]/[tn]` -> markers stripped, inner text kept
- EXTERNAL links (`"label":url` and bare URLs) are REMOVED entirely
  (field decision: they are noise in a tagging reference, and the app
  never opens a browser). Sections left empty by that removal — the
  usual "External links" heading and its bullets — are pruned so the
  document doesn't end in a dangling header.

Anything unrecognized degrades to plain text; nothing here raises on
malformed markup. Pure python, no Qt.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from urllib.parse import quote


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------
@dataclass
class Para:
    html: str


@dataclass
class Heading:
    level: int
    html: str
    text: str
    anchor: str = ""      # `h4#angle.` -> "angle"; a TOC jump target


@dataclass
class Gallery:
    post_ids: list[int] = field(default_factory=list)
    # `!asset #N` embeds, which come from a different endpoint than
    # posts. Character wiki pages use these for outfit references, so
    # a gallery may legitimately hold only assets.
    asset_ids: list[int] = field(default_factory=list)
    # post id -> rendered caption html. Real Examples sections label
    # each entry ("!post #123: [[Pumps]]"), and those labels ARE the
    # taxonomy the reader is there for, so they travel with the ids.
    captions: dict[int, str] = field(default_factory=dict)


@dataclass
class Bullets:
    items: list[str] = field(default_factory=list)


@dataclass
class _Dropped:
    """Marker: a block existed here but became empty once external
    links were stripped. Used only to decide whether the heading above
    it still has a section worth showing; never rendered."""


Block = object


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
# [[wiki link]] or {{search link}}
_LINKISH = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]|\{\{([^}]*)\}\}")
# "label":target where target is [bracketed] or bare. The target may be
# an absolute URL, a site-relative path, or a #page-anchor.
_EXT_LINK = re.compile(r'"([^"]*)":(\[[^\]]*\]|\S+)')
_BARE_URL = re.compile(r"(?<![\"\[>])(https?://[^\s<\]]+)")
# One pass over both forms so a labelled link is never half-consumed
# by the bare-URL pattern. g1/g2 = labelled, g3 = bare.
_EXTERNAL_ANY = re.compile(
    r'"([^"]*)":(\[[^\]]*\]|\S+)'
    r"|(?<![\"\[>])(https?://[^\s<\]]+)")
_ANCHOR_PREFIX = "dtext-"
_INLINE_TAG = re.compile(r"\[/?(?:b|i|u|s|tn|nodtext)\]", re.IGNORECASE)
_CONTAINER_TAG = re.compile(
    r"\[/?(?:quote|spoiler|expand(?:=[^\]]*)?|code|section(?:=[^\]]*)?)\]",
    re.IGNORECASE)
# Some Danbooru wiki bodies mix LITERAL HTML formatting tags into the
# DText (e.g. "<b>looking away</b>" instead of the DText "[b]...[/b]").
# Without handling, html.escape turns these into "&lt;b&gt;", which the
# rich-text label then shows as visible raw "<b>" text. We strip the
# same inline formatting tags DText's own [b]/[i]/... are stripped to
# (see _INLINE_TAG use in render_inline), so a literal <b> disappears
# exactly like its DText counterpart rather than leaking markup. Only
# these safe, self-contained formatting tags are matched — arbitrary
# HTML is still escaped, so this does not open a rendering hole.
_HTML_INLINE_TAG = re.compile(
    r"</?(?:b|i|u|s|strong|em)\s*>", re.IGNORECASE)
# Tolerant post reference; group 1 present => embed form (leading !).
# `(!)` marks the embed form. The optional whitespace belongs INSIDE
# the bang group: letting it lead the whole match swallowed the space
# before a bare "post #N", which both ran words together and destroyed
# the word boundary that the chip linker relies on.
_ANY_POST = re.compile(r"(!\s*)?\bpost\s*#\s*(\d+)", re.IGNORECASE)
_HEADING = re.compile(r"^h([1-6])(?:#([\w-]+))?\.\s*(.*)$")
# `!asset #N` embeds a MEDIA ASSET rather than a post — a different
# endpoint. Character pages use these for "Appearance" outfit
# breakdowns, so they are fetched and shown like post embeds.
_ASSET = re.compile(r"!\s*asset\s*#\s*(\d+)", re.IGNORECASE)
_BULLET = re.compile(r"^\*+\s+(.*)$")

_WIKI_STYLE = "text-decoration:none;"
_LINK_STYLE = "text-decoration:underline;"
BASE_SITE = "https://danbooru.donmai.us"


def _fold_target(target: str) -> str:
    return target.strip().lower().replace(" ", "_")


def _tag_link(target: str, label: str) -> str:
    href = "tag:" + quote(_fold_target(target))
    return (f'<a href="{href}" style="{_WIKI_STYLE}">'
            f"{_html.escape(label)}</a>")


def _render_search_link(content: str) -> str:
    """`{{...}}` is a post SEARCH, not a wiki link. A single plain tag
    is navigable in-app; a metatag search (`pool:Juicy_Details`,
    `user:x`) is not, so it renders as readable text instead of raw
    braces."""
    content = (content or "").strip()
    if not content:
        return ""
    if ":" in content:
        prefix, _, rest = content.partition(":")
        pretty = rest.replace("_", " ").strip()
        prefix = prefix.strip().lower()
        if pretty:
            return _html.escape(f"{pretty} ({prefix})")
        return _html.escape(content)
    if re.search(r"\s", content):
        # Multi-tag search: show the terms, don't pretend it's one tag.
        return _html.escape(content.replace("_", " "))
    return _tag_link(content, content.replace("_", " "))


def _clean_anchor(target: str) -> str:
    """`#dtext-angle` -> `angle`. Danbooru prefixes rendered heading
    ids; the wiki source writes the bare form in the heading."""
    a = target.lstrip("#").strip()
    if a.startswith(_ANCHOR_PREFIX):
        a = a[len(_ANCHOR_PREFIX):]
    return a


def _external_html(label: str, target: str) -> str:
    """Render one external reference.

    Links are KEPT and clickable, but clicking copies the address
    rather than opening a browser — for a lot of artist tags the
    social links ARE the whole page, so stripping them threw away the
    only content there was. Nothing here ever launches a browser.
    """
    target = target.strip()
    if target.startswith("[") and target.endswith("]"):
        target = target[1:-1].strip()
    label = (label or target).strip() or target
    if target.startswith("#"):
        return (f'<a href="anchor:{quote(_clean_anchor(target))}" '
                f'style="{_WIKI_STYLE}">{_html.escape(label)}</a>')
    if target.startswith("/"):
        target = BASE_SITE + target
    if not target:
        return _html.escape(label)
    return (f'<a href="copy:{quote(target, safe="")}" '
            f'style="{_LINK_STYLE}">{_html.escape(label)}</a>')


def _render_externals(text: str) -> str:
    """Escape a RAW run of text while turning its external references
    into copy links.

    Escaping first would be wrong: html.escape turns `"` into `&quot;`,
    and every DText external link is quoted (`"label":url`), so the
    pattern would never match again. Hence the gaps are escaped here,
    piece by piece, around the matches.
    """
    out: list[str] = []
    pos = 0
    for m in _EXTERNAL_ANY.finditer(text):
        out.append(_html.escape(text[pos:m.start()], quote=False))
        if m.group(3) is not None:
            out.append(_external_html(m.group(3), m.group(3)))
        else:
            out.append(_external_html(m.group(1), m.group(2)))
        pos = m.end()
    out.append(_html.escape(text[pos:], quote=False))
    return "".join(out)


def render_inline(text: str) -> str:
    """Render one run of DText to safe HTML (no block structure)."""
    text = _CONTAINER_TAG.sub("", text)
    text = _INLINE_TAG.sub("", text)
    # Literal HTML inline formatting (some wikis mix it into DText) is
    # stripped like its DText equivalent, so it does not survive to be
    # escaped into visible "&lt;b&gt;" text.
    text = _HTML_INLINE_TAG.sub("", text)
    text = _ASSET.sub(r"asset #\1", text)
    out: list[str] = []
    pos = 0
    for m in _LINKISH.finditer(text):
        out.append(_render_externals(text[pos:m.start()]))
        if m.group(3) is not None:                    # {{search}}
            out.append(_render_search_link(m.group(3)))
        else:                                          # [[wiki link]]
            target = (m.group(1) or "").strip()
            label = (m.group(2) or target).strip() or target
            out.append(_tag_link(target, label))
        pos = m.end()
    out.append(_render_externals(text[pos:]))
    return _link_post_refs(_tidy_spaces("".join(out)))


_POST_STYLE = "text-decoration:none;"


def _link_post_refs(html: str) -> str:
    """Turn every `post #N` / `!post #N` into a clickable chip.

    Applied to ALREADY-rendered html, at the very end, so it reaches
    every context — paragraphs, bullet items, headings. Bullets were
    the field bug: an Examples section written as `* !post #123` never
    went through paragraph handling, so the reference stayed literal
    text with no link, no thumbnail and no error. A chip needs no
    network to appear and always opens the example.

    Safe against the links already emitted above it: those carry
    `href="post:123"` (colon), which this pattern cannot match.
    """
    return _ANY_POST.sub(
        lambda m: (f'<a href="post:{m.group(2)}" '
                   f'style="{_POST_STYLE}">post #{m.group(2)}</a>'),
        html)


def _tidy_spaces(html: str) -> str:
    html = re.sub(r"[ \t]{2,}", " ", html)
    html = re.sub(r"\s+([,.;:!?])", r"\1", html)
    return html.strip()


# ---------------------------------------------------------------------------
# Block parsing
# ---------------------------------------------------------------------------
def _asset_refs(text: str) -> list[int]:
    return [int(m.group(1)) for m in _ASSET.finditer(text)]


def _post_refs(text: str) -> list[tuple[bool, int]]:
    """(is_embed, post_id) in document order."""
    out: list[tuple[bool, int]] = []
    for m in _ANY_POST.finditer(text):
        try:
            out.append((bool(m.group(1)), int(m.group(2))))
        except (TypeError, ValueError):
            continue
    return out


def parse_dtext(body: str) -> list[Block]:
    """Parse a wiki body into document-order blocks. Never raises."""
    blocks: list[Block] = []
    if not body:
        return blocks
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    # `[expand=Table of Contents]` is a collapsible box on the site.
    # Promote its title to a heading so the list beneath it is not an
    # unlabelled block of links.
    body = re.sub(r"\[expand=([^\]]*)\]",
                  lambda m: f"\nh5. {m.group(1).strip()}\n",
                  body, flags=re.IGNORECASE)
    body = _CONTAINER_TAG.sub("", body)

    para_lines: list[str] = []
    bullet_items: list[str] = []
    pending_gallery: list[tuple[int, str]] = []
    pending_assets: list[tuple[int, str]] = []

    def flush_gallery() -> None:
        if not pending_gallery and not pending_assets:
            return
        ids = [pid for pid, _ in pending_gallery]
        caps = {pid: cap for pid, cap in pending_gallery if cap}
        aids = [aid for aid, _ in pending_assets]
        for aid, cap in pending_assets:
            if cap:
                caps[-aid] = cap        # negative key = asset caption
        pending_gallery.clear()
        pending_assets.clear()
        blocks.append(Gallery(post_ids=ids, captions=caps,
                              asset_ids=aids))

    def flush_bullets() -> None:
        raw = list(bullet_items)
        bullet_items.clear()
        items = [i for i in raw if i.strip()]
        if items:
            blocks.append(Bullets(items=items))
        elif raw:
            blocks.append(_Dropped())      # was external links only

    def flush_para() -> None:
        if not para_lines:
            return
        text = " ".join(para_lines).strip()
        para_lines.clear()
        if not text:
            return
        assets = _asset_refs(text)
        refs = _post_refs(text)
        if not refs and not assets:
            html = render_inline(text)
            if html.strip():
                blocks.append(Para(html=html))
            return
        residual = _ASSET.sub("", _ANY_POST.sub("", text)).strip()
        if not residual:
            # Paragraph is nothing but post references: all of them
            # are examples, embed marker or not.
            blocks.append(Gallery(post_ids=[pid for _, pid in refs],
                                  asset_ids=assets))
            return
        # Prose with references. Explicit !post embeds are examples
        # and split out into a gallery; bare `post #N` stays inline
        # text because prose uses it for counter-examples too.
        embed_ids = [pid for is_embed, pid in refs if is_embed]
        prose = _ANY_POST.sub(
            lambda m: "" if m.group(1) else f"post #{m.group(2)}",
            text)
        html = render_inline(prose)
        if html.strip():
            blocks.append(Para(html=html))
        if embed_ids or assets:
            blocks.append(Gallery(post_ids=embed_ids,
                                  asset_ids=assets))

    for raw in body.split("\n"):
        line = raw.rstrip()
        h = _HEADING.match(line)
        b = _BULLET.match(line)
        if h:
            flush_para()
            flush_bullets()
            flush_gallery()
            blocks.append(Heading(
                level=int(h.group(1)),
                html=render_inline(h.group(3)),
                text=_LINKISH.sub(
                    lambda m: (m.group(2) or m.group(1)
                               or m.group(3) or ""),
                    h.group(3)).strip(),
                anchor=_clean_anchor(h.group(2) or "")))
        elif b:
            flush_para()
            item = b.group(1).strip()
            asset = _ASSET.match(item)
            if asset:
                flush_bullets()
                cap_raw = item[asset.end():].lstrip(" \t:\u2014\u2013-")
                pending_assets.append(
                    (int(asset.group(1)),
                     render_inline(cap_raw) if cap_raw.strip() else ""))
                continue
            ref = _ANY_POST.match(item)
            if ref:
                # Real Examples sections are labelled bullets:
                #   * !post #4229961: [[Pumps]]
                # The line STARTS with the reference; whatever follows
                # is that example's caption. A bullet that merely
                # mentions a post mid-sentence is ordinary prose.
                flush_bullets()
                pid = int(ref.group(2))
                caption_raw = item[ref.end():].lstrip(" \t:\u2014\u2013-")
                pending_gallery.append(
                    (pid, render_inline(caption_raw)
                     if caption_raw.strip() else ""))
            else:
                # NOT flushing the gallery here is deliberate: an
                # entry we cannot fetch (`!asset #N`) sitting mid-list
                # would otherwise split one Examples grid into two
                # grids with a stray line between them. The note is
                # emitted above the grid instead.
                bullet_items.append(render_inline(item))
        elif not line.strip():
            flush_para()
            flush_bullets()
            flush_gallery()
        else:
            flush_bullets()
            flush_gallery()
            para_lines.append(line)
    flush_para()
    flush_bullets()
    flush_gallery()

    merged: list[Block] = []
    for blk in blocks:
        if (isinstance(blk, Gallery) and merged
                and isinstance(merged[-1], Gallery)):
            merged[-1].post_ids.extend(blk.post_ids)
            merged[-1].captions.update(blk.captions)
        else:
            merged.append(blk)
    return _prune_empty_sections(merged)


def _prune_empty_sections(blocks: list[Block]) -> list[Block]:
    """Drop headings that no longer head anything.

    Two cases, both from real pages:
    - a heading with nothing after it at all;
    - a heading whose section content WAS external links and is now
      gone (the `_Dropped` marker). A trailing footer paragraph after
      such a section — the "aliased to this tag" note — is left in
      place on its own rather than resurrecting the header.

    A heading followed by a DEEPER heading is a parent section and is
    always kept (h4 Examples over h6 Types)."""
    keep = [True] * len(blocks)
    for i, blk in enumerate(blocks):
        if not isinstance(blk, Heading):
            continue
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if nxt is None:
            keep[i] = False                       # dangling heading
        elif isinstance(nxt, _Dropped):
            keep[i] = False                       # section was links
        elif isinstance(nxt, Heading) and nxt.level <= blk.level:
            keep[i] = False                       # empty section
    return [b for b, k in zip(blocks, keep)
            if k and not isinstance(b, _Dropped)]


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------
def extract_post_ids(blocks: list[Block]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for blk in blocks:
        if isinstance(blk, Gallery):
            for pid in blk.post_ids:
                if pid not in seen:
                    seen.add(pid)
                    out.append(pid)
    return out


def extract_wiki_links(body: str) -> list[tuple[str, str]]:
    """(folded_target, label) for every [[wiki link]], document order,
    de-duplicated. These are the editorial related tags."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _LINKISH.finditer(body or ""):
        if m.group(3) is not None:
            continue                       # {{search}} is not a wiki link
        target = _fold_target(m.group(1) or "")
        label = (m.group(2) or m.group(1) or "").strip()
        if target and target not in seen:
            seen.add(target)
            out.append((target, label))
    return out


def blocks_to_html(blocks: list[Block]) -> str:
    parts: list[str] = []
    for blk in blocks:
        if isinstance(blk, Heading):
            parts.append(f"<h{blk.level}>{blk.html}</h{blk.level}>")
        elif isinstance(blk, Para):
            parts.append(f"<p>{blk.html}</p>")
        elif isinstance(blk, Bullets):
            items = "".join(f"<li>{i}</li>" for i in blk.items)
            parts.append(f"<ul>{items}</ul>")
        elif isinstance(blk, Gallery):
            parts.append(
                f"<p><i>[{len(blk.post_ids)} example(s)]</i></p>")
    return "\n".join(parts)
