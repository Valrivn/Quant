"""Tests for discovery.wayback_pit (offline; network calls mocked)."""

import json
import sqlite3

import pytest

from discovery import wayback_pit as wp


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "pit_test.db")
    conn = sqlite3.connect(p)
    wp.WaybackPitHarvester._create_tables(conn)
    conn.close()
    return p


@pytest.fixture
def _rows_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _seed(conn, rows):
    conn.executemany(
        "INSERT OR IGNORE INTO pit_rating_snapshots "
        "(ticker, source, valid_date, rating, review_count, detail_json, original_url, snapshot_ts, parse_pattern, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def test_month_snapshots_filters_and_first_wins(monkeypatch):
    calls = []

    def fake_cdx(params):
        calls.append(params)
        return [
            ["20080601000000", "http://www.glassdoor.com/Reviews/NVIDIA-Reviews-EI_IE7633.0,6.htm"],
            ["20080705000000", "http://www.glassdoor.com/Reviews/NVIDIA-Reviews-EI_IE7633.0,6.htm"],
            ["20080620000000", "http://www.glassdoor.com/Reviews/NVIDIA-Jobs-EI_IE7633.0,6.htm"],
        ]

    monkeypatch.setattr(wp, "cdx_rows", fake_cdx)
    months = wp.month_snapshots("NVDA", since="200806", until="200807")
    assert calls[0]["matchType"] == "prefix"
    assert calls[0]["url"].endswith("-Reviews-E7633")
    assert list(months.keys()) == ["200806", "200807"]
    assert months["200806"][0] == "20080601000000"


def test_month_snapshots_canonical_avoids_family_scan(monkeypatch):
    calls = []

    def fake_cdx(params):
        calls.append(params["url"])
        rows = [
            [f"2008{m:02d}05000000", "http://www.glassdoor.com/Reviews/NVIDIA-Reviews-EI_IE7633.0,6.htm"]
            for m in range(1, 13)
        ]
        return rows

    monkeypatch.setattr(wp, "cdx_rows", fake_cdx)
    months = wp.month_snapshots("NVDA")
    assert len(months) == 12
    assert calls[0].endswith("-Reviews-E7633")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "html,expected",
    [
        ('<div class="ratingNum">4.5</div>', 4.5),
        ('{"aggregateRating": {"ratingValue": "4.2", "reviewCount": 310}}', 4.2),
        ('<meta itemprop="ratingValue" content="3.9" />', 3.9),
        ('<div data-test="ratingNumber">2.8</div>', 2.8),
        ('<div class="bigRating">4.0</div>', 4.0),
        ("random page without a rating", None),
        ('<div class="ratingNum">7.2</div>', None),
    ],
)
def test_parse_glassdoor_rating(html, expected):
    parsed = wp.parse_glassdoor_rating(html)
    if expected is None:
        assert parsed is None
    else:
        assert parsed["rating"] == expected
        assert parsed["pattern"]


def test_parse_review_count():
    parsed = wp.parse_glassdoor_rating(
        '<div class="ratingNum">4.5</div><span class="reviewsCount">1,234</span>'
    )
    assert parsed["review_count"] == 1234


def test_store_rating_idempotent_and_provenance(db_path, _rows_conn):
    h = wp.WaybackPitHarvester(db_path=db_path, fetch_sleep=0)
    parsed = {"rating": 4.1, "pattern": "class_ratingNum", "snippet": "<div>4.1</div>", "review_count": 50}

    assert h.store_rating("NVDA", "glassdoor", "20110312000000", "http://u", parsed) == "ok"
    assert h.store_rating("NVDA", "glassdoor", "20110312000000", "http://u", parsed) == "skipped"

    row = _rows_conn.execute("SELECT * FROM pit_rating_snapshots").fetchone()
    assert row["valid_date"] == "2011-03-12"
    assert row["rating"] == 4.1
    assert row["original_url"] == "http://u"
    assert row["parse_pattern"] == "class_ratingNum"
    assert json.loads(row["detail_json"])["review_count"] == 50


def test_store_unparseable_marks_failed(db_path, _rows_conn):
    h = wp.WaybackPitHarvester(db_path=db_path, fetch_sleep=0)
    assert h.store_rating("NVDA", "glassdoor", "20090601000000", "http://u", None) == "failed"
    row = _rows_conn.execute(
        "SELECT rating, parse_pattern FROM pit_rating_snapshots WHERE valid_date = '2009-06-01'"
    ).fetchone()
    assert row["rating"] is None


def test_harvest_skips_existing_months(monkeypatch, db_path):
    conn = sqlite3.connect(db_path)
    _seed(
        conn,
        [
            ("NVDA", "glassdoor", "2008-06-03", 3.9, 100, "{}", "http://u", "20080603000000", "a", 1),
            ("NVDA", "glassdoor", "2008-07-05", 4.0, 120, "{}", "http://u", "20080705000000", "a", 1),
        ],
    )
    conn.close()

    fetched = {}

    def fake_fetch(ts, original):
        fetched[ts] = original
        return '<div class="ratingNum">4.4</div>'

    monkeypatch.setattr(
        wp, "month_snapshots",
        lambda *a, **k: {
            "200806": ("20080601000000", "http://nvidia"),
            "200807": ("20080701000000", "http://nvidia"),
            "200808": ("20080801000000", "http://nvidia"),
        },
    )
    monkeypatch.setattr(wp, "fetch_snapshot", fake_fetch)

    h = wp.WaybackPitHarvester(db_path=db_path, fetch_sleep=0)
    counts = h.harvest_ticker("NVDA")
    assert counts["skipped"] == 2
    assert counts["ok"] == 1
    assert "20080801000000" in fetched
    assert "20080601000000" not in fetched


