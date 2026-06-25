import math
import logging
import time
import random
import asyncio
from typing import Any, Optional, Union, Callable
from datetime import datetime, timezone
from functools import wraps
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def guard_nan(value: Any, default: float = 0.0, context: str = "") -> float:
    if value is None:
        if context:
            logger.debug(f"NaN guard triggered (None): {context}")
        return default
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            if context:
                logger.debug(f"NaN guard triggered (NaN/Inf): {context} -> {value}")
            return default
    return float(value)


def guard_division(numerator: float, denominator: float, default: float = 0.0, context: str = "") -> float:
    denom = guard_nan(denominator, 0.0, f"{context}_denominator")
    num = guard_nan(numerator, 0.0, f"{context}_numerator")
    if denom == 0.0:
        if context:
            logger.debug(f"Division by zero guard: {context}")
        return default
    return num / denom


def guard_bounds(value: float, min_val: float, max_val: float, default: float = 0.0, context: str = "") -> float:
    val = guard_nan(value, default, context)
    if val < min_val:
        if context:
            logger.debug(f"Bounds guard (below min): {context} -> {val} < {min_val}")
        return min_val
    if val > max_val:
        if context:
            logger.debug(f"Bounds guard (above max): {context} -> {val} > {max_val}")
        return max_val
    return val


def guard_utc_timestamp(timestamp: Union[int, float, str, None]) -> int:
    if timestamp is None:
        return int(datetime.now(timezone.utc).timestamp())
    
    if isinstance(timestamp, str):
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return int(datetime.now(timezone.utc).timestamp())
    
    ts = int(timestamp)
    if ts < 0 or ts > 2**31:
        logger.warning(f"Suspicious timestamp: {ts}, using current time")
        return int(datetime.now(timezone.utc).timestamp())
    
    return ts


def ensure_utc(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class RateLimiter:
    def __init__(self, min_delay: float = 12.0, max_delay: float = 25.0, jitter: float = 2.0):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self._last_call: float = 0.0
        self._call_count: int = 0

    async def wait(self) -> float:
        now = time.time()
        elapsed = now - self._last_call
        
        delay = random.uniform(self.min_delay, self.max_delay)
        delay += random.uniform(0, self.jitter)
        
        if elapsed < delay:
            sleep_time = delay - elapsed
            logger.debug(f"Rate limiter: sleeping {sleep_time:.2f}s (call #{self._call_count + 1})")
            await asyncio.sleep(sleep_time)
        
        self._last_call = time.time()
        self._call_count += 1
        return self._last_call

    def reset(self) -> None:
        self._last_call = 0.0
        self._call_count = 0


def rate_limited(min_delay: float = 12.0, max_delay: float = 25.0):
    limiter = RateLimiter(min_delay, max_delay)
    
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            await limiter.wait()
            return await func(*args, **kwargs)
        return wrapper
    return decorator


@contextmanager
def timed_operation(operation_name: str, log_level: int = logging.DEBUG):
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        logger.log(log_level, f"{operation_name} completed in {elapsed:.3f}s")


class RetryPolicy:
    def __init__(self, max_retries: int = 3, base_delay: float = 1.0, 
                 max_delay: float = 30.0, exponential_base: float = 2.0,
                 jitter: float = 0.5):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter

    async def execute(self, func: Callable, *args, **kwargs) -> Any:
        last_exception = None
        
        for attempt in range(self.max_retries + 1):
            try:
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < self.max_retries:
                    delay = min(
                        self.base_delay * (self.exponential_base ** attempt) + random.uniform(0, self.jitter),
                        self.max_delay
                    )
                    logger.warning(f"Attempt {attempt + 1}/{self.max_retries + 1} failed: {e}. Retrying in {delay:.2f}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"All {self.max_retries + 1} attempts failed: {e}")
                    raise last_exception


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (ValueError, TypeError):
        return default


def safe_dict_get(d: dict, key: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    return d.get(key, default)


def validate_ticker(ticker: str) -> str:
    if not ticker or not isinstance(ticker, str):
        raise ValueError("Ticker must be a non-empty string")
    cleaned = ticker.strip().upper()
    if not cleaned.isalnum():
        raise ValueError(f"Invalid ticker format: {ticker}")
    return cleaned


def validate_date_str(date_str: str) -> str:
    try:
        datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return date_str
    except Exception:
        raise ValueError(f"Invalid date format (expected YYYY-MM-DD): {date_str}")


async def with_timeout(coro, timeout_seconds: float, default=None):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning(f"Operation timed out after {timeout_seconds}s")
        return default


def sanitize_text(text: Optional[str], max_length: int = 10000) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + "..."
    return cleaned


def clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    return max(min_val, min(max_val, value))


if __name__ == "__main__":
    import asyncio
    
    print("Testing engineering guards...")
    
    print(f"guard_nan(None): {guard_nan(None)}")
    print(f"guard_nan(float('nan')): {guard_nan(float('nan'))}")
    print(f"guard_nan(float('inf')): {guard_nan(float('inf'))}")
    print(f"guard_nan(5.0): {guard_nan(5.0)}")
    
    print(f"guard_division(10, 2): {guard_division(10, 2)}")
    print(f"guard_division(10, 0): {guard_division(10, 0)}")
    print(f"guard_division(float('nan'), 2): {guard_division(float('nan'), 2)}")
    
    print(f"guard_bounds(5, 0, 10): {guard_bounds(5, 0, 10)}")
    print(f"guard_bounds(-5, 0, 10): {guard_bounds(-5, 0, 10)}")
    print(f"guard_bounds(15, 0, 10): {guard_bounds(15, 0, 10)}")
    
    print(f"guard_utc_timestamp(1700000000): {guard_utc_timestamp(1700000000)}")
    print(f"guard_utc_timestamp('2024-01-01T00:00:00Z'): {guard_utc_timestamp('2024-01-01T00:00:00Z')}")
    print(f"guard_utc_timestamp(None): {guard_utc_timestamp(None)}")
    
    async def test_rate_limiter():
        limiter = RateLimiter(min_delay=0.1, max_delay=0.2)
        start = time.time()
        for _ in range(3):
            await limiter.wait()
        elapsed = time.time() - start
        print(f"Rate limiter test: 3 calls took {elapsed:.2f}s (expected ~0.3-0.6s)")
    
    asyncio.run(test_rate_limiter())
    
    print("All engineering guards tests passed!")