"""Tests for the bulk tag-reformat engine (underscore <-> space).

The engine reuses rename_tag_globally per tag, coalesced into ONE undo
step. The properties that matter for a dataset-wide rewrite:

  * Per-tag transform: comma+space separators between tags are NEVER
    touched, only characters inside each tag.
  * Merge + dedup: converting a tag into one that already exists merges
    them, and an image carrying both forms ends with a single tag.
  * One undo step reverses the entire reformat.
  * Counts/tree stay correct (merged tag's count combines).
  * Preview reports without mutating anything.

Run: python3 tests/test_tag_reformat.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from core.state import SessionState
from core.scanner import scan
import core.tag_io as tag_io


def _dataset(images):
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for name, tags in images:
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")
    return d


def _raw(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def test_underscores_to_spaces() -> None:
    d = _dataset([
        ("a", ["black_elbow_gloves", "blue_eyes"]),
        ("b", ["blue_eyes", "blue eyes"]),   # both forms -> merge+dedup
        ("c", ["long hair"]),                # already spaced -> untouched
        ("d", ["red_hair"]),
    ])
    st = SessionState(scan(d))
    by_name = {p.name: p for p in st._image_tags}

    rmap = st.build_reformat_map(to_spaces=True)
    assert set(rmap.keys()) == {"black_elbow_gloves", "blue_eyes", "red_hair"}, rmap
    assert rmap["black_elbow_gloves"] == "black elbow gloves"

    prev = st.preview_tag_rename_map(rmap)
    assert prev["tags"] == 3, prev
    assert prev["images"] == 3, prev  # a, b, d (not c)

    res = st.apply_tag_rename_map(rmap)
    assert res["tags"] == 3 and res["images"] == 3 and res["failed"] == [], res

    a = [p for p in st._image_tags if p.name == "a.png"][0]
    b = [p for p in st._image_tags if p.name == "b.png"][0]
    c = [p for p in st._image_tags if p.name == "c.png"][0]
    dd = [p for p in st._image_tags if p.name == "d.png"][0]

    assert st._image_tags[a] == ["black elbow gloves", "blue eyes"], st._image_tags[a]
    # b had both forms -> single deduped tag.
    assert st._image_tags[b] == ["blue eyes"], st._image_tags[b]
    assert st._image_tags[c] == ["long hair"], "spaced tag untouched"
    assert st._image_tags[dd] == ["red hair"], st._image_tags[dd]

    # Separator integrity: the raw file keeps ", " between tags and has
    # NO underscores left.
    assert _raw(a.with_suffix(".txt")) == "black elbow gloves, blue eyes"
    assert "_" not in _raw(a.with_suffix(".txt"))

    # Merge: the underscored tag is gone; the spaced one carries both images.
    assert "blue_eyes" not in st._tag_counts
    assert st._tag_counts.get("blue eyes") == 2  # images a and b

    # ONE undo step reverses everything.
    assert len(st._undo_stack) == 1, f"reformat is one undo step, got {len(st._undo_stack)}"
    assert st.undo() is True
    assert st._image_tags[a] == ["black_elbow_gloves", "blue_eyes"]
    assert st._image_tags[b] == ["blue_eyes", "blue eyes"]
    assert st._image_tags[dd] == ["red_hair"]
    assert st._tag_counts.get("blue_eyes") == 2
    print("OK: _ -> space converts per-tag, merges+dedups, one-step undo restores")


def test_spaces_to_underscores_roundtrip() -> None:
    # Clean dataset (no pre-existing collisions) so the round-trip is exact.
    d = _dataset([
        ("a", ["black_elbow_gloves", "blue_eyes"]),
        ("b", ["red_hair", "short_hair"]),
    ])
    st = SessionState(scan(d))
    a = [p for p in st._image_tags if p.name == "a.png"][0]

    st.apply_tag_rename_map(st.build_reformat_map(to_spaces=True))
    assert st._image_tags[a] == ["black elbow gloves", "blue eyes"]

    # Now convert back: spaces -> underscores. All 4 distinct multi-word
    # tags convert (a has 2, b has 2).
    res = st.apply_tag_rename_map(st.build_reformat_map(to_spaces=False))
    assert res["tags"] == 4, res
    assert st._image_tags[a] == ["black_elbow_gloves", "blue_eyes"], st._image_tags[a]
    print("OK: space -> _ round-trips cleanly on collision-free tags")


def test_preview_does_not_mutate() -> None:
    d = _dataset([("a", ["black_elbow_gloves"])])
    st = SessionState(scan(d))
    a = [p for p in st._image_tags if p.name == "a.png"][0]
    before = list(st._image_tags[a])
    rmap = st.build_reformat_map(to_spaces=True)
    st.preview_tag_rename_map(rmap)
    assert st._image_tags[a] == before, "preview must not change anything"
    assert len(st._undo_stack) == 0, "preview must not push undo"
    print("OK: preview is read-only")


def test_noop_when_nothing_matches() -> None:
    d = _dataset([("a", ["long hair", "blue eyes"])])  # already all-spaces
    st = SessionState(scan(d))
    rmap = st.build_reformat_map(to_spaces=True)
    assert rmap == {}, rmap
    res = st.apply_tag_rename_map(rmap)
    assert res["tags"] == 0 and res["images"] == 0, res
    assert len(st._undo_stack) == 0
    print("OK: no-op when no tag needs the conversion")


def run() -> None:
    test_underscores_to_spaces()
    test_spaces_to_underscores_roundtrip()
    test_preview_does_not_mutate()
    test_noop_when_nothing_matches()
    print("\nALL PASS: tag reformat engine")


if __name__ == "__main__":
    run()
