import pytest
import asyncio
import time
from datetime import datetime, timezone
from psychological.engineering_guards import (
    guard_nan, guard_division, guard_bounds, guard_utc_timestamp, ensure_utc,
    RateLimiter, rate_limited, timed_operation, RetryPolicy,
    safe_float, safe_int, safe_dict_get, validate_ticker, validate_date_str,
    with_timeout, sanitize_text, clamp
)


class TestGuardNan:
    def test_guard_nan_none(self):
        assert guard_nan(None) == 0.0
        assert guard_nan(None, default=5.0) == 5.0

    def test_guard_nan_nan(self):
        assert guard_nan(float('nan')) == 0.0

    def test_guard_nan_inf(self):
        assert guard_nan(float('inf')) == 0.0
        assert guard_nan(float('-inf')) == 0.0

    def test_guard_nan_valid(self):
        assert guard_nan(5.0) == 5.0
        assert guard_nan(0.0) == 0.0
        assert guard_nan(-3.14) == -3.14

    def test_guard_nan_int(self):
        assert guard_nan(5) == 5.0


class TestGuardDivision:
    def test_guard_division_normal(self):
        assert guard_division(10, 2) == 5.0
        assert guard_division(10.0, 4.0) == 2.5

    def test_guard_division_by_zero(self):
        assert guard_division(10, 0) == 0.0
        assert guard_division(10, 0, default=999) == 999

    def test_guard_division_nan_numerator(self):
        assert guard_division(float('nan'), 2) == 0.0

    def test_guard_division_nan_denominator(self):
        assert guard_division(10, float('nan')) == 0.0


class TestGuardBounds:
    def test_guard_bounds_within(self):
        assert guard_bounds(5, 0, 10) == 5
        assert guard_bounds(0, 0, 10) == 0
        assert guard_bounds(10, 0, 10) == 10

    def test_guard_bounds_below_min(self):
        assert guard_bounds(-5, 0, 10) == 0
        assert guard_bounds(-1, 5, 10) == 5

    def test_guard_bounds_above_max(self):
        assert guard_bounds(15, 0, 10) == 10
        assert guard_bounds(100, 0, 50) == 50

    def test_guard_bounds_nan(self):
        assert guard_bounds(float('nan'), 0, 10) == 0


class TestGuardUtcTimestamp:
    def test_guard_utc_timestamp_int(self):
        ts = guard_utc_timestamp(1700000000)
        assert ts == 1700000000

    def test_guard_utc_timestamp_float(self):
        ts = guard_utc_timestamp(1700000000.5)
        assert ts == 1700000000

    def test_guard_utc_timestamp_str_iso(self):
        ts = guard_utc_timestamp('2024-01-01T00:00:00Z')
        expected = int(datetime.fromisoformat('2024-01-01T00:00:00+00:00').timestamp())
        assert ts == expected

    def test_guard_utc_timestamp_str_no_z(self):
        ts = guard_utc_timestamp('2024-01-01T00:00:00')
        expected = int(datetime.fromisoformat('2024-01-01T00:00:00').replace(tzinfo=timezone.utc).timestamp())
        assert ts == expected

    def test_guard_utc_timestamp_none(self):
        ts = guard_utc_timestamp(None)
        expected = int(datetime.now(timezone.utc).timestamp())
        assert abs(ts - expected) < 5


