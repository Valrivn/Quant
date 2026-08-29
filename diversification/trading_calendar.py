import pandas as pd
from datetime import datetime, time

# List of school break/no-school days for Grade 12 (2026-2027)
NO_SCHOOL_DAYS = {
    # 2026
    "2026-09-07", # Labor Day
    "2026-09-08", # Faculty Development
    "2026-09-28", # Faculty Development
    "2026-10-19", # Faculty Development
    "2026-11-09", # Faculty Development
    "2026-11-20", # Faculty Development
    "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27", # Thanksgiving Break
    "2026-11-30", # Faculty Development (Grade 9, 10, 12)
    # Winter Break: Dec 18, 2026 - Jan 1, 2027
    "2026-12-18", "2026-12-21", "2026-12-22", "2026-12-23", "2026-12-24", "2026-12-25",
    "2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31",
    # 2027
    "2027-01-01",
    "2027-01-04", # Faculty Development
    "2027-01-18", # MLK Day
    "2027-02-15", # Presidents Day
    "2027-02-16", # Faculty Development
    "2027-02-26", # Faculty Development
    "2027-03-01", # Faculty Development
    # Spring Break: March 15 - March 19
    "2027-03-15", "2027-03-16", "2027-03-17", "2027-03-18", "2027-03-19",
    "2027-03-22", # Faculty Development
    "2027-04-02", # Faculty Development
}

# Half Days (August 3 to August 7, 2026)
HALF_DAYS = {
    "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"
}

# Graduation / Last Class Day for Grade 12
GRADUATION_DATE = "2027-05-21"

def is_trading_allowed(dt: datetime) -> bool:
    """
    Returns True if trading/rebalancing is permitted according to the Grade 12 schedule:
      - Weekends: Market is closed, but prep is allowed.
      - Wednesdays: Allowed after 1:30 PM (13:30).
      - Daily: 1:00 PM to 1:30 PM (13:00 to 13:30) only for extreme steals.
      - No-School Days: Fully allowed.
      - Half-Days: Allowed after 12:30 PM.
      - Post-Graduation: Fully allowed.
    """
    date_str = dt.strftime("%Y-%m-%d")
    
    # 1. Post graduation check
    if date_str >= GRADUATION_DATE:
        return True
        
    # 2. Weekends
    if dt.weekday() in (5, 6):
        return False
        
    # 3. No school days / Holidays
    if date_str in NO_SCHOOL_DAYS:
        return True
        
    t = dt.time()
    
    # 4. Half days
    if date_str in HALF_DAYS:
        return t >= time(12, 30)
        
    # 5. Regular Wednesday window (after 1:30 PM)
    if dt.weekday() == 2: # Wednesday
        return t >= time(13, 30)
        
    # 6. Default daily micro window (1:00 PM - 1:30 PM) for emergency/absolute steals
    # (Disabled by default to avoid taking up non-flexible class time)
    return False

def get_allowed_trading_dates(all_dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Filter a DatetimeIndex to only keep dates where trading is permitted."""
    allowed = []
    for d in all_dates:
        # For daily close data, treat it as PM / end of day
        # E.g. we evaluate the close at 4 PM, so we check if trading was allowed that afternoon.
        dt_val = datetime(d.year, d.month, d.day, 16, 0)
        if is_trading_allowed(dt_val):
            allowed.append(d)
    return pd.DatetimeIndex(allowed)
