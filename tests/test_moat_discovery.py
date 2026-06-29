import pytest
import json
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone
from psychological.scrapers.moat_discovery import (
    MoatNode, MoatTree, MoatDiscoveryEngine, create_moat_discovery_engine,
)


class MockCurlResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class MockCurlSession:
    def __init__(self):
        self._responses = {}

    async def get(self, url, headers=None, timeout=30):
        response = self._responses.get(url)
        if response:
            return response
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = "<html></html>"
        return mock_resp

    async def close(self):
        pass

    def set_response(self, url, status_code=200, text=""):
        mock_resp = Mock()
        mock_resp.status_code = status_code
        mock_resp.text = text
        self._responses[url] = mock_resp


WIKIPEDIA_INFOBOX_HTML_NVDA = """<html><body>
<table class="infobox">
<tr><th>Products</th><td>
<a href="/wiki/NVIDIA_Apex">Apex</a>,
<a href="/wiki/CUDA">CUDA</a>,
<a href="/wiki/NVIDIA_GeForce">GeForce</a>,
<a href="/wiki/NVIDIA_Tegra">Tegra</a>,
<a href="/wiki/NVIDIA_Drive">Drive</a>,
<a href="/wiki/NVIDIA_Omniverse">Omniverse</a>
</td></tr>
</table></body></html>"""

WIKIPEDIA_INFOBOX_HTML_NO_PRODUCTS = """<html><body>
<table class="infobox">
<tr><th>Founded</th><td>1993</td></tr>
<tr><th>Headquarters</th><td>Santa Clara</td></tr>
</table></body></html>"""

WIKIPEDIA_NO_INFOBOX = """<html><body><div>No infobox here</div></body></html>"""

GITHUB_REPOS_JSON_NVDA = json.dumps([
    {"name": "cuda-samples", "full_name": "NVIDIA/cuda-samples", "stargazers_count": 1500, "forks_count": 500, "description": "CUDA samples", "html_url": "https://github.com/NVIDIA/cuda-samples"},
    {"name": "TensorRT", "full_name": "NVIDIA/TensorRT", "stargazers_count": 8000, "forks_count": 2000, "description": "TensorRT", "html_url": "https://github.com/NVIDIA/TensorRT"},
    {"name": "cutlass", "full_name": "NVIDIA/cutlass", "stargazers_count": 3500, "forks_count": 700, "description": "CUTLASS", "html_url": "https://github.com/NVIDIA/cutlass"},
    {"name": "tiny-samples", "full_name": "NVIDIA/tiny-samples", "stargazers_count": 50, "forks_count": 10, "description": "small repo", "html_url": "https://github.com/NVIDIA/tiny-samples"},
    {"name": "NeMo", "full_name": "NVIDIA/NeMo", "stargazers_count": 9000, "forks_count": 2000, "description": "NeMo framework", "html_url": "https://github.com/NVIDIA/NeMo"},
])

GITHUB_REPOS_JSON_LOW_STARS = json.dumps([
    {"name": "low-star-repo", "full_name": "NVIDIA/low-star-repo", "stargazers_count": 10, "forks_count": 1, "description": "small", "html_url": "https://github.com/NVIDIA/low-star-repo"},
    {"name": "another-low", "full_name": "NVIDIA/another-low", "stargazers_count": 99, "forks_count": 2, "description": "tiny", "html_url": "https://github.com/NVIDIA/another-low"},
])

GITHUB_REPOS_EMPTY = json.dumps([])


class TestMoatNode:
    def test_moat_node_equality(self):
        n1 = MoatNode(name="CUDA", source="wikipedia", ticker="NVDA", node_type="platform")
        n2 = MoatNode(name="cuda", source="wikipedia", ticker="NVDA", node_type="platform")
        assert n1 == n2

    def test_moat_node_inequality(self):
        n1 = MoatNode(name="CUDA", source="wikipedia", ticker="NVDA", node_type="platform")
        n2 = MoatNode(name="GeForce", source="wikipedia", ticker="NVDA", node_type="platform")
        assert n1 != n2

    def test_moat_node_hash(self):
        n1 = MoatNode(name="CUDA", source="wikipedia", ticker="NVDA", node_type="platform")
        n2 = MoatNode(name="cuda", source="wikipedia", ticker="NVDA", node_type="platform")
        assert hash(n1) == hash(n2)

    def test_moat_node_creation(self):
        node = MoatNode(name="Test", source="github", ticker="NVDA", node_type="repository", stars=500)
        assert node.name == "Test"
        assert node.source == "github"
        assert node.stars == 500
        assert node.ticker == "NVDA"


