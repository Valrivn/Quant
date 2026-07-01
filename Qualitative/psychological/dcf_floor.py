import logging
from typing import Dict, Optional, Any
from dataclasses import dataclass
from abc import ABC, abstractmethod
from config import load_hybrid_config

logger = logging.getLogger(__name__)


@dataclass
class DCFFloorOutput:
    ticker: str
    date: str
    intrinsic_floor: float
    intrinsic_ceiling: float
    current_price: float
    margin_of_safety: float
    wacc: float
    fcf_projection: float
    terminal_value: float
    model_version: str
    upside_ratio: float
    risk_adjusted_upside: float


class DCFFloorInterface(ABC):
    @abstractmethod
    def calculate_dcf_floor(self, ticker: str, current_price: float, 
                            fcf_projection: float, wacc: float, 
                            terminal_growth: float, projection_years: int = 5) -> DCFFloorOutput:
        pass
    
    @abstractmethod
    def get_dcf_floor(self, ticker: str) -> Optional[DCFFloorOutput]:
        pass


class DCFFloorStub(DCFFloorInterface):
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.dcf_config = self.config.get("quantitative_dcf", {})
        self.default_wacc = self.dcf_config.get("default_wacc", 0.10)
        self.default_terminal_growth = self.dcf_config.get("default_terminal_growth", 0.03)
        self.projection_years = self.dcf_config.get("projection_years", 5)
        self.margin_of_safety = self.dcf_config.get("margin_of_safety", 0.25)
        self.model_version = self.dcf_config.get("model_version", "stub_v1")
        
        self._cache: Dict[str, DCFFloorOutput] = {}

    def calculate_dcf_floor(self, ticker: str, current_price: float, 
                            fcf_projection: float, wacc: float, 
                            terminal_growth: float, projection_years: int = 5) -> DCFFloorOutput:
        if wacc <= terminal_growth:
            raise ValueError("WACC must be greater than terminal growth rate")
        
        pv_fcf = 0.0
        for year in range(1, projection_years + 1):
            pv_fcf += fcf_projection / ((1 + wacc) ** year)
        
        terminal_fcf = fcf_projection * (1 + terminal_growth)
        terminal_value = terminal_fcf / (wacc - terminal_growth)
        pv_terminal = terminal_value / ((1 + wacc) ** projection_years)
        
        intrinsic_value = pv_fcf + pv_terminal
        floor = intrinsic_value * (1 - self.margin_of_safety)
        ceiling = intrinsic_value * (1 + self.margin_of_safety)
        
        upside_ratio = (ceiling - current_price) / (ceiling - floor) if ceiling > floor else 0.5
        risk_adjusted_upside = upside_ratio * (1 + self.margin_of_safety)
        
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        output = DCFFloorOutput(
            ticker=ticker,
            date=date_str,
            intrinsic_floor=floor,
            intrinsic_ceiling=ceiling,
            current_price=current_price,
            margin_of_safety=self.margin_of_safety,
            wacc=wacc,
            fcf_projection=fcf_projection,
            terminal_value=terminal_value,
            model_version=self.model_version,
            upside_ratio=min(1.0, max(0.0, upside_ratio)),
            risk_adjusted_upside=min(1.0, max(0.0, risk_adjusted_upside))
        )
        
        self._cache[ticker] = output
        return output

    def get_dcf_floor(self, ticker: str) -> Optional[DCFFloorOutput]:
        return self._cache.get(ticker)


class DCFFloorClient:
    def __init__(self, db_path: str = "reddit_quant.db", config_dict: dict = None):
        self.db_path = db_path
        self.config = config_dict or load_hybrid_config()
        self.stub = DCFFloorStub(config_dict)

    def fetch_and_store(self, ticker: str, current_price: float, 
                        fcf_projection: float, wacc: float = None,
                        terminal_growth: float = None) -> DCFFloorOutput:
        wacc = wacc or self.stub.default_wacc
        terminal_growth = terminal_growth or self.stub.default_terminal_growth
        
        dcf_output = self.stub.calculate_dcf_floor(
            ticker, current_price, fcf_projection, wacc, terminal_growth, self.stub.projection_years
        )
        
        self._store_dcf_floor(dcf_output)
        return dcf_output

    def _store_dcf_floor(self, dcf: DCFFloorOutput) -> None:
        import sqlite3
        from datetime import datetime, timezone
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO quantitative_dcf_floor
                (ticker, date, intrinsic_floor, intrinsic_ceiling, margin_of_safety,
                 current_price, wacc, fcf_projection, terminal_value, model_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                dcf.ticker,
                dcf.date,
                dcf.intrinsic_floor,
                dcf.intrinsic_ceiling,
                dcf.margin_of_safety,
                dcf.current_price,
                dcf.wacc,
                dcf.fcf_projection,
                dcf.terminal_value,
                dcf.model_version,
                int(datetime.now(timezone.utc).timestamp())
            ))
            conn.commit()

    def get_latest(self, ticker: str) -> Optional[Dict]:
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT intrinsic_floor, intrinsic_ceiling, current_price, margin_of_safety,
                       wacc, fcf_projection, terminal_value, model_version
                FROM quantitative_dcf_floor WHERE ticker = ? ORDER BY date DESC LIMIT 1
            """, (ticker,))
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
            logger.warning(f"Error fetching DCF floor for {ticker}: {e}")
        return None


def create_dcf_floor_stub(config_dict: dict = None) -> DCFFloorStub:
    return DCFFloorStub(config_dict)


def create_dcf_floor_client(db_path: str = "reddit_quant.db", config_dict: dict = None) -> DCFFloorClient:
    return DCFFloorClient(db_path, config_dict)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    client = create_dcf_floor_client(":memory:")
    
    test_tickers = [
        ("NVDA", 800, 50_000_000_000, 0.10, 0.03),
        ("AMD", 150, 8_000_000_000, 0.11, 0.03),
        ("MSFT", 400, 80_000_000_000, 0.09, 0.03),
    ]
    
    for ticker, price, fcf, wacc, growth in test_tickers:
        result = client.fetch_and_store(ticker, price, fcf, wacc, growth)
        print(f"\n{ticker}:")
        print(f"  Current: ${price:.2f}")
        print(f"  Floor: ${result.intrinsic_floor:.2f}")
        print(f"  Ceiling: ${result.intrinsic_ceiling:.2f}")
        print(f"  Upside Ratio: {result.upside_ratio:.3f}")
        print(f"  Risk-Adjusted: {result.risk_adjusted_upside:.3f}")