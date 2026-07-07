import datetime
from typing import List, Dict, Any

class DataProvenanceAudit:
    def get_logs(self) -> List[Dict[str, Any]]:
        now = datetime.datetime.now()
        return [
            {
                "metric": "Inflation Rate (CPI)",
                "raw_value": 4.5,
                "source": "FRED (Federal Reserve Economic Data)",
                "timestamp": (now - datetime.timedelta(days=7)).isoformat(),
                "staleness_days": 7
            },
            {
                "metric": "S&P 500 Performance (Daily)",
                "raw_value": -0.015,
                "source": "Yahoo Finance API",
                "timestamp": (now - datetime.timedelta(hours=2)).isoformat(),
                "staleness_days": 0
            },
            {
                "metric": "Gold Spot Price (USD/oz)",
                "raw_value": 2050.75,
                "source": "LBMA (London Bullion Market Association)",
                "timestamp": (now - datetime.timedelta(minutes=30)).isoformat(),
                "staleness_days": 0
            }
        ]
