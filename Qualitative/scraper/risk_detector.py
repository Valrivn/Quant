import pandas as pd
import numpy as np
import logging
from db.connection import get_db_connection

logger = logging.getLogger(__name__)

def detect_risk_narratives(window_days: int = 14, z_threshold: float = 1.5) -> pd.DataFrame:
    """
    Scans risk_signals history to identify trending risk narratives.
    Computes a Z-score of frequency relative to the historical average.
    Returns a DataFrame of active anomalies / trending risk categories.
    """
    logger.debug(f"Detecting risk narratives (window={window_days}, threshold={z_threshold})")
    conn = get_db_connection()
    
    # Query daily risk frequencies by category & type
    query = """
        SELECT date, risk_type, category, SUM(frequency) as total_frequency
        FROM risk_signals
        GROUP BY date, risk_type, category
        ORDER BY date ASC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    if df.empty:
        logger.debug("No risk signals found in database")
        return pd.DataFrame(columns=["date", "risk_type", "category", "frequency", "mean", "std", "z_score", "is_trending"])
        
    # Pivot to get timeseries
    df_pivot = df.pivot(index="date", columns=["category", "risk_type"], values="total_frequency").fillna(0.0)
    
    # Calculate rolling mean and standard deviation
    rolling_mean = df_pivot.rolling(window=window_days, min_periods=3).mean()
    rolling_std = df_pivot.rolling(window=window_days, min_periods=3).std().replace(0.0, np.nan)
    
    # Calculate Z-score
    z_scores = (df_pivot - rolling_mean) / rolling_std
    
    # Unpivot back to long format for filtering
    trending_list = []
    for (cat, rtype) in df_pivot.columns:
        for date in df_pivot.index:
            freq = df_pivot.loc[date, (cat, rtype)]
            mean_val = rolling_mean.loc[date, (cat, rtype)]
            std_val = rolling_std.loc[date, (cat, rtype)]
            z_val = z_scores.loc[date, (cat, rtype)]
            
            if not np.isnan(z_val) and z_val >= z_threshold:
                trending_list.append({
                    "date": date,
                    "category": cat,
                    "risk_type": rtype,
                    "frequency": int(freq),
                    "mean": float(mean_val),
                    "std": float(std_val) if not np.isnan(std_val) else 0.0,
                    "z_score": float(z_val),
                    "is_trending": True
                })
    
    result_df = pd.DataFrame(trending_list)
    if not result_df.empty:
        logger.info(f"Detected {len(result_df)} trending risk narratives")
        logger.debug(f"Top risks: {result_df[['risk_type', 'z_score']].head().to_dict()}")
    else:
        logger.debug("No trending risk narratives detected")
                
    return result_df