def test_load_pit_panel(db_path):
    conn = sqlite3.connect(db_path)
    _seed(
        conn,
        [
            ("NVDA", "glassdoor", "2010-01-10", 3.7, 100, "{}", "http://u", "20100110000000", "a", 1),
            ("INTC", "glassdoor", "2010-01-11", 3.1, 200, "{}", "http://u", "20100111000000", "a", 1),
            ("NVDA", "glassdoor", "2010-02-09", 3.8, 105, "{}", "http://u", "20100209000000", "a", 1),
            ("NVDA", "capterra", "2010-01-10", 4.9, 5, "{}", "http://u", "20100110000000", "a", 1),
        ],
    )
    conn.close()

    panel = wp.load_pit_panel(db_path=db_path)
    assert list(panel.columns) == ["INTC", "NVDA"]
    assert panel.loc["2010-01-10", "NVDA"] == 3.7
    assert panel.loc["2010-02-09", "NVDA"] == 3.8

    gd_only = wp.load_pit_panel(source="glassdoor", db_path=db_path)
    assert "NVDA" in gd_only.columns


def test_dry_run_does_not_write(db_path):
    parsed = {"rating": 4.2, "pattern": "class_ratingNum", "snippet": "<div>4.2</div>", "review_count": 3}
    h = wp.WaybackPitHarvester(db_path=db_path, dry_run=True, fetch_sleep=0)
    assert h.store_rating("MSFT", "glassdoor", "20150501000000", "http://u", parsed) == "ok"
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM pit_rating_snapshots").fetchone()[0]
    conn.close()
    assert n == 0


def test_list_tickers_filter():
    tickers = wp.list_tickers(include=["NVDA", "ZZZ", "AMD"])
    assert tickers == ["NVDA", "AMD"]


def test_list_tickers_glassdoor_count():
    tickers = wp.list_tickers()
    assert len(tickers) == 28
    assert "NVDA" in tickers
    assert "ORCL" in tickers
    assert "ABNB" in tickers


def test_list_tickers_capterra_empty():
    assert wp.list_tickers(source=wp.SOURCE_CAPTERRA) == []


def test_list_tickers_capterra_with_include():
    assert wp.list_tickers(include=["NVDA"], source=wp.SOURCE_CAPTERRA) == []


def test_eid_for_known_entry():
    assert wp.eid_for("NVDA") == 7633
    assert wp.eid_for("AMD") == 15
    assert wp.eid_for("IBM") == 354


def test_eid_for_missing_returns_none():
    assert wp.eid_for("ZZZZZ") is None


def test_eid_for_sentinel_zero_returns_none():
    assert wp.eid_for("ORCL") is None
    assert wp.eid_for("ABNB") is None


def test_eid_for_custom_registry():
    reg = {"FOO": 42, "BAR": 0}
    assert wp.eid_for("FOO", registry=reg) == 42
    assert wp.eid_for("BAR", registry=reg) is None
    assert wp.eid_for("BAZ", registry=reg) is None


@pytest.mark.parametrize(
    "html,expected_rating,expected_pattern",
    [
        # JSON-LD variant
        (
            '<script type="application/ld+json">'
            '{"@type":"Product","aggregateRating":{"@type":"AggregateRating",'
            '"ratingValue":"4.2","reviewCount":150}}</script>',
            4.2,
            "jsonld_ratingValue",
        ),
        # Legacy div average-rating variant
        (
            '<div class="average-rating">3.7</div><span>1,234 reviews</span>',
            3.7,
            "average_rating",
        ),
        # Embedded JSON variant
        (
            '<script>var data = {"rating": 4.5, "name": "Acme"}</script>',
            4.5,
            "embedded_rating_json",
        ),
        # Junk returns None
        ("totally random html without any rating", None, None),
        ("", None, None),
    ],
)
def test_parse_capterra_rating(html, expected_rating, expected_pattern):
    parsed = wp.parse_capterra_rating(html)
    if expected_rating is None:
        assert parsed is None
    else:
        assert parsed["rating"] == expected_rating
        assert parsed["pattern"] == expected_pattern


def test_parse_capterra_review_count():
    html = (
        '<script type="application/ld+json">'
        '{"aggregateRating":{"ratingValue":"4.0","reviewCount":87}}'
        '</script>'
    )
    parsed = wp.parse_capterra_rating(html)
    assert parsed["rating"] == 4.0
    assert parsed["review_count"] == 87


def test_parse_capterra_review_count_text():
    html = '<div class="average-rating">3.5</div><p>2,345 ratings</p>'
    parsed = wp.parse_capterra_rating(html)
    assert parsed["rating"] == 3.5
    assert parsed["review_count"] == 2345


def test_parse_capterra_out_of_range_rejected():
    html = '{"ratingValue": "7.0"}'
    assert wp.parse_capterra_rating(html) is None


def test_month_snapshots_sentinel_eid_skips_canonical(monkeypatch):
    """Ticker with eid=0 sentinel goes straight to family scan."""
    calls = []

    def fake_cdx(params):
        calls.append(params["url"])
        return [
            [f"2008{m:02d}05000000", "http://www.glassdoor.com/Reviews/Oracle-Reviews.htm"]
            for m in range(1, 7)
        ]

    monkeypatch.setattr(wp, "cdx_rows", fake_cdx)
    months = wp.month_snapshots("ORCL", since="200801", until="200806")
    # eid_for("ORCL") -> None, so canonical page was never queried.
    assert not any("Reviews-E" in u for u in calls)
    assert len(months) == 6