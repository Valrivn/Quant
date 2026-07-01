from psychological.scrapers.reddit_primary import RedditPrimaryScraper, create_reddit_scraper
from psychological.scrapers.github_tracker import GitHubTracker, create_github_tracker
from psychological.scrapers.corp_anonymous import CorpAnonymousScraper, create_corp_anonymous_scraper
from psychological.scrapers.corp_audit import (
    GlassdoorScraper,
    CorpAuditEngine,
    create_corp_audit_engine,
    create_glassdoor_scraper,
    G2EmployerScraper,
    create_g2_employer_scraper,
    IndeedScraper,
    IndeedScore,
    create_indeed_scraper,
    ComparablyScraper,
    create_comparably_scraper,
)
from psychological.scrapers.product_intel import G2Scraper, CapterraScraper, AppStoreScraper, ProductIntelEngine, create_product_intel_engine
# lightweight_scraper imports seleniumbase which calls os.path.abspath(".")
# at module level — this crashes if CWD doesn't exist (e.g., inside worktree subshells).
# Guard with try/except so corp_audit and other scrapers remain importable.
try:
    from psychological.scrapers.reddit_custom import RedditScraper, create_reddit_scraper as create_old_reddit_scraper
except Exception as _exc:  # noqa: BLE001
    import warnings
    warnings.warn(f"reddit_custom import failed (seleniumbase dependency): {_exc}", ImportWarning, stacklevel=1)
    RedditScraper = None  # type: ignore[assignment,misc]
    create_old_reddit_scraper = None  # type: ignore[assignment]

try:
    from psychological.scrapers.lightweight_scraper import UnifiedScraperSession, ScraperConfig, ScraperSessionPool, create_scraper_session
except Exception as _exc:  # noqa: BLE001
    import warnings
    warnings.warn(f"lightweight_scraper import failed (seleniumbase dependency): {_exc}", ImportWarning, stacklevel=1)
    UnifiedScraperSession = None  # type: ignore[assignment,misc]
    ScraperConfig = None  # type: ignore[assignment,misc]
    ScraperSessionPool = None  # type: ignore[assignment,misc]
    create_scraper_session = None  # type: ignore[assignment]
from psychological.scrapers.validation_gate import CrossValidationGate, ValidationGateResult, create_validation_gate
from psychological.scrapers.hiring_velocity import HiringVelocityEngine, JobSpySnapshot, create_hiring_velocity
from psychological.scrapers.moat_discovery import MoatNode, MoatTree, MoatDiscoveryEngine, create_moat_discovery_engine
from psychological.scrapers.cross_validation import CrossValidationEngine, CrossValidationResult, create_cross_validation_engine
from psychological.scrapers.company_resolver import CompanyResolver, CompanyEntity
from psychological.scrapers.employer_translator import EmployerTranslator, EmployerSentimentResult
from psychological.signal_matrix import SignalMatrix, SignalMatrixOutput, ExecutionDirective, PsychologicalRegime, create_signal_matrix
from psychological.dcf_floor import DCFFloorInterface, DCFFloorStub, DCFFloorClient, DCFFloorOutput, create_dcf_floor_stub, create_dcf_floor_client
from psychological.engineering_guards import (
    guard_nan, guard_division, guard_bounds, guard_utc_timestamp, ensure_utc,
    RateLimiter, rate_limited, timed_operation, RetryPolicy,
    safe_float, safe_int, safe_dict_get, validate_ticker, validate_date_str,
    with_timeout, sanitize_text, clamp
)

__all__ = [
    "RedditPrimaryScraper",
    "create_reddit_scraper",
    "GitHubTracker",
    "create_github_tracker",
    "CorpAnonymousScraper",
    "create_corp_anonymous_scraper",
    "GlassdoorScraper",
    "G2EmployerScraper",
    "IndeedScraper",
    "IndeedScore",
    "CorpAuditEngine",
    "create_corp_audit_engine",
    "create_glassdoor_scraper",
    "create_g2_employer_scraper",
    "create_indeed_scraper",
    "ComparablyScraper",
    "create_comparably_scraper",
    "G2Scraper",
    "CapterraScraper",
    "AppStoreScraper",
    "ProductIntelEngine",
    "create_product_intel_engine",
    "RedditScraper",
    "create_old_reddit_scraper",
    "UnifiedScraperSession",
    "ScraperConfig",
    "ScraperSessionPool",
    "create_scraper_session",
    "CrossValidationGate",
    "ValidationGateResult",
    "create_validation_gate",
    "HiringVelocityEngine",
    "JobSpySnapshot",
    "create_hiring_velocity",
    "MoatNode",
    "MoatTree",
    "MoatDiscoveryEngine",
    "create_moat_discovery_engine",
    "CrossValidationEngine",
    "CrossValidationResult",
    "create_cross_validation_engine",
    "CompanyResolver",
    "CompanyEntity",
    "EmployerTranslator",
    "EmployerSentimentResult",
    "SignalMatrix",
    "SignalMatrixOutput",
    "ExecutionDirective",
    "PsychologicalRegime",
    "create_signal_matrix",
    "DCFFloorInterface",
    "DCFFloorStub",
    "DCFFloorClient",
    "DCFFloorOutput",
    "create_dcf_floor_stub",
    "create_dcf_floor_client",
    "guard_nan", "guard_division", "guard_bounds", "guard_utc_timestamp", "ensure_utc",
    "RateLimiter", "rate_limited", "timed_operation", "RetryPolicy",
    "safe_float", "safe_int", "safe_dict_get", "validate_ticker", "validate_date_str",
    "with_timeout", "sanitize_text", "clamp",
]