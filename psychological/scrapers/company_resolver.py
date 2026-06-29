import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from config import load_hybrid_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompanyEntity:
    ticker: str
    legal_name: str
    common_name: str
    glassdoor_slug: str
    comparably_slug: str
    indeed_slug: str
    g2_slug: str
    capterra_slug: str
    github_org: str
    sec_cik: str
    app_store_name: str
    metadata: Dict[str, str] = field(default_factory=dict)


class CompanyResolver:
    def __init__(self, config_dict: Optional[dict] = None):
        self._config = config_dict or load_hybrid_config()
        raw = self._config.get("companies", {})
        self._entities: Dict[str, CompanyEntity] = {}
        self._by_slug: Dict[str, str] = {}
        self._load(raw)

    def _load(self, raw: dict) -> None:
        for ticker, data in raw.items():
            entity = CompanyEntity(
                ticker=ticker,
                legal_name=data.get("legal_name", ""),
                common_name=data.get("common_name", ""),
                glassdoor_slug=data.get("glassdoor_slug", ""),
                comparably_slug=data.get("comparably_slug", ""),
                indeed_slug=data.get("indeed_slug", ""),
                g2_slug=data.get("g2_slug", ""),
                capterra_slug=data.get("capterra_slug", ""),
                github_org=data.get("github_org", ""),
                sec_cik=data.get("sec_cik", ""),
                app_store_name=data.get("app_store_name", ""),
                metadata={k: v for k, v in data.items() if k not in (
                    "legal_name", "common_name", "glassdoor_slug", "comparably_slug",
                    "indeed_slug", "g2_slug", "capterra_slug", "github_org",
                    "sec_cik", "app_store_name"
                )},
            )
            self._entities[ticker] = entity
            for slug_field in ("glassdoor_slug", "comparably_slug", "indeed_slug", "g2_slug", "capterra_slug"):
                slug_val = data.get(slug_field)
                if slug_val:
                    self._by_slug[slug_val] = ticker

    def resolve(self, ticker: str) -> Optional[CompanyEntity]:
        return self._entities.get(ticker.upper())

    def resolve_by_slug(self, slug: str) -> Optional[CompanyEntity]:
        ticker = self._by_slug.get(slug)
        if ticker:
            return self._entities.get(ticker)
        return None

    def get_slug(self, ticker: str, source: str) -> Optional[str]:
        entity = self.resolve(ticker)
        if entity is None:
            return None
        slug_map = {
            "glassdoor": entity.glassdoor_slug,
            "comparably": entity.comparably_slug,
            "indeed": entity.indeed_slug,
            "g2": entity.g2_slug,
            "capterra": entity.capterra_slug,
        }
        return slug_map.get(source)

    def get_all_tickers(self) -> List[str]:
        return list(self._entities.keys())

    def get_all_entities(self) -> List[CompanyEntity]:
        return list(self._entities.values())
