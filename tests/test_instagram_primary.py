"""Offline tests for the Instagram anti-bot scraper (D-20260807-002).

No network, no browser: exercises the pure static functions, the cookie
fail-closed gate, and the discovery-compatible row mapping. The session
cookie check is validated to raise BEFORE any browser launch.
"""

import asyncio
import os

import pytest

from psychological.scrapers.instagram_primary import (
    InstagramConfig,
    InstagramCookieMissing,
    InstagramSession,
    InstagramCoolDown,
    InstagramChallengeDetected,
    InstagramSessionUnavailable,
    detect_instagram_challenge,
    detect_instagram_login_wall,
    parse_shared_data,
    extract_tickers,
    compute_sentiment,
    to_mention_row,
    fetch_instagram_mentions,
    parse_embedded_media_json,
    _unwrap_nodriver,
    _run_scrape_session,
    scrape_instagram_long,
)

TAG_HTML = """
<html><body><script type="text/javascript">
window._sharedData = {"entry_data": {"TagPage": [{"graphql": {"hashtag": {"edge_hashtag_to_media": {"edges": [
  {"node": {"shortcode": "ABC123", "edge_media_to_caption": {"edges": [{"node": {"text": "Tesla moon #stocks #TSLA"}}]}, "edge_liked_by": {"count": 10}, "edge_media_to_comment": {"count": 2}, "video_view_count": 1000, "owner": {"username": "trader", "edge_followed_by": {"count": 500}, "is_verified": true}}},
  {"node": {"shortcode": "DEF456", "edge_media_to_caption": {"edges": [{"node": {"text": "AMD #trading"}}]}, "edge_media_preview_like": {"count": 5}, "edge_media_to_comment": {"count": 1}, "owner": {"username": "investor", "edge_followed_by": {"count": 100}, "is_verified": false}}}
]}}}}]}}};
</script></body></html>
"""

ADDITIONAL_HTML = """
<html><body><script type="text/javascript">
window.__additionalDataLoaded('extra',{"entry_data": {"TagPage": [{"graphql": {"hashtag": {"edge_hashtag_to_media": {"edges": [
  {"node": {"shortcode": "GHI789", "edge_media_to_caption": {"edges": [{"node": {"text": "Nvidia #nvidia"}}]}, "edge_liked_by": {"count": 7}, "edge_media_to_comment": {"count": 1}, "video_view_count": 2000, "owner": {"username": "nv", "edge_followed_by": {"count": 50}, "is_verified": false}}}
]}}}}]}});
</script></body></html>
"""


class TestDetectChallenge:
    def test_challenge_snippet_is_true(self):
        html = '<a href="/accounts/challenge?next=%2F">We detected unusual activity</a>'
        assert detect_instagram_challenge(html) is True

    def test_normal_html_is_false(self):
        assert detect_instagram_challenge("<html><body>hello world</body></html>") is False

    def test_empty_is_false(self):
        assert detect_instagram_challenge("") is False


class TestDetectLoginWall:
    def test_login_wall_is_true(self):
        assert detect_instagram_login_wall("Log in to see photos and videos") is True

    def test_normal_page_is_false(self):
        assert detect_instagram_login_wall("<html><body>posts here</body></html>") is False


class TestParseSharedData:
    def test_parses_tag_page_edges(self):
        posts = parse_shared_data(TAG_HTML)
        assert len(posts) == 2
        video = posts[0]
        assert video["shortcode"] == "ABC123"
        assert video["caption"] == "Tesla moon #stocks #TSLA"
        assert video["hashtags"] == ["stocks", "TSLA"]
        assert video["likes"] == 10
        assert video["comments"] == 2
        assert video["views"] == 1000
        assert video["author_username"] == "trader"
        assert video["author_followers"] == 500
        assert video["author_verified"] is True
        image = posts[1]
        assert image["shortcode"] == "DEF456"
        assert image["views"] is None
        assert image["likes"] == 5
        assert image["author_verified"] is False

    def test_parses_additional_data_loaded(self):
        posts = parse_shared_data(ADDITIONAL_HTML)
        assert len(posts) == 1
        assert posts[0]["shortcode"] == "GHI789"
        assert posts[0]["views"] == 2000

    def test_empty_html_returns_empty(self):
        assert parse_shared_data("") == []
        assert parse_shared_data("<html>no json</html>") == []


