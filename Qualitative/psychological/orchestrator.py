import asyncio
import logging
from typing import List, Dict, Optional
from datetime import datetime, timezone
from dataclasses import dataclass

from psychological.nlp_engine import NLPEngine, create_nlp_engine
from psychological.velocity_tracker import VelocityTracker, create_velocity_tracker
from psychological.state_machine import PsychologicalStateMachine, create_state_machine
from psychological.behavioral_feature_store import BehavioralFeatureStore, create_behavioral_feature_store
from psychological.scrapers import (
    RedditScraper, create_old_reddit_scraper,
    GitHubTracker, create_github_tracker,
    CorpAnonymousScraper, create_corp_anonymous_scraper,
    GlassdoorScraper, create_glassdoor_scraper,
    ComparablyScraper, create_comparably_scraper,
    ProductIntelEngine, create_product_intel_engine,
    CrossValidationEngine, create_cross_validation_engine,
    ScraperConfig
)
from psychological.signal_matrix import SignalMatrix, create_signal_matrix
from psychological.interfaces import (
    PsychologicalFeatureVector, RegimeOutput, RedditCommentPayload,
    VelocitySnapshot, NLPMetrics, VelocityMetrics, CorporateAffinity
)
from psychological.scrapers.validation_gate import CrossValidationGate
from psychological.scrapers.moat_discovery import (
    MoatWeightingLayer, MoatScoringEngine, MoatTree, MoatNode,
    create_moat_weighting_layer, create_moat_scoring_engine
)
from config import load_hybrid_config

logger = logging.getLogger(__name__)


@dataclass
class PsychologicalScrapeResult:
    source: str
    tickers_processed: List[str]
    vectors_committed: int
    regimes_committed: int
    errors: List[str]