class TestMoatTree:
    def test_empty_tree(self):
        tree = MoatTree(ticker="NVDA", company_name="NVIDIA")
        assert tree.count == 0
        assert tree.wikipedia_nodes == []
        assert tree.github_nodes == []

    def test_add_node(self):
        tree = MoatTree(ticker="NVDA", company_name="NVIDIA")
        node = MoatNode(name="CUDA", source="wikipedia", ticker="NVDA", node_type="platform")
        tree.add_node(node)
        assert tree.count == 1
        assert node in tree.nodes

    def test_add_duplicate_node(self):
        tree = MoatTree(ticker="NVDA", company_name="NVIDIA")
        n1 = MoatNode(name="CUDA", source="wikipedia", ticker="NVDA", node_type="platform")
        n2 = MoatNode(name="cuda", source="github", ticker="NVDA", node_type="repository")
        tree.add_node(n1)
        tree.add_node(n2)
        assert tree.count == 1

    def test_source_filters(self):
        tree = MoatTree(ticker="NVDA", company_name="NVIDIA")
        wn = MoatNode(name="CUDA", source="wikipedia", ticker="NVDA", node_type="platform")
        gn = MoatNode(name="cutlass", source="github", ticker="NVDA", node_type="repository")
        tree.add_node(wn)
        tree.add_node(gn)
        assert len(tree.wikipedia_nodes) == 1
        assert len(tree.github_nodes) == 1


