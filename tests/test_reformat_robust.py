"""Tests for the reformat apply rewrite: each file written ONCE, transient
write failures retried, persistent failures reported (not silently dropped).

These cover the bug where the old tag-by-tag approach rewrote each caption
file many times, provoking transient Windows file locks that silently
dropped a single tag.

Run: python3 tests/test_reformat_robust.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image

from core.state import SessionState
from core.scanner import scan
from core import tag_io


def _dataset(images):
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for name, tags in images:
        Image.new("RGB", (8, 8)).save(d / f"{name}.png")
        (d / f"{name}.txt").write_text(", ".join(tags), encoding="utf-8")
    return d


def test_single_write_per_file() -> None:
    d = _dataset([
        ("a", ["long_hair", "blue_eyes", "short_sleeves", "closed_mouth"]),
        ("b", ["long_hair", "red_eyes", "open_mouth"]),
        ("c", ["very_long_hair", "twin_braids"]),
    ])
    st = SessionState(scan(d))

    writes: dict = {}
    real = tag_io.write_tags

    def spy(path, tags):
        writes[Path(path).name] = writes.get(Path(path).name, 0) + 1
        return real(path, tags)

    tag_io.write_tags = spy
    try:
        res = st.apply_tag_rename_map(st.build_reformat_map(to_spaces=True))
    finally:
        tag_io.write_tags = real

    # Every changed file written EXACTLY once, despite multiple converting tags.
    for name in ("a.txt", "b.txt", "c.txt"):
        assert writes.get(name) == 1, f"{name} written {writes.get(name)} times (want 1)"
    # And everything actually converted.
    ap = {p.name: p for p in st._image_tags}
    assert st._image_tags[ap["a.png"]] == ["long hair", "blue eyes", "short sleeves", "closed mouth"]
    assert res["failed"] == [], res
    assert res["images"] == 3, res
    print("OK: each caption file is rewritten exactly once, all tags converted")


def test_persistent_failure_is_reported() -> None:
    d = _dataset([
        ("a", ["long_hair", "blue_eyes"]),
        ("b", ["long_hair", "red_eyes"]),
        ("c", ["short_hair", "green_eyes"]),
    ])
    st = SessionState(scan(d))
    real = tag_io.write_tags

    def spy(path, tags):
        if Path(path).name == "b.txt":
            raise OSError("simulated lock on b.txt")
        return real(path, tags)

    tag_io.write_tags = spy
    try:
        res = st.apply_tag_rename_map(st.build_reformat_map(to_spaces=True))
    finally:
        tag_io.write_tags = real

    # b.txt is reported, not silently dropped.
    assert "b.txt" in res["failed"], res
    ap = {p.name: p for p in st._image_tags}
    # a and c converted; b left entirely unchanged (memory matches disk).
    assert st._image_tags[ap["a.png"]] == ["long hair", "blue eyes"]
    assert st._image_tags[ap["c.png"]] == ["short hair", "green eyes"]
    assert st._image_tags[ap["b.png"]] == ["long_hair", "red_eyes"], st._image_tags[ap["b.png"]]
    # The on-disk b.txt is also unchanged.
    assert "long_hair" in (d / "b.txt").read_text(encoding="utf-8")
    # Undo still cleanly reverses the parts that did apply.
    assert st.undo() is True
    assert st._image_tags[ap["a.png"]] == ["long_hair", "blue_eyes"]
    print("OK: a persistently-locked file is reported in 'failed', others convert, undo holds")


def test_transient_failure_retried() -> None:
    d = _dataset([("a", ["long_hair", "blue_eyes"]), ("b", ["short_hair"])])
    st = SessionState(scan(d))
    real = tag_io.write_tags
    fail_once = {"a.txt": True}

    def spy(path, tags):
        n = Path(path).name
        if fail_once.get(n):
            fail_once[n] = False
            raise OSError("transient")
        return real(path, tags)

    tag_io.write_tags = spy
    try:
        res = st.apply_tag_rename_map(st.build_reformat_map(to_spaces=True))
    finally:
        tag_io.write_tags = real

    # The retry rode over the one transient failure: nothing reported.
    assert res["failed"] == [], res
    ap = {p.name: p for p in st._image_tags}
    assert st._image_tags[ap["a.png"]] == ["long hair", "blue eyes"], st._image_tags[ap["a.png"]]
    print("OK: a transient write failure is retried and recovers (not reported)")


def run() -> None:
    test_single_write_per_file()
    test_persistent_failure_is_reported()
    test_transient_failure_retried()
    print("\nALL PASS: reformat robustness (one write/file, retry, report)")


if __name__ == "__main__":
    run()
