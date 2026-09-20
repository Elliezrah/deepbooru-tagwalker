"""
core/metadata_reader.py

Reads Stable-Diffusion generation metadata embedded in an image and
returns it in a display-ready form. This is the "meta info" the program
shows for an image: the prompt, negative prompt, and settings the image
was generated with — when they are present.

Why this is more than "read one field": there is no single standard.
Different tools write metadata differently, and different file types
carry it in different places:

  PNG  — metadata lives in tEXt/iTXt chunks (PIL exposes these as
         Image.info). The two common producers:
           * Automatic1111 / Forge: one 'parameters' chunk holding a
             flat text blob (prompt, then "Negative prompt:", then a
             comma-separated settings line).
           * ComfyUI: a 'workflow' chunk (the whole node graph as JSON)
             and usually a 'prompt' chunk (the API prompt as JSON).
  JPEG / WebP — no text chunks, so the same A1111 blob is written into
         EXIF UserComment instead.

So this module tries, in order: PNG text chunks (A1111 flat, then
ComfyUI JSON), then EXIF (A1111 flat). If nothing generation-related is
found it says so cleanly — many images (stripped, re-saved, or simply
not AI-generated) carry no such data, and that is a normal result, not
an error.

The parsing is best-effort and defensive: a malformed blob yields the
raw text rather than a crash, and the raw text is always available
alongside any parsed fields, because ComfyUI graphs and unusual formats
cannot be tidily field-parsed and the user may still want to read them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class GenerationMetadata:
    """Parsed generation metadata for one image.

    `found` is False when the image carries no recognizable generation
    data (the common, non-error case). When True, `raw` always holds the
    full original text; `fields` holds parsed key/value pairs when the
    format allowed parsing (A1111), and `prompt`/`negative` are pulled
    out for prominent display when available. `source` names where it
    came from, for the UI header.
    """
    found: bool = False
    source: str = ""              # e.g. "PNG (Automatic1111)", "EXIF", "ComfyUI"
    prompt: str = ""
    negative: str = ""
    fields: list[tuple[str, str]] = field(default_factory=list)
    raw: str = ""
    note: str = ""                # optional human note (e.g. why nothing found)


# Keys in the A1111 settings line we lift into named fields, in a sensible
# display order. Anything not listed still shows (in file order) after these.
_A1111_PREFERRED_ORDER = (
    "Steps", "Sampler", "Schedule type", "CFG scale", "Seed", "Size",
    "Model", "Model hash", "VAE", "VAE hash", "Denoising strength",
    "Clip skip", "Hires upscale", "Hires steps", "Hires upscaler",
    "Version",
)


def read_generation_metadata(image_path: Path) -> GenerationMetadata:
    """Read and parse generation metadata from `image_path`.

    Never raises for an unreadable or metadata-less image — returns a
    GenerationMetadata with found=False and an explanatory note instead,
    so the caller can display a clean "no data" state.
    """
    path = Path(image_path)
    try:
        from PIL import Image
    except Exception as exc:  # Pillow missing — should not happen in-app
        return GenerationMetadata(
            found=False, note=f"could not load image library ({exc})")

    try:
        with Image.open(path) as img:
            info = dict(getattr(img, "info", {}) or {})
            fmt = (img.format or "").upper()
            exif = None
            try:
                exif = img.getexif()
            except Exception:
                exif = None
    except FileNotFoundError:
        return GenerationMetadata(found=False, note="image file not found")
    except Exception as exc:
        return GenerationMetadata(
            found=False, note=f"could not read image ({exc})")

    # --- 1. PNG / general: Automatic1111 'parameters' text chunk ---------
    # PIL puts PNG tEXt/iTXt entries in img.info. A1111/Forge use the key
    # 'parameters'. Some tools use 'Comment' or 'Description' for the same
    # blob, so check those too.
    for key in ("parameters", "Parameters", "Comment", "Description"):
        blob = info.get(key)
        if isinstance(blob, str) and blob.strip():
            parsed = _parse_a1111(blob)
            if parsed is not None:
                parsed.source = f"{fmt or 'image'} \u2014 Automatic1111 / Forge"
                return parsed

    # --- 2. ComfyUI: 'workflow' and/or 'prompt' JSON chunks --------------
    comfy = _parse_comfy(info)
    if comfy is not None:
        return comfy

    # --- 3. EXIF UserComment (JPEG / WebP carry the A1111 blob here) ------
    blob = _exif_user_comment(exif)
    if blob:
        parsed = _parse_a1111(blob)
        if parsed is not None:
            parsed.source = f"{fmt or 'image'} \u2014 EXIF (Automatic1111)"
            return parsed
        # Non-A1111 EXIF comment — still show it raw rather than discard.
        return GenerationMetadata(
            found=True, source=f"{fmt or 'image'} \u2014 EXIF",
            raw=blob.strip())

    # --- Nothing generation-related present ------------------------------
    return GenerationMetadata(
        found=False,
        note=("No generation metadata found in this image. It may have "
              "been created without embedding parameters, saved by a tool "
              "that strips them, or re-exported (uploading to some sites "
              "removes this data)."))


def _parse_a1111(blob: str) -> Optional[GenerationMetadata]:
    """Parse an Automatic1111 / Forge parameter blob.

    Shape:
        <positive prompt, possibly multi-line>
        Negative prompt: <negative prompt, possibly multi-line>
        Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 123, ...

    Returns None if the text does not look like an A1111 blob at all (so
    the caller can fall through to other parsers), otherwise a populated
    GenerationMetadata. The last line that is a comma-separated run of
    "Key: value" pairs is the settings line; everything before the
    "Negative prompt:" marker is the positive prompt.
    """
    text = blob.strip()
    if not text:
        return None

    lines = text.split("\n")

    # Find the settings line: the last line that parses as several
    # "Key: value" pairs. A1111 always puts it last.
    settings_idx = -1
    settings_pairs: list[tuple[str, str]] = []
    for i in range(len(lines) - 1, -1, -1):
        pairs = _parse_settings_line(lines[i])
        if pairs and len(pairs) >= 2:
            settings_idx = i
            settings_pairs = pairs
            break

    # Split the text above the settings line into positive / negative.
    body_lines = lines[:settings_idx] if settings_idx >= 0 else lines
    body = "\n".join(body_lines).strip() if body_lines else ""

    positive = body
    negative = ""
    marker = "Negative prompt:"
    mpos = body.find(marker)
    if mpos != -1:
        positive = body[:mpos].strip()
        negative = body[mpos + len(marker):].strip()

    # If we found neither a settings line NOR a negative-prompt marker,
    # this probably is not an A1111 blob — let the caller try other paths.
    if settings_idx < 0 and mpos == -1:
        return None

    fields = _order_fields(settings_pairs)
    return GenerationMetadata(
        found=True, prompt=positive, negative=negative,
        fields=fields, raw=text)


def _parse_settings_line(line: str) -> list[tuple[str, str]]:
    """Parse one 'Key: value, Key: value, ...' line into pairs.

    Splits on commas that separate pairs, but not commas inside a value
    (A1111 values rarely contain commas except things like Size which use
    'x'; we split on ', ' followed by a 'Word:' pattern to be safe).
    """
    line = line.strip()
    if ":" not in line:
        return []
    import re
    # Split before each "Key:" that follows ", ". Keys are word-ish.
    parts = re.split(r",\s+(?=[A-Za-z][A-Za-z0-9 _\-/]*:\s)", line)
    pairs: list[tuple[str, str]] = []
    for part in parts:
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        k = k.strip()
        v = v.strip()
        if k and v:
            pairs.append((k, v))
    return pairs


def _order_fields(
        pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Reorder parsed settings so the common ones come first, then the
    rest in their original order."""
    by_key = {}
    order = []
    for k, v in pairs:
        by_key[k] = v
        order.append(k)
    result: list[tuple[str, str]] = []
    used = set()
    for k in _A1111_PREFERRED_ORDER:
        if k in by_key and k not in used:
            result.append((k, by_key[k]))
            used.add(k)
    for k in order:
        if k not in used:
            result.append((k, by_key[k]))
            used.add(k)
    return result


