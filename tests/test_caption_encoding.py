"""
tests/test_caption_encoding.py

How caption files are decoded, and what happens when they are written
back.

Found during an audit rather than reported, but the consequence was
the worst kind: silent, permanent loss of the user's own work.

Windows Notepad's ANSI mode writes CP932 on a Japanese system. Reading
such a file with errors="replace" turned every Japanese tag into
U+FFFD, and the next tag edit wrote that back as UTF-8 — destroying
the original with no warning and no way to recover it. The publisher
of this program is Japanese and works on Windows.

The second bug was quieter: tags were split on commas only, and
str.strip removes leading and trailing whitespace but not the middle,
so a caption wrapped across lines produced one tag containing a line
break rather than two tags.

Run: python3 tests/test_caption_encoding.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tag_io


def _write(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


def test_every_encoding_a_caption_arrives_in() -> None:
    d = Path(tempfile.mkdtemp())
    cases = [
        ("utf8", "女の子, 猫耳".encode("utf-8"), ["女の子", "猫耳"]),
        ("utf8bom", b"\xef\xbb\xbf1girl, solo", ["1girl", "solo"]),
        # Windows Notepad, ANSI mode, Japanese system.
        ("sjis", "女の子, 猫耳, 制服".encode("cp932"),
         ["女の子", "猫耳", "制服"]),
        ("u16le", b"\xff\xfe" + "1girl, 猫耳".encode("utf-16-le"),
         ["1girl", "猫耳"]),
        ("u16be", b"\xfe\xff" + "1girl, 猫耳".encode("utf-16-be"),
         ["1girl", "猫耳"]),
    ]
    for name, data, expected in cases:
        assert tag_io.read_tags(_write(d, f"{name}.txt", data)) == \
            expected, name

    # Valid UTF-8 must always win: the fallbacks only see bytes UTF-8
    # has already rejected, or a byte-order mark says otherwise.
    _text, encoding = tag_io.decode_caption(
        _write(d, "plain.txt", "女の子".encode("utf-8")))
    assert encoding == "utf-8-sig"
    _text, encoding = tag_io.decode_caption(d / "sjis.txt")
    assert encoding == "cp932"
    print("OK: captions decode from UTF-8, CP932 and UTF-16 with or "
          "without a byte-order mark, and valid UTF-8 always wins")


def test_a_japanese_caption_survives_being_edited() -> None:
    """The actual data-loss path: read lossily, then write."""
    d = Path(tempfile.mkdtemp())
    path = _write(d, "jp.txt", "女の子, 猫耳, 制服".encode("cp932"))
    tags = tag_io.read_tags(path)
    assert tags == ["女の子", "猫耳", "制服"]

    tag_io.write_tags(path, tags + ["1girl"])
    assert path.read_text(encoding="utf-8").strip() == \
        "女の子, 猫耳, 制服, 1girl"

    # And if a decode genuinely failed, writing is refused rather than
    # making the damage permanent.
    try:
        tag_io.write_tags(path, ["ok", "bad\ufffdtag"])
        raise AssertionError("should have refused")
    except ValueError as exc:
        assert "could not be decoded" in str(exc)
    print("OK: a Shift-JIS caption survives an edit intact, and a "
          "failed decode is never written back over the original")


def test_newlines_separate_tags() -> None:
    d = Path(tempfile.mkdtemp())
    wrapped = _write(d, "wrap.txt",
                     "1girl, solo\nsmile, happy\n".encode("utf-8"))
    assert tag_io.read_tags(wrapped) == [
        "1girl", "solo", "smile", "happy"]
    crlf = _write(d, "crlf.txt", b"1girl, solo\r\nsmile\r\n")
    assert tag_io.read_tags(crlf) == ["1girl", "solo", "smile"]
    # No tag may carry a line break or a stray byte-order mark.
    for path in (wrapped, crlf):
        for tag in tag_io.read_tags(path):
            assert "\n" not in tag and "\r" not in tag
            assert "\ufeff" not in tag
    print("OK: a caption wrapped across lines yields separate tags "
          "rather than one containing a line break")


def run() -> None:
    test_every_encoding_a_caption_arrives_in()
    test_a_japanese_caption_survives_being_edited()
    test_newlines_separate_tags()
    print("\nALL PASS: caption encoding")


if __name__ == "__main__":
    run()
