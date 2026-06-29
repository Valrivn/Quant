import asyncio
import logging
import time
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from config import load_hybrid_config

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation, requests go through
    OPEN = "open"           # Failing, requests blocked
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5          # Number of failures before opening
    success_threshold: int = 2          # Successes in half-open before closing
    timeout: float = 60.0               # Seconds before half-open
    excluded_exceptions: tuple = (asyncio.TimeoutError,)


@dataclass
class CircuitBreakerStats:
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    state_changes: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    last_state_change: Optional[float] = None


class CircuitBreaker:
    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()
        self.stats = CircuitBreakerStats()

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if self._last_failure_time and (time.time() - self._last_failure_time) >= self.config.timeout:
                return CircuitState.HALF_OPEN
        return self._state

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        async with self._lock:
            current_state = self.state
            
            if current_state == CircuitState.OPEN:
                self.stats.rejected_calls += 1
                raise CircuitBreakerOpenError(f"Circuit breaker '{self.name}' is OPEN")
            
            self.stats.total_calls += 1

        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            
            await self._on_success()
            return result
            
        except self.config.excluded_exceptions as e:
            await self._on_failure(e)
            raise
        except Exception as e:
            await self._on_failure(e)
            raise

    async def _on_success(self) -> None:
        async with self._lock:
            self.stats.successful_calls += 1
            self.stats.last_success_time = time.time()
            
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    await self._transition_to_closed()
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    async def _on_failure(self, exception: Exception) -> None:
        async with self._lock:
            self.stats.failed_calls += 1
            self.stats.last_failure_time = time.time()
            self._last_failure_time = time.time()
            
            if self._state == CircuitState.HALF_OPEN:
                await self._transition_to_open()
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    await self._transition_to_open()

    async def _transition_to_open(self) -> None:
        if self._state != CircuitState.OPEN:
            self._state = CircuitState.OPEN
            self._failure_count = 0
            self._success_count = 0
            self.stats.state_changes += 1
            self.stats.last_state_change = time.time()
            logger.warning(f"Circuit breaker '{self.name}' OPENED after {self.config.failure_threshold} failures")

    async def _transition_to_half_open(self) -> None:
        if self._state != CircuitState.HALF_OPEN:
            self._state = CircuitState.HALF_OPEN
            self._success_count = 0
            self.stats.state_changes += 1
            self.stats.last_state_change = time.time()
            logger.info(f"Circuit breaker '{self.name}' HALF_OPEN - testing recovery")

    async def _transition_to_closed(self) -> None:
        if self._state != CircuitState.CLOSED:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self.stats.state_changes += 1
            self.stats.last_state_change = time.time()
            logger.info(f"Circuit breaker '{self.name}' CLOSED - service recovered")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            **self.stats.__dict__,
        }

    async def reset(self) -> None:
        async with self._lock:
            await self._transition_to_closed()


class CircuitBreakerRegistry:
    def __init__(self):
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, name: str, config: Optional[CircuitBreakerConfig] = None) -> CircuitBreaker:
        async with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name, config)
            return self._breakers[name]

    async def get(self, name: str) -> Optional[CircuitBreaker]:
        return self._breakers.get(name)

    async def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        return {name: cb.get_stats() for name, cb in self._breakers.items()}

    async def reset_all(self) -> None:
        async with self._lock:
            for cb in self._breakers.values():
                await cb.reset()


# Global registry
_circuit_breaker_registry: Optional[CircuitBreakerRegistry] = None


async def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    global _circuit_breaker_registry
    if _circuit_breaker_registry is None:
        _circuit_breaker_registry = CircuitBreakerRegistry()
    return _circuit_breaker_registry


class CircuitBreakerOpenError(Exception):
    pass


async def with_circuit_breaker(
    name: str,
    func: Callable,
    *args,
    config: Optional[CircuitBreakerConfig] = None,
    fallback: Optional[Callable] = None,
    **kwargs
) -> Any:
    registry = await get_circuit_breaker_registry()
    breaker = await registry.get_or_create(name, config)
    
    try:
        return await breaker.call(func, *args, **kwargs)
    except CircuitBreakerOpenError:
        if fallback:
            logger.info(f"Circuit breaker '{name}' open, executing fallback")
            if asyncio.iscoroutinefunction(fallback):
                return await fallback(*args, **kwargs)
            return fallback(*args, **kwargs)
        raise


if __name__ == "__main__":
    import asyncio
    
    async def test():
        config = CircuitBreakerConfig(failure_threshold=3, timeout=5.0)
        registry = await get_circuit_breaker_registry()
        breaker = await registry.get_or_create("test_service", config)
        
        async def failing_service():
            raise ConnectionError("Service unavailable")
        
        async def fallback():
            return "fallback result"
        
        for i in range(5):
            try:
                result = await with_circuit_breaker("test_service", failing_service, fallback=fallback, config=config)
                print(f"Call {i+1}: {result}")
            except Exception as e:
                print(f"Call {i+1} failed: {e}")
        
        print(f"Stats: {breaker.get_stats()}")
        
        # Wait for timeout
        await asyncio.sleep(6)
        
        # Try again - should be half-open
        try:
            result = await with_circuit_breaker("test_service", failing_service, fallback=fallback, config=config)
            print(f"After timeout: {result}")
        except Exception as e:
            print(f"After timeout failed: {e}")
        
        print(f"Final stats: {breaker.get_stats()}")

    asyncio.run(test())