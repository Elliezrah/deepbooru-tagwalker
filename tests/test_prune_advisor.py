"""
tests/test_prune_advisor.py

Tests for core/prune_advisor.py and its Tools-menu dialog.

The property the whole feature rests on is pinned first and hardest:
this tool CHANGES NOTHING. After a full analysis and export, every
caption file must be byte-identical.

Everything else is about the advice being coherent. Three bugs found
while building are kept as regressions, because each produced output
that looked plausible and was wrong:

- ubiquitous tags manufacturing false "duplicates" (any two tags on
  90%+ of a set overlap by 80%+ whatever they mean);
- the narrower tag of a pair being offered as the one to drop, which
  contradicted the advice text telling the user to keep it;
- a cluster of always-together tags chaining into advice that told
  the user to drop a tag AND to keep it as another tag's replacement.

Run: python3 tests/test_prune_advisor.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

from PIL import Image
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from core import prune_advisor as advisor
from core import tag_reference
from core.scanner import scan
from core.state import SessionState
from ui.prune_advisor_dialog import PruneAdvisorDialog

_notes: list = []
QMessageBox.information = lambda *a, **k: _notes.append(a[-1] if a else "")
QMessageBox.warning = lambda *a, **k: _notes.append(a[-1] if a else "")

_CATS = {"absurdres": "meta", "highres": "meta", "wada_arco": "artist",
         "hatsune_miku": "character", "vocaloid": "copyright"}


def _cat(tag):
    return _CATS.get(tag, "general")


def _cost(tag):
    return len(tag.split("_")) + 1


def _sample():
    """One dataset containing each pattern the advisor looks for."""
    return {
        "long_hair": set(range(190)),          # ubiquitous
        "red_eyes": set(range(5, 200)),        # ubiquitous
        "wada_arco": set(range(198)),          # ubiquitous artist
        "hatsune_miku": set(range(196)),
        "vocaloid": set(range(196)),
        "absurdres": set(range(100)),          # meta
        "rare_thing": {1, 2},                  # unlearnable
        "sword": set(range(60)),               # broader half of a pair
        "holding_sword": set(range(58)),       # narrower half
        "smile": set(range(100, 190)),         # ordinary middle band
    }, 200


def _tags(report, tier):
    return {c.tag for c in getattr(report, tier)}


def test_tier_one_needs_no_knowledge_of_intent() -> None:
    tags, n = _sample()
    r = advisor.analyse(tags, n, set(range(20)), _cat, _cost,
                        ["character"])
    assert "absurdres" in _tags(r, "tier1")     # describes the file
    assert "rare_thing" in _tags(r, "tier1")    # too few to learn
    # The descriptive middle band is not a candidate at all.
    assert "smile" not in (_tags(r, "tier1") | _tags(r, "tier2")
                           | _tags(r, "tier3"))
    assert r.tokens_reclaimable() > 0
    assert any("Tier 1 alone reclaims" in note for note in r.notes)
    print("OK: meta tags and unlearnable rarities are found without "
          "knowing the training goal, and the middle band is left "
          "alone")


def test_the_goal_decides_the_verdict() -> None:
    """The same tag, the same frequency, opposite answers — which is
    the whole reason the tool asks what you are training."""
    tags, n = _sample()
    char = advisor.analyse(tags, n, set(), _cat, _cost, ["character"])
    style = advisor.analyse(tags, n, set(), _cat, _cost, ["style"])

    # Character: the artist must stay described or the style fuses in.
    assert "wada_arco" in _tags(char, "tier3")
    assert "red_eyes" in _tags(char, "tier2")
    # Style: exactly reversed.
    assert "wada_arco" in _tags(style, "tier2")
    assert "red_eyes" in _tags(style, "tier3")
    assert "hatsune_miku" in _tags(style, "tier3")
    print("OK: artist and content tags swap tiers between a character "
          "goal and a style goal, on identical data")


def test_mixed_goals_report_disagreement_instead_of_choosing() -> None:
    tags, n = _sample()
    mixed = advisor.analyse(tags, n, set(), _cat, _cost,
                            ["style", "concept"])
    # Filter on the reason KEY, not on wording: the row text now
    # names which goal wants what, so "disagree" no longer appears.
    conflicts = [c for c in mixed.tier2
                 if c.kind == advisor.KIND_CONFLICT]
    assert any(c.tag == "red_eyes" for c in conflicts)
    assert any("disagree" in note for note in mixed.notes)
    # Where the goals agree, it stays a recommendation.
    assert "wada_arco" in _tags(mixed, "tier2")
    print("OK: when two goals want opposite things from a category, "
          "the tag is put to the user rather than silently decided")


def test_near_duplicate_pairing_regressions() -> None:
    tags, n = _sample()
    r = advisor.analyse(tags, n, set(), _cat, _cost, ["character"])
    pairs = [c for c in r.tier2 if c.partner]

    # Only the genuine middle-band pair: two tags at 90%+ of a set
    # necessarily overlap by 80%+, so running this over ubiquitous
    # tags invents duplicates out of tags that merely co-occur.
    assert len(pairs) == 1
    # The BROADER tag is the candidate; the narrower one is kept,
    # which is what the advice text tells the user to do.
    assert pairs[0].tag == "sword"
    assert pairs[0].partner == "holding_sword"
    assert not any(c.partner for c in r.tier2
                   if c.tag in ("long_hair", "red_eyes", "vocaloid"))
    assert all(c.partner != "absurdres" for c in r.tier2)

    # A template block of always-together tags must not chain into
    # advice that drops a tag and also names it as the replacement.
    block = {f"filler_tag_{j}": set(range(12)) for j in range(12)}
    block["anchor"] = set(range(40))
    rb = advisor.analyse(block, 60, set(), _cat, _cost, ["character"])
    drops = {c.tag for c in rb.tier2 if c.partner}
    keeps = {c.partner for c in rb.tier2 if c.partner}
    assert drops and not (drops & keeps)
    print("OK: pairing fires only in the middle band, flags the "
          "broader tag, and never tells the user to drop a tag it "
          "also named as the one to keep")


def test_ranking_and_export() -> None:
    tags, n = _sample()
    over = set(range(20))
    r = advisor.analyse(tags, n, over, _cat, _cost, ["character"])
    # Candidates relieving the captions actually in trouble come first.
    assert r.tier1[0].over_limit_hits >= r.tier1[-1].over_limit_hits
    rare = next(c for c in r.tier1 if c.tag == "rare_thing")
    assert rare.tokens_saved == _cost("rare_thing") * 2

    md = advisor.build_prune_report(r, ["/data/set"])
    assert "Tier 1" in md and "Tier 2" in md and "Tier 3" in md
    assert "Training goal(s): Character" in md
    assert "nothing here has been changed" in md.lower()

    empty = advisor.analyse({}, 0, set(), _cat, _cost, ["style"])
    assert not empty.tier1 and "No captions" in empty.notes[0]
    print("OK: candidates rank by relief to over-limit captions, and "
          "the export states plainly that nothing was changed")


def test_dialog_and_the_no_write_guarantee() -> None:
    from ui.main_window import MainWindow

    s = Settings()
    initialize_theme(s.theme)
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    total = 60
    for i in range(total):
        tags = ["my_trigger", "hatsune_miku", "vocaloid", "wada_arco",
                "long_hair", "red_eyes", "absurdres", "highres"]
        if i < 36:
            tags.append("sword")
        if i < 35:
            tags.append("holding_sword")
        if i < 25:
            tags.append("smile")
        if i == 3:
            tags.append("once_only_tag")
        Image.new("RGB", (8, 8)).save(d / f"img{i:03d}.png")
        (d / f"img{i:03d}.txt").write_text(", ".join(tags),
                                           encoding="utf-8")
    before = {p.name: p.read_bytes() for p in d.iterdir()}

    mw = MainWindow(s)
    tools = [m for m in mw.findChildren(QMenu)
             if m.title() and "Tools" in m.title()]
    labels = [a.text() for a in tools[0].actions() if a.text()]
    assert any("Pruning Advisor" in t for t in labels)

    dlg = PruneAdvisorDialog(SessionState(scan(d)), mw)
    assert set(dlg._goals) == {"character", "style", "concept"}
    assert all(len(b.toolTip()) > 150 for b in dlg._goals.values())

    # With no goal selected there is no answer to give, and it says so.
    dlg._goals["character"].setChecked(False)
    mark = len(_notes)
    dlg.analyse()
    assert len(_notes) == mark + 1
    assert "opposite verdicts" in _notes[-1]
    assert dlg._report is None

    dlg._goals["character"].setChecked(True)
    dlg.analyse()
    report = dlg._report
    assert report is not None and report.total_images == total
    assert {"absurdres", "highres"} <= _tags(report, "tier1")
    assert "once_only_tag" in _tags(report, "tier1")
    assert "wada_arco" in _tags(report, "tier3")

    # Three tier groups, every row explaining itself on hover.
    top = [dlg.tree.topLevelItem(i).text(0)
           for i in range(dlg.tree.topLevelItemCount())]
    assert len(top) == 3 and "Tier 1" in top[0]
    row = dlg.tree.topLevelItem(0).child(0)
    assert len(row.toolTip(0)) > 120

    dlg._goals["style"].setChecked(True)
    assert "disagree" in dlg.goal_note.text()
    dlg.analyse()
    assert dlg._markdown()

    # The rarity-mode switch, and its inputs staying idle until used.
    assert dlg.rarity_mode.currentData() == "share"
    assert not dlg.spin_epochs.isEnabled()
    dlg.rarity_mode.setCurrentIndex(1)
    assert dlg.spin_epochs.isEnabled()
    assert not hasattr(dlg, "spin_floor")     # the tool's call, not ours

    # Repeats are entered by hand. Nothing is assumed from the folder
    # name, because the name and the trainer's setting can disagree
    # and a wrong number here distorts every exposure figure.
    assert dlg.repeat_table.rowCount() == len(dlg._repeat_spins)
    assert all(sp.value() == 1 for sp in dlg._repeat_spins.values())
    dlg._fill_repeats()                        # explicit convenience
    dlg.spin_epochs.setValue(16)
    dlg.analyse()
    assert dlg._report.epochs == 16
    assert "exposure" in dlg.summary.text()

    # THE guarantee.
    after = {p.name: p.read_bytes() for p in d.iterdir()}
    assert before == after
    print("OK: the dialog analyses, explains every row on hover, "
          "refuses without a goal — and leaves every caption file "
          "byte-identical")


def test_repeats_are_read_from_kohya_folder_names() -> None:
    assert advisor.parse_repeats("3_unique_concept") == 3
    assert advisor.parse_repeats("10_many") == 10
    assert advisor.parse_repeats("1_general") == 1
    # Anything not in the convention means one pass.
    assert advisor.parse_repeats("concept_only") == 1
    assert advisor.parse_repeats("0_bad") == 1
    assert advisor.parse_repeats("") == 1
    print("OK: kohya's <repeats>_<name> folder convention is read "
          "rather than asked for, and anything else counts as 1x")


def test_exposure_mode_answers_a_different_question() -> None:
    """Share of the dataset is a proxy. Whether an association forms
    depends on how many times the tag was SEEN — images x repeats x
    epochs — which does not vary with the size of the rest of the set.

    The gap shows up exactly where a real dataset uses per-folder
    repeats: the same tag count can mean triple the exposure purely by
    which folder the images sit in."""
    repeats = {}
    for i in range(200):
        repeats[f"a{i}"] = 3
    for i in range(200):
        repeats[f"b{i}"] = 2
    for i in range(500):
        repeats[f"c{i}"] = 1
    total = len(repeats)

    tags = {
        "rare_in_3x": {f"a{i}" for i in range(4)},   # 4 x 3 x 16 = 192
        "rare_in_1x": {f"c{i}" for i in range(4)},   # 4 x 1 x 16 = 64
        "very_rare": {"c0", "c1"},                   # 2 x 1 x 16 = 32
        "common": {f"c{i}" for i in range(300)},
    }
    share = advisor.analyse(tags, total, set(), _cat, _cost,
                            ["character"])
    exposure = advisor.analyse(
        tags, total, set(), _cat, _cost, ["character"],
        epochs=16, repeats_of=lambda i: repeats.get(i, 1))

    shared_tier1 = _tags(share, "tier1")
    exp_tier1 = _tags(exposure, "tier1")
    # 0.5% of 900 is 5, so share mode condemns both four-image tags...
    assert {"rare_in_3x", "rare_in_1x"} <= shared_tier1
    # ...while exposure mode sees they are seen 192 and 64 times.
    assert "rare_in_3x" not in exp_tier1
    assert "rare_in_1x" not in exp_tier1
    # Both agree about the genuinely unlearnable one.
    assert "very_rare" in shared_tier1 and "very_rare" in exp_tier1

    candidate = next(c for c in exposure.tier1
                     if c.tag == "very_rare")
    assert candidate.exposure == 32
    assert "32" in candidate.reason
    assert "rule of thumb" in candidate.detail   # hedged, not a law
    assert exposure.epochs == 16
    assert share.epochs == 0

    md = advisor.build_prune_report(exposure)
    assert "training exposure" in md.lower() and "16 epoch" in md
    assert "share of the dataset" in advisor.build_prune_report(
        share).lower()
    print("OK: exposure mode counts images x folder repeats x epochs, "
          "sparing tags the share rule wrongly condemns, and the "
          "export records which rule was used")


def test_the_floor_is_the_tools_judgement_not_an_input() -> None:
    """Someone configuring a training run has no way to know where an
    association stabilises. Asking them for the number would defeat
    the point of asking the tool, so the tool owns it — and owns it
    conservatively, because a kept tag costs a few tokens while a
    wrongly deleted one costs the trait."""
    assert isinstance(advisor.EXPOSURE_FLOOR, int)
    assert advisor.analyse.__code__.co_varnames[
        :advisor.analyse.__code__.co_argcount].count(
            "exposure_floor") == 0

    repeats = {f"c{i}": 1 for i in range(900)}
    tags = {"tiny": {"c0", "c1"},              # 2 x 1 x 16 = 32
            "ok": {f"c{i}" for i in range(8)}, # 8 x 1 x 16 = 128
            "common": {f"c{i}" for i in range(400)}}
    r = advisor.analyse(tags, 900, set(), _cat, _cost, ["character"],
                        epochs=16, repeats_of=lambda i: 1)
    flagged = _tags(r, "tier1")
    assert "tiny" in flagged and "ok" not in flagged
    assert r.exposure_floor == advisor.EXPOSURE_FLOOR
    detail = next(c for c in r.tier1 if c.tag == "tiny").detail
    assert "rule of thumb" in detail          # hedged, not asserted
    assert str(advisor.EXPOSURE_FLOOR) in detail
    print("OK: the learnability floor is the tool's own conservative "
          "judgement, reported with each tag's exposure rather than "
          "asked of the user")


def test_help_is_paged_so_it_cannot_outgrow_the_screen() -> None:
    """FIELD BUG: the help was one message box, and a box sized to its
    text ran off the edge of the screen with no way to scroll back."""
    from ui.paged_help_dialog import PagedHelpDialog
    from ui.prune_advisor_dialog import HELP_PAGES

    assert len(HELP_PAGES) >= 6
    assert max(len(body) for _h, body in HELP_PAGES) < 800

    dlg = PagedHelpDialog("Test", HELP_PAGES)
    assert dlg.lbl_page.text().startswith("1 /")
    assert not dlg.btn_prev.isEnabled()
    dlg.step(1)
    assert dlg.lbl_page.text().startswith("2 /")
    assert dlg.btn_prev.isEnabled()
    for _ in range(50):
        dlg.step(1)
    assert dlg.lbl_page.text() == f"{len(HELP_PAGES)} / {len(HELP_PAGES)}"
    assert not dlg.btn_next.isEnabled()
    # The size is fixed, which is the whole point.
    assert dlg.width() <= 700 and dlg.height() <= 560
    # An empty page list must not crash the window open.
    assert PagedHelpDialog("Empty", []).lbl_page.text() == "1 / 1"
    print("OK: help pages through at a fixed window size instead of "
          "growing a message box past the screen edge")


def test_rare_but_known_to_the_base_model_is_not_unlearnable() -> None:
    """FIELD REPORT: `elf` was being called "too few to learn" on a
    handful of images. It is one of the best-known tags there is.

    The rarity rule had a hidden assumption — that the tag needs to be
    LEARNED here. A tag the base model already knows is doing a
    different job: naming a feature so it is attributed to the tag
    rather than absorbed into the trigger. That works from one image,
    and pruning it does not remove the feature from the picture, only
    from the caption, which is precisely how a concept picks up
    baggage.

    So rarity condemns a tag only when the base model has never seen
    it either."""
    known = {"elf": 68462, "pointy_ears": 377965, "barely": 40}
    tags = {
        "elf": {1, 2, 3},              # rare here, model knows it
        "barely": {4, 5, 6},           # rare here, model barely does
        "my_oc_token": {7, 8},         # rare here, invented
        "common": set(range(400)),
    }
    r = advisor.analyse(tags, 900, set(), _cat, _cost, ["character"],
                        base_model_count=lambda t: known.get(t, 0))
    assert "elf" not in _tags(r, "tier1")
    assert "elf" in _tags(r, "tier3")        # shown, with the reason
    # No prior to build on: these are the genuine cases.
    assert {"barely", "my_oc_token"} <= _tags(r, "tier1")

    candidate = next(c for c in r.tier3 if c.tag == "elf")
    assert "68,462" in candidate.reason
    assert "attributed to" in candidate.detail
    assert "trigger" in candidate.detail
    assert "unexplained" in candidate.detail

    # Exposure mode inherits the same protection.
    e = advisor.analyse(tags, 900, set(), _cat, _cost, ["character"],
                        epochs=16, repeats_of=lambda i: 1,
                        base_model_count=lambda t: known.get(t, 0))
    assert "elf" not in _tags(e, "tier1")

    # Without a count source nothing is assumed to be known, which is
    # the old behaviour rather than a silent change of verdict.
    plain = advisor.analyse(tags, 900, set(), _cat, _cost,
                            ["character"])
    assert "elf" in _tags(plain, "tier1")
    print("OK: a rare tag the base model already knows is kept for "
          "attribution rather than condemned as unlearnable; only "
          "tags with no prior are flagged")


def test_base_model_counts_come_from_the_shipped_snapshots() -> None:
    """The dialog feeds real Danbooru counts, so the distinction holds
    on actual tags rather than only on fixtures."""
    s = Settings()
    initialize_theme(s.theme)
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for i in range(900):
        tags = ["my_trigger", "1girl", "long_hair"]
        if i < 3:
            tags += ["elf", "pointy_ears", "zzq_invented_token"]
        Image.new("RGB", (8, 8)).save(d / f"img{i:03d}.png")
        (d / f"img{i:03d}.txt").write_text(", ".join(tags),
                                           encoding="utf-8")
    dlg = PruneAdvisorDialog(SessionState(scan(d)), None)
    dlg.analyse()
    tier1 = _tags(dlg._report, "tier1")
    tier3 = _tags(dlg._report, "tier3")
    assert "elf" not in tier1 and "elf" in tier3
    assert "pointy_ears" not in tier1
    assert "zzq_invented_token" in tier1
    print("OK: real snapshot counts drive the distinction, so famous "
          "tags survive a small appearance count and invented ones "
          "still do not")


def test_era_follows_the_preference() -> None:
    """Whether a tag counts as "already known" depends on which
    shipped snapshot the advice is measured against, so the tool
    orients to the tag database chosen in Preferences — the same one
    the Tag Referencer uses.

    Deliberately no selector of its own: two era controls could
    disagree, and the same tag would then get contradictory verdicts
    in two windows of one program."""
    s = Settings()
    initialize_theme(s.theme)
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for i in range(900):
        tags = ["my_trigger", "1girl", "long_hair"]
        if i < 3:
            tags += ["elf", "hair_intakes"]
        Image.new("RGB", (8, 8)).save(d / f"img{i:03d}.png")
        (d / f"img{i:03d}.txt").write_text(", ".join(tags),
                                           encoding="utf-8")
    st = SessionState(scan(d))

    # Illustrious-era: elf has ~68k posts, so being rare here is fine.
    s.tag_database_choice = "mid2024"
    dlg = PruneAdvisorDialog(st, None, s)
    assert dlg.era_key() == "mid2024"
    assert "Illustrious" in dlg.era_label.text()
    assert "Settings" in dlg.era_label.text()
    dlg.analyse()
    assert "elf" in _tags(dlg._report, "tier3")
    assert "Illustrious" in dlg._report.era_label

    # 2017-era: elf had ~500 posts, so a model of that vintage barely
    # knew it and rarity here really does condemn the tag.
    s.tag_database_choice = "ancient"
    dlg2 = PruneAdvisorDialog(st, None, s)
    assert dlg2.era_key() == "ancient"
    assert "Ancient" in dlg2.era_label.text()
    dlg2.analyse()
    assert "elf" in _tags(dlg2._report, "tier1")
    markdown = dlg2._markdown()
    assert "Ancient" in markdown and "tag database" in markdown

    # A preference changed while the window sat open is picked up.
    s.tag_database_choice = "current"
    dlg2.show()
    assert "April 2026" in dlg2.era_label.text()

    # An unrecognised value falls back rather than raising.
    s.tag_database_choice = "not_a_real_era"
    assert PruneAdvisorDialog(st, None, s).era_key() == "mid2024"
    print("OK: the advisor judges against the tag database chosen in "
          "Preferences, re-orienting its verdicts when that changes, "
          "with no second era control to disagree with")


def test_sibling_detection_protects_majority_values() -> None:
    """FIELD REPORT: `1girl` was offered as a bake-in candidate on a
    dataset whose multi-subject images were correctly tagged.

    The ubiquity rule was asking "is this tag on nearly every image?"
    when the question that matters is "is this ATTRIBUTE constant?".
    Those come apart exactly when a dominant value has explicitly
    tagged alternatives: subject count is not constant in such a set,
    it varies, and `1girl` is merely its majority value.

    Baking in a real constant is free. Baking in the majority value of
    a varying attribute costs control, because the minority tags must
    then override a bias welded into the trigger.

    Derived from the captions, not from a list of known pairs, so the
    same test catches 1boy/2boys and red_eyes/blue_eyes."""
    n = 1000
    everything = set(range(n))
    # Eye colour spread independently of subject count, as in real data.
    blue = {i for i in range(n) if i % 12 == 0}
    tags = {
        "1girl": everything - set(range(900, n)),      # 90%
        "2girls": set(range(900, 980)),
        "multiple_girls": set(range(900, n)),
        "1boy": everything - set(range(920, n)),       # 92%
        "2boys": set(range(920, n)),
        "red_eyes": everything - blue,                 # 92%
        "blue_eyes": blue,
        "my_trigger": everything,                      # true constant
        "long_hair": set(range(950)),                  # gap unexplained
    }
    r = advisor.analyse(tags, n, set(), _cat, _cost, ["character"])
    tier2, tier3 = _tags(r, "tier2"), _tags(r, "tier3")

    assert "1girl" in tier3 and "1girl" not in tier2
    assert "1boy" in tier3                    # same rule, not hardcoded
    assert "red_eyes" in tier3
    # A tag whose gap nothing explains is a genuine constant.
    assert "long_hair" in tier2 and "long_hair" not in tier3
    assert "my_trigger" in tier2

    # The SMALLEST set that explains the gap is named, not every tag
    # that happens to sit inside it — several unrelated tags can be
    # statistically disjoint by coincidence.
    girl = next(c for c in r.tier3 if c.tag == "1girl")
    assert set(girl.siblings) <= {"2girls", "multiple_girls"}
    assert next(c for c in r.tier3
                if c.tag == "1boy").siblings == ["2boys"]
    assert next(c for c in r.tier3
                if c.tag == "red_eyes").siblings == ["blue_eyes"]
    assert "2girls" in girl.reason or "multiple_girls" in girl.reason
    assert "OVERRIDE" in girl.detail and "neutral" in girl.detail

    # Real captions contain mistakes; one stray tag must not defeat it.
    noisy = dict(tags)
    noisy["1girl"] = tags["1girl"] | {905}
    assert "1girl" in _tags(
        advisor.analyse(noisy, n, set(), _cat, _cost, ["character"]),
        "tier3")
    print("OK: a ubiquitous tag whose gap is covered by mutually "
          "exclusive alternatives is kept as the majority value of a "
          "varying attribute, naming the minimal explaining set")


def test_per_caption_and_total_token_costs_are_separate() -> None:
    """`1girl` costs 3 tokens in a caption but ~3,000 across a
    dataset. Only the first answers "will this get my long captions
    under the limit?", so showing one total made a cheap tag look like
    a big win."""
    tags = {"1girl": set(range(900)), "smile": set(range(100))}
    r = advisor.analyse(tags, 1000, set(), _cat, _cost, ["character"])
    every = r.tier1 + r.tier2 + r.tier3
    for c in every:
        assert c.tokens_saved == c.token_cost * c.count
    md = advisor.build_prune_report(r)
    assert "tokens each" in md and "tokens total" in md
    print("OK: per-caption cost and dataset total are reported "
          "separately, in the tree and the export")


def test_trigger_tokens_are_never_recommended() -> None:
    """SELF-AUDIT: the tool was offering the user's own trigger token
    as a bake-in candidate, because ubiquity is what makes an ordinary
    tag one.

    For the trigger, ubiquity is the entire mechanism — it is the
    handle everything else attaches to. Removing it does not weaken
    the LoRA, it leaves nothing to activate it. Recognised two ways:
    the locked-token list the app already keeps, and a tag the base
    model has never seen sitting on almost every image."""
    n = 1000
    tags = {"my_secret_trigger": set(range(n)),      # custom, 100%
            "long_hair": set(range(940)),
            "smile": set(range(300))}
    r = advisor.analyse(tags, n, set(), _cat, _cost, ["character"],
                        base_model_count=lambda t: 0
                        if t == "my_secret_trigger" else 900000)
    assert "my_secret_trigger" not in _tags(r, "tier1")
    assert "my_secret_trigger" not in _tags(r, "tier2")
    # Shown as refused rather than quietly omitted.
    assert "my_secret_trigger" in _tags(r, "tier3")
    candidate = next(c for c in r.tier3
                     if c.tag == "my_secret_trigger")
    assert "never drop" in candidate.reason
    assert "inert" in candidate.detail

    # An explicit locked token wins even for a tag the model knows.
    locked = advisor.analyse(
        tags, n, set(), _cat, _cost, ["character"],
        base_model_count=lambda t: 900000,
        protected_tags=["Long_Hair"])            # case-folded
    protected = next(c for c in locked.tier3 if c.tag == "long_hair")
    assert "trigger token" in protected.reason
    assert "locked" in protected.detail
    # Without a count source every tag looks unknown, so the
    # heuristic must stay silent rather than calling ordinary
    # ubiquitous tags triggers. Missing data degrades to the previous
    # behaviour, never to a confident wrong verdict.
    blind = advisor.analyse(tags, n, set(), _cat, _cost,
                            ["character"])
    assert "long_hair" not in _tags(blind, "tier3")
    print("OK: the trigger token is never offered for pruning, "
          "recognised from the locked-token list or from being an "
          "unknown tag on nearly every image")


def test_medium_tags_are_not_treated_as_bookkeeping() -> None:
    """SELF-AUDIT: Danbooru files `highres` and `traditional_media`
    under one "meta" category, and the tool auto-pruned the lot.

    Only the first is bookkeeping. photo_(medium), official_art,
    game_cg and scan ARE the look of the picture, and dropping them
    welds that look into the trigger — worst for a style LoRA, where
    the medium is close to the whole point."""
    assert advisor.is_bookkeeping_meta("absurdres")
    assert advisor.is_bookkeeping_meta("translated")
    assert advisor.is_bookkeeping_meta("artist_request")   # suffix rule
    assert not advisor.is_bookkeeping_meta("traditional_media")
    assert not advisor.is_bookkeeping_meta("scan")
    assert not advisor.is_bookkeeping_meta("photo_(medium)")

    meta = {"absurdres", "translated", "artist_request",
            "traditional_media", "scan"}
    known = {"traditional_media": 85584, "scan": 85390,
             "absurdres": 1721881}
    n = 1000
    tags = {"absurdres": set(range(800)),
            "translated": set(range(300)),
            "artist_request": set(range(120)),
            "traditional_media": set(range(950)),   # ubiquitous medium
            "scan": {1, 2, 3},                      # rare medium
            "obsolete_junk": {4, 5},                # rare, unknown
            "long_hair": set(range(940))}
    r = advisor.analyse(
        tags, n, set(), lambda t: "meta" if t in meta else "general",
        _cost, ["style"], base_model_count=lambda t: known.get(t, 0))
    tier1, tier3 = _tags(r, "tier1"), _tags(r, "tier3")

    assert {"absurdres", "translated", "artist_request"} <= tier1
    assert not ({"traditional_media", "scan"} & tier1)
    # A style goal must keep the medium described.
    assert "traditional_media" in tier3
    # Rare medium survives on the base-model-knows-it rule.
    assert "scan" in tier3
    # Genuinely unknown rarities are still pruned.
    assert "obsolete_junk" in tier1
    print("OK: only meta tags with no visual content are auto-pruned; "
          "medium and production tags take the ordinary path and stay "
          "protected for a style goal")


def test_tier3_is_grouped_by_reason() -> None:
    """FIELD REPORT: "tier 3 is indeed enormous".

    It is, and mostly correctly — on a real dataset most tags do
    legitimately belong there. The fault was presentation: an
    undifferentiated list buries the handful of rows that need a
    decision among hundreds where the tool is simply showing its
    working.

    Every candidate now carries a machine-readable reason key, the
    window sub-groups by it, and only the group that needs an answer
    opens."""
    n = 1000
    everything = set(range(n))
    tags = {"my_trigger": everything,                 # trigger
            "1girl": set(range(900)),                 # majority value
            "2girls": set(range(900, n)),
            "scan": {1, 2, 3},                        # rare but known
            "long_hair": set(range(950))}
    known = {"1girl": 5889398, "2girls": 999643,
             "scan": 85390, "long_hair": 4255309}
    r = advisor.analyse(
        tags, n, set(), lambda t: "meta" if t == "scan" else "general",
        _cost, ["character"],
        base_model_count=lambda t: known.get(t, 0))

    assert r.tier3
    assert all(c.kind for c in r.tier3)      # every row is classified
    kinds = {c.kind for c in r.tier3}
    assert advisor.KIND_TRIGGER in kinds
    assert advisor.KIND_MAJORITY in kinds
    assert advisor.KIND_KNOWN in kinds
    for kind in kinds:
        assert kind in advisor.KIND_LABELS, kind

    # The export carries the same grouping: a flat list of two hundred
    # rows is no more readable in a file than on screen.
    markdown = advisor.build_prune_report(r)
    for kind in kinds:
        assert advisor.KIND_LABELS[kind][0] in markdown
    print("OK: every Tier 3 row carries a reason key, and both the "
          "report and the export group by it")


def test_conflicts_name_which_goal_wants_what() -> None:
    """"Your goals disagree" is true but unhelpful on its own: the
    user still had to open a tooltip to find out which goal wanted
    which outcome."""
    n = 1000
    tags = {"blue_dress": set(range(950)),
            "my_trigger": set(range(n))}
    r = advisor.analyse(tags, n, set(), lambda t: "general", _cost,
                        ["character", "style"],
                        base_model_count=lambda t: 900000)
    conflicts = [c for c in r.tier1 + r.tier2 + r.tier3
                 if c.kind == advisor.KIND_CONFLICT]
    assert conflicts
    reason = conflicts[0].reason
    assert "wants it baked in" in reason
    assert "wants it described" in reason
    assert "Character" in reason and "Style" in reason

    # A single goal cannot disagree with itself.
    solo = advisor.analyse(tags, n, set(), lambda t: "general", _cost,
                           ["character"],
                           base_model_count=lambda t: 900000)
    assert not [c for c in solo.tier1 + solo.tier2 + solo.tier3
                if c.kind == advisor.KIND_CONFLICT]
    print("OK: a conflict row names the goal that wants the tag baked "
          "in and the one that wants it described")


def test_tier3_groups_render_and_only_decisions_open() -> None:
    # NOT patching QMessageBox here: an earlier version of this test
    # replaced it globally and never restored it, which silently broke
    # a later test that counts the notes the dialog raises. Tests in
    # one file share a process; a global patch is a side effect.
    s = Settings()
    initialize_theme(s.theme)
    folder = Path(tempfile.mkdtemp()) / "ds"
    folder.mkdir(parents=True)
    for i in range(1000):
        tags = ["zzq_trigger", "long_hair", "blue_dress"]
        tags.append("1girl" if i < 900 else "2girls")
        if i < 3:
            tags.append("scan")
        Image.new("RGB", (8, 8)).save(folder / f"i{i:04d}.png")
        (folder / f"i{i:04d}.txt").write_text(", ".join(tags),
                                              encoding="utf-8")
    state = SessionState(scan(folder))
    state.set_front_locked_tokens(["zzq_trigger"])
    dlg = PruneAdvisorDialog(state, None, s)
    for key, box in dlg._goals.items():
        box.setChecked(key in ("character", "style"))
    dlg.analyse()

    root = None
    for i in range(dlg.tree.topLevelItemCount()):
        item = dlg.tree.topLevelItem(i)
        if item.text(0).startswith("Tier 3"):
            root = item
    assert root is not None
    assert root.childCount() > 1                  # sub-grouped
    groups = [root.child(i) for i in range(root.childCount())]
    assert sum(g.childCount() for g in groups) == len(dlg._report.tier3)
    for group in groups:
        should_open = group.text(0).startswith("Your goals disagree")
        assert group.isExpanded() == should_open, group.text(0)
    print("OK: the window sub-groups Tier 3 by reason, the counts add "
          "up, and only the group needing a decision is open")


def test_a_broader_tag_covered_by_a_narrower_one() -> None:
    """FIELD QUESTION: "it's normal to have both mole and
    mole_under_mouth. What if I drop mole and keep only the granular
    one?"

    Valid, and the reasoning is not about fine control. At 100%
    presence neither tag gives control — you cannot turn off something
    that was always on. It is about WHICH token gets narrowed.

    Keep the general tag and your LoRA's "pillow" (140,800 posts)
    comes to mean "head resting on one". Keep the specific one and you
    narrow "head_on_pillow" (3,651 posts), which you would only prompt
    in this situation anyway. So the broader tag is the one to drop.
    """
    def posts(tag: str) -> int:
        return next((e.count for e in
                     tag_reference.lookup(tag, "mid2024").era_counts
                     if e.key == "mid2024"), 0) or 0

    n = 200
    everything = set(range(n))
    tags = {"zzq_trigger": everything, "1girl": everything,
            "mole": everything, "mole_under_mouth": everything,
            "pillow": everything, "head_on_pillow": everything}
    costs = {"mole": 1, "mole_under_mouth": 4,
             "pillow": 1, "head_on_pillow": 5}
    report = advisor.analyse(
        tags, n, set(), lambda t: "general",
        lambda t: costs.get(t, 2), ["character"],
        base_model_count=posts)

    # TIER 2, not Tier 1. CORRECTED: this was "safe to cut", and it
    # is not. Dropping the broader tag costs a control handle — a
    # word you can no longer put in a negative prompt to suppress the
    # feature. That is a judgement about how you intend to use the
    # model, which the tool cannot make for you.
    assert not [c for c in report.tier1
                if c.kind == advisor.KIND_BROADER]
    flagged = {c.tag: c for c in report.tier2
               if c.kind == advisor.KIND_BROADER}
    assert "mole" in flagged and "pillow" in flagged
    assert "mole_under_mouth" in flagged["mole"].reason
    # The narrower tags are never the ones flagged.
    assert "mole_under_mouth" not in flagged
    assert "head_on_pillow" not in flagged
    print("OK: a broader tag covered by a narrower one is raised for "
          "a decision rather than declared safe to cut")


def test_the_advice_weighs_how_strong_each_tag_is() -> None:
    """FIELD QUESTION: "shouldn't this be case by case? What if the
    target tag has very weak association in the base model?"

    Yes. The first version checked only that the narrower tag cleared
    an absolute floor of 1,000 posts, which treats 1,001 identically
    to 3,366,013. Measured against real pairs that is badly wrong:

        collar  185,335  <  sailor_collar   267,773   (145%)
        mole    244,794  <  mole_under_mouth 49,949   ( 20%)
        pillow  140,800  <  head_on_pillow    3,651   (2.6%)
        breasts 3,366,013 < framed_breasts    4,973   (0.1%)

    Advising "drop breasts, framed_breasts covers it" trades a
    3.4-million-post handle for a 5,000-post one.
    """
    def posts(tag: str) -> int:
        return next((e.count for e in
                     tag_reference.lookup(tag, "mid2024").era_counts
                     if e.key == "mid2024"), 0) or 0

    n = 200
    everything = set(range(n))
    tags = {"zzq_trigger": everything,
            "collar": everything, "sailor_collar": everything,
            "breasts": everything, "framed_breasts": everything}
    report = advisor.analyse(
        tags, n, set(), lambda t: "general", lambda t: 2,
        ["character"], base_model_count=posts)
    flagged = {c.tag: c for c in report.tier2
               if c.kind == advisor.KIND_BROADER}

    # Comparable strength: dropping the broader is a fair trade.
    assert "fair trade" in flagged["collar"].reason
    # Far weaker: keeping both is defensible, and it says so.
    assert "far rarer" in flagged["breasts"].reason
    assert "keeping both is defensible" in flagged["breasts"].reason

    # BOTH figures appear, so the trade can be judged at a glance
    # instead of taken on trust.
    assert "4,973" in flagged["breasts"].reason
    assert "3,366,013" in flagged["breasts"].reason

    # The weak case offers real alternatives rather than one verdict.
    detail = flagged["breasts"].detail
    assert "Keep BOTH" in detail
    assert "negative prompt" in detail
    print("OK: the advice weighs the two tags against each other, "
          "shows both figures, and offers alternatives when the "
          "narrower tag is the weaker handle")


def test_coverage_is_matched_on_whole_words() -> None:
    """Raw substring matching would find "ear" inside "bear"."""
    assert advisor.covers("pillow", "head_on_pillow")
    assert advisor.covers("mole", "mole_under_mouth")
    assert advisor.covers("skirt", "pleated_skirt")
    assert not advisor.covers("ear", "bear")
    assert not advisor.covers("mole", "mole")        # not itself
    assert not advisor.covers("1girl", "2girls")
    # NEGATIONS contain the very word they negate. Without this,
    # "no_bra" reads as a more specific kind of "bra" and the advice
    # becomes "drop bra, its own negation covers it" — backwards, and
    # confidently so. 78 such pairs exist in the vocabulary.
    assert not advisor.covers("bra", "no_bra")
    assert not advisor.covers("panties", "no_panties")
    assert not advisor.covers("apron", "no_apron")
    assert not advisor.covers("animal_ears", "fake_animal_ears")

    # A PARENTHETICAL qualifier marks a different thing sharing a
    # name, not a narrower version. "mole" and "mole_(animal)" are a
    # skin blemish and a burrowing mammal. 10,521 such pairs exist,
    # mostly character variants like "houshou_marine_(nun)".
    assert not advisor.covers("mole", "mole_(animal)")
    assert not advisor.covers("bow", "bow_(weapon)")

    # And the genuine ones survive both filters.
    assert advisor.covers("shirt", "off-shoulder_shirt")
    assert advisor.covers("breasts", "framed_breasts")
    assert advisor.covers("holding", "holding_polearm")

    # Honest limitation: cat_ears implies animal_ears and nothing in
    # the tag text says so. The shipped co-occurrence data cannot
    # substitute — p(mole|mole_under_mouth) is 0.641, not 1.0, because
    # Danbooru never applied implications retroactively. A partial
    # answer beats a guessed one.
    assert not advisor.covers("animal_ears", "cat_ears")
    print("OK: coverage is matched on whole underscore-separated "
          "words, not raw substrings")


def test_the_rare_verdict_shows_what_the_base_model_knows() -> None:
    """FIELD REPORT: well-known tags appeared in Tier 1 "too few to
    learn" and looked like a bug. They were not — each was under the
    threshold in the SELECTED era — but the row gave no way to tell a
    correct verdict from a wrong era setting."""
    def posts_in(era):
        def count(tag: str) -> int:
            return next((e.count for e in
                         tag_reference.lookup(tag, era).era_counts
                         if e.key == era), 0) or 0
        return count

    tags = {"1girl": set(range(200)), "hair_intakes": {0, 1}}

    modern = advisor.analyse(
        tags, 200, set(), lambda t: "general", _cost, ["character"],
        base_model_count=posts_in("mid2024"))
    kept = {c.tag for c in modern.tier3}
    assert "hair_intakes" in kept          # 109,971 posts in 2024

    ancient = advisor.analyse(
        tags, 200, set(), lambda t: "general", _cost, ["character"],
        base_model_count=posts_in("ancient"))
    cut = {c.tag: c.reason for c in ancient.tier1}
    assert "hair_intakes" in cut           # only 353 posts in 2017
    # The figure that decided it must be visible in the row.
    assert "353" in cut["hair_intakes"], cut["hair_intakes"]
    assert "base model" in cut["hair_intakes"]
    print("OK: a rare-tag verdict names the base-model post count, so "
          "a correct call can be told from a wrong era setting")


def test_a_settled_tag_is_not_argued_again() -> None:
    """FIELD REPORT: "some of these were in Tier 1 and I decided not
    to prune. I want to preserve the token because I will add more
    images later."

    The advisor reasons from the dataset, and neither of the user's
    two reasons is in the dataset. "I will collect more images" and "I
    want this for a negative prompt" are intentions about the future;
    no amount of counting reaches them.

    Repeating a recommendation someone has considered and rejected is
    how a tool teaches people to stop reading it, so a settled tag
    leaves the tiers entirely and is listed as decided."""
    def posts(tag: str) -> int:
        return next((e.count for e in
                     tag_reference.lookup(tag, "mid2024").era_counts
                     if e.key == "mid2024"), 0) or 0

    n = 200
    everything = set(range(n))
    tags = {"zzq_trig": everything, "1girl": everything,
            "mole": everything, "mole_under_mouth": everything,
            "rare_thing": {1, 2}}

    before = advisor.analyse(
        tags, n, set(), lambda t: "general", lambda t: 2,
        ["character"], base_model_count=posts)
    listed = {c.tag for c in
              before.tier1 + before.tier2 + before.tier3}
    assert "mole" in listed
    assert "rare_thing" in listed

    after = advisor.analyse(
        tags, n, set(), lambda t: "general", lambda t: 2,
        ["character"], base_model_count=posts,
        decisions={"mole": advisor.DECISION_KEEP_CONTROL,
                   "rare_thing": advisor.DECISION_KEEP_FUTURE})
    still_listed = {c.tag for c in
                    after.tier1 + after.tier2 + after.tier3}
    assert "mole" not in still_listed
    assert "rare_thing" not in still_listed

    settled = {c.tag: c for c in after.settled}
    assert set(settled) == {"mole", "rare_thing"}
    # The reason travels with it, so the record says WHY.
    assert "prompt control" in settled["mole"].reason
    assert "later" in settled["rare_thing"].reason
    # Everything else is judged exactly as before.
    assert "mole_under_mouth" in still_listed
    print("OK: a tag the user has settled leaves the tiers and is "
          "recorded with the reason instead of being re-argued")


def test_families_give_the_ai_the_data_it_was_missing() -> None:
    """FIELD NOTE: asked to compare the glove variants in a dataset,
    an AI could only refuse — the export carried the advisor's
    findings but never the vocabulary the question was about, and it
    was right to refuse rather than invent.

    The fix was not a better instruction. It was the missing table.

    COVERAGE is the figure that decides most of these: of the images
    carrying the broad tag, how many also carry a narrow one. At 100%
    the broad tag describes nothing the narrow ones do not; below it,
    the shortfall is exactly how many captions would fall silent."""
    tag_images = {
        "socks": set(range(200)),
        "black_socks": set(range(120)),
        "blue_socks": set(range(120, 200)),
        "gloves": set(range(100)),
        "fingerless_gloves": set(range(60)),
    }
    families = {f.broad: f for f in advisor.find_families(tag_images)}

    assert "socks" in families and "gloves" in families
    # Fully covered: every sock image carries a colour.
    assert families["socks"].coverage == 1.0
    # Partly covered: forty glove images carry no narrower tag, and
    # dropping "gloves" would leave those captions silent.
    assert abs(families["gloves"].coverage - 0.6) < 0.01
    assert families["gloves"].broad_count - families["gloves"].covered == 40
    # A tag with no narrower form is not a family.
    assert "black_socks" not in families

    section = advisor.build_families_section(
        advisor.find_families(tag_images))
    assert "fully covered" in section
    assert "no narrower tag" in section
    print("OK: families are reported with coverage, so the question "
          "the tool could not answer is answerable from the file")


def test_the_briefing_instructs_rather_than_interrogates() -> None:
    """REWRITTEN. The first version listed questions for the AI to
    answer — but "which families exist?" is answerable from the data,
    and "which should you keep?" depends on intent the AI cannot see.
    So it asked the model to guess at the only part that mattered.

    Where the objective is already settled the briefing now instructs
    directly; interviewing is reserved for the cases intent decides.
    """
    text = advisor.BRIEFING

    # Instructs the audit outright for families.
    assert "do not ask first" in text
    assert "case FOR" in text and "case AGAINST" in text
    assert "reverse your" in text
    # Reserves asking for the three intent questions.
    assert "not yet collected" in text
    assert "prompt control" in text
    assert "Where one of those decides a case, ask." in text
    # Says what is absent, and where to get it. This is the sentence
    # that would have prevented the original failure.
    assert "not their\nfull vocabulary" in text or \
        "full vocabulary" in text
    assert "Export Tag Statistics" in text
    assert "Never state a\ncount that is not in front of you" in text \
        or "Never state a" in text
    # No worked-example BLOCK: the original listed several users'
    # specific cases as if universal. A tag named illustratively in a
    # sentence (latex_gloves, to explain material inheritance) is
    # fine; a catalogue of someone's particular decisions is not.
    assert "yellow onesie" not in text
    # Still concise.
    assert len(text) < 3600

    report = advisor.PruneReport(
        total_images=200, total_tags=48, era_label="Illustrious",
        modes=["character"])
    block = advisor.build_questionnaire(report)
    assert "200" in block and "Illustrious" in block
    assert "Open questions" not in block
    print("OK: the briefing instructs a direct audit where the "
          "objective is settled and reserves questions for intent")


def test_the_analysis_does_not_freeze_a_real_dataset() -> None:
    """FIELD REPORT: "Pruning Advisor takes considerable time now — it
    hangs the program for about six seconds, and the popup crashed
    once."

    MEASURED on a 1,641-tag set: five seconds, and the cause was two
    separate all-pairs scans. Both find_families and the tier verdicts
    compared every tag against every other, at 1.56 million covers()
    calls each. On top of that the near-duplicate pass copied a list
    slice on every iteration — most of a million element copies.

    A six-second freeze is also the likeliest explanation for the
    crash: an unresponsive window is one Windows offers to close.

    The shape is asserted rather than the seconds, because a wall
    clock on shared hardware is a flaky test. What must hold is that
    the work stops being quadratic in the tag count."""
    import random

    random.seed(1)
    words = ["socks", "gloves", "hair", "eyes", "dress", "shirt",
             "skirt", "boots", "black", "blue", "red", "long",
             "short", "lace", "latex", "thigh", "ribbon", "collar"]

    def build(count: int) -> dict:
        tags = {}
        for i in range(count):
            size = random.choice([1, 1, 2, 2, 3])
            name = "_".join(random.sample(words, size))
            if random.random() < 0.5:
                name += f"_{i}"
            tags[name] = set(random.sample(range(1199),
                                           random.randint(1, 300)))
        return tags

    calls = [0]
    original = advisor.covers

    def counting(broad, narrow):
        calls[0] += 1
        return original(broad, narrow)

    advisor.covers = counting
    try:
        small = build(300)
        calls[0] = 0
        advisor.find_families(small)
        small_calls = calls[0]

        large = build(1200)
        calls[0] = 0
        advisor.find_families(large)
        large_calls = calls[0]
    finally:
        advisor.covers = original

    # Compared against the quadratic baseline, which is what the old
    # code actually did: every tag against every other.
    #
    # A ratio between the two sizes would be a weaker test here, since
    # this fixture draws from only eighteen words and so packs the
    # index buckets far tighter than a real vocabulary of thousands of
    # distinct words. Measuring against n-squared holds whatever shape
    # the vocabulary has.
    assert large_calls < (1200 * 1200) * 0.10, large_calls
    assert small_calls < (300 * 300) * 0.10, small_calls
    print(f"OK: the search costs a small fraction of comparing every "
          f"tag against every other ({large_calls:,} against "
          f"{1200 * 1200:,} at 1,200 tags)")


def test_indexing_finds_the_same_families() -> None:
    """The optimisation must not quietly lose a family. An absent one
    leaves no trace in the output, so it would never be reported."""
    tag_images = {
        "socks": set(range(200)),
        "black_socks": set(range(120)),
        "blue_socks": set(range(120, 200)),
        "gloves": set(range(100)),
        "fingerless_gloves": set(range(60)),
        "hair": set(range(50)),
        "long_hair": set(range(50)),
        "unrelated": set(range(10)),
    }
    families = {f.broad: f for f in advisor.find_families(tag_images)}
    assert set(families) == {"socks", "gloves", "hair"}
    assert {t for t, _c, _k in families["socks"].members} == \
        {"black_socks", "blue_socks"}
    assert families["socks"].coverage == 1.0
    assert abs(families["gloves"].coverage - 0.6) < 0.01
    print("OK: the indexed search finds exactly the families the "
          "pairwise scan did")


def test_families_name_the_images_that_carry_the_broad_tag_alone() -> None:
    """FIELD REQUEST: "I am dropping some broad tags but must check the
    images carrying them alone still have granular tokens. My current
    way is to filter to the tag and type out every variant by hand."

    The advisor already computes that set to get the coverage count,
    then threw it away. Keeping it, and listing the files by name,
    turns "12 images somewhere" into a list that can be opened — no
    variant filters typed by hand."""
    from pathlib import Path

    def P(name):
        return Path(f"D:/sets/{name}.png")

    tag_images = {
        "thighhighs": {P(f"img{i}") for i in range(20)},
        "black_thighhighs": {P(f"img{i}") for i in range(12)},
        "lace_thighhighs": {P(f"img{i}") for i in range(8, 18)},
    }
    family = advisor.find_families(tag_images)[0]
    # Covered: 0-17. Alone: 18, 19.
    assert {p.name for p in family.uncovered} == {"img18.png",
                                                  "img19.png"}
    assert family.broad_count - family.covered == 2

    section = advisor.build_families_section(
        advisor.find_families(tag_images))
    assert "carries `thighhighs` alone" in section
    assert "img18.png" in section and "img19.png" in section
    print("OK: a family names the exact images that carry the broad "
          "tag with no narrower one")


def test_the_uncovered_listing_is_capped() -> None:
    """One pathological broad tag must not bury the report under a
    thousand filenames; the count stays exact regardless."""
    from pathlib import Path

    tag_images = {
        "hair": {Path(f"/d/img{i}.png") for i in range(500)},
        "long_hair": {Path("/d/img0.png")},   # covers only one
    }
    family = advisor.find_families(tag_images)[0]
    assert len(family.uncovered) == 499

    section = advisor.build_families_section(
        advisor.find_families(tag_images))
    listed = section.count("    - img")
    assert listed <= advisor.UNCOVERED_LIST_CAP
    assert "more" in section          # the overflow is acknowledged
    print("OK: the uncovered listing caps its length while the count "
          "remains exact")


def test_the_briefing_covers_material_inheritance() -> None:
    """FIELD CASE the user raised: dropping latex_gloves when the set
    also has black_gloves, on the reasoning that latex + black_gloves
    signals a latex glove — valid only if no image pairs black_gloves
    with a different material.

    A distinct kind of judgement from broad/narrow coverage, so the
    briefing has to name it and, more importantly, name the condition
    that makes it safe."""
    text = advisor.BRIEFING
    assert "material" in text
    # The safety condition, not just the idea.
    assert "consistent" in text or "every instance of the form" in text
    assert "different material" in text.lower()
    print("OK: the briefing explains when a material-form compound tag "
          "is redundant and when dropping it would mislead")


def run() -> None:
    test_tier_one_needs_no_knowledge_of_intent()
    test_the_goal_decides_the_verdict()
    test_mixed_goals_report_disagreement_instead_of_choosing()
    test_near_duplicate_pairing_regressions()
    test_ranking_and_export()
    test_repeats_are_read_from_kohya_folder_names()
    test_exposure_mode_answers_a_different_question()
    test_the_floor_is_the_tools_judgement_not_an_input()
    test_help_is_paged_so_it_cannot_outgrow_the_screen()
    test_rare_but_known_to_the_base_model_is_not_unlearnable()
    test_base_model_counts_come_from_the_shipped_snapshots()
    test_trigger_tokens_are_never_recommended()
    test_medium_tags_are_not_treated_as_bookkeeping()
    test_sibling_detection_protects_majority_values()
    test_per_caption_and_total_token_costs_are_separate()
    test_era_follows_the_preference()
    test_tier3_is_grouped_by_reason()
    test_conflicts_name_which_goal_wants_what()
    test_tier3_groups_render_and_only_decisions_open()
    test_a_broader_tag_covered_by_a_narrower_one()
    test_the_advice_weighs_how_strong_each_tag_is()
    test_coverage_is_matched_on_whole_words()
    test_the_rare_verdict_shows_what_the_base_model_knows()
    test_a_settled_tag_is_not_argued_again()
    test_families_give_the_ai_the_data_it_was_missing()
    test_indexing_finds_the_same_families()
    test_the_analysis_does_not_freeze_a_real_dataset()
    test_the_briefing_instructs_rather_than_interrogates()
    test_families_name_the_images_that_carry_the_broad_tag_alone()
    test_the_uncovered_listing_is_capped()
    test_the_briefing_covers_material_inheritance()
    test_dialog_and_the_no_write_guarantee()
    print("\nALL PASS: prune advisor")


if __name__ == "__main__":
    run()
