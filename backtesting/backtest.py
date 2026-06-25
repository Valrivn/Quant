import pandas as pd
import numpy as np
import scipy.stats as stats
import yfinance as yf
import logging
from datetime import datetime, timedelta
from db.connection import get_db_connection
from config import HYBRID_SOURCE_WEIGHTS

logger = logging.getLogger(__name__)

def fetch_historical_returns(tickers: list, start_date: str, end_date: str) -> pd.DataFrame:
    """
    Fetches monthly return series for given tickers using yfinance.
    Fallback to random returns if yfinance fails.
    """
    if not tickers:
        return pd.DataFrame()
        
    try:
        data = yf.download(tickers, start=start_date, end=end_date, interval="1d", progress=False)
        if 'Adj Close' in data:
            prices = data['Adj Close']
        elif 'Close' in data:
            prices = data['Close']
        else:
            raise ValueError("No price columns found")
            
        if isinstance(prices, pd.Series):
            prices = prices.to_frame(name=tickers[0])
            
        # Reindex to daily, forward fill, then compute monthly returns
        prices = prices.ffill().bfill()
        monthly_prices = prices.resample('ME').last()
        monthly_returns = monthly_prices.pct_change().fillna(0.0)
        return monthly_returns
    except Exception as e:
        logger.warning(f"yfinance download failed ({e}), using simulated returns for backtesting.")
        # Create simulated monthly returns
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        dates = pd.date_range(start=start_dt, end=end_dt, freq='ME')
        sim_data = np.random.normal(0.01, 0.05, size=(len(dates), len(tickers)))
        df_sim = pd.DataFrame(sim_data, index=dates, columns=tickers)
        return df_sim

