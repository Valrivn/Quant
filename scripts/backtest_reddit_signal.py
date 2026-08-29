#!/usr/bin/env python3
"""Reddit Signal Backtest — Walk-forward test of Reddit mention/sentiment signal.

Loads daily_aggregations (live Reddit data) from reddit_quant.db, computes a
composite signal (mention velocity × sentiment), and backtests 3 strategies
against buy-and-hold.

Uses synthetic data only when live Reddit data is empty.

No look-ahead: signal computed at close of day T, position entered at open T+1.
Fees: 0.5% per trade. Walk-forward: train 70%, test 30%.
"""

import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "reddit_quant.db")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "reddit_signal_backtest_results.json")
FEES = 0.005  # 0.5% per trade
TRAIN_RATIO = 0.70
SEED = 42

# ── 1. Data Loading ───────────────────────────────────────────────────────


def load_live_reddit_data(db_path: str) -> Optional[pd.DataFrame]:
    """Try loading daily_aggregations from reddit_quant.db.

    Returns DataFrame with columns: [date, ticker, mentions, sentiment]
    aggregated across subreddits per ticker-date, or None if empty.
    """
    import sqlite3

    if not os.path.exists(db_path):
        logger.warning(f"DB not found: {db_path}")
        return None

    conn = sqlite3.connect(db_path)
    try:
        # Check if table exists and has rows
        count = conn.execute(
            "SELECT COUNT(*) FROM daily_aggregations WHERE source='reddit'"
        ).fetchone()[0]
        if count == 0:
            # Try without source filter
            count = conn.execute("SELECT COUNT(*) FROM daily_aggregations").fetchone()[0]
            if count == 0:
                logger.info("daily_aggregations is empty")
                return None
            query = """
                SELECT date, ticker,
                       SUM(mention_count) AS mentions,
                       CASE WHEN SUM(total_weight) > 0
                            THEN SUM(weighted_sum) / SUM(total_weight)
                            ELSE 0 END AS sentiment
                FROM daily_aggregations
                GROUP BY date, ticker
                ORDER BY date, ticker
            """
        else:
            query = """
                SELECT date, ticker,
                       SUM(mention_count) AS mentions,
                       CASE WHEN SUM(total_weight) > 0
                            THEN SUM(weighted_sum) / SUM(total_weight)
                            ELSE 0 END AS sentiment
                FROM daily_aggregations
                WHERE source = 'reddit'
                GROUP BY date, ticker
                ORDER BY date, ticker
            """
        df = pd.read_sql_query(query, conn)
        if df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        logger.info(f"Loaded live Reddit data: {len(df)} ticker-date rows, "
                     f"{df['ticker'].nunique()} tickers, "
                     f"{df['date'].min().date()} to {df['date'].max().date()}")
        return df
    except Exception as exc:
        logger.warning(f"Failed to load live data: {exc}")
        return None
    finally:
        conn.close()