def _parse_comfy(info: dict) -> Optional[GenerationMetadata]:
    """Parse ComfyUI metadata from PNG info chunks.

    ComfyUI writes 'workflow' (the editor graph) and/or 'prompt' (the API
    graph) as JSON strings. We do NOT try to render the graph — instead
    we surface any text that looks like a prompt (from CLIPTextEncode-type
    nodes) for readability, and always keep the full JSON as raw so the
    user can inspect everything.
    """
    workflow = info.get("workflow")
    prompt_json = info.get("prompt")
    if not (isinstance(workflow, str) and workflow.strip()) and \
       not (isinstance(prompt_json, str) and prompt_json.strip()):
        return None

    raw_parts = []
    if isinstance(prompt_json, str) and prompt_json.strip():
        raw_parts.append("=== prompt ===\n" + prompt_json.strip())
    if isinstance(workflow, str) and workflow.strip():
        raw_parts.append("=== workflow ===\n" + workflow.strip())
    raw = "\n\n".join(raw_parts)

    # Best-effort: pull text from the API 'prompt' graph. It's a dict of
    # node-id -> {class_type, inputs}. Text lives in inputs['text'] on
    # CLIPTextEncode-ish nodes.
    extracted: list[str] = []
    if isinstance(prompt_json, str):
        try:
            graph = json.loads(prompt_json)
            if isinstance(graph, dict):
                for node in graph.values():
                    if not isinstance(node, dict):
                        continue
                    ctype = str(node.get("class_type", ""))
                    inputs = node.get("inputs", {})
                    if isinstance(inputs, dict):
                        t = inputs.get("text")
                        if isinstance(t, str) and t.strip():
                            extracted.append(f"[{ctype}] {t.strip()}")
        except Exception:
            pass

    md = GenerationMetadata(found=True, source="ComfyUI", raw=raw)
    if extracted:
        # Show extracted prompts in the prompt area (joined), so the user
        # sees the text without reading JSON. We cannot reliably tell
        # positive from negative in an arbitrary graph, so present them
        # together and let raw carry the full detail.
        md.prompt = "\n\n".join(extracted)
        md.note = ("Prompt text extracted from the ComfyUI graph. Positive "
                   "and negative cannot always be told apart in a node "
                   "graph \u2014 see the raw workflow for full detail.")
    else:
        md.note = ("ComfyUI workflow found. It is a node graph rather than "
                   "flat text \u2014 see the raw view for the full "
                   "workflow.")
    return md