class TestExtractTickers:
    def test_cashtag_ticker_extracted(self):
        result = extract_tickers("$TSLA is a great buy today")
        assert "TSLA" in result

    def test_long_allcaps_word_not_matched(self):
        result = extract_tickers("NVIDIA earnings call")
        assert result == []
        assert "NVDA" not in result
        assert "NVIDIA" not in result

    def test_blacklisted_words_excluded(self):
        result = extract_tickers("I love THE TOP stock AMD")
        assert "AMD" in result
        assert "THE" not in result
        assert "TOP" not in result

    def test_universe_filters_english_words(self):
        from psychological.scrapers.instagram_primary import _real_ticker_universe

        universe = _real_ticker_universe()
        assert "BEAR" not in universe
        assert "BUY" not in universe
        assert "HTTPS" not in universe
        result = extract_tickers("BUY HOLD BEAR CHEAP MORE BLOG HTTPS")
        assert result == []

    def test_universe_keeps_real_tickers(self):
        result = extract_tickers("Ero Copper TSX:ERO HOLD ... NVDA to the moon")
        assert "NVDA" in result
        assert "ERO" in result

    def test_universe_drops_unregistered_tokens(self):
        from psychological.scrapers.instagram_primary import _real_ticker_universe

        assert "ALGO" not in _real_ticker_universe()
        result = extract_tickers("ALGO is the future, BTC to 100k")
        assert "ALGO" not in result

    def test_lowercase_collision_words_do_not_match(self):
        # Regression: lowercase prose words that collide with real SEC tickers
        # must NOT be extracted (previously text.upper() turned them into
        # false candidates).
        for phrase, word in [
            ("Small accessory. Big main character energy.", "MAIN"),
            ("One bracelet, four different vibes!", "FOUR"),
            ("Join us at the link in bio", "LINK"),
            ("Buy NOW before it moons", "NOW"),
            ("Stay bold with your picks", "BOLD"),
            ("Anything else you want?", "ANY"),
            ("He gave five reasons today", "FIVE"),
            ("We saw real growth in revenue", "REAL"),
        ]:
            result = extract_tickers(phrase)
            assert word not in result, f"{word} leaked from: {phrase!r} -> {result}"

    def test_uppercase_ticker_still_extracted_in_mixed_case(self):
        # Uppercase tickers in otherwise-lowercase prose are still found.
        result = extract_tickers("I am a bold investor buying TSLA today")
        assert "TSLA" in result
        assert "BOLD" not in result

    def test_collision_words_blacklisted_even_when_uppercased(self):
        # Safety net: even if text is fully uppercased upstream, collision
        # words are filtered by the augmented _COMMON_WORDS blacklist.
        from psychological.scrapers.instagram_primary import _COMMON_WORDS

        for w in ["MAIN", "FOUR", "LINK", "NOW", "BOLD", "ANY", "FIVE"]:
            assert w in _COMMON_WORDS, f"{w} missing from _COMMON_WORDS"
        result = extract_tickers("MAIN FOUR LINK NOW BOLD ANY FIVE")
        assert result == []


class TestComputeSentiment:
    def test_bullish_positive(self):
        score = compute_sentiment("bullish call moon")
        assert score is not None
        assert score > 0

    def test_bearish_negative(self):
        score = compute_sentiment("crash dump overvalued")
        assert score is not None
        assert score < 0

    def test_no_match_returns_none(self):
        assert compute_sentiment("hello world") is None


