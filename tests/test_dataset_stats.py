"""
tests/test_dataset_stats.py

The statistics window's Dataset health tab.

The other tabs report on the WALK — completion percentage, decisions
made, tags finished. Useful, but that is "how far through the work am
I", which the progress bar already says. This tab answers the question
that decides whether training succeeds: is the data any good.

Everything here is a count of something real. No estimates and no
predictions — a training predictor was considered and rejected,
because final quality depends on learning rate, optimiser, scheduler
and rank, and a confident wrong number is worse than no number.

Run: python3 tests/test_dataset_stats.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import dataset_stats as dstats
from core import prewarm
from core.scanner import scan, scan_many
from core.state import SessionState
from ui.stats_window import StatsWindow, _folder_of, _repeats_from_folder_name


def _settings() -> Settings:
    s = Settings()
    initialize_theme(s.theme)
    return s


def _dataset(specs, many: bool = False) -> SessionState:
    base = Path(tempfile.mkdtemp())
    roots = []
    for name, count in specs:
        folder = base / name
        folder.mkdir(parents=True)
        roots.append(folder)
        for i in range(count):
            Image.new("RGB", (24, 24)).save(folder / f"i{i:03d}.png")
            (folder / f"i{i:03d}.txt").write_text(
                "1girl, solo, long_hair", encoding="utf-8")
    return SessionState(scan_many(roots) if many else scan(roots[0]))


def test_every_image_and_tag_is_counted_once() -> None:
    """The bands partition the data — if they did not, the charts
    would be quietly lying about proportions."""
    random.seed(3)
    tags = {}
    for i in range(100):
        row = ["1girl", "solo"]
        if i < 10:
            row.append(f"rare_{i}")           # one image each
        row += [f"filler_{j}" for j in range(i % 30)]
        tags[f"img{i}"] = row

    known = {"1girl": 5_889_398, "solo": 4_904_995}
    stats = dstats.analyse(
        tags, lambda text: len(text.split(",")) * 3,
        base_model_count=lambda t: known.get(t, 0),
        era_label="Illustrious", limit=225)

    assert sum(c for _l, c in stats.caption_pairs()) == 100
    assert sum(c for _l, c in stats.density_pairs()) == 100
    assert sum(c for _l, c in stats.frequency_pairs()) == stats.total_tags
    assert sum(c for _l, c in stats.vocabulary_pairs()) == stats.total_tags
    assert stats.rare_tags >= 10
    assert stats.vocabulary[0].count == 2      # the two the model knows
    assert stats.thinnest < stats.fattest
    # Nothing may explode on an empty dataset.
    assert dstats.analyse({}, lambda t: 0).total_images == 0
    print("OK: caption length, density, frequency and vocabulary bands "
          "each partition the data exactly once")


def test_repeats_are_read_from_folder_names() -> None:
    class _Entry:
        def __init__(self, folder):
            self.subfolder = folder
            self.image_path = Path("/tmp") / folder / "a.png"

    for name, expected in (("5_character", 5), ("1_style", 1),
                           ("plain", 1), ("10_concept", 10)):
        assert _repeats_from_folder_name(_Entry(name)) == expected, name
    print("OK: kohya N_ repeat prefixes are parsed, and a folder "
          "without one counts as 1")


def test_the_folder_panel_appears_only_when_it_says_something() -> None:
    """A chart that is always present and usually blank reads as
    broken. With one folder and no repeats there is nothing to
    compare, so the panel is not drawn at all.

    The folder key comes from the caller because the two ways of
    loading put it in different places: one root with subfolders
    records them on the entry, while several roots loaded together are
    each scanned root-only and are told apart by their own directory
    name. Reading only the first showed nothing for the multi-folder
    case — precisely the case this panel exists for.
    """
    prewarm.reset_for_tests()
    prewarm.run_all()
    s = _settings()

    def panel(state):
        window = StatsWindow()
        window._settings = s
        window.attach(state)
        window.show()
        return window

    multi = panel(_dataset([("5_character", 20), ("1_style", 40)],
                           many=True))
    # isHidden, not isVisible: the tab is not the active one, so
    # isVisible is False for everything on it regardless.
    assert not multi._ds_folder_section.isHidden()
    assert dict(multi._ds_folder_chart._data) == {
        "5_character": 100, "1_style": 40}      # images x repeats

    plain = panel(_dataset([("plain", 10)]))
    assert plain._ds_folder_section.isHidden()

    # One folder is still worth showing if it carries a repeat count.
    repeated = panel(_dataset([("7_concept", 10)]))
    assert not repeated._ds_folder_section.isHidden()
    assert dict(repeated._ds_folder_chart._data) == {"7_concept": 70}
    print("OK: the folder panel is drawn for multiple folders or a "
          "repeat count, and omitted when it would be a single bar")


def test_the_tab_reports_what_it_found() -> None:
    prewarm.reset_for_tests()
    prewarm.run_all()
    s = _settings()
    random.seed(11)
    vocab = [f"tag_{i:03d}" for i in range(120)]
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    for i in range(200):
        Image.new("RGB", (24, 24)).save(folder / f"i{i:04d}.png")
        count = random.choice([4, 10, 18, 30, 45])
        (folder / f"i{i:04d}.txt").write_text(
            ", ".join(["1girl", "solo", "long_hair"]
                      + random.sample(vocab, count)), encoding="utf-8")

    window = StatsWindow()
    window._settings = s
    window.attach(SessionState(scan(folder)))
    window.show()

    labels = [window._tabs.tabText(i)
              for i in range(window._tabs.count())]
    assert "Dataset health" in labels

    # Charts have data, and the notes say what the numbers mean.
    assert sum(v for _l, v in window._ds_caption_chart._data) == 200
    assert sum(v for _l, v in window._ds_density_chart._data) == 200
    assert window._ds_freq_chart._data
    assert window._ds_vocab_bar._data
    assert "token" in window._ds_caption_note.text()
    assert "tags per image" in window._ds_density_note.text()
    assert "distinct tags" in window._ds_freq_note.text()
    # The era is named, so the vocabulary figure can be interpreted.
    assert "Illustrious" in window._ds_vocab_note.text()
    print("OK: the tab draws every chart from the loaded dataset and "
          "explains each number in words")


def test_cooccurrence_compares_instead_of_listing_rare_tags() -> None:
    """FIELD REPORT: "least often appears with" was noise. It was noise
    BY CONSTRUCTION — the bottom of a long tail is arbitrary, so the
    pane listed whichever rare tags happened to land there.

    The useful question is not which partners are rare, but which ones
    your captions treat differently from the site the base model
    learned on."""
    from core.dataset_stats import compare_with_danbooru

    # 100 images carry the subject tag.
    counts = {"apron": 95, "maid_headdress": 50, "zzq_trigger": 100,
              "smile": 40, "rare": 2}
    danbooru = [("apron", 0.37), ("maid_headdress", 0.46),
                ("smile", 0.55), ("thighhighs", 0.60)]
    you_more, they_more = compare_with_danbooru(counts, 100, danbooru)

    # Your own token is absent from their list entirely — the
    # strongest form of "you pair this more".
    trigger = next(d for d in you_more if d.partner == "zzq_trigger")
    assert trigger.unlisted
    # Labels must survive a narrow, non-resizable pane. The first
    # version read "(not on their list)" and was cut off mid-word in
    # the real window.
    assert len(trigger.label()) <= 32, trigger.label()

    apron = next(d for d in you_more if d.partner == "apron")
    assert apron.gap == 58                      # 95% against 37%
    assert "95" in apron.label() and "37" in apron.label()
    assert len(apron.label()) <= 32

    assert any(d.partner == "thighhighs" and d.gap == 60
               for d in they_more)
    # A partner on two images is counting noise, not a finding.
    assert not any(d.partner == "rare" for d in you_more)
    assert compare_with_danbooru({}, 0, []) == ([], [])
    print("OK: co-occurrence reports where your captions and "
          "Danbooru's habits disagree, in both directions")


def test_the_divergence_panes_render() -> None:
    prewarm.reset_for_tests()
    prewarm.run_all()
    s = _settings()
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    for i in range(60):
        tags = ["maid", "zzq_mychar", "apron", "1girl"]
        if i < 8:
            tags.append("maid_headdress")
        Image.new("RGB", (24, 24)).save(folder / f"i{i:03d}.png")
        (folder / f"i{i:03d}.txt").write_text(", ".join(tags),
                                              encoding="utf-8")

    window = StatsWindow()
    window._settings = s
    window.attach(SessionState(scan(folder)))
    window.show()
    index = window._cooccur_tag.findText("maid")
    assert index >= 0
    window._cooccur_tag.setCurrentIndex(index)
    window._refresh_cooccur()

    yours = [label for label, _v in window._cooccur_bottom._data]
    theirs = [label for label, _v in window._cooccur_missing._data]
    assert any("zzq_mychar" in l for l in yours)
    assert any("apron" in l for l in yours)
    assert theirs                                # they expect more
    assert all("%" in l for l in yours)          # both figures shown

    # A tag Danbooru has never heard of says so, rather than drawing
    # two empty panes and leaving the user to guess.
    own = window._cooccur_tag.findText("zzq_mychar")
    if own >= 0:
        window._cooccur_tag.setCurrentIndex(own)
        window._refresh_cooccur()
        assert "nothing to compare" in window._cooccur_note.text()
    print("OK: the comparison panes draw for a known tag and explain "
          "themselves for one Danbooru has never seen")


def test_partners_are_not_falsely_reported_as_new() -> None:
    """FIELD BUG, visible in a screenshot: six of seven entries were
    wrong.

    The co-occurrence data stores thirty partners per tag. For a hub
    tag like 1girl — with thousands of partners — its own top thirty
    are all ubiquitous, so ordinary tags like sweat, nude and censored
    fall off it and were reported as pairings Danbooru does not make.
    Danbooru makes them constantly; they are simply stored from the
    other direction.
    """
    from core.dataset_stats import compare_with_danbooru
    from ui.stats_window import _reverse_share

    prewarm.reset_for_tests()
    prewarm.run_all()

    # Every one of these was wrongly shown as "not on their list".
    for partner in ("sweat", "close-up", "lips", "nude", "censored"):
        assert _reverse_share("1girl", partner) is not None, partner
    # A pair neither side records stays genuinely unknown, which is
    # the only case where claiming "new" is honest.
    assert _reverse_share("1girl", "pregnant") is None

    # With the reverse lookup supplied, a known pairing gets a real
    # figure instead of being called new.
    counts = {"sweat": 50, "zzq_mine": 50}
    you_more, _they = compare_with_danbooru(
        counts, 50, [("solo", 0.4)],
        reverse_lookup=lambda p: _reverse_share("1girl", p))
    sweat = next(d for d in you_more if d.partner == "sweat")
    assert not sweat.unlisted
    mine = next(d for d in you_more if d.partner == "zzq_mine")
    assert mine.unlisted                        # nothing knows this one
    print("OK: a partner stored only from the other direction gets a "
          "real percentage instead of being called new")


def test_the_explanation_renders_as_rich_text() -> None:
    """Qt only guesses rich text when the string LOOKS like markup
    from its opening characters. This one starts with a sentence, so
    the <b> tags were drawn literally on screen."""
    from PySide6.QtCore import Qt

    s = _settings()
    window = StatsWindow()
    window._settings = s
    assert window._cooccur_note.textFormat() == Qt.TextFormat.RichText
    assert "<b>" in window._cooccur_note.text()
    print("OK: the co-occurrence explanation renders its markup "
          "instead of printing it")


def test_the_explanation_is_never_left_stale() -> None:
    """FIELD BUG: selecting a tag Danbooru has no data for replaced
    the standing explanation with a message about that tag — and
    nothing ever put it back. It stayed on screen describing a tag the
    user had long since moved on from.

    The message was written once and never taken back. Every path
    through the refresh now sets this label, so it cannot be left
    saying something untrue."""
    from ui.stats_window import COOCCUR_HELP

    prewarm.reset_for_tests()
    prewarm.run_all()
    s = _settings()
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    for i in range(40):
        Image.new("RGB", (24, 24)).save(folder / f"i{i:03d}.png")
        (folder / f"i{i:03d}.txt").write_text(
            "1girl, solo, zzq_unique_token", encoding="utf-8")

    window = StatsWindow()
    window._settings = s
    window.attach(SessionState(scan(folder)))
    window.show()

    def select(tag: str) -> None:
        index = window._cooccur_tag.findText(tag)
        assert index >= 0, tag
        window._cooccur_tag.setCurrentIndex(index)
        window._refresh_cooccur()

    select("1girl")
    assert window._cooccur_note.text() == COOCCUR_HELP
    select("zzq_unique_token")
    assert "No Danbooru co-occurrence data" in window._cooccur_note.text()
    # The line that was missing: going back must restore it.
    select("1girl")
    assert window._cooccur_note.text() == COOCCUR_HELP

    for tag in ("zzq_unique_token", "1girl", "zzq_unique_token", "solo"):
        select(tag)
    assert window._cooccur_note.text() == COOCCUR_HELP

    # And closing the dataset must not leave a message about a tag
    # from a folder that is no longer open.
    window.attach(None)
    window._refresh_cooccur()
    assert window._cooccur_note.text() == COOCCUR_HELP
    print("OK: the co-occurrence explanation is restored on every "
          "refresh, so a message about one tag cannot outlive it")


def run() -> None:
    test_every_image_and_tag_is_counted_once()
    test_repeats_are_read_from_folder_names()
    test_the_folder_panel_appears_only_when_it_says_something()
    test_the_tab_reports_what_it_found()
    test_cooccurrence_compares_instead_of_listing_rare_tags()
    test_the_divergence_panes_render()
    test_partners_are_not_falsely_reported_as_new()
    test_the_explanation_renders_as_rich_text()
    test_the_explanation_is_never_left_stale()
    print("\nALL PASS: dataset stats")


if __name__ == "__main__":
    run()
