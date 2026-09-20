"""
tests/test_paths.py

Non-ASCII paths, and what happens when a path gets too long.

Japanese Windows names its own folders in Japanese — デスクトップ,
ドキュメント, ダウンロード — and the user profile can be Japanese too,
so AppData sits under a Japanese path as well. A program that assumes
ASCII paths fails on that machine and nowhere else, which is exactly
the kind of fault that survives testing. Every feature here that
touches the filesystem is driven through a fully Japanese path.

The length half is a SIMULATION and should be read as one. Windows
refuses paths over 260 characters; Linux, where this suite runs, does
not. So the limit is imposed artificially and the question asked is
narrower but still worth pinning: when the filesystem says no, does
the code fail loudly and leave the user's work intact, or does it
corrupt something on the way down?

Run: python3 tests/test_paths.py
"""
import builtins
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The config directory is Japanese too, as it would be on that machine.
_CFG = Path(tempfile.mkdtemp()) / "ユーザー" / "アプリデータ"
_CFG.mkdir(parents=True)
os.environ["TAGWALKER_CONFIG_DIR"] = str(_CFG)

from PIL import Image
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import bookmarks as bmk
from core import danbooru_api as dapi
from core import dataset_marker as marker
from core import tag_io
from core.scanner import scan
from core.state import SessionState
from ui.tag_reference_window import TagReferenceWindow
from ui.token_counter_dialog import TokenCounterDialog

QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes
QMessageBox.information = lambda *a, **k: None
QMessageBox.warning = lambda *a, **k: None

WINDOWS_MAX_PATH = 260
_JAPANESE_NAMES = ["女の子_001", "猫耳　少女", "ＡＢＣ全角",
                   "画像 with spaces"]
_LONG_CAPTION = ", ".join(f"tag_{i}" for i in range(40))


class _FakeFetcher:
    def __init__(self, routes):
        self.routes = dict(routes)

    def get(self, url, cb):
        r = self.routes.get(url)
        cb(None, 404) if r is None else cb(r[0], r[1])


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return buf.getvalue()


def _japanese_dataset() -> Path:
    root = (Path(tempfile.mkdtemp()) / "ユーザー" / "デスクトップ"
            / "学習データ" / "キャラクター（１）")
    root.mkdir(parents=True)
    for name in _JAPANESE_NAMES:
        Image.new("RGB", (8, 8)).save(root / f"{name}.png")
        (root / f"{name}.txt").write_text(
            "女の子, 猫耳, " + _LONG_CAPTION, encoding="utf-8")
    return root


def test_a_fully_japanese_path() -> None:
    root = _japanese_dataset()
    state = SessionState(scan(root))
    assert len(state.all_images) == len(_JAPANESE_NAMES)
    assert "女の子" in state.all_tags

    entry = state.all_images[0]
    tags = tag_io.read_tags(entry.txt_path)
    tag_io.write_tags(entry.txt_path, tags + ["追加タグ"])
    assert "追加タグ" in tag_io.read_tags(entry.txt_path)

    settings = Settings()
    initialize_theme(settings.theme)
    settings.danbooru_block_custom = "猫耳\nguro\n"
    assert "猫耳" in Settings().danbooru_block_custom
    bmk._STORES.clear()
    store = bmk.store_for(settings)
    store.add("tag", "猫耳", "猫耳")
    saved = Path(settings.bookmarks_path).read_text(encoding="utf-8")
    assert "猫耳" in saved          # readable, not \u-escaped
    print("OK: scanning, caption editing, settings and bookmarks all "
          "work under a fully Japanese path")


def test_japanese_paths_through_the_writing_features() -> None:
    root = _japanese_dataset()
    settings = Settings()
    initialize_theme(settings.theme)

    pairs = [(root / f"{n}.png", root / f"{n}.txt")
             for n in _JAPANESE_NAMES]
    result = marker.apply_plan(
        marker.plan_marks(pairs, {p for p, _t in pairs}))
    assert result.marked == len(_JAPANESE_NAMES)
    assert not result.skipped
    assert all((root / f"!{n}.png").exists()
               and (root / f"!{n}.txt").exists()
               for n in _JAPANESE_NAMES)
    marker.apply_plan(marker.plan_marks(
        [(root / f"!{n}.png", root / f"!{n}.txt")
         for n in _JAPANESE_NAMES], set()))
    assert all((root / f"{n}.png").exists() for n in _JAPANESE_NAMES)

    out = Path(tempfile.mkdtemp()) / "出力フォルダ"
    out.mkdir(parents=True)
    settings.danbooru_export_dir = str(out)
    settings.danbooru_block_custom = ""      # nothing in the way
    post = dapi.parse_posts([{
        "id": 5, "preview_file_url": "https://x/p",
        "large_file_url": "https://x/l.png", "file_url": "https://x/o.png",
        "file_ext": "png", "rating": "g",
        "tag_string": "女の子 制服", "tag_count": 2}])[0]
    win = TagReferenceWindow(
        settings, fetcher=_FakeFetcher({"https://x/l.png": (_png(), 200)}))
    win.show()
    win.open_inspector(post)
    win._inspectors[-1]._export()
    assert sorted(p.name for p in out.iterdir()) == [
        "danbooru_5.png", "danbooru_5.txt"]
    assert "女の子" in (out / "danbooru_5.txt").read_text(encoding="utf-8")

    class _Main(QWidget):
        def _start_scan(self, roots):
            pass

    dlg = TokenCounterDialog(SessionState(scan(root)), _Main())
    assert len(dlg._paths) == len(_JAPANESE_NAMES)
    dlg._do_marks(True)
    assert any(p.name.startswith("!") for p in root.iterdir())
    print("OK: renaming, exporting and token-counter marking all "
          "operate on Japanese paths and preserve Japanese content")