class TestMoatDiscoveryEngine:
    @pytest.fixture
    def engine(self):
        with patch("psychological.scrapers.moat_discovery.load_hybrid_config") as mock_load:
            mock_load.return_value = {}
            eng = MoatDiscoveryEngine(config_dict={})
            eng._curl_session = MockCurlSession()
            yield eng

    def test_guard_namespace_single_word(self):
        assert MoatDiscoveryEngine._guard_namespace("Apex", "NVIDIA") == "NVIDIA Apex"

    def test_guard_namespace_multi_word(self):
        assert MoatDiscoveryEngine._guard_namespace("NVIDIA CUDA", "NVIDIA") == "NVIDIA CUDA"

    def test_guard_namespace_trimmed(self):
        assert MoatDiscoveryEngine._guard_namespace("  Apex  ", "NVIDIA") == "NVIDIA Apex"

    def test_deduplicate(self, engine):
        nodes = [
            MoatNode(name="CUDA", source="wikipedia", ticker="NVDA", node_type="platform"),
            MoatNode(name="cuda", source="github", ticker="NVDA", node_type="repository"),
            MoatNode(name="GeForce", source="wikipedia", ticker="NVDA", node_type="platform"),
        ]
        deduped = MoatDiscoveryEngine._deduplicate(nodes)
        assert len(deduped) == 2

    def test_deduplicate_no_dupes(self, engine):
        nodes = [
            MoatNode(name="CUDA", source="wikipedia", ticker="NVDA", node_type="platform"),
            MoatNode(name="GeForce", source="wikipedia", ticker="NVDA", node_type="platform"),
        ]
        deduped = MoatDiscoveryEngine._deduplicate(nodes)
        assert len(deduped) == 2

    @pytest.mark.asyncio
    async def test_discover_wikipedia_success(self, engine):
        sessions = engine._curl_session
        sessions.set_response(
            "https://en.wikipedia.org/wiki/Nvidia",
            status_code=200,
            text=WIKIPEDIA_INFOBOX_HTML_NVDA,
        )

        nodes = await engine.discover_wikipedia_nodes("NVDA")
        assert len(nodes) == 6
        names = [n.name for n in nodes]
        assert "NVIDIA Apex" in names
        assert "NVIDIA CUDA" in names
        assert "NVIDIA GeForce" in names

    @pytest.mark.asyncio
    async def test_discover_wikipedia_no_products_row(self, engine):
        sessions = engine._curl_session
        sessions.set_response(
            "https://en.wikipedia.org/wiki/Nvidia",
            status_code=200,
            text=WIKIPEDIA_INFOBOX_HTML_NO_PRODUCTS,
        )
        nodes = await engine.discover_wikipedia_nodes("NVDA")
        assert nodes == []

    @pytest.mark.asyncio
    async def test_discover_wikipedia_no_infobox(self, engine):
        sessions = engine._curl_session
        sessions.set_response(
            "https://en.wikipedia.org/wiki/Nvidia",
            status_code=200,
            text=WIKIPEDIA_NO_INFOBOX,
        )
        nodes = await engine.discover_wikipedia_nodes("NVDA")
        assert nodes == []

    @pytest.mark.asyncio
    async def test_discover_wikipedia_no_slug(self, engine):
        nodes = await engine.discover_wikipedia_nodes("UNKNOWN")
        assert nodes == []

    @pytest.mark.asyncio
    async def test_discover_wikipedia_http_error(self, engine):
        sessions = engine._curl_session
        sessions.set_response(
            "https://en.wikipedia.org/wiki/Nvidia",
            status_code=404,
            text="Not Found",
        )
        nodes = await engine.discover_wikipedia_nodes("NVDA")
        assert nodes == []

    @pytest.mark.asyncio
    async def test_discover_github_success(self, engine):
        sessions = engine._curl_session
        sessions.set_response(
            "https://api.github.com/orgs/NVIDIA/repos?per_page=100&sort=stars&direction=desc",
            status_code=200,
            text=GITHUB_REPOS_JSON_NVDA,
        )

        nodes = await engine.discover_github_nodes("NVDA")
        assert len(nodes) == 4
        names = [n.name for n in nodes]
        assert "NVIDIA cuda-samples" in names
        assert "NVIDIA TensorRT" in names
        assert "NVIDIA cutlass" in names
        assert "NVIDIA NeMo" in names
        assert "NVIDIA tiny-samples" not in names

        for node in nodes:
            assert node.stars > 100

    @pytest.mark.asyncio
    async def test_discover_github_low_stars_filtered(self, engine):
        sessions = engine._curl_session
        sessions.set_response(
            "https://api.github.com/orgs/NVIDIA/repos?per_page=100&sort=stars&direction=desc",
            status_code=200,
            text=GITHUB_REPOS_JSON_LOW_STARS,
        )

        nodes = await engine.discover_github_nodes("NVDA")
        assert nodes == []

    @pytest.mark.asyncio
    async def test_discover_github_empty(self, engine):
        sessions = engine._curl_session
        sessions.set_response(
            "https://api.github.com/orgs/NVIDIA/repos?per_page=100&sort=stars&direction=desc",
            status_code=200,
            text=GITHUB_REPOS_EMPTY,
        )
        nodes = await engine.discover_github_nodes("NVDA")
        assert nodes == []

    @pytest.mark.asyncio
    async def test_discover_github_no_org(self, engine):
        nodes = await engine.discover_github_nodes("UNKNOWN")
        assert nodes == []

    @pytest.mark.asyncio
    async def test_discover_github_rate_limited(self, engine):
        sessions = engine._curl_session
        sessions.set_response(
            "https://api.github.com/orgs/NVIDIA/repos?per_page=100&sort=stars&direction=desc",
            status_code=403,
            text='{"message": "rate limit exceeded"}',
        )
        nodes = await engine.discover_github_nodes("NVDA")
        assert nodes == []

    @pytest.mark.asyncio
    async def test_discover_integration(self, engine):
        sessions = engine._curl_session
        sessions.set_response(
            "https://en.wikipedia.org/wiki/Nvidia",
            status_code=200,
            text=WIKIPEDIA_INFOBOX_HTML_NVDA,
        )
        sessions.set_response(
            "https://api.github.com/orgs/NVIDIA/repos?per_page=100&sort=stars&direction=desc",
            status_code=200,
            text=GITHUB_REPOS_JSON_NVDA,
        )

        tree = await engine.discover("NVDA")
        assert tree.ticker == "NVDA"
        assert tree.company_name == "NVIDIA"
        assert len(tree.nodes) > 0
        assert tree.count <= 8

        wiki_node_names = {n.name for n in tree.wikipedia_nodes}
        gh_node_names = {n.name for n in tree.github_nodes}
        assert "NVIDIA Apex" in wiki_node_names
        assert "NVIDIA TensorRT" in gh_node_names

    @pytest.mark.asyncio
    async def test_discover_deduplicates(self, engine):
        sessions = engine._curl_session
        gh_json = json.dumps([
            {"name": "CUDA", "full_name": "NVIDIA/CUDA", "stargazers_count": 5000, "forks_count": 1000, "description": "CUDA", "html_url": "https://github.com/NVIDIA/CUDA"},
        ])
        sessions.set_response(
            "https://en.wikipedia.org/wiki/Nvidia",
            status_code=200,
            text=WIKIPEDIA_INFOBOX_HTML_NVDA,
        )
        sessions.set_response(
            "https://api.github.com/orgs/NVIDIA/repos?per_page=100&sort=stars&direction=desc",
            status_code=200,
            text=gh_json,
        )

        tree = await engine.discover("NVDA")
        cuda_nodes = [n for n in tree.nodes if "CUDA" in n.name]
        assert len(cuda_nodes) == 1

    @pytest.mark.asyncio
    async def test_discover_capped_at_eight(self, engine):
        sessions = engine._curl_session
        many_names = [f"repo-{i}" for i in range(20)]
        many_repos = [
            {"name": name, "full_name": f"NVIDIA/{name}", "stargazers_count": 500,
             "forks_count": 100, "description": "test", "html_url": f"https://github.com/NVIDIA/{name}"}
            for name in many_names
        ]
        sessions.set_response(
            "https://en.wikipedia.org/wiki/Nvidia",
            status_code=200,
            text=WIKIPEDIA_INFOBOX_HTML_NVDA,
        )
        sessions.set_response(
            "https://api.github.com/orgs/NVIDIA/repos?per_page=100&sort=stars&direction=desc",
            status_code=200,
            text=json.dumps(many_repos),
        )

        tree = await engine.discover("NVDA")
        assert tree.count <= 8

    @pytest.mark.asyncio
    async def test_discover_unknown_ticker(self, engine):
        tree = await engine.discover("UNKNOWN")
        assert tree.ticker == "UNKNOWN"
        assert tree.count == 0

    @pytest.mark.asyncio
    async def test_create_moat_discovery_engine(self):
        with patch("psychological.scrapers.moat_discovery.load_hybrid_config") as mock_load:
            mock_load.return_value = {}
            eng = await create_moat_discovery_engine({})
            assert isinstance(eng, MoatDiscoveryEngine)


class TestNamespaceGuarding:
    def test_guard_single_word_variants(self):
        company = "NVIDIA"
        cases = [
            ("Apex", "NVIDIA Apex"),
            ("CUDA", "NVIDIA CUDA"),
            ("GeForce", "NVIDIA GeForce"),
            ("Tegra", "NVIDIA Tegra"),
        ]
        for raw, expected in cases:
            assert MoatDiscoveryEngine._guard_namespace(raw, company) == expected

    def test_guard_multi_word(self):
        assert MoatDiscoveryEngine._guard_namespace("NVIDIA CUDA", "NVIDIA") == "NVIDIA CUDA"
        assert MoatDiscoveryEngine._guard_namespace("Google Cloud Platform", "Google") == "Google Cloud Platform"

    def test_guard_edge_case_empty(self):
        assert MoatDiscoveryEngine._guard_namespace("", "NVIDIA") == "NVIDIA "

    def test_guard_edge_case_single_letter(self):
        assert MoatDiscoveryEngine._guard_namespace("A", "NVIDIA") == "NVIDIA A"