class PsychologicalOrchestrator:
    """
    Primary Contrarian Sentiment Engine.
    
    Pipeline:
    1. Primary: Reddit -> NLP -> Velocity -> State Machine -> Feature Store
    2. Secondary: GitHub + Adzuna (with Paradigm 5 error handling)
    3. Supplementary: Fintech confirmation + Quantitative value signal
    4. Regime-level fusion (not feature-level)
    """

    def __init__(self, db_path: str = "reddit_quant.db", config_dict: dict = None):
        self.db_path = db_path
        hybrid_config = load_hybrid_config()
        self.config = config_dict or hybrid_config.get("psychological", {})
        self.fusion_weights = self.config.get("fusion_weights", {
            "psychological_regime": 0.60,
            "fintech_confirmation": 0.25,
            "quantitative_value": 0.15
        })
        
        self.nlp_engine = create_nlp_engine(self.config)
        self.velocity_tracker = create_velocity_tracker(db_path, self.config)
        self.state_machine = create_state_machine(self.config)
        self.feature_store = create_behavioral_feature_store(db_path)
        self.validation_gate = CrossValidationGate(self.config)
        self.cross_validation_engine = create_cross_validation_engine(self.config)
        self.signal_matrix = create_signal_matrix(self.config)
        
        self.reddit_scraper = None
        self.github_tracker = None
        self.corp_scraper = None
        self.glassdoor_scraper = None
        self.comparably_scraper = None
        self.product_intel_engine = None

    async def initialize_scrapers(self):
        scraper_config = ScraperConfig(
            headless=True,
            uc_mode=False,
            binary_location="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            min_delay=0.1,
            max_delay=0.2
        )
        try:
            self.reddit_scraper = await create_old_reddit_scraper(self.config, scraper_config)
        except Exception as e:
            logger.warning(f"Failed to initialize Reddit scraper: {e}")
            self.reddit_scraper = None
        
        try:
            self.github_tracker = await create_github_tracker(self.config)
        except Exception as e:
            logger.warning(f"Failed to initialize GitHub tracker: {e}")
            self.github_tracker = None
        
        try:
            self.corp_scraper = await create_corp_anonymous_scraper(self.config)
        except Exception as e:
            logger.warning(f"Failed to initialize corp anonymous scraper: {e}")
            self.corp_scraper = None
        
        # Glassdoor and Comparably are slow/blocked - initialize lazily or skip
        self.glassdoor_scraper = None
        self.comparably_scraper = None
        
        try:
            self.product_intel_engine = await create_product_intel_engine(self.config)
        except Exception as e:
            logger.warning(f"Failed to initialize product intel engine: {e}")
            self.product_intel_engine = None

    async def run_primary_pipeline(self, tickers: List[str]) -> PsychologicalScrapeResult:
        """Primary pipeline: Reddit -> NLP -> Velocity -> State Machine -> Feature Store"""
        errors = []
        vectors_committed = 0
        regimes_committed = 0
        tickers_processed = []
        
        validation_data = {}
        # Skip Glassdoor/Comparably in primary pipeline - they're too slow and blocked
        # Validation data is fetched in secondary pipeline instead
        
        try:
            async for comment_payload in self.reddit_scraper.harvest_raw_commentary(tickers):
                ticker = comment_payload["ticker"]
                text = comment_payload["text"]
                
                if ticker not in tickers_processed:
                    tickers_processed.append(ticker)
                
                nlp_result = self.nlp_engine.analyze(text)
                
                velocity_snapshot = VelocitySnapshot(
                    ticker=ticker,
                    window_start=int(datetime.now(timezone.utc).timestamp()),
                    window_end=int(datetime.now(timezone.utc).timestamp()),
                    window_type="1h",
                    mention_count=1,
                    comment_volume=1,
                    unique_authors=1
                )
                self.velocity_tracker.record_snapshot(velocity_snapshot)
                
                velocity_metrics = self.velocity_tracker.calculate_velocity_metrics(ticker, 24)
                
                vg_data = validation_data.get(ticker, {})
                glassdoor_raw = vg_data.get("glassdoor_raw")
                comparably_badge = vg_data.get("comparably_badge")
                
                vector = PsychologicalFeatureVector(
                    ticker=ticker,
                    timestamp=int(datetime.now(timezone.utc).timestamp()),
                    source_provenance=f"reddit:{comment_payload['subreddit']}",
                    raw_text=text,
                    compound_vader=nlp_result["compound_vader"],
                    bull_bear_ratio=nlp_result["bull_bear_ratio"],
                    bullish_count=nlp_result["bullish_count"],
                    bearish_count=nlp_result["bearish_count"],
                    mention_velocity=velocity_metrics["mention_velocity"],
                    comment_volume_sigma=velocity_metrics["comment_volume_sigma"],
                    acceleration=velocity_metrics["acceleration"],
                    employee_sentiment_proxy=None,
                    dev_fork_acceleration=None,
                    metadata_json=str(comment_payload)
                )
                
                self.feature_store.commit_vector(vector)
                vectors_committed += 1
                
                regime_result = self.state_machine.evaluate(
                    bull_bear_ratio=nlp_result["bull_bear_ratio"],
                    velocity_sigma=velocity_metrics["comment_volume_sigma"],
                    glassdoor_raw=glassdoor_raw,
                    comparably_badge=comparably_badge
                )
                
                date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                self.feature_store.commit_regime(
                    ticker=ticker,
                    date=date_str,
                    regime=regime_result["regime"],
                    contrarian_buy=regime_result["contrarian_buy_authorized"],
                    confidence=regime_result["confidence"],
                    bull_bear_ratio=nlp_result["bull_bear_ratio"],
                    velocity_sigma=velocity_metrics["comment_volume_sigma"]
                )
                regimes_committed += 1
                
        except Exception as e:
            logger.error(f"Primary pipeline error: {e}")
            errors.append(str(e))
            
        return PsychologicalScrapeResult(
            source="reddit_custom",
            tickers_processed=tickers_processed,
            vectors_committed=vectors_committed,
            regimes_committed=regimes_committed,
            errors=errors
        )

    async def run_secondary_pipeline(self, tickers: List[str]) -> Dict[str, CorporateAffinity]:
        """Secondary pipeline: GitHub + Adzuna + Product Intel with graceful degradation"""
        corporate_affinities = {}
        
        try:
            github_metrics = await self.github_tracker.get_all_metrics()
            for ticker in tickers:
                if ticker in github_metrics:
                    metrics = github_metrics[ticker]
                    velocities = self.github_tracker.calculate_velocities(metrics)
                    corporate_affinities[ticker] = CorporateAffinity(
                        employee_sentiment_proxy=None,
                        dev_fork_acceleration=velocities.get("dev_fork_acceleration", 0.0)
                    )
        except Exception as e:
            logger.warning(f"GitHub tracker failed (graceful degradation): {e}")
            for ticker in tickers:
                if ticker not in corporate_affinities:
                    corporate_affinities[ticker] = CorporateAffinity(
                        employee_sentiment_proxy=None,
                        dev_fork_acceleration=0.0
                    )
        
        try:
            adzuna_snapshots = await self.corp_scraper.get_all_snapshots()
            for ticker in tickers:
                if ticker in adzuna_snapshots:
                    snapshot = adzuna_snapshots[ticker]
                    sentiment_proxy = self.corp_scraper.calculate_sentiment_proxy(snapshot)
                    if ticker in corporate_affinities:
                        corporate_affinities[ticker]["employee_sentiment_proxy"] = sentiment_proxy
                    else:
                        corporate_affinities[ticker] = CorporateAffinity(
                            employee_sentiment_proxy=sentiment_proxy,
                            dev_fork_acceleration=0.0
                        )
        except Exception as e:
            logger.warning(f"Adzuna scraper failed (graceful degradation): {e}")
            for ticker in tickers:
                if ticker not in corporate_affinities:
                    corporate_affinities[ticker] = CorporateAffinity(
                        employee_sentiment_proxy=0.0,
                        dev_fork_acceleration=0.0
                    )
                elif corporate_affinities[ticker].get("employee_sentiment_proxy") is None:
                    corporate_affinities[ticker]["employee_sentiment_proxy"] = 0.0

        try:
            product_snapshots = await self.product_intel_engine.get_all_snapshots()
            for ticker in tickers:
                if ticker in product_snapshots:
                    product_data = product_snapshots[ticker]
                    all_reviews = []
                    for platform in ["g2", "capterra", "app_store"]:
                        if platform in product_data:
                            all_reviews.extend(product_data[platform])
                    product_sentiment = self.product_intel_engine.compute_product_sentiment(all_reviews)
                    if ticker in corporate_affinities:
                        corporate_affinities[ticker]["product_sentiment_proxy"] = product_sentiment
                    else:
                        corporate_affinities[ticker] = CorporateAffinity(
                            employee_sentiment_proxy=0.0,
                            dev_fork_acceleration=0.0,
                            product_sentiment_proxy=product_sentiment
                        )
        except Exception as e:
            logger.warning(f"Product intel failed (graceful degradation): {e}")
            for ticker in tickers:
                if ticker not in corporate_affinities:
                    corporate_affinities[ticker] = CorporateAffinity(
                        employee_sentiment_proxy=0.0,
                        dev_fork_acceleration=0.0,
                        product_sentiment_proxy=0.0
                    )
                elif corporate_affinities[ticker].get("product_sentiment_proxy") is None:
                    corporate_affinities[ticker]["product_sentiment_proxy"] = 0.0
                    
        return corporate_affinities

    def get_quantitative_value_signal(self, ticker: str) -> float:
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT intrinsic_floor, intrinsic_ceiling, current_price FROM quantitative_dcf_floor WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (ticker,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                floor, ceiling, current = row[0], row[1], row[2]
                if ceiling > floor:
                    val = (ceiling - current) / (ceiling - floor)
                    return min(1.0, max(0.0, val))
                return 0.5
        except Exception as e:
            logger.warning(f"Error reading quantitative_dcf_floor: {e}")
        return 0.5

    def get_dcf_floor_data(self, ticker: str) -> Optional[Dict]:
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT intrinsic_floor, intrinsic_ceiling, current_price, margin_of_safety, wacc, fcf_projection, terminal_value, model_version FROM quantitative_dcf_floor WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (ticker,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "intrinsic_floor": row[0],
                    "intrinsic_ceiling": row[1],
                    "current_price": row[2],
                    "margin_of_safety": row[3],
                    "wacc": row[4],
                    "fcf_projection": row[5],
                    "terminal_value": row[6],
                    "model_version": row[7]
                }
        except Exception as e:
            logger.warning(f"Error reading quantitative_dcf_floor: {e}")
        return None

    async def fetch_fintech_confirmation(self, tickers: List[str]) -> Dict[str, float]:
        from scraper.fintech_clients.factory import FintechClientFactory
        factory = FintechClientFactory()
        try:
            health = await factory.health_check_all()
            healthy_sources = factory.get_healthy_sources(health)
            
            fintech_sentiment = {t: 0.5 for t in tickers}
            ticker_counts = {t: 0 for t in tickers}
            ticker_sums = {t: 0.0 for t in tickers}
            
            for source in healthy_sources:
                try:
                    client = factory.get_client(source)
                    messages = await client.fetch_messages(tickers, limit=100)
                    for m in messages:
                        t = m.ticker.upper()
                        if t in ticker_sums and m.sentiment_score is not None:
                            norm_score = (m.sentiment_score + 1.0) / 2.0
                            ticker_sums[t] += norm_score
                            ticker_counts[t] += 1
                except Exception as e:
                    logger.warning(f"Error fetching fintech messages for {source}: {e}")
            
            for t in tickers:
                if ticker_counts[t] > 0:
                    fintech_sentiment[t] = ticker_sums[t] / ticker_counts[t]
            return fintech_sentiment
        except Exception as e:
            logger.warning(f"Failed to fetch fintech confirmation: {e}")
            return {t: 0.5 for t in tickers}
        finally:
            await factory.close_all()

    def compute_cross_validation_penalty(self, ticker: str, reddit_ratio: float, 
                                         fintech_sentiment: float, dev_velocity: float) -> float:
        penalty = 1.0
        norm_reddit = min(reddit_ratio / 4.0, 1.0) if reddit_ratio is not None else 0.5
        if abs(norm_reddit - fintech_sentiment) > 0.5:
            penalty *= 0.8
        if norm_reddit > 0.7 and dev_velocity is not None and dev_velocity < 0.0:
            penalty *= 0.85
        return penalty

    def compute_fused_confidence(self, regime: RegimeOutput, fintech_confirmation: float, 
                                  quantitative_value: float) -> float:
        """Regime-level fusion (not feature-level)"""
        regime_confidence = regime["confidence"]
        fused = (
            self.fusion_weights["psychological_regime"] * regime_confidence +
            self.fusion_weights["fintech_confirmation"] * fintech_confirmation +
            self.fusion_weights["quantitative_value"] * quantitative_value
        )
        return min(1.0, max(0.0, fused))

    async def run_full_pipeline(self, tickers: List[str], 
                                fintech_confirmation: Dict[str, float] = None,
                                quantitative_value: Dict[str, float] = None) -> Dict[str, Dict]:
        """Run complete psychological pipeline with fusion"""
        await self.initialize_scrapers()
        
        primary_result = await self.run_primary_pipeline(tickers)
        corporate_affinities = await self.run_secondary_pipeline(tickers)
        
        if fintech_confirmation is None:
            fintech_confirmation = await self.fetch_fintech_confirmation(tickers)
            
        results = {}
        for ticker in tickers:
            regimes = self.feature_store.get_regimes(ticker)
            latest_regime = regimes[0] if regimes else None
            
            if latest_regime:
                regime_output = RegimeOutput(
                    regime=latest_regime["active_regime"],
                    contrarian_buy_authorized=latest_regime["contrarian_buy_authorized"],
                    confidence=latest_regime["confidence_score"]
                )
                
                fintech_conf = fintech_confirmation.get(ticker, 0.5)
                
                if quantitative_value and ticker in quantitative_value:
                    quant_val = quantitative_value[ticker]
                else:
                    quant_val = self.get_quantitative_value_signal(ticker)
                
                # Fetch dev velocity from corporate_affinities
                affinity = corporate_affinities.get(ticker, {})
                dev_velocity = affinity.get("dev_fork_acceleration", 0.0) if hasattr(affinity, "get") else 0.0
                
                fused_confidence = self.compute_fused_confidence(regime_output, fintech_conf, quant_val)
                
                # Apply cross-validation convergence penalty (Layer 4)
                penalty = self.compute_cross_validation_penalty(
                    ticker, 
                    latest_regime.get("bull_bear_ratio", 1.0),
                    fintech_conf,
                    dev_velocity
                )
                fused_confidence *= penalty
                
                # Apply 4-layer cross-validation
                validation_result = self.cross_validation_engine.evaluate_all_layers(
                    ticker=ticker,
                    regime_data=latest_regime,
                    fintech_sentiment=fintech_conf,
                    reddit_bull_bear_ratio=latest_regime.get("bull_bear_ratio", 1.0),
                    dev_velocity=dev_velocity,
                    quant_value=quant_val
                )
                
                if validation_result.get("final_override", False):
                    fused_confidence = 0.0
                    latest_regime["validation_override"] = True
                else:
                    latest_regime["validation_override"] = False
                    
                latest_regime["validation_details"] = validation_result
                fused_confidence *= validation_result.get("combined_penalty", 1.0)
                
                latest_regime["fused_confidence"] = min(1.0, max(0.0, fused_confidence))
                latest_regime["corporate_affinity"] = corporate_affinities.get(ticker, {})
                
                # Fetch DCF floor data for Signal Matrix
                dcf_floor_data = self.get_dcf_floor_data(ticker)
                
                # Run Signal Matrix for execution directive
                signal_output = self.signal_matrix.evaluate(
                    regime=latest_regime["active_regime"],
                    fused_confidence=latest_regime["fused_confidence"],
                    dcf_floor_data=dcf_floor_data,
                    validation_passed=not validation_result.get("final_override", False),
                    validation_details=validation_result,
                    contrarian_buy_authorized=latest_regime["contrarian_buy_authorized"]
                )
                
                latest_regime["signal_matrix"] = {
                    "execution_directive": signal_output.execution_directive,
                    "contrarian_buy_authorized": signal_output.contrarian_buy_authorized,
                    "dcf_floor_signal": signal_output.dcf_floor_signal,
                    "validation_passed": signal_output.validation_passed,
                    "rationale": signal_output.rationale
                }
                
                results[ticker] = latest_regime
            else:
                results[ticker] = {
                    "error": "No regime computed",
                    "corporate_affinity": corporate_affinities.get(ticker, {})
                }
                
        return {
            "primary_result": primary_result,
            "corporate_affinities": corporate_affinities,
            "fused_results": results
        }

    async def run_qualitative_pipeline(self, ticker: str) -> Dict:
        """Merged Branch 1 (Employer Sentiment) and Branch 2 (Moat Discovery) outputs."""
        await self.initialize_scrapers()

        branch1_result = await self.run_primary_pipeline([ticker])
        corporate_affinities = await self.run_secondary_pipeline([ticker])

        moat_layer = await create_moat_weighting_layer(self.config)
        scoring_engine = await create_moat_scoring_engine(self.config)
        await scoring_engine.initialize(
            product_intel=self.product_intel_engine,
            reddit_scraper=self.reddit_scraper
        )

        company_name = self.config.get("company_name", ticker)
        sample_nodes = [
            MoatNode(name=f"{company_name} Core", source="discovered", ticker=ticker, node_type="platform",
                     stars=5000, description="Core platform product"),
        ]
        moat_tree = MoatTree(ticker=ticker, company_name=company_name, nodes=sample_nodes)
        ranked = moat_layer.rank_nodes(moat_tree)
        scored = await scoring_engine.score_tree(moat_tree)

        validation_result = {}
        if self.cross_validation_engine:
            validation_result = self.cross_validation_engine.validate_moat_convergence(
                moat_tree=scored,
                github_metrics=corporate_affinities.get(ticker, {}).get("dev_fork_acceleration", 0.0),
                product_sentiment=corporate_affinities.get(ticker, {}).get("product_sentiment_proxy", 0.0)
            )

        return {
            "ticker": ticker,
            "branch1_employer_sentiment": corporate_affinities.get(ticker, {}),
            "branch2_moat_discovery": {
                "tree": scored,
                "top_nodes": [n.name for n in scored.nodes],
                "top_stars": [n.stars for n in scored.nodes],
            },
            "moat_convergence": validation_result,
            "primary_pipeline": branch1_result
        }

    def get_regime_status(self, ticker: str) -> Optional[Dict]:
        """Get current regime status for a ticker"""
        regimes = self.feature_store.get_regimes(ticker)
        if regimes:
            latest = regimes[0]
            fintech_score = latest.get("fintech_confirmation_score")
            quant_score = latest.get("quantitative_value_signal")
            if fintech_score is None:
                fintech_score = 0.5
            if quant_score is None:
                quant_score = 0.5
            latest["fused_confidence"] = self.compute_fused_confidence(
                RegimeOutput(
                    regime=latest["active_regime"],
                    contrarian_buy_authorized=latest["contrarian_buy_authorized"],
                    confidence=latest["confidence_score"]
                ),
                fintech_score,
                quant_score
            )
            return latest
        return None


async def create_psychological_orchestrator(db_path: str = "reddit_quant.db", 
                                             config_dict: dict = None) -> PsychologicalOrchestrator:
    return PsychologicalOrchestrator(db_path, config_dict)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    async def test():
        orchestrator = await create_psychological_orchestrator()
        results = await orchestrator.run_full_pipeline(["AAPL", "TSLA", "NVDA"])
        print(results)
        
    asyncio.run(test())