class TestToMentionRow:
    def test_row_shape(self):
        post = {
            "shortcode": "ABC123",
            "caption": "$TSLA rocket call",
            "hashtags": ["stocks"],
            "likes": 100,
            "comments": 5,
            "views": 1000,
            "author_followers": 500,
            "author_verified": True,
        }
        rows = to_mention_row(post, fetch_ts=1234)
        assert len(rows) == 1
        row = rows[0]
        assert row["entity"] == "TSLA"
        assert row["topic"] == "Stocks"
        assert row["source_confidence"] == 0.6
        assert row["volume_or_rank"] == 1000
        assert isinstance(row["sentiment"], (int, float))
        assert "ABC123" in row["external_id"]
        for key in ("caption", "hashtags", "comments", "views", "followers", "verified", "brand_account"):
            assert key in row
        # FinBERT fields ride along fail-closed (None offline / live-gated).
        assert row["finbert_label"] is None
        assert row["finbert_sentiment"] is None
        assert row["finbert_confidence"] is None

    def test_no_tickers_returns_empty(self):
        # Every word here is in the verbatim common-words blacklist, so the
        # extractor must yield no tickers and no mention rows.
        assert to_mention_row({"caption": "the and for but all with can has old"}, fetch_ts=1) == []

    def test_transcribe_video_audio_graceful_fail(self):
        # Since Whisper/ffmpeg might be absent or network call fails, it should fail closed/gracefully.
        from psychological.scrapers.instagram_primary import transcribe_video_audio
        res = transcribe_video_audio("https://example.com/nonexistent_video.mp4")
        assert res == ""


class TestFetchMentionsCookieGate:
    def test_missing_cookie_raises_before_browser(self, tmp_path):
        cfg = InstagramConfig({"session_file": str(tmp_path / "nope.json")})
        with pytest.raises(InstagramCookieMissing):
            fetch_instagram_mentions(limit=5, config=cfg)


