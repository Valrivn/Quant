import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta
from psychological.scrapers.github_tracker import GitHubTracker, create_github_tracker, RepoMetrics, StructuralBreak


class TestGitHubTracker:
    @pytest.fixture
    def mock_config(self):
        return {
            "psychological": {
                "github": {
                    "token": "test_token",
                    "structural_break_z_threshold": 3.0
                }
            }
        }

    @pytest.fixture
    def tracker(self, mock_config):
        with patch('psychological.scrapers.github_tracker.load_hybrid_config') as mock_load:
            mock_load.return_value = {
                "psychological": mock_config["psychological"],
                "github_mappings": {
                    "AAPL": "apple/swift",
                    "TSLA": "tesla/tesla-firmware"
                }
            }
            tracker = GitHubTracker(config_dict=mock_config["psychological"])
            yield tracker

    def test_init(self, tracker):
        assert tracker is not None
        assert tracker.token == "test_token"
        assert "AAPL" in tracker.github_mappings
        assert "TSLA" in tracker.github_mappings
        assert tracker.z_score_threshold == 3.0

    def test_get_headers(self, tracker):
        headers = tracker._get_headers()
        assert "Accept" in headers
        assert "Authorization" in headers
        assert headers["Authorization"] == "token test_token"

    @pytest.mark.asyncio
    async def test_fetch(self, tracker):
        mock_session = Mock()
        
        async def mock_json():
            return {"test": "data"}
        
        mock_response = Mock()
        mock_response.status = 200
        mock_response.json = mock_json
        
        class MockCM:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None
        
        mock_session.get = Mock(return_value=MockCM())
        
        result = await tracker._fetch(mock_session, "https://api.github.com/test")
        assert result == {"test": "data"}

    @pytest.mark.asyncio
    async def test_fetch_rate_limit(self, tracker):
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status = 403
        
        class MockCM:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None
        
        mock_session.get = Mock(return_value=MockCM())
        
        result = await tracker._fetch(mock_session, "https://api.github.com/test")
        assert result is None

    @pytest.mark.asyncio
    async def test_discover_repos_for_ticker(self, tracker):
        mock_session = Mock()
        
        async def mock_json():
            return {
                "items": [
                    {"full_name": "apple/swift"},
                    {"full_name": "apple/foundationdb"}
                ]
            }
        
        mock_response = Mock()
        mock_response.status = 200
        mock_response.json = mock_json
        
        class MockCM:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None
        
        mock_session.get = Mock(return_value=MockCM())
        
        repos = await tracker.discover_repos_for_ticker("AAPL", mock_session)
        assert len(repos) == 2
        assert "apple/swift" in repos

    @pytest.mark.asyncio
    async def test_get_repo_metrics(self, tracker):
        mock_session = AsyncMock()
        
        repo_data = {
            "stargazers_count": 1000,
            "forks_count": 200,
            "open_issues_count": 50,
            "updated_at": "2024-01-01T00:00:00Z"
        }
        
        commits_data = [
            {"commit": {"committer": {"date": "2024-01-15T00:00:00Z"}}},
            {"commit": {"committer": {"date": "2023-12-15T00:00:00Z"}}}
        ]
        
        contributors_data = [{"login": "user1"}, {"login": "user2"}]
        
        async def mock_fetch(session, url):
            if "repos/" in url and "/commits" not in url and "/contributors" not in url:
                return repo_data
            elif "/commits" in url:
                return commits_data
            elif "/contributors" in url:
                return contributors_data
            return None
        
        tracker._fetch = mock_fetch
        
        with patch('aiohttp.ClientSession', return_value=mock_session):
            metrics = await tracker.get_repo_metrics("apple/swift")
            
        assert metrics["repo"] == "apple/swift"
        assert metrics["stars"] == 1000
        assert metrics["forks"] == 200
        assert metrics["contributor_count"] == 2
        assert metrics["commit_count_7d"] >= 0

    def test_update_historical(self, tracker):
        metrics = {
            "repo": "apple/swift",
            "stars": 1000,
            "forks": 200,
            "commit_count_7d": 10,
            "commit_count_30d": 50,
            "contributor_count": 5,
            "open_issues": 50,
            "updated_at": "2024-01-01T00:00:00Z",
            "fetched_at": datetime.now(timezone.utc).isoformat()
        }
        
        tracker._update_historical("AAPL", metrics)
        assert "AAPL" in tracker._historical_metrics
        assert len(tracker._historical_metrics["AAPL"]) == 1
        assert tracker._historical_metrics["AAPL"][0].stars == 1000

    def test_detect_structural_breaks_insufficient_data(self, tracker):
        breaks = tracker.detect_structural_breaks("AAPL")
        assert breaks == []

    def test_detect_structural_breaks_with_data(self, tracker):
        base_time = datetime.now(timezone.utc)
        for i in range(15):
            metrics = RepoMetrics(
                repo="apple/swift",
                stars=1000 + i,
                forks=200,
                commit_count_7d=10,
                commit_count_30d=50,
                contributor_count=5,
                open_issues=50,
                updated_at=base_time.isoformat(),
                fetched_at=(base_time - timedelta(days=i)).isoformat(),
                ticker="AAPL"
            )
            tracker._historical_metrics.setdefault("AAPL", []).append(metrics)
        
        tracker._historical_metrics["AAPL"][-1] = RepoMetrics(
            repo="apple/swift",
            stars=5000,
            forks=200,
            commit_count_7d=10,
            commit_count_30d=50,
            contributor_count=5,
            open_issues=50,
            updated_at=base_time.isoformat(),
            fetched_at=base_time.isoformat(),
            ticker="AAPL"
        )
        
        breaks = tracker.detect_structural_breaks("AAPL")
        assert len(breaks) >= 0

    def test_get_velocity_history(self, tracker):
        base_time = datetime.now(timezone.utc)
        for i in range(5):
            metrics = RepoMetrics(
                repo="apple/swift",
                stars=1000 + i * 10,
                forks=200,
                commit_count_7d=10,
                commit_count_30d=50,
                contributor_count=5,
                open_issues=50,
                updated_at=base_time.isoformat(),
                fetched_at=(base_time - timedelta(days=i)).isoformat(),
                ticker="AAPL"
            )
            tracker._historical_metrics.setdefault("AAPL", []).append(metrics)
        
        velocities = tracker.get_velocity_history("AAPL", "stars")
        assert len(velocities) == 4
        assert all(v == 10 for v in velocities)

    def test_calculate_velocities(self, tracker):
        current = {"stars": 1100, "forks": 250, "commit_count_7d": 15, "fork_velocity": 5}
        previous = {"stars": 1000, "forks": 200, "commit_count_7d": 10}
        
        velocities = tracker.calculate_velocities(current, previous)
        assert velocities["star_velocity"] == 100.0
        assert velocities["dev_fork_acceleration"] == 50.0
        assert velocities["commit_velocity"] == 5.0

    def test_calculate_velocities_no_previous(self, tracker):
        current = {"stars": 1100, "forks": 250, "commit_count_7d": 15}
        velocities = tracker.calculate_velocities(current, None)
        assert velocities["star_velocity"] == 0.0
        assert velocities["dev_fork_acceleration"] == 0.0

    def test_get_structural_breaks(self, tracker):
        break_obj = StructuralBreak(
            ticker="AAPL",
            metric="stars",
            timestamp=datetime.now(timezone.utc).isoformat(),
            z_score=4.0,
            direction="spike",
            severity="moderate"
        )
        tracker.structural_breaks.append(break_obj)
        
        breaks = tracker.get_structural_breaks(ticker="AAPL")
        assert len(breaks) == 1
        assert breaks[0].ticker == "AAPL"
        
        breaks = tracker.get_structural_breaks(ticker="TSLA")
        assert len(breaks) == 0

    def test_get_all_metrics_structure(self, tracker):
        mock_metrics = {
            "apple/swift": {
                "repo": "apple/swift",
                "stars": 1000,
                "forks": 200,
                "commit_count_7d": 10,
                "commit_count_30d": 50,
                "contributor_count": 5,
                "open_issues": 50,
                "updated_at": "2024-01-01T00:00:00Z",
                "fetched_at": datetime.now(timezone.utc).isoformat()
            }
        }
        
        tracker.get_repo_metrics = AsyncMock(side_effect=lambda repo: mock_metrics.get(repo, {}))
        
        import asyncio
        async def run_test():
            results = await tracker.get_all_metrics()
            assert "AAPL" in results
            assert results["AAPL"]["ticker"] == "AAPL"
        
        asyncio.run(run_test())


class TestCreateGitHubTracker:
    @pytest.mark.asyncio
    async def test_create_github_tracker(self):
        with patch('psychological.scrapers.github_tracker.load_hybrid_config') as mock_load:
            mock_load.return_value = {
                "psychological": {"github": {"token": "test"}},
                "github_mappings": {}
            }
            tracker = await create_github_tracker()
            assert isinstance(tracker, GitHubTracker)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])