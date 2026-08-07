"""Tests for the video-signal hygiene sanitizers (D-20260806-001 P2, SEC 4).

Covers: clout-chaser (runup + velocity + lag), niche/minimal-popularity band,
and ad/sponsored detection (hashtag / affiliate-link / brand / engagement).
All deterministic, fixture-based, no network.
"""

import pytest

from discovery.sanitizers import (
    CloutChaserSanitizer,
    NicheSanitizer,
    AdSanitizer,
    KEEP,
    EXCLUDE,
)


class TestCloutChaser:
    def setup_method(self):
        self.s = CloutChaserSanitizer()

    def test_exclude_when_runup_velocity_lag_all_hold(self):
        # price near 52w high, high velocity, spike lags run-up start.
        v = self.s.evaluate(
            price=95.0, week_52_high=100.0,
            mentions_7d=30.0, mentions_28d=5.0, explosion_lag_days=5,
        )
        assert v.excluded
        assert "runup_ratio>=floor" in v.reason_codes
        assert "spike_lags_runup" in v.reason_codes

    def test_keep_when_runup_low(self):
        v = self.s.evaluate(
            price=50.0, week_52_high=100.0,
            mentions_7d=30.0, mentions_28d=5.0, explosion_lag_days=5,
        )
        assert v.action == KEEP

    def test_keep_when_velocity_low(self):
        v = self.s.evaluate(
            price=95.0, week_52_high=100.0,
            mentions_7d=3.0, mentions_28d=5.0, explosion_lag_days=5,
        )
        assert v.action == KEEP

    def test_keep_when_spike_leads_runup(self):
        # explosion_lag <= 0 => spike does NOT lag the run-up start.
        v = self.s.evaluate(
            price=95.0, week_52_high=100.0,
            mentions_7d=30.0, mentions_28d=5.0, explosion_lag_days=0,
        )
        assert v.action == KEEP


class TestNicheSanitizer:
    def setup_method(self):
        self.s = NicheSanitizer()

    def test_keep_low_popularity_healthy_engagement(self):
        v = self.s.evaluate(views=50000, comments=500, followers=10000)
        assert v.action == KEEP

    def test_exclude_viral_views(self):
        v = self.s.evaluate(views=500000, comments=5000, followers=10000)
        assert v.excluded
        assert any("views" in r for r in v.reason_codes)

    def test_exclude_engagement_out_of_band(self):
        # comment-to-view ratio far above the healthy band (viral engagement).
        v = self.s.evaluate(views=1000, comments=900, followers=10000)
        assert v.excluded
        assert any("comment_to_view" in r for r in v.reason_codes)


class TestAdSanitizer:
    def setup_method(self):
        self.s = AdSanitizer()

    def test_keep_clean_caption(self):
        v = self.s.evaluate(caption="How ASML enables chipmakers", views=1000, comments=10, followers=1000)
        assert v.action == KEEP

    def test_exclude_ad_hashtag_case_insensitive(self):
        v = self.s.evaluate(caption="Great stock", hashtags=["#Ad"])
        assert v.excluded
        assert any("hashtag:" in r for r in v.reason_codes)

    def test_exclude_affiliate_link(self):
        v = self.s.evaluate(caption="buy now https://bit.ly/xyz")
        assert v.excluded
        assert any("affiliate" in r for r in v.reason_codes)

    def test_exclude_utm_tracked(self):
        v = self.s.evaluate(caption="deal?utm_source=ig")
        assert v.excluded

    def test_exclude_brand_account_signal(self):
        v = self.s.evaluate(caption="official", brand_account=True)
        assert v.excluded

    def test_exclude_engagement_anomaly(self):
        # view-to-follower ratio far outside the healthy band.
        v = self.s.evaluate(views=1000000, comments=10, followers=100)
        assert v.excluded
        assert any("engagement" in r for r in v.reason_codes)