class TestEnsureUtc:
    def test_ensure_utc_none(self):
        dt = ensure_utc(None)
        assert dt.tzinfo == timezone.utc

    def test_ensure_utc_naive(self):
        dt = ensure_utc(datetime(2024, 1, 1, 12, 0, 0))
        assert dt.tzinfo == timezone.utc

    def test_ensure_utc_aware(self):
        dt = ensure_utc(datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        assert dt.tzinfo == timezone.utc


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_rate_limiter_wait(self):
        limiter = RateLimiter(min_delay=0.01, max_delay=0.02, jitter=0.0)
        start = time.time()
        
        await limiter.wait()
        await limiter.wait()
        
        elapsed = time.time() - start
        assert elapsed >= 0.01  # at least 1 * 0.01 minus some timing variance

    @pytest.mark.asyncio
    async def test_rate_limiter_first_call_no_wait(self):
        limiter = RateLimiter(min_delay=0.1, max_delay=0.2)
        start = time.time()
        
        await limiter.wait()
        
        elapsed = time.time() - start
        assert elapsed < 0.05  # should be almost instant

    def test_rate_limiter_reset(self):
        limiter = RateLimiter(min_delay=0.1, max_delay=0.2)
        limiter._last_call = time.time()
        limiter._call_count = 5
        
        limiter.reset()
        
        assert limiter._last_call == 0.0
        assert limiter._call_count == 0


class TestRateLimited:
    @pytest.mark.asyncio
    async def test_rate_limited_decorator(self):
        call_times = []
        
        @rate_limited(min_delay=0.01, max_delay=0.02)
        async def test_func():
            call_times.append(time.time())
            return "ok"
        
        await test_func()
        await test_func()
        
        assert len(call_times) == 2
        assert call_times[1] - call_times[0] >= 0.01


class TestTimedOperation:
    def test_timed_operation(self):
        with timed_operation("test_op") as _:
            time.sleep(0.01)
        
        # Just verify it doesn't raise


class TestRetryPolicy:
    @pytest.mark.asyncio
    async def test_retry_policy_success(self):
        policy = RetryPolicy(max_retries=3, base_delay=0.01)
        
        async def success_func():
            return "success"
        
        result = await policy.execute(success_func)
        assert result == "success"

    @pytest.mark.asyncio
    async def test_retry_policy_retry_then_success(self):
        policy = RetryPolicy(max_retries=3, base_delay=0.01)
        attempts = []
        
        async def flaky_func():
            attempts.append(1)
            if len(attempts) < 2:
                raise ValueError("Fail")
            return "success"
        
        result = await policy.execute(flaky_func)
        assert result == "success"
        assert len(attempts) == 2

    @pytest.mark.asyncio
    async def test_retry_policy_all_fail(self):
        policy = RetryPolicy(max_retries=2, base_delay=0.01)
        
        async def always_fail():
            raise ValueError("Fail")
        
        with pytest.raises(ValueError, match="Fail"):
            await policy.execute(always_fail)


class TestSafeFloat:
    def test_safe_float_valid(self):
        assert safe_float(5.0) == 5.0
        assert safe_float("3.14") == 3.14
        assert safe_float(42) == 42.0

    def test_safe_float_none(self):
        assert safe_float(None) == 0.0
        assert safe_float(None, default=99.9) == 99.9

    def test_safe_float_invalid(self):
        assert safe_float("abc") == 0.0
        assert safe_float([1, 2]) == 0.0


class TestSafeInt:
    def test_safe_int_valid(self):
        assert safe_int(5) == 5
        assert safe_int("42") == 42
        assert safe_int(3.7) == 3
        assert safe_int("3.7") == 3

    def test_safe_int_none(self):
        assert safe_int(None) == 0
        assert safe_int(None, default=99) == 99

    def test_safe_int_invalid(self):
        assert safe_int("abc") == 0
        assert safe_int([1, 2]) == 0


class TestSafeDictGet:
    def test_safe_dict_get_exists(self):
        d = {"a": 1, "b": 2}
        assert safe_dict_get(d, "a") == 1
        assert safe_dict_get(d, "b") == 2

    def test_safe_dict_get_missing(self):
        d = {"a": 1}
        assert safe_dict_get(d, "b") is None
        assert safe_dict_get(d, "b", default="default") == "default"

    def test_safe_dict_get_not_dict(self):
        assert safe_dict_get("not a dict", "a") is None
        assert safe_dict_get(None, "a", default=5) == 5
        assert safe_dict_get([1, 2], "a", default=5) == 5


class TestValidateTicker:
    def test_validate_ticker_valid(self):
        assert validate_ticker("AAPL") == "AAPL"
        assert validate_ticker("  aapl  ") == "AAPL"
        assert validate_ticker("TSLA") == "TSLA"

    def test_validate_ticker_invalid(self):
        with pytest.raises(ValueError, match="non-empty string"):
            validate_ticker("")
        with pytest.raises(ValueError, match="non-empty string"):
            validate_ticker(None)
        
        with pytest.raises(ValueError, match="Invalid ticker format"):
            validate_ticker("AAPL!")
        with pytest.raises(ValueError, match="Invalid ticker format"):
            validate_ticker("MS FT")


class TestValidateDateStr:
    def test_validate_date_str_valid(self):
        assert validate_date_str("2024-01-15") == "2024-01-15"
        assert validate_date_str("2024-01-15T00:00:00Z") == "2024-01-15T00:00:00Z"

    def test_validate_date_str_invalid(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            validate_date_str("not-a-date")
        with pytest.raises(ValueError, match="Invalid date format"):
            validate_date_str("2024/01/15")


class TestWithTimeout:
    @pytest.mark.asyncio
    async def test_with_timeout_completes(self):
        async def fast_func():
            await asyncio.sleep(0.01)
            return "done"
        
        result = await with_timeout(fast_func(), 1.0)
        assert result == "done"

    @pytest.mark.asyncio
    async def test_with_timeout_times_out(self):
        async def slow_func():
            await asyncio.sleep(10)
            return "done"
        
        result = await with_timeout(slow_func(), 0.01, default="timeout")
        assert result == "timeout"


class TestSanitizeText:
    def test_sanitize_text_normal(self):
        assert sanitize_text("  hello world  ") == "hello world"
        assert sanitize_text("hello") == "hello"

    def test_sanitize_text_none(self):
        assert sanitize_text(None) == ""
        assert sanitize_text("") == ""

    def test_sanitize_text_truncated(self):
        long_text = "a" * 15000
        result = sanitize_text(long_text, max_length=100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")


class TestClamp:
    def test_clamp_within(self):
        assert clamp(0.5, 0.0, 1.0) == 0.5
        assert clamp(0.0, 0.0, 1.0) == 0.0
        assert clamp(1.0, 0.0, 1.0) == 1.0

    def test_clamp_below_min(self):
        assert clamp(-0.5, 0.0, 1.0) == 0.0
        assert clamp(-10, 0, 100) == 0

    def test_clamp_above_max(self):
        assert clamp(1.5, 0.0, 1.0) == 1.0
        assert clamp(200, 0, 100) == 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])