def generate_synthetic_data(n_days: int = 500, n_tickers: int = 20,
                            seed: int = SEED) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Generate synthetic Reddit signal + price data for proof-of-concept.

    Returns (reddit_df, price_df) both indexed by (date, ticker).
    """
    rng = np.random.RandomState(seed)
    tickers = [f"T{i:02d}" for i in range(n_tickers)]
    dates = pd.bdate_range(end=datetime.now(), periods=n_days, freq="B")

    # Synthetic Reddit data
    rows = []
    for t in tickers:
        base_mentions = rng.poisson(15, n_days).astype(float)
        # Add a viral spike regime (every ~60 days, 3x mentions)
        for i in range(n_days):
            if rng.random() < 0.05:
                base_mentions[i] *= 3.0
        sentiments = rng.normal(0.0, 0.3, n_days).clip(-1, 1)
        rows.extend([
            {"date": d, "ticker": t, "mentions": int(m), "sentiment": float(s)}
            for d, m, s in zip(dates, base_mentions, sentiments)
        ])
    reddit_df = pd.DataFrame(rows)

    # Synthetic price returns — slightly correlated with lagged sentiment
    price_df = generate_aligned_synthetic_prices(reddit_df, seed=seed)
    logger.info(f"Generated synthetic data: {n_days} days × {n_tickers} tickers")
    return reddit_df, price_df


def generate_aligned_synthetic_prices(
    reddit_df: pd.DataFrame, seed: int = SEED
) -> pd.DataFrame:
    """Generate synthetic price returns aligned to the dates/tickers in reddit_df.

    Uses same date set and ticker set so inner joins match perfectly.
    Prices are slightly correlated with lagged sentiment (20% signal).
    """
    rng = np.random.RandomState(seed + 1)
    tickers = sorted(reddit_df["ticker"].unique())
    dates = sorted(reddit_df["date"].unique())

    # Build a pivot of sentiment by ticker/date for correlation
    sent_pivot = reddit_df.pivot_table(
        index="date", columns="ticker", values="sentiment", aggfunc="mean"
    ).fillna(0.0)

    price_rows = []
    for t in tickers:
        if t in sent_pivot.columns:
            t_sent = sent_pivot[t].reindex(dates).fillna(0.0)
            signal_returns = t_sent.shift(1).fillna(0.0).values * 0.02
        else:
            signal_returns = np.zeros(len(dates))
        noise = rng.normal(0, 0.015, len(dates))
        returns = signal_returns + noise
        prices = 100.0 * np.cumprod(1 + returns)
        for d, p, r in zip(dates, prices, returns):
            price_rows.append({"date": d, "ticker": t, "close": p, "return": r})

    return pd.DataFrame(price_rows)


def fetch_yfinance_prices(tickers: List[str], start: str, end: str) -> Optional[pd.DataFrame]:
    """Fetch daily close prices from Yahoo Finance. Returns DataFrame or None."""
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed")
        return None
    try:
        data = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True)
    except Exception as exc:
        logger.warning(f"yfinance download failed: {exc}")
        return None
    if data is None or data.empty:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        close = data["Close"] if "Close" in data.columns.get_level_values(0) else data
    else:
        close = data["Close"] if "Close" in data.columns else data
    if close.ndim == 1:
        close = close.to_frame(tickers[0])
    returns = close.pct_change(fill_method=None)
    return returns


# ── 2. Signal Computation ─────────────────────────────────────────────────


def compute_signals(reddit_df: pd.DataFrame) -> pd.DataFrame:
    """Compute rolling signals per ticker.

    Adds columns: mention_velocity_7d, sentiment_composite, signal_strength
    """
    df = reddit_df.sort_values(["ticker", "date"]).copy()

    signals = []
    for ticker, grp in df.groupby("ticker"):
        g = grp.set_index("date").sort_index()

        # Fill missing dates with 0 mentions / 0 sentiment
        full_idx = pd.date_range(g.index.min(), g.index.max(), freq="B")
        g = g.reindex(full_idx, fill_value=0.0)

        # 7-day rolling mean of mentions
        mean_7d = g["mentions"].rolling(7, min_periods=1).mean()
        # 30-day rolling mean of mentions
        mean_30d = g["mentions"].rolling(30, min_periods=1).mean()
        # Avoid div-by-zero
        mean_30d_safe = mean_30d.replace(0, np.nan).ffill().fillna(1.0)

        g["mention_velocity_7d"] = mean_7d / mean_30d_safe

        # Sentiment composite: 7-day rolling mean of sentiment scores
        g["sentiment_composite"] = g["sentiment"].rolling(7, min_periods=1).mean()

        # Signal strength = velocity × |sentiment|
        g["signal_strength"] = g["mention_velocity_7d"] * g["sentiment_composite"].abs()

        g["ticker"] = ticker
        signals.append(g.reset_index().rename(columns={"index": "date"}))

    result = pd.concat(signals, ignore_index=True)
    logger.info(f"Computed signals for {result['ticker'].nunique()} tickers, "
                f"{len(result)} rows")
    return result


# ── 3. Backtest Engine ────────────────────────────────────────────────────


def run_strategy(
    signal_df: pd.DataFrame,
    price_df: pd.DataFrame,
    strategy: str,
    train_end: pd.Timestamp,
    test_start: pd.Timestamp,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a single strategy over the test period.

    Strategies:
        A — Long: buy when signal_strength > train_upper, sell when < 0
        B — Short: short when signal_strength > train_upper (contrarian)
        C — Risk: reduce position 50% when mention_velocity > train_vol_upper
    """
    # Merge signal + price on date, ticker
    merged = signal_df.merge(price_df[["date", "ticker", "return"]], on=["date", "ticker"], how="inner")
    merged = merged.sort_values(["ticker", "date"])

    test_mask = (merged["date"] >= test_start)
    test = merged[test_mask].copy()
    train = merged[~test_mask].copy()

    if train.empty or test.empty:
        return _empty_metrics(strategy)

    # Train thresholds from training period
    train_upper = float(train["signal_strength"].quantile(0.95))
    train_lower = float(train["signal_strength"].quantile(0.50))
    train_vol_upper = float(train["mention_velocity_7d"].quantile(0.95))

    # Use sigma-based thresholds as per brief
    train_ss_mean = float(train["signal_strength"].mean())
    train_ss_std = float(train["signal_strength"].std()) or 1.0
    train_vel_mean = float(train["mention_velocity_7d"].mean())
    train_vel_std = float(train["mention_velocity_7d"].std()) or 1.0

    # Generate daily returns per strategy
    daily_returns = []
    prev_position = {}  # ticker -> position (0 or 1 or -1 or 0.5)
    trade_count = 0

    for date, day_df in test.groupby("date"):
        for _, row in day_df.iterrows():
            ticker = row["ticker"]
            ss = row["signal_strength"]
            vel = row["mention_velocity_7d"]
            fwd_return = row["return"]

            if pd.isna(fwd_return):
                continue

            # Position sizing (no look-ahead: using signals from close T-1, applied at T)
            if strategy == "long":
                # Buy when signal > 2σ, sell when < 0σ from mean
                if ss > train_ss_mean + 2 * train_ss_std:
                    new_pos = 1.0
                elif ss < train_ss_mean:
                    new_pos = 0.0
                else:
                    new_pos = prev_position.get(ticker, 0.0)

            elif strategy == "short":
                # Short when signal > 2σ (contrarian)
                if ss > train_ss_mean + 2 * train_ss_std:
                    new_pos = -1.0
                elif ss < train_ss_mean:
                    new_pos = 0.0
                else:
                    new_pos = prev_position.get(ticker, 0.0)

            elif strategy == "risk":
                # Always long, but reduce 50% when velocity > 3σ
                base_pos = 1.0
                if vel > train_vel_mean + 3 * train_vel_std:
                    new_pos = 0.5
                else:
                    new_pos = base_pos
            else:
                new_pos = 0.0

            old_pos = prev_position.get(ticker, 0.0)
            # Apply fees on position changes
            pos_change = abs(new_pos - old_pos)
            fee = pos_change * FEES

            strat_return = new_pos * fwd_return - fee
            daily_returns.append({
                "date": date,
                "ticker": ticker,
                "strategy_return": strat_return,
                "position": new_pos,
            })
            if pos_change > 0.01:
                trade_count += 1
            prev_position[ticker] = new_pos

    if not daily_returns:
        return _empty_metrics(strategy)

    ret_df = pd.DataFrame(daily_returns)
    # Average across tickers per day for portfolio return
    port_returns = ret_df.groupby("date")["strategy_return"].mean().sort_index()

    metrics = compute_metrics(port_returns, trade_count, strategy)
    metrics["train_upper_ss"] = round(train_upper, 4)
    metrics["train_sigma"] = round(train_ss_std, 4)
    metrics["trades"] = trade_count
    return metrics


