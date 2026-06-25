import logging
from typing import Dict, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from collections import defaultdict
from scraper.fintech_clients.base import FintechHealth

logger = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open and cannot execute."""
    pass


class CircuitBreaker:
    """Circuit breaker wrapper for backward compatibility."""
    
    def __init__(self, source: str, failure_threshold: int = 5, success_threshold: int = 2, timeout_seconds: int = 60):
        self.source = source
        self._state = CircuitState(
            source=source, 
            failure_threshold=failure_threshold, 
            success_threshold=success_threshold,
            timeout_seconds=timeout_seconds
        )
    
    @property
    def state(self) -> str:
        return self._state.state
    
    def can_execute(self) -> bool:
        return self._state.can_execute()
    
    def record_success(self):
        self._state.record_success()
    
    def record_failure(self):
        self._state.record_failure()
    
    def _transition(self, new_state: str):
        self._state._transition(new_state)


@dataclass
class CircuitState:
    """Circuit breaker state for a data source."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
    
    source: str
    state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    failure_count: int = 0
    success_count: int = 0
    last_failure: Optional[datetime] = None
    last_success: Optional[datetime] = None
    next_attempt: Optional[datetime] = None

    # Configuration
    failure_threshold: int = 5
    success_threshold: int = 2
    timeout_seconds: int = 60

    def record_success(self):
        self.success_count += 1
        self.failure_count = 0
        self.last_success = datetime.utcnow()
        if self.state == "HALF_OPEN" and self.success_count >= self.success_threshold:
            self.state = "CLOSED"
            logger.info(f"Circuit {self.source} CLOSED after recovery")

    def record_failure(self):
        self.failure_count += 1
        self.success_count = 0
        self.last_failure = datetime.utcnow()
        if self.state == "CLOSED" and self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            self.next_attempt = datetime.utcnow() + timedelta(seconds=self.timeout_seconds)
            logger.warning(f"Circuit {self.source} OPENED after {self.failure_count} failures")
        elif self.state == "HALF_OPEN":
            self.state = "OPEN"
            self.next_attempt = datetime.utcnow() + timedelta(seconds=self.timeout_seconds)
            logger.warning(f"Circuit {self.source} RE-OPENED after half-open failure")

    def can_execute(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if self.next_attempt and datetime.utcnow() >= self.next_attempt:
                self.state = "HALF_OPEN"
                self.success_count = 0
                logger.info(f"Circuit {self.source} entering HALF_OPEN")
                return True
            return False
        return True  # HALF_OPEN

    def _transition(self, new_state: str):
        """Transition to a new state."""
        self.state = new_state
        if new_state == "HALF_OPEN":
            self.success_count = 0


class HealthMonitor:
    """Monitors health of all data sources with circuit breakers."""

    def __init__(self):
        self.circuits: Dict[str, CircuitState] = {}
        self.latencies: Dict[str, list] = defaultdict(list)
        self.request_counts: Dict[str, int] = defaultdict(int)
        self.error_counts: Dict[str, int] = defaultdict(int)

    def get_circuit(self, source: str) -> CircuitState:
        if source not in self.circuits:
            self.circuits[source] = CircuitState(source=source)
        return self.circuits[source]

    def record_request(self, source: str, latency_ms: float, success: bool):
        """Record a request outcome."""
        self.request_counts[source] += 1
        self.latencies[source].append(latency_ms)
        if len(self.latencies[source]) > 100:
            self.latencies[source] = self.latencies[source][-100:]

        circuit = self.get_circuit(source)
        if success:
            circuit.record_success()
        else:
            circuit.record_failure()
            self.error_counts[source] += 1

    def record_result(self, source: str, success: bool, latency_ms: float = 0):
        """Alias for record_request for backward compatibility."""
        self.record_request(source, latency_ms, success)

    def get_status(self, source: str) -> Dict:
        """Get health status for a source."""
        circuit = self.get_circuit(source)
        latencies = self.latencies.get(source, [])
        return {
            "source": source,
            "circuit_state": circuit.state.lower(),
            "failure_count": circuit.failure_count,
            "success_count": circuit.success_count,
            "success_rate_100": (self.request_counts[source] - self.error_counts[source]) / max(self.request_counts[source], 1),
            "avg_latency_ms": sum(latencies) / len(latencies) if latencies else 0,
            "can_execute": circuit.can_execute(),
            "last_failure": circuit.last_failure.isoformat() if circuit.last_failure else None,
            "last_success": circuit.last_success.isoformat() if circuit.last_success else None,
        }

    def get_source_status(self, source: str) -> Dict:
        """Alias for get_status for backward compatibility."""
        return self.get_status(source)

    def get_all_status(self) -> Dict[str, Dict]:
        """Get status for all sources."""
        return {source: self.get_status(source) for source in self.circuits}

    def is_healthy(self, source: str) -> bool:
        """Check if source is healthy enough to execute."""
        circuit = self.get_circuit(source)
        return circuit.can_execute() and circuit.state != "OPEN"

    def force_open(self, source: str):
        """Manually open a circuit."""
        circuit = self.get_circuit(source)
        circuit.state = "OPEN"
        circuit.next_attempt = datetime.utcnow() + timedelta(seconds=circuit.timeout_seconds)

    def force_close(self, source: str):
        """Manually close a circuit."""
        circuit = self.get_circuit(source)
        circuit.state = "CLOSED"
        circuit.failure_count = 0
        circuit.success_count = 0

    def reset_source(self, source: str):
        """Reset all metrics for a source."""
        if source in self.circuits:
            del self.circuits[source]
        if source in self.latencies:
            del self.latencies[source]
        self.request_counts[source] = 0
        self.error_counts[source] = 0