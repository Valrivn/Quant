"""Offline tests for the Instagram anti-bot scraper (D-20260807-002).

No network, no browser: exercises the pure static functions, the cookie
fail-closed gate, and the discovery-compatible row mapping. The session
cookie check is validated to raise BEFORE any browser launch.
"""

import pytest

from psychological.scrapers.instagram_primary import (
    InstagramConfig,
    InstagramCookieMissing,
    detect_instagram_challenge,
    detect_instagram_login_wall,
    parse_shared_data,
    extract_tickers,
    compute_sentiment,
    to_mention_row,
    fetch_instagram_mentions,
    parse_embedded_media_json,
    _unwrap_nodriver,
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

    def test_no_tickers_returns_empty(self):
        # Every word here is in the verbatim common-words blacklist, so the
        # extractor must yield no tickers and no mention rows.
        assert to_mention_row({"caption": "the and for but all with can has old"}, fetch_ts=1) == []


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
