# Fix trailing stop with min_hold_days and cooldown

with open('Quantitative/exit_engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Update evaluate_exit_signal signature and add min_hold_days logic
old_func = '''def evaluate_exit_signal(
    ticker: str,
    current_price: float,
    dcf_result: Dict[str, Any],
    entry_signal: Optional[Dict[str, Any]] = None,
    high_watermark: Optional[float] = None,
    trailing_pct: float = 0.15,
) -> ExitSignal:
    """Evaluate complete exit signal for a position."""
    iv_fair = dcf_result.get("iv_p50", 0)  # Fair value = IV_P50
    iv_p25 = dcf_result.get("iv_p25", 0)
    
    warnings = []
    
    # Initialize high watermark
    if high_watermark is None or high_watermark <= 0:
        if entry_signal and "entry_price" in entry_signal:
            initial_hw = float(entry_signal["entry_price"])
        else:
            initial_hw = current_price
    else:
        initial_hw = high_watermark

    # Trailing Stop
    ts_triggered, ts_level, new_hw = evaluate_trailing_stop(current_price, initial_hw, trailing_pct)'''

new_func = '''def evaluate_exit_signal(
    ticker: str,
    current_price: float,
    dcf_result: Dict[str, Any],
    entry_signal: Optional[Dict[str, Any]] = None,
    high_watermark: Optional[float] = None,
    trailing_pct: float = 0.15,
    entry_date: Optional[Any] = None,  # Date of position entry
    current_date: Optional[Any] = None,  # Current evaluation date
    min_hold_days: int = 30,  # Minimum days before trailing stop can trigger
    trailing_cooldown_days: int = 60,  # Cooldown after trailing stop trigger
    last_trailing_trigger_date: Optional[Any] = None,  # Date of last trailing stop trigger
) -> ExitSignal:
    """Evaluate complete exit signal for a position."""
    iv_fair = dcf_result.get("iv_p50", 0)  # Fair value = IV_P50
    iv_p25 = dcf_result.get("iv_p25", 0)
    
    warnings = []
    
    # Initialize high watermark
    if high_watermark is None or high_watermark <= 0:
        if entry_signal and "entry_price" in entry_signal:
            initial_hw = float(entry_signal["entry_price"])
        else:
            initial_hw = current_price
    else:
        initial_hw = high_watermark

    # Check minimum hold period for trailing stop
    ts_eligible = True
    if entry_date is not None and current_date is not None:
        entry_dt = pd.Timestamp(entry_date)
        current_dt = pd.Timestamp(current_date)
        days_held = (current_dt - entry_dt).days
        if days_held < min_hold_days:
            ts_eligible = False
            warnings.append(f"Trailing stop not eligible: only {days_held} days held (min {min_hold_days})")
    
    # Check trailing stop cooldown
    if ts_eligible and last_trailing_trigger_date is not None and current_date is not None:
        last_dt = pd.Timestamp(last_trailing_trigger_date)
        current_dt = pd.Timestamp(current_date)
        days_since_last_ts = (current_dt - last_dt).days
        if days_since_last_ts < trailing_cooldown_days:
            ts_eligible = False
            warnings.append(f"Trailing stop in cooldown: {days_since_last_ts} days since last trigger (cooldown {trailing_cooldown_days})")

    # Initialize high watermark
    if high_watermark is None or high_watermark <= 0:
        if entry_signal and "entry_price" in entry_signal:
            initial_hw = float(entry_signal["entry_price"])
        else:
            initial_hw = current_price
    else:
        initial_hw = high_watermark

    # Trailing Stop (only if eligible)
    if ts_eligible:
        ts_triggered, ts_level, new_hw = evaluate_trailing_stop(current_price, initial_hw, trailing_pct)
    else:
        ts_triggered, ts_level, new_hw = False, 0.0, initial_hw'''

content = content.replace(old_func, new_func)

with open('Quantitative/exit_engine.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Added min_hold_days and cooldown to trailing stop')