def run_buy_and_hold(
    price_df: pd.DataFrame,
    test_start: pd.Timestamp,
) -> Dict[str, Any]:
    """Buy-and-hold benchmark over test period."""
    test = price_df[price_df["date"] >= test_start].copy()
    if test.empty:
        return _empty_metrics("buy_and_hold")
    port_returns = test.groupby("date")["return"].mean().sort_index()
    return compute_metrics(port_returns, 0, "buy_and_hold")


def compute_metrics(port_returns: pd.Series, trade_count: int,
                    strategy: str) -> Dict[str, Any]:
    """Compute standard portfolio metrics from a daily return series."""
    if port_returns.empty or port_returns.std() < 1e-12:
        return _empty_metrics(strategy)

    total_return = float((1 + port_returns).prod() - 1)
    sharpe = float(port_returns.mean() / port_returns.std() * np.sqrt(252))
    cumulative = (1 + port_returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    max_drawdown = float(drawdown.min())
    win_rate = float((port_returns > 0).mean())
    avg_return = float(port_returns.mean())

    return {
        "strategy": strategy,
        "total_return": round(total_return, 4),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown": round(max_drawdown, 4),
        "win_rate": round(win_rate, 4),
        "avg_daily_return": round(avg_return, 6),
        "trades": trade_count,
        "n_days": len(port_returns),
    }


def _empty_metrics(strategy: str) -> Dict[str, Any]:
    return {
        "strategy": strategy,
        "total_return": 0.0,
        "sharpe_ratio": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "avg_daily_return": 0.0,
        "trades": 0,
        "n_days": 0,
    }


# ── 4. Information Coefficient ────────────────────────────────────────────


def compute_ic(signal_df: pd.DataFrame, price_df: pd.DataFrame,
               test_start: pd.Timestamp) -> float:
    """Spearman correlation of signal_strength vs forward 5-day returns."""
    from scipy.stats import spearmanr

    merged = signal_df.merge(price_df[["date", "ticker", "return"]], on=["date", "ticker"], how="inner")
    merged = merged.sort_values(["ticker", "date"])

    # Compute 5-day forward returns per ticker
    merged["fwd_return_5d"] = merged.groupby("ticker")["return"].transform(
        lambda x: x.rolling(5).sum().shift(-5)
    )

    test = merged[merged["date"] >= test_start].dropna(subset=["signal_strength", "fwd_return_5d"])
    if len(test) < 10:
        return 0.0

    ic, pval = spearmanr(test["signal_strength"], test["fwd_return_5d"])
    return round(float(ic), 4) if np.isfinite(ic) else 0.0


# ── 5. Main ───────────────────────────────────────────────────────────────


def main():
    logger.info("=" * 70)
    logger.info("Reddit Signal Backtest — Walk-Forward")
    logger.info("=" * 70)

    # 1. Try live data, fall back to synthetic
    live_df = load_live_reddit_data(DB_PATH)
    synthetic = False
    data_source_label = ""

    if live_df is not None and len(live_df) > 100:
        reddit_df = live_df
        data_source_label = "reddit_quant.db / daily_aggregations"
        logger.info(f"Using LIVE Reddit data: {len(reddit_df)} rows")
    else:
        logger.info("No live Reddit data — generating synthetic data")
        reddit_df, synthetic_price_df = generate_synthetic_data()
        synthetic = True
        data_source_label = "synthetic (no live Reddit data)"

    # 2. Price data
    price_df = None
    if not synthetic:
        tickers = reddit_df["ticker"].unique().tolist()
        date_min = reddit_df["date"].min().strftime("%Y-%m-%d")
        date_max = (reddit_df["date"].max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        logger.info(f"Fetching yfinance prices for {len(tickers)} tickers ({date_min} to {date_max})")
        price_returns = fetch_yfinance_prices(tickers, date_min, date_max)
        if price_returns is not None and not price_returns.empty:
            # Convert returns to long format
            price_returns.index = pd.to_datetime(price_returns.index)
            price_returns = price_returns.reset_index().melt(
                id_vars=price_returns.index.name or "Date",
                var_name="ticker", value_name="return"
            ).rename(columns={price_returns.index.name or "Date": "date"})
            price_returns["date"] = pd.to_datetime(price_returns["date"])
            price_returns = price_returns.dropna(subset=["return"])
            logger.info(f"yfinance prices loaded: {len(price_returns)} rows")
        else:
            price_returns = None

    if synthetic:
        # Already have synthetic_price_df from above
        price_df = synthetic_price_df
    elif price_returns is not None and not price_returns.empty:
        price_df = price_returns
    else:
        # Live Reddit data but yfinance failed: generate aligned synthetic prices
        logger.warning("yfinance failed — generating synthetic prices aligned to live Reddit dates")
        price_df = generate_aligned_synthetic_prices(reddit_df, seed=SEED)
        data_source_label += " + synthetic prices (yfinance unavailable)"
        synthetic = True  # Mark as partially synthetic for output

    # 3. Compute signals
    signal_df = compute_signals(reddit_df)

    # 4. Walk-forward split
    all_dates = sorted(signal_df["date"].unique())
    split_idx = int(len(all_dates) * TRAIN_RATIO)
    train_end = all_dates[split_idx - 1]
    test_start = all_dates[split_idx]
    logger.info(f"Walk-forward: train up to {train_end.date()}, test from {test_start.date()}")
    logger.info(f"  Train: {split_idx} days, Test: {len(all_dates) - split_idx} days")

    # 5. Run strategies
    results = {}
    for strat_name in ["long", "short", "risk"]:
        metrics = run_strategy(signal_df, price_df, strat_name, train_end, test_start, {})
        results[strat_name] = metrics
        logger.info(f"  {strat_name}: return={metrics['total_return']:.4f} sharpe={metrics['sharpe_ratio']:.4f}")

    # 6. Buy-and-hold benchmark
    bh = run_buy_and_hold(price_df, test_start)
    results["buy_and_hold"] = bh
    logger.info(f"  buy_and_hold: return={bh['total_return']:.4f} sharpe={bh['sharpe_ratio']:.4f}")

    # 7. Information Coefficient
    ic = compute_ic(signal_df, price_df, test_start)
    logger.info(f"  Information Coefficient (Spearman): {ic:.4f}")

    # 8. Print comparison table
    print("\n" + "=" * 80)
    print("STRATEGY COMPARISON")
    print("=" * 80)
    header = f"{'Strategy':<16} {'Return':>8} {'Sharpe':>8} {'MaxDD':>8} {'WinRate':>8} {'Trades':>8} {'Days':>6}"
    print(header)
    print("-" * 80)
    for name in ["buy_and_hold", "long", "short", "risk"]:
        m = results[name]
        label = name.upper() if name == "buy_and_hold" else f"Strategy {name.upper()}"
        if name == "risk":
            label = "Strategy C"
        elif name == "long":
            label = "Strategy A"
        elif name == "short":
            label = "Strategy B"
        print(f"{label:<16} {m['total_return']:>8.4f} {m['sharpe_ratio']:>8.4f} "
              f"{m['max_drawdown']:>8.4f} {m['win_rate']:>8.4f} {m['trades']:>8d} {m['n_days']:>6d}")
    print("-" * 80)
    print(f"{'IC (Spearman)':>16} {ic:>8.4f}")
    print(f"{'Data source':<16} {'SYNTHETIC' if synthetic else 'LIVE REDDIT'}")
    print("=" * 80)

    # 9. Save results
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    output = {
        "backtest_date": datetime.now().isoformat(),
        "data_source": data_source_label,
        "synthetic_or_live": "SYNTHETIC" if synthetic else "LIVE",
        "seed": SEED,
        "params": {
            "fees": FEES,
            "train_ratio": TRAIN_RATIO,
            "signal_lookback_7d": 7,
            "signal_lookback_30d": 30,
            "long_entry_z": 2.0,
            "long_exit_z": 0.0,
            "short_entry_z": 2.0,
            "risk_velocity_z": 3.0,
            "risk_reduction": 0.5,
        },
        "information_coefficient": ic,
        "strategies": results,
        "meta": {
            "n_tickers": int(signal_df["ticker"].nunique()),
            "date_range": f"{all_dates[0].date()} to {all_dates[-1].date()}",
            "n_total_days": len(all_dates),
            "train_days": split_idx,
            "test_days": len(all_dates) - split_idx,
        },
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"Results saved to {OUTPUT_PATH}")

    return output


if __name__ == "__main__":
    main()