def test_over_long_paths_fail_safely() -> None:
    """SIMULATED. Windows rejects paths over 260 characters; Linux
    does not, so the limit is imposed here artificially. The claim
    being tested is not "long paths work" but "when the filesystem
    refuses, nothing is corrupted on the way down"."""
    original = "女の子, 猫耳, 制服"
    root = Path(tempfile.mkdtemp())
    real_open, real_rename = builtins.open, os.rename

    def refuse_open(file, *a, **kw):
        if len(str(file)) > WINDOWS_MAX_PATH:
            raise OSError(3, "The system cannot find the path specified")
        return real_open(file, *a, **kw)

    def refuse_rename(src, dst):
        if len(str(dst)) > WINDOWS_MAX_PATH:
            raise OSError(3, "The system cannot find the path specified")
        return real_rename(src, dst)

    # A short filename in a deep folder: the target fits, but the
    # atomic write's temporary name does not.
    deep = root
    while len(str(deep)) < WINDOWS_MAX_PATH - 20:
        deep = deep / "sub"
    deep.mkdir(parents=True, exist_ok=True)
    caption = deep / "c.txt"
    caption.write_text(original, encoding="utf-8")

    builtins.open = refuse_open
    raised = None
    try:
        tag_io.write_tags(caption, ["a", "b"])
    except OSError as exc:
        raised = exc
    finally:
        builtins.open = real_open
    assert raised is not None                     # loud, not silent
    assert caption.read_text(encoding="utf-8") == original
    assert not any(f.name.startswith(".tw_tmp")
                   for f in deep.iterdir())       # no litter

    # The marker adds one character, which is enough to tip a pair
    # over. It must refuse rather than rename half of it.
    deep2 = root / "x"
    while len(str(deep2)) < WINDOWS_MAX_PATH - 7:
        deep2 = deep2 / "y"
    deep2.mkdir(parents=True, exist_ok=True)
    image, text = deep2 / "i.png", deep2 / "i.txt"
    image.write_bytes(b"IMG")
    text.write_text(original, encoding="utf-8")

    os.rename = refuse_rename
    try:
        result = marker.apply_plan(
            marker.plan_marks([(image, text)], {image}))
    finally:
        os.rename = real_rename

    old_pair = image.exists() and text.exists()
    new_pair = ((deep2 / "!i.png").exists()
                and (deep2 / "!i.txt").exists())
    assert old_pair != new_pair and (old_pair or new_pair)
    live = text if text.exists() else deep2 / "!i.txt"
    assert live.read_text(encoding="utf-8") == original
    assert result.marked == 1 or result.skipped   # reported either way
    print("OK: when a path is too long the write fails loudly with the "
          "original intact and no temp litter, and the marker refuses "
          "rather than half-renaming a pair")


def test_awkward_filename_shapes() -> None:
    folder = Path(tempfile.mkdtemp())
    shapes = ["trailing space ", "dots...", "UPPER_lower",
              "emoji_\U0001F3B4_tag", "a" * 120]
    made = []
    for name in shapes:
        try:
            Image.new("RGB", (8, 8)).save(folder / f"{name}.png")
            (folder / f"{name}.txt").write_text("1girl, solo",
                                                encoding="utf-8")
            made.append(name)
        except OSError:
            pass          # the host filesystem may refuse some
    state = SessionState(scan(folder))
    assert len(state.all_images) == len(made)

    pairs = [(folder / f"{n}.png", folder / f"{n}.txt") for n in made]
    marker.apply_plan(marker.plan_marks(pairs, {p for p, _t in pairs}))
    for name in made:
        # Whatever happened, the image and its caption agree.
        assert ((folder / f"!{name}.png").exists()
                == (folder / f"!{name}.txt").exists())
    print("OK: spaces, dots, mixed case, emoji and very long names "
          "never leave an image and its caption disagreeing")


def run() -> None:
    test_a_fully_japanese_path()
    test_japanese_paths_through_the_writing_features()
    test_over_long_paths_fail_safely()
    test_awkward_filename_shapes()
    print("\nALL PASS: paths")


if __name__ == "__main__":
    run()
