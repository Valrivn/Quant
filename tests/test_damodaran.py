"""Tests for Damodaran discovery module and refresh script.

Fully offline unit tests using canned HTML and synthetic BIFF .xls data created with dummy BIFF bytes or mock xlrd workbook.
"""

import io
import unittest.mock as mock
import pandas as pd
import pytest

from discovery.damodaran import (
    parse_betas_html,
    parse_members_xls,
    industry_beta_map,
    sorted_companies,
    sanity_trial,
    _pin_ipv4,
)


def _make_canned_betas_html() -> bytes:
    html = """
    <html>
    <body>
    <table>
        <tr>
            <td>Industry Name</td>
            <td>Number of firms</td>
            <td>Beta</td>
            <td>D/E Ratio</td>
            <td>Effective Tax rate</td>
            <td>Unlevered beta</td>
            <td>Cash/Firm value</td>
            <td>Unlevered beta corrected for cash</td>
            <td>HiLo Risk</td>
            <td>Standard deviation of equity</td>
            <td>Standard deviation in operating income (last 10 years)</td>
        </tr>
        <tr>
            <td>Advertising</td>
            <td>60</td>
            <td>1.20</td>
            <td>0.25</td>
            <td>0.18</td>
            <td>1.05</td>
            <td>0.05</td>
            <td>1.10</td>
            <td>0.12</td>
            <td>0.25</td>
            <td>0.15</td>
        </tr>
        <tr>
            <td>Semiconductor</td>
            <td>85</td>
            <td>1.50</td>
            <td>0.10</td>
            <td>0.15</td>
            <td>1.40</td>
            <td>0.10</td>
            <td>1.55</td>
            <td>0.20</td>
            <td>0.35</td>
            <td>0.25</td>
        </tr>
        <tr>
            <td>Utility (Water)</td>
            <td>20</td>
            <td>0.60</td>
            <td>0.50</td>
            <td>0.20</td>
            <td>0.45</td>
            <td>0.02</td>
            <td>0.46</td>
            <td>0.05</td>
            <td>0.15</td>
            <td>0.08</td>
        </tr>
        <tr>
            <td>Bank (Money Center)</td>
            <td>15</td>
            <td>1.10</td>
            <td>0.80</td>
            <td>0.21</td>
            <td>-0.10</td>
            <td>0.05</td>
            <td>0.00</td>
            <td>0.10</td>
            <td>0.20</td>
            <td>0.12</td>
        </tr>
        <tr>
            <td>Distressed Sector</td>
            <td>5</td>
            <td>N/A</td>
            <td>N/A</td>
            <td>N/A</td>
            <td>N/A</td>
            <td>N/A</td>
            <td>N/A</td>
            <td>N/A</td>
            <td>N/A</td>
            <td>N/A</td>
        </tr>
        <tr>
            <td>Total Market</td>
            <td>9000</td>
            <td>1.00</td>
            <td>0.30</td>
            <td>0.20</td>
            <td>0.85</td>
            <td>0.08</td>
            <td>0.90</td>
            <td>0.10</td>
            <td>0.20</td>
            <td>0.10</td>
        </tr>
    </table>
    </body>
    </html>
    """
    return html.encode("utf-8")


def _make_mock_xlrd_workbook():
    rows = [
        ["Company Name", "Exchange:Ticker", "Industry Group", "Primary Sector", "SIC Code", "Country"],
        ["Ad Corp", "NYSE:ADC", "Advertising", "Communication Services", 7311.0, "United States"],
        ["SemiTech", "Nasdaq:SMTC", "Semiconductor", "Information Technology", 3674.0, "United States"],
        ["AquaPure", "AQUP", "Utility (Water)", "Utilities", 4941.0, "Canada"],
        ["Global Ad", "LSE:GAD", "Advertising", "Communication Services", 7311.0, "United Kingdom"],
        ["US Water", "USW", "Utility (Water)", "Utilities", 4941.0, "United States"],
    ]

    mock_sheet = mock.MagicMock()
    mock_sheet.nrows = len(rows)
    def row_values(r, end_colx=6):
        return rows[r][:end_colx]
    mock_sheet.row_values = row_values

    mock_wb = mock.MagicMock()
    mock_wb.sheet_by_name.return_value = mock_sheet
    return mock_wb