class TestEmbeddedMediaJson:
    def test_parse_embedded_media_json_finds_web_info(self):
        json_data = {
            "require": [
                [
                    "ScheduledServerJS",
                    "handle",
                    None,
                    [
                        {
                            "__bbox": {
                                "require": [
                                    [
                                        "RelayPrefetchedStreamCache",
                                        "next",
                                        [],
                                        [
                                            "adp_PolarisPostRootQueryRelayPreloader_abc",
                                            {
                                                "__bbox": {
                                                    "result": {
                                                        "data": {
                                                            "xdt_api__v1__media__shortcode__web_info": {
                                                                "items": [
                                                                    {
                                                                        "code": "ABC123",
                                                                        "like_count": 10,
                                                                        "comment_count": 2,
                                                                        "caption": {"text": "Buy $NVDA #stocks"},
                                                                        "user": {"username": "bob", "is_verified": False}
                                                                    }
                                                                ]
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        ]
                                    ]
                                ]
                            }
                        }
                    ]
                ]
            ]
        }
        import json
        html = f'<html><body><script type="application/json" data-processed="1">{json.dumps(json_data)}</script></body></html>'
        posts = parse_embedded_media_json(html)
        assert len(posts) == 1
        p = posts[0]
        assert p["shortcode"] == "ABC123"
        assert p["likes"] == 10
        assert "NVDA" in p["caption"]
        assert p["author_username"] == "bob"
        assert p["author_verified"] is False

    def test_parse_embedded_media_json_empty_on_garbage(self):
        assert parse_embedded_media_json("<html><body><script>garbage</script></body></html>") == []

    def test_unwrap_nodriver(self):
        wrapped_scalar = {"type": "string", "value": "/p/abc/"}
        assert _unwrap_nodriver(wrapped_scalar) == "/p/abc/"

        wrapped_list = [{"type": "string", "value": "/p/abc/"}, {"type": "string", "value": "/p/def/"}]
        assert _unwrap_nodriver(wrapped_list) == ["/p/abc/", "/p/def/"]

        wrapped_obj = {
            "type": "object",
            "value": [
                ["key1", {"type": "string", "value": "val1"}],
                ["key2", {"type": "number", "value": 42}]
            ]
        }
        assert _unwrap_nodriver(wrapped_obj) == {"key1": "val1", "key2": 42}

        plain_dict = {"a": 1, "b": [2, 3]}
        assert _unwrap_nodriver(plain_dict) == {"a": 1, "b": [2, 3]}

    def test_parse_embedded_media_json_dedupes_by_shortcode(self):
        item1 = {
            "code": "ABC123",
            "like_count": 10,
            "comment_count": 2,
            "caption": {"text": "Buy $NVDA #stocks"},
            "user": {"username": "bob", "is_verified": False}
        }
        item2 = {
            "code": "ABC123",
            "like_count": 20,
            "comment_count": 5,
            "caption": {"text": "Buy $NVDA #stocks again"},
            "user": {"username": "bob", "is_verified": False}
        }
        json_data = {
            "xdt_api__v1__media__shortcode__web_info": {
                "items": [item1, item2]
            }
        }
        import json
        html = f'<html><body><script type="application/json">{json.dumps(json_data)}</script></body></html>'
        posts = parse_embedded_media_json(html)
        assert len(posts) == 1
        assert posts[0]["shortcode"] == "ABC123"
        assert posts[0]["likes"] == 10


class TestPrivateApi:
    def test_decode_shortcode(self):
        from psychological.scrapers.instagram_primary import _decode_shortcode

        assert _decode_shortcode("DVJ1lFriGsm") == "3839835802494855974"

    def test_normalize_private_api_item(self):
        from psychological.scrapers.instagram_primary import _normalize_private_api_item

        item = {
            "code": "ABC123",
            "like_count": 242620,
            "comment_count": 844,
            "caption": {"text": "Buy $NVDA and $AMD #stocks #trading"},
            "user": {"username": "intevia.analytics", "is_verified": True},
            "view_count": 12000,
        }
        post = _normalize_private_api_item(item)
        assert post["shortcode"] == "ABC123"
        assert post["likes"] == 242620
        assert post["comments"] == 844
        assert post["views"] == 12000
        assert post["author_username"] == "intevia.analytics"
        assert post["author_verified"] is True
        assert "NVDA" in post["caption"]
        assert "stocks" in post["hashtags"]

    def test_normalize_private_api_item_defensive(self):
        from psychological.scrapers.instagram_primary import _normalize_private_api_item

        post = _normalize_private_api_item({})
        assert post["shortcode"] == ""
        assert post["caption"] == ""
        assert post["likes"] == 0
        assert post["comments"] == 0
        assert post["author_username"] == ""
        assert post["author_verified"] is False


class TestPacingConfig:
    def test_defaults_via_dict(self):
        cfg = InstagramConfig({
            "max_active_hours": 6.0,
            "inter_block_gap_seconds": [60, 120],
            "max_empty_blocks": 5,
        })
        assert cfg.max_active_hours == 6.0
        assert cfg.inter_block_gap_seconds == (60.0, 120.0)
        assert cfg.max_empty_blocks == 5

    def test_bad_gap_values_fall_back(self):
        cfg = InstagramConfig({"inter_block_gap_seconds": "bogus"})
        assert cfg.inter_block_gap_seconds == (1200.0, 2400.0)


class TestPacingSupervisor:
    def _cfg(self, **overrides):
        base = {
            "session_file": "config/instagram_cookies.json",
            "max_active_hours": 1.0,
            "inter_block_gap_seconds": [0.0001, 0.0001],
            "session_cool_down_seconds": 0.001,
        }
        base.update(overrides)
        return InstagramConfig(base)

    def test_hard_stop_after_max_active(self, monkeypatch):
        rows = [{"entity": "TSLA", "external_id": "https://www.instagram.com/p/ABC/"}]

        async def fake_fetch(limit, config):
            await asyncio.sleep(0.05)
            return rows

        monkeypatch.setattr(
            "psychological.scrapers.instagram_primary._fetch_mentions_async",
            fake_fetch,
        )
        # 0.00005h == 0.18s active cap; each block takes ~0.05s so the loop
        # must terminate after a handful of blocks (proves hard stop, no hang).
        summary = asyncio.run(_run_scrape_session(10, self._cfg(max_active_hours=0.00005)))
        assert summary["blocks"] >= 1
        assert summary["rows"] == summary["blocks"] * len(rows)

    def test_stops_after_max_empty_blocks(self, monkeypatch):
        async def fake_fetch(limit, config):
            return []

        monkeypatch.setattr(
            "psychological.scrapers.instagram_primary._fetch_mentions_async",
            fake_fetch,
        )
        summary = asyncio.run(_run_scrape_session(10, self._cfg(max_empty_blocks=3)))
        assert summary["blocks"] == 3
        assert summary["rows"] == 0

    def test_challenge_fails_hard(self, monkeypatch):
        async def fake_fetch(limit, config):
            raise InstagramChallengeDetected("challenge")

        monkeypatch.setattr(
            "psychological.scrapers.instagram_primary._fetch_mentions_async",
            fake_fetch,
        )
        with pytest.raises(InstagramChallengeDetected):
            asyncio.run(_run_scrape_session(10, self._cfg()))

    def test_login_wall_fails_hard(self, monkeypatch):
        async def fake_fetch(limit, config):
            raise InstagramSessionUnavailable("login wall")

        monkeypatch.setattr(
            "psychological.scrapers.instagram_primary._fetch_mentions_async",
            fake_fetch,
        )
        with pytest.raises(InstagramSessionUnavailable):
            asyncio.run(_run_scrape_session(10, self._cfg()))

    def test_cool_down_then_continue(self, monkeypatch):
        calls = {"n": 0}
        rows = [{"entity": "NVDA", "external_id": "https://www.instagram.com/p/DEF/"}]

        async def fake_fetch(limit, config):
            calls["n"] += 1
            if calls["n"] == 1:
                raise InstagramCoolDown("page budget exhausted")
            if calls["n"] == 2:
                return rows
            return []  # after recovery, empty passes drive the empty-streak stop

        monkeypatch.setattr(
            "psychological.scrapers.instagram_primary._fetch_mentions_async",
            fake_fetch,
        )
        summary = asyncio.run(_run_scrape_session(10, self._cfg()))
        # cool-down did NOT fail-hard (block 2 ran and yielded rows) and the
        # run still terminated via the empty-streak rule.
        assert summary["blocks"] >= 3
        assert summary["rows"] == len(rows)

    def test_long_entrypoint_cookie_gate(self, tmp_path):
        cfg = InstagramConfig({"session_file": str(tmp_path / "nope.json")})
        with pytest.raises(InstagramCookieMissing):
            scrape_instagram_long(limit=5, config=cfg)


class _FakeCookie:
    def __init__(self, name):
        self.name = name
        self.value = "v"
        self.domain = ".instagram.com"
        self.path = "/"
        self.secure = True
        self.expires = None


class _FakeJar:
    def __init__(self, names):
        self._cookies = [_FakeCookie(n) for n in names]

    async def get_all(self):
        return self._cookies


class _FakeTab:
    def __init__(self, names):
        self.cookies = _FakeJar(names)


class _FakeSession:
    def __init__(self, names):
        self._tab = _FakeTab(names)

    def get_tab(self):
        return self._tab


class TestSaveCookiesGuestGuard:
    def test_guest_jar_never_overwrites(self, tmp_path):
        target = str(tmp_path / "cookies.json")
        with open(target, "w") as f:
            f.write("{}")
        session = InstagramSession(InstagramConfig({"session_file": target}))
        session._session = _FakeSession(["mid", "ig_did"])  # no sessionid/ds_user_id
        asyncio.run(session.save_cookies())
        assert open(target).read() == "{}"

    def test_authenticated_jar_is_saved(self, tmp_path):
        target = str(tmp_path / "cookies.json")
        session = InstagramSession(InstagramConfig({"session_file": target}))
        session._session = _FakeSession(["sessionid", "ig_did"])
        asyncio.run(session.save_cookies())
        assert os.path.exists(target)
        assert "sessionid" in open(target).read()


class TestFinBertGrading:
    """FinBERT is live-gated (DISCOVERY_LIVE=1) and fail-closed offline.

    Offline the grader must never attempt a model load: grading returns None
    and the mention-row mapping keeps its fail-closed FinBERT fields.
    """

    def test_grade_text_returns_none_offline(self, monkeypatch):
        monkeypatch.delenv("DISCOVERY_LIVE", raising=False)
        from psychological.scrapers.finbert_sentiment import grade_text
        assert grade_text("Apple reported record earnings.") is None

    def test_grade_batch_returns_none_list_offline(self, monkeypatch):
        monkeypatch.delenv("DISCOVERY_LIVE", raising=False)
        from psychological.scrapers.finbert_sentiment import grade_batch
        assert grade_batch(["buy", "sell", ""]) == [None, None, None]

    def test_grade_text_empty_is_none(self, monkeypatch):
        monkeypatch.setenv("DISCOVERY_LIVE", "1")
        from psychological.scrapers.finbert_sentiment import grade_text
        assert grade_text("") is None
        assert grade_text("   ") is None
