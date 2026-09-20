"""
tests/test_caption_format_agnostic.py

Captions written with spaces must work as well as captions written
with underscores.

FIELD REPORT: "the tag audit can't be used with spaced tagging format
— all captions come back 'not a known Danbooru tag'." True, and worse
than reported: the audit was one of two tools affected. Co-occurrence
returned nothing at all for a spaced tag, so the hints shown during
the walk and the entire comparison panel in the statistics window went
silently empty rather than visibly wrong.

Both formats are supported ON PURPOSE — the program ships a bulk
underscore<->space reformat tool with an emoticon guard. Reference
data is stored with underscores because Danbooru stores it that way,
so every lookup has to fold. Three of five already did; two did not.

Run: python3 tests/test_caption_format_agnostic.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from core import cooccurrence, tag_database, tag_reference

# Ordinary multi-word tags, in both styles.
PAIRS = [
    ("long_hair", "long hair"),
    ("blue_eyes", "blue eyes"),
    ("cat_ears", "cat ears"),
    ("looking_at_viewer", "looking at viewer"),
]


def test_every_reference_lookup_folds_spaces() -> None:
    tag_reference.ensure_loaded()

    def era_count(tag: str):
        info = tag_reference.lookup(tag)
        return next((e.count for e in info.era_counts
                     if e.key == "mid2024"), None)

    def partners(tag: str) -> int:
        return len(cooccurrence.get_db().get_cooccurring(tag, limit=5))

    for underscored, spaced in PAIRS:
        assert era_count(underscored) == era_count(spaced), underscored
        # This one returned 0 for the spaced form, so the walk hints
        # and the statistics comparison were empty rather than wrong —
        # the kind of failure nobody reports as a bug.
        assert partners(underscored) == partners(spaced), underscored
        assert (tag_database.bucket_for(underscored)
                == tag_database.bucket_for(spaced)), underscored
    print("OK: era counts, co-occurrence and tag classification agree "
          "whichever way the caption is written")


def test_the_audit_accepts_spaced_captions() -> None:
    db = tag_database.get_database()
    db.ensure_loaded()
    categories = set(tag_database.CATEGORY_NAMES.keys())

    for underscored, spaced in PAIRS:
        assert db.classify(underscored, categories)[0] == "valid"
        assert db.classify(spaced, categories)[0] == "valid", spaced
    # Case is folded too, and a genuinely invented tag stays unknown —
    # the audit must not become permissive in the process of becoming
    # format-agnostic.
    assert db.classify("Long Hair", categories)[0] == "valid"
    assert db.classify("zzq_invented_token", categories)[0] == "unknown"
    assert db.classify("zzq invented token", categories)[0] == "unknown"
    print("OK: the audit accepts spaced captions without accepting "
          "tags that are genuinely not real")


def test_a_replacement_is_written_in_the_captions_own_style() -> None:
    """Handing a space-formatted dataset an underscored replacement
    would leave one tag written unlike every other — the very
    inconsistency the audit exists to remove."""
    db = tag_database.get_database()
    db.ensure_loaded()
    categories = set(tag_database.CATEGORY_NAMES.keys())

    verdict, suggestion, _cat = db.classify("dark skinned female",
                                            categories)
    assert verdict == "alias"
    assert suggestion == "dark skin"          # spaced in, spaced out

    verdict, suggestion, _cat = db.classify("dark_skinned_female",
                                            categories)
    assert verdict == "alias"
    assert suggestion == "dark_skin"          # underscored stays so

    # A suggestion with nothing to convert is returned untouched.
    _v, suggestion, _c = db.classify("sole female", categories)
    assert suggestion == "1girl"

    # Emoticon underscores are structural, not word separators, and
    # must survive: "^_^" must never become "^ ^".
    from core.reformat_guard import is_protected_tag
    assert is_protected_tag("^_^")
    print("OK: a suggested replacement matches the style of the tag it "
          "replaces, and emoticons are left alone")


def test_every_feature_agrees_across_both_formats() -> None:
    """The same dataset written both ways must produce identical
    results everywhere.

    Checking the lookups one at a time missed a case: the statistics
    comparison held the user's partner counts keyed by their own
    spelling and Danbooru's keyed by underscore, and compared them
    literally. A caption reading "blue eyes" was reported as paired
    far MORE than Danbooru (100% vs 0%) and far LESS (0% vs 30%) — the
    same tag in both panes, contradicting itself.

    So this builds one dataset twice and compares the whole surface,
    which is the only way to be sure.
    """
    import random

    from PIL import Image
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])

    from core import clip_token_counter as ctc
    from core import dataset_stats as ds
    from core import prewarm
    from core import prune_advisor as pa
    from core.cooccurrence import get_db
    from core.scanner import scan
    from core.state import SessionState

    prewarm.reset_for_tests()
    prewarm.run_all()

    vocabulary = ["1girl", "long_hair", "blue_eyes", "cat_ears",
                  "looking_at_viewer", "school_uniform",
                  "zzq_trigger", "solo", "thighhighs"]

    def build(spaced: bool) -> SessionState:
        folder = Path(tempfile.mkdtemp()) / "ds"
        folder.mkdir(parents=True)
        random.seed(4)
        for i in range(120):
            tags = list(vocabulary) if i % 3 else vocabulary[:5]
            if spaced:
                tags = [t.replace("_", " ") for t in tags]
            Image.new("RGB", (24, 24)).save(folder / f"i{i:03d}.png")
            (folder / f"i{i:03d}.txt").write_text(", ".join(tags),
                                                  encoding="utf-8")
        return SessionState(scan(folder))

    def posts(tag: str) -> int:
        info = tag_reference.lookup(tag)
        return next((e.count for e in info.era_counts
                     if e.key == "mid2024"), 0) or 0

    def measure(spaced: bool) -> dict:
        state = build(spaced)
        out: dict = {}
        tag_images: dict = {}
        for entry in state.all_images:
            for tag in state.get_image_tags(entry.image_path):
                tag_images.setdefault(tag, set()).add(entry.image_path)
        report = pa.analyse(
            tag_images, len(state.all_images), set(),
            lambda t: "general", lambda t: 3, ["character"],
            base_model_count=posts)
        out["prune"] = (len(report.tier1), len(report.tier2),
                        len(report.tier3))

        stats = ds.analyse(
            {e: state.get_image_tags(e.image_path)
             for e in state.all_images},
            ctc.count_tokens, base_model_count=posts)
        out["vocabulary"] = tuple(b.count for b in stats.vocabulary)
        out["captions"] = tuple(c for _l, c in stats.caption_pairs())
        out["density"] = tuple(c for _l, c in stats.density_pairs())

        subject = "long hair" if spaced else "long_hair"
        counts: dict = {}
        carrying = 0
        for entry in state.all_images:
            tags = state.get_image_tags(entry.image_path)
            if subject not in tags:
                continue
            carrying += 1
            for other in tags:
                if other != subject:
                    counts[other] = counts.get(other, 0) + 1
        danbooru = [(n, p) for n, _o, p
                    in get_db().get_cooccurring(subject, limit=30)]
        you_more, they_more = ds.compare_with_danbooru(
            counts, carrying, danbooru)
        out["divergence"] = (len(you_more), len(they_more))
        # The same tag must never appear in both panes.
        assert not ({d.partner for d in you_more}
                    & {d.partner for d in they_more})

        db = tag_database.get_database()
        db.ensure_loaded()
        categories = set(tag_database.CATEGORY_NAMES.keys())
        out["unknown"] = sum(1 for t in state.all_tags
                             if db.classify(t, categories)[0] == "unknown")
        out["hints"] = len(state.get_cooccurrence(
            subject, limit=10, too_common_pct=100)[0])
        return out

    underscored, spaced = measure(False), measure(True)
    for key in underscored:
        assert underscored[key] == spaced[key], (
            f"{key}: {underscored[key]} underscored vs "
            f"{spaced[key]} spaced")
    print("OK: pruning, vocabulary, caption length, density, "
          "divergence, audit and hints all agree whichever way the "
          "captions are written")


def run() -> None:
    test_every_reference_lookup_folds_spaces()
    test_the_audit_accepts_spaced_captions()
    test_a_replacement_is_written_in_the_captions_own_style()
    test_every_feature_agrees_across_both_formats()
    print("\nALL PASS: caption format agnostic")


if __name__ == "__main__":
    run()
