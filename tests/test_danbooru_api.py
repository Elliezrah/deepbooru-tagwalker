"""
tests/test_danbooru_api.py

Tests for core/danbooru_api.py — URL contracts, tolerant response
parsing, and the two independent disk caches (DESIGN_TAG_REFERENCE.md
decisions 4/7 and the "Available data by endpoint" section). All
offline: canned JSON stands in for the network, per the spec's
testing approach.

Run: python3 tests/test_danbooru_api.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TAGWALKER_CONFIG_DIR", tempfile.mkdtemp())

from pathlib import Path

from core import danbooru_api as api


def test_url_contracts() -> None:
    assert api.wiki_url("Tag group:Attire") == \
        "https://danbooru.donmai.us/wiki_pages/tag_group%3Aattire.json"
    # The batch is ONE id metatag — a single search term no matter
    # how many ids, so the anonymous term cap is never at risk.
    assert api.posts_by_ids_url([100, 101, 202]) == \
        ("https://danbooru.donmai.us/posts.json"
         "?tags=id%3A100%2C101%2C202&limit=3")
    assert api.post_url(55) == \
        "https://danbooru.donmai.us/posts/55.json"
    assert "name_matches" in api.tag_search_url("red_curtia")
    assert api.USER_AGENT.startswith("TagWalker/")
    print("OK: URL builders match the spec — folded wiki paths, "
          "single-term id batch, per-id fallback, similarity search")


def test_wiki_parsing_tolerant() -> None:
    w = api.parse_wiki({"title": "high_heels", "body": "Footwear...",
                        "other_names": ["\u30cf\u30a4\u30d2\u30fc\u30eb"],
                        "updated_at": "2026-01-01"})
    assert w is not None and w.title == "high_heels"
    assert w.other_names == ["\u30cf\u30a4\u30d2\u30fc\u30eb"]
    assert api.parse_wiki({"success": False}) is None
    assert api.parse_wiki([]) is None
    assert api.parse_wiki(None) is None
    one = api.parse_wiki([{"title": "t", "body": "b"}])
    assert one is not None and one.title == "t"    # list form tolerated
    print("OK: wiki parsing takes dict or list, every field "
          "defaulted, unusable pages return None for the "
          "similarity path")


def test_post_parsing_order_skip_and_tag_count() -> None:
    posts_json = [
        {"id": 202, "preview_file_url": "p202",
         "large_file_url": "l202", "rating": "g",
         "tag_string": "a b c", "tag_count": 3},
        {"id": 100, "preview_file_url": "", "large_file_url": "",
         "rating": "e", "tag_string": "x", "is_banned": True},
        {"id": 101, "preview_file_url": "p101",
         "large_file_url": "l101", "rating": "s",
         "tag_string": "one two three four"},
    ]
    posts = api.parse_posts(posts_json, wanted_order=[100, 101, 202])
    assert [p.id for p in posts] == [100, 101, 202]   # editors' order
    assert posts[0].skip and posts[0].skip_reason == "banned"
    assert posts[2].tag_count == 3                     # field wins
    assert posts[1].tag_count == 4                     # fallback: split
    assert api.parse_posts({"id": 7, "preview_file_url": "p"})[0].id == 7
    assert api.parse_posts("garbage") == []
    blank = api.parse_posts([{"id": 9}])[0]
    assert blank.skip and "preview" in blank.skip_reason
    print("OK: posts restore the wiki's embed order; banned/deleted/"
          "blank-URL posts flagged skip with a reason; tag_count "
          "(decision 17) from the field with a tag_string fallback")


def test_text_cache_ttl_and_corruption() -> None:
    d = Path(tempfile.mkdtemp())
    tc = api.TextCache(d / "text", ttl_days=1)
    tc.put("high_heels", {"title": "x"})
    assert tc.get("high_heels") == {"title": "x"}
    p = d / "text" / "high_heels.json"
    os.utime(p, (time.time() - 3 * 86400, time.time() - 3 * 86400))
    assert tc.get("high_heels") is None            # TTL expiry
    (d / "text" / "broken.json").write_text("{no", encoding="utf-8")
    assert tc.get("broken") is None                # corrupt -> miss
    assert tc.clear() >= 2 and tc.size_bytes() == 0
    print("OK: text cache round-trips, expires by TTL, shrugs off "
          "corruption, clears completely")


def test_image_cache_lru() -> None:
    d = Path(tempfile.mkdtemp())
    # memory_cap_bytes=0 so this exercises the DISK cache alone: the
    # memory cache would otherwise still serve an entry the disk has
    # evicted, which is correct behaviour but not what is under test.
    ic = api.ImageCache(d / "img", cap_bytes=250, memory_cap_bytes=0)
    ic.put("1", b"a" * 100)
    time.sleep(0.02)
    ic.put("2", b"b" * 100)
    time.sleep(0.02)
    assert ic.get("1") == b"a" * 100               # touch 1
    time.sleep(0.02)
    ic.put("3", b"c" * 100)                        # 300 > 250
    # Eviction no longer runs on the write path: sweeping the cache
    # directory after every write is what made browsing seize up as
    # the cache filled. The LRU ORDER is unchanged; only when the
    # sweep happens has moved.
    # NOT ic.get("2") here: reading an entry touches it, which would
    # make it the newest and change which one the sweep drops. Check
    # the file directly instead.
    assert (ic.dir / "2.bin").exists()             # still there
    ic.maybe_evict(force=True)
    assert ic.get("2") is None                     # LRU evicted
    assert ic.get("1") == b"a" * 100
    assert ic.get("3") == b"c" * 100
    assert ic.size_bytes() <= 250
    assert ic.clear() == 2 and ic.size_bytes() == 0
    print("OK: image cache evicts least-recently-used to the byte "
          "cap, access refreshes recency, clear is independent of "
          "the text cache")


def run() -> None:
    test_url_contracts()
    test_wiki_parsing_tolerant()
    test_post_parsing_order_skip_and_tag_count()
    test_text_cache_ttl_and_corruption()
    test_image_cache_lru()
    print("\nALL PASS: danbooru api contracts + caches")


if __name__ == "__main__":
    run()