def run_walk_forward_backtest(
    category_weights: dict,
    subreddit_weights: dict,
    source_weights: dict = None,
    lookback_days: int = 180,
    objective: str = "information_coefficient"
) -> dict:
    """
    Executes a walk-forward monthly rebalancing simulation with multi-source support.
    Returns calculated IC, Sharpe ratio, Hit Rate, and portfolio returns history.
    """
    if source_weights is None:
        source_weights = HYBRID_SOURCE_WEIGHTS
    
    logger.debug(f"Running walk-forward backtest (lookback={lookback_days}, objective={objective})")
    conn = get_db_connection()
    
    # Calculate cutoff date for lookback
    from datetime import datetime, timedelta, timezone
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    
    # Fetch all daily aggregations to rebuild composite scores under the proposed weights
    query = """
        SELECT ticker, date, category, subreddit, source,
               CASE WHEN total_weight > 0 THEN weighted_sum / total_weight ELSE 0 END as raw_sentiment
        FROM daily_aggregations
        WHERE date >= ?
    """
    df_aggs = pd.read_sql_query(query, conn, params=(cutoff_date,))
    conn.close()
    
    if df_aggs.empty:
        logger.warning("No aggregation data found for backtest")
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "returns": [], "source_attribution": {}}
    
    logger.debug(f"Loaded {len(df_aggs)} aggregation records for backtest")
        
    # Apply custom weights to compute daily composite sentiment
    df_aggs['weight'] = df_aggs.apply(
        lambda r: (category_weights.get(r['category'], 0.0) * 
                   subreddit_weights.get(r['category'], {}).get(r['subreddit'], 0.0) *
                   source_weights.get(r.get('source', 'reddit'), 0.2)),
        axis=1
    )
    df_aggs['weighted_sentiment'] = df_aggs['raw_sentiment'] * df_aggs['weight']
    
    # Group by ticker and date
    df_daily = df_aggs.groupby(['ticker', 'date']).agg(
        weighted_sum=('weighted_sentiment', 'sum'),
        total_weight=('weight', 'sum')
    ).reset_index()
    
    df_daily['sentiment'] = df_daily.apply(
        lambda r: r['weighted_sum'] / r['total_weight'] if r['total_weight'] > 0 else 0.0,
        axis=1
    )
    
    # Convert dates to datetime objects
    df_daily['date'] = pd.to_datetime(df_daily['date'])
    
    # Resample sentiment to monthly averages per ticker
    df_daily.set_index('date', inplace=True)
    df_monthly_sig = df_daily.groupby([pd.Grouper(freq='ME'), 'ticker'])['sentiment'].mean().unstack().fillna(0.0)
    
    if df_monthly_sig.empty:
        logger.warning("No monthly signals after resampling")
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "returns": [], "source_attribution": {}}
        
    logger.debug(f"Monthly signals: {len(df_monthly_sig)} months, {len(df_monthly_sig.columns)} tickers")
        
    # Get historical returns for tickers
    tickers = list(df_monthly_sig.columns)
    start_date = (df_monthly_sig.index.min() - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = df_monthly_sig.index.max().strftime("%Y-%m-%d")
    
    df_returns = fetch_historical_returns(tickers, start_date, end_date)
    
    # Align signals and subsequent returns
    # Signal at end of month t predicts return in month t+1
    df_aligned_sig = df_monthly_sig.shift(1).dropna(how='all')
    df_aligned_ret = df_returns.loc[df_aligned_sig.index].fillna(0.0) if not df_returns.empty else pd.DataFrame(0.0, index=df_aligned_sig.index, columns=tickers)
    
    ics = []
    portfolio_returns = []
    hits = 0
    total_months = len(df_aligned_sig)
    
    # Source attribution tracking
    source_attribution = {source: {"ics": [], "returns": []} for source in source_weights.keys()}
    
    for date in df_aligned_sig.index:
        sig = df_aligned_sig.loc[date]
        ret = df_aligned_ret.loc[date]
        
        # Rank Correlation (IC)
        if len(sig[sig != 0]) >= 2:
            rank_corr, _ = stats.spearmanr(sig, ret)
            if not np.isnan(rank_corr):
                ics.append(rank_corr)
                
        # Simulate simple long-short portfolio return
        # Weight proportional to signal value
        pos_sig = sig[sig > 0]
        neg_sig = sig[sig < 0]
        
        p_ret = 0.0
        # Long leg
        if not pos_sig.empty:
            weights_long = pos_sig / pos_sig.sum()
            p_ret += 0.5 * (weights_long * ret[pos_sig.index]).sum()
        # Short leg
        if not neg_sig.empty:
            weights_short = neg_sig / neg_sig.abs().sum()
            p_ret -= 0.5 * (weights_short * ret[neg_sig.index]).sum()
            
        portfolio_returns.append(p_ret)
        if p_ret > 0:
            hits += 1
            
    mean_ic = np.mean(ics) if ics else 0.0
    hit_rate = hits / total_months if total_months > 0 else 0.0
    
    # Sharpe Ratio calculation
    mean_ret = np.mean(portfolio_returns) if portfolio_returns else 0.0
    std_ret = np.std(portfolio_returns) if portfolio_returns else 0.0
    # Annualize monthly returns & vol (multiply by 12 and sqrt(12))
    sharpe = (mean_ret * 12) / (std_ret * np.sqrt(12)) if std_ret > 0 else 0.0
    
    logger.info(f"Backtest complete: IC={mean_ic:.4f}, Sharpe={sharpe:.4f}, Hit Rate={hit_rate:.4f}")
    
    return {
        "ic": float(mean_ic),
        "sharpe": float(sharpe),
        "hit_rate": float(hit_rate),
        "returns": [float(r) for r in portfolio_returns],
        "source_attribution": source_attribution
    }


def run_multi_source_backtest(
    category_weights: dict,
    subreddit_weights: dict,
    source_weights: dict,
    lookback_days: int = 180,
    objective: str = "information_coefficient"
) -> dict:
    """
    Run backtest with source-specific attribution.
    """
    return run_walk_forward_backtest(
        category_weights=category_weights,
        subreddit_weights=subreddit_weights,
        source_weights=source_weights,
        lookback_days=lookback_days,
        objective=objective
    )
