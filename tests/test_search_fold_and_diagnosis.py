"""Tests for search Unicode-folding and the zero-result diagnosis.

Field report #2: searching the full tag "light_smile" showed nothing
while the prefix "light_smi" matched. The engine was correct — the
dataset held an IMPOSTOR tag: a near-typo (light_smilie / light_smiling)
or a visually identical tag carrying an invisible / fullwidth Unicode
character. Two fixes, both pinned here:

1. Matching now folds both sides (NFKC + strip zero-widths + lowercase
   + space→underscore), so invisible-char lookalikes match the text the
   user can actually see — in substring, exact, and negated forms.
2. When a search truly matches nothing, the queue panel names the
   nearest real tags with counts instead of a silent empty list, and
   the status reads "0 images match the search" rather than the false
   "No tag selected".

Run: python3 tests/test_search_fold_and_diagnosis.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path
from PIL import Image
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from core.state import SessionState, FilterMode, _fold_for_match
from core.scanner import scan
from ui.queue_panel import QueuePanel

ZWSP = "\u200b"


def _state():
    d = Path(tempfile.mkdtemp()) / "ds"
    d.mkdir(parents=True)
    for n, t in {
        "a": f"1girl, light_smi{ZWSP}le",      # invisible-char impostor
        "b": "1girl, light_smilie",             # near-typo
        "c": "1girl, light_smiling",            # variant
        "d": "1girl, smile",
    }.items():
        Image.new("RGB", (8, 8)).save(d / f"{n}.png")
        (d / f"{n}.txt").write_text(t, encoding="utf-8")
    st = SessionState(scan(d))
    st.select_tag("1girl")
    st.set_filter_mode(FilterMode.ALL)
    return st


def _q(st):
    return sorted(i.image_path.stem for i in st.get_current_queue())


def test_fold_helper() -> None:
    f = _fold_for_match
    assert f(f"light_smi{ZWSP}le") == "light_smile"
    assert f("light_smi\uff4ce") == "light_smile"      # fullwidth l
    assert f("light\uff3fsmile") == "light_smile"      # fullwidth _
    assert f("Light Smile") == "light_smile"
    print("OK: _fold_for_match collapses zero-widths, fullwidth forms, "
          "case and spaces")


def test_full_query_matches_invisible_impostor() -> None:
    st = _state()
    st.set_queue_search("light_smile")                  # the field query
    assert _q(st) == ["a"], _q(st)                      # was [] before
    st.set_queue_search_exact(True)
    st.set_queue_search("")
    st.set_queue_search("light_smile")
    assert _q(st) == ["a"], _q(st)
    st.set_queue_search_exact(False)
    st.set_queue_search("")
    print("OK: the full tag text now matches an invisible-character "
          "impostor, substring and exact modes alike")


def test_folded_negation_and_pasted_fullwidth_query() -> None:
    st = _state()
    st.set_queue_search("-light_smile")
    assert _q(st) == ["b", "c", "d"], _q(st)            # impostor excluded
    st.set_queue_search("light_smi\uff4ce")             # pasted fullwidth
    assert _q(st) == ["a"], _q(st)
    st.set_queue_search("")
    print("OK: negation folds too, and a fullwidth-polluted QUERY still "
          "finds the tag")


def test_true_variants_still_do_not_match() -> None:
    st = _state()
    st.set_queue_search("light_smile")
    assert "b" not in _q(st) and "c" not in _q(st)
    st.set_queue_search("")
    print("OK: genuinely different spellings (smilie/smiling) are still "
          "distinct tags — folding is not fuzzy matching")


def test_suggest_similar_tags() -> None:
    st = _state()
    near = dict(st.suggest_similar_tags("light_smirk"))
    assert "light_smilie" in near and "light_smiling" in near, near
    assert f"light_smi{ZWSP}le" in near, near
    assert st.suggest_similar_tags("") == []
    assert st.suggest_similar_tags("zzz_totally_unrelated") == []
    print("OK: suggest_similar_tags names the near tags (impostor "
          "included) with counts")


def test_zero_result_diagnosis_in_panel() -> None:
    st = _state()
    panel = QueuePanel()
    panel.attach(st)
    st.set_queue_search("light_smirk")                  # matches nothing
    assert panel.search_hint.isHidden() is False
    txt = panel.search_hint.text()
    assert "light_smilie" in txt and "light_smiling" in txt, txt
    assert panel.status_label.text() == "0 images match the search"
    st.set_queue_search("smile")                        # has matches
    assert panel.search_hint.isHidden() is True
    st.set_queue_search("")
    assert panel.search_hint.isHidden() is True
    print("OK: a no-match search shows the diagnosis label + truthful "
          "status; it hides again on matches or clear")


def run() -> None:
    test_fold_helper()
    test_full_query_matches_invisible_impostor()
    test_folded_negation_and_pasted_fullwidth_query()
    test_true_variants_still_do_not_match()
    test_suggest_similar_tags()
    test_zero_result_diagnosis_in_panel()
    print("\nALL PASS: search Unicode-folding + zero-result diagnosis")


if __name__ == "__main__":
    run()
