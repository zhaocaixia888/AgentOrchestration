"""Metrics collection and reporting."""

import copy
import time
from collections import defaultdict
from typing import Dict, List
from threading import Lock


MAX_HISTOGRAM_SAMPLES = 1000


class MetricsCollector:
    def __init__(self):
        self._lock = Lock()
        self._counters: Dict[str, int] = defaultdict(int)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._timers: Dict[str, float] = {}
        self._histogram_cache: Dict[str, Dict] = {}
        self._histogram_dirty: Dict[str, bool] = defaultdict(lambda: True)

    def increment(self, metric: str, value: int = 1) -> None:
        with self._lock:
            self._counters[metric] += value

    def gauge(self, metric: str, value: float) -> None:
        if not isinstance(value, (int, float)):
            raise TypeError(
                f"Gauge value must be numeric, got {type(value).__name__}"
            )
        with self._lock:
            self._gauges[metric] = float(value)

    def observe(self, metric: str, value: float) -> None:
        with self._lock:
            samples = self._histograms[metric]
            samples.append(value)
            if len(samples) > MAX_HISTOGRAM_SAMPLES:
                self._histograms[metric] = samples[-MAX_HISTOGRAM_SAMPLES:]
            self._histogram_dirty[metric] = True

    def start_timer(self, metric: str) -> None:
        with self._lock:
            self._timers[metric] = time.time()

    def stop_timer(self, metric: str) -> float:
        with self._lock:
            if metric in self._timers:
                duration = time.time() - self._timers.pop(metric)
                self.observe(metric, duration)
                return duration
        return 0.0

    def snapshot(self) -> Dict:
        with self._lock:
            histograms = {}
            for k, v in self._histograms.items():
                if self._histogram_dirty.get(k, True) or k not in self._histogram_cache:
                    self._histogram_cache[k] = {
                        "count": len(v),
                        "sum": sum(v),
                        "avg": sum(v) / len(v) if v else 0,
                        "min": min(v) if v else 0,
                        "max": max(v) if v else 0,
                        "samples": v[:],
                    }
                    self._histogram_dirty[k] = False
                histograms[k] = self._histogram_cache[k]
            result = {
                "counters": dict(self._counters),
                "gauges": {k: float(v) for k, v in self._gauges.items()},
                "histograms": copy.deepcopy(histograms),
            }
            return result


metrics = MetricsCollector()
