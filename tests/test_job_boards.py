"""Tests for Greenhouse and Lever job board fetchers."""

import os
import pytest
from unittest.mock import patch, MagicMock
from discovery.job_boards import (
    fetch_greenhouse_jobs,
    fetch_lever_jobs,
    run_pipeline,
)


@pytest.fixture
def mock_greenhouse_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {
        "jobs": [
            {
                "id": 12345,
                "title": "Software Engineer",
                "location": {"name": "San Francisco, CA"},
                "first_published": "2026-08-20T12:00:00Z",
                "updated_at": "2026-08-20T12:00:00Z",
            }
        ]
    }
    return mock


@pytest.fixture
def mock_lever_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = [
        {
            "id": "lever-abc-123",
            "text": "Product Manager",
            "categories": {"location": "New York"},
            "createdAt": 1784569799619,
        }
    ]
    return mock


@patch("discovery.job_boards.requests.get")
def test_fetch_greenhouse_jobs(mock_get, mock_greenhouse_response):
    mock_get.return_value = mock_greenhouse_response
    jobs = fetch_greenhouse_jobs("stripe")
    assert len(jobs) == 1
    assert jobs[0]["source"] == "greenhouse"
    assert jobs[0]["company"] == "stripe"
    assert jobs[0]["job_id"] == "12345"
    assert jobs[0]["title"] == "Software Engineer"
    assert jobs[0]["location"] == "San Francisco, CA"
    assert jobs[0]["first_published"] == "2026-08-20T12:00:00Z"


@patch("discovery.job_boards.requests.get")
def test_fetch_lever_jobs(mock_get, mock_lever_response):
    mock_get.return_value = mock_lever_response
    jobs = fetch_lever_jobs("spotify")
    assert len(jobs) == 1
    assert jobs[0]["source"] == "lever"
    assert jobs[0]["company"] == "spotify"
    assert jobs[0]["job_id"] == "lever-abc-123"
    assert jobs[0]["title"] == "Product Manager"
    assert jobs[0]["location"] == "New York"
    assert jobs[0]["first_published"] is not None


@patch("discovery.job_boards.requests.get")
def test_run_pipeline(mock_get, mock_greenhouse_response, tmp_path):
    mock_get.return_value = mock_greenhouse_response
    test_csv = tmp_path / "test_jobs.csv"
    
    with patch("discovery.job_boards.CSV_FILE", str(test_csv)):
        companies = [{"company": "stripe", "source": "greenhouse", "ticker": "STRP"}]
        new_count = run_pipeline(companies)
        
        assert new_count == 1
        assert os.path.exists(test_csv)
        
        # Verify deduplication / append-safe logic
        # Run again with same data
        new_count_dup = run_pipeline(companies)
        assert new_count_dup == 0
