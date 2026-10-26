# Fix load_spy function in walkforward_backtest.py

with open('walkforward_backtest.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix load_spy function
old_load_spy = '''def load_spy():
    path = Path("data/master_prices/SPY.parquet")
    if path.exists():
        df = pd.read_parquet(path)
        if "date" in df.columns and "close" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")["close"].sort_index()
            return df
    return None'''

new_load_spy = '''def load_spy():
    path = Path("data/master_prices/SPY.parquet")
    if path.exists():
        df = pd.read_parquet(path)
        # Handle both formats: date as column or index
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")["close"].sort_index()
        elif df.index.name == "Date" or df.index.name == "date":
            df = df["Close"].sort_index() if "Close" in df.columns else df["close"].sort_index()
            df.index = pd.to_datetime(df.index)
        return df
    return None'''

content = content.replace(old_load_spy, new_load_spy)

# Also fix load_prices function
old_load_prices = '''def load_prices(tickers: List[str]) -> Dict[str, pd.Series]:
    prices = {}
    for t in tickers:
        path = Path(f"data/master_prices/{t}.parquet")
        if path.exists():
            df = pd.read_parquet(path)
            if "date" in df.columns and "close" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")["close"].sort_index()
                prices[t] = df
    return prices'''

new_load_prices = '''def load_prices(tickers: List[str]) -> Dict[str, pd.Series]:
    prices = {}
    for t in tickers:
        path = Path(f"data/master_prices/{t}.parquet")
        if path.exists():
            df = pd.read_parquet(path)
            # Handle both formats
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")["close"].sort_index()
            elif df.index.name in ("Date", "date"):
                col = "Close" if "Close" in df.columns else "close"
                df = df[col].sort_index()
                df.index = pd.to_datetime(df.index)
            else:
                continue
            prices[t] = df
    return prices'''

content = content.replace(old_load_prices, new_load_prices)

with open('walkforward_backtest.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Fixed load_spy and load_prices functions')