class TestDamodaranParsers:
    def test_parse_betas_html(self):
        html_bytes = _make_canned_betas_html()
        df = parse_betas_html(html_bytes)

        # Total Market, negative unlevered_beta (-0.10), and N/A unlevered_beta rows should be dropped.
        # Valid remaining industries: Advertising, Semiconductor, Utility (Water).
        assert len(df) == 3
        assert list(df["industry"]) == ["Advertising", "Semiconductor", "Utility (Water)"]
        assert list(df.columns) == [
            "industry",
            "number_of_firms",
            "beta",
            "debt_to_equity",
            "effective_tax_rate",
            "unlevered_beta",
            "cash_firm_value",
            "unlevered_beta_cash",
            "hilo_risk",
            "sd_equity",
            "sd_operating_income",
        ]
        assert df["unlevered_beta"].iloc[0] == 1.05
        assert df["unlevered_beta"].iloc[1] == 1.40
        assert df["unlevered_beta"].iloc[2] == 0.45

    def test_parse_members_xls(self):
        mock_wb = _make_mock_xlrd_workbook()
        with mock.patch("xlrd.open_workbook", return_value=mock_wb):
            df = parse_members_xls(b"fake_content")

        assert len(df) == 5
        assert list(df.columns) == [
            "company",
            "ticker",
            "industry_group",
            "primary_sector",
            "sic_code",
            "country",
        ]
        # Coercion of float SIC 7311.0 to "7311"
        assert df["sic_code"].iloc[0] == "7311"
        assert df["company"].iloc[0] == "Ad Corp"

    def test_industry_beta_map(self):
        html_bytes = _make_canned_betas_html()
        betas_df = parse_betas_html(html_bytes)
        b_map = industry_beta_map(betas_df)

        assert b_map == {
            "Advertising": 1.05,
            "Semiconductor": 1.40,
            "Utility (Water)": 0.45,
        }
        # Verify order
        assert list(b_map.keys()) == ["Advertising", "Semiconductor", "Utility (Water)"]

    def test_sorted_companies_and_country_filter(self):
        html_bytes = _make_canned_betas_html()
        mock_wb = _make_mock_xlrd_workbook()
        with mock.patch("xlrd.open_workbook", return_value=mock_wb):
            members_df = parse_members_xls(b"fake_content")
        betas_df = parse_betas_html(html_bytes)

        # Filter US
        sc_us = sorted_companies(betas_df, members_df, country_filter="US")
        # AquaPure is Canada & AQUP (no Nasdaq/NYSE/etc) -> excluded
        # Global Ad is United Kingdom & LSE:GAD -> excluded
        # Included: Ad Corp (US), SemiTech (US), US Water (US)
        assert len(sc_us) == 3
        # Sorted ascending by unlevered_beta: Utility (Water)=0.45, Advertising=1.05, Semiconductor=1.40
        assert list(sc_us["company"]) == ["US Water", "Ad Corp", "SemiTech"]

        # Sort descending
        sc_desc = sorted_companies(betas_df, members_df, ascending=False, country_filter="US")
        assert list(sc_desc["company"]) == ["SemiTech", "Ad Corp", "US Water"]

    def test_sanity_trial(self):
        html_bytes = _make_canned_betas_html()
        mock_wb = _make_mock_xlrd_workbook()
        with mock.patch("xlrd.open_workbook", return_value=mock_wb):
            members_df = parse_members_xls(b"fake_content")
        betas_df = parse_betas_html(html_bytes)

        st = sanity_trial(betas_df, members_df)
        assert st == {
            "industries": 3,
            "companies": 5,
            "us_companies": 3,
            "beta_range": (0.45, 1.4),
        }

    def test_pin_ipv4_helper(self):
        # Ensure offline helper doesn't throw
        _pin_ipv4()