def _exif_user_comment(exif) -> str:
    """Extract the UserComment (tag 0x9286) from an EXIF object as text.

    A1111 writes its blob here for JPEG/WebP. UserComment is often
    prefixed with an 8-byte character-code header (e.g. b'UNICODE\\x00')
    and may be UTF-16; handle the common encodings defensively.
    """
    if exif is None:
        return ""
    try:
        # 0x9286 = UserComment. getexif() may not expose it directly; try
        # the IFD too.
        val = None
        try:
            val = exif.get(0x9286)
        except Exception:
            val = None
        if val is None:
            try:
                from PIL.ExifTags import IFD  # type: ignore
                ex = exif.get_ifd(IFD.Exif)
                val = ex.get(0x9286)
            except Exception:
                val = None
        if val is None:
            return ""
        if isinstance(val, str):
            return val
        if isinstance(val, (bytes, bytearray)):
            b = bytes(val)
            # Strip the standard 8-byte type header if present.
            for header, enc in ((b"UNICODE\x00", "utf-16"),
                                 (b"ASCII\x00\x00\x00", "ascii"),
                                 (b"UTF-8\x00\x00\x00", "utf-8")):
                if b.startswith(header):
                    payload = b[len(header):]
                    try:
                        # A1111 UNICODE comments are often UTF-16-BE.
                        if enc == "utf-16":
                            for e in ("utf-16-be", "utf-16-le", "utf-16"):
                                try:
                                    return payload.decode(e).strip("\x00")
                                except Exception:
                                    continue
                        return payload.decode(enc, "replace").strip("\x00")
                    except Exception:
                        return payload.decode("utf-8", "replace").strip("\x00")
            # No header — try UTF-8 then UTF-16.
            for e in ("utf-8", "utf-16-be", "utf-16-le"):
                try:
                    return b.decode(e).strip("\x00")
                except Exception:
                    continue
            return b.decode("utf-8", "replace").strip("\x00")
    except Exception:
        return ""
    return ""
