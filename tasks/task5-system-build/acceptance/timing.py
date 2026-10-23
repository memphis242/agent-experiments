"""Serialized live timing, independent scheduler evidence and deadline predicates.

The coordinator lock is shared by acceptance builds and live attempts on this
host. A live attempt also requires an explicit CPU reservation; arbitrary host
builds cannot be inferred reliably from process names.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator

from common import EnvironmentFail, Fail

LOCK_PATH = Path("/workspace/tmp-acceptance/task5-host-lane.lock")
WATCH_INTERVAL = 0.05
STALL_LIMIT = 0.5


@dataclass(frozen=True)
class Window:
    minimum: float
    maximum: float

    def contains(self, elapsed: float) -> bool:
        assert self.minimum <= self.maximum
        return self.minimum <= elapsed <= self.maximum

    def require(self, elapsed: float, behaviour: str) -> None:
        if not self.contains(elapsed):
            raise Fail(f"{behaviour}: expected elapsed [{self.minimum},{self.maximum}] s; observed {elapsed:.6f} s")


LIVE_WINDOWS = {
    "gw-link": Window(5, 8), "gw-failback": Window(10, 12),
    "gw-connect": Window(18, 26), "peer-onboard": Window(10, 12),
    "peer-cut": Window(9, 12), "mode-recovery": Window(20, 25),
    "request": Window(0, 2),
}


def expired(now: float, start: float, duration: float) -> bool:
    """Exact synthetic-time semantics: a deadline is due at equality."""
    assert duration >= 0 and now >= start
    return now - start >= duration


def scheduling_stalls(samples: tuple[float, ...], threshold: float = STALL_LIMIT) -> tuple[float, ...]:
    assert threshold > 0
    assert all(b >= a for a, b in zip(samples, samples[1:]))
    return tuple(b - a for a, b in zip(samples, samples[1:]) if b - a > threshold)


class Watchdog:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._samples: list[float] = []
        self._thread = threading.Thread(target=self._watch, name="host-scheduler-watchdog", daemon=True)

    def _watch(self) -> None:
        self._samples.append(time.monotonic())
        while not self._stop.wait(WATCH_INTERVAL):
            self._samples.append(time.monotonic())
        self._samples.append(time.monotonic())

    def __enter__(self) -> "Watchdog":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join()

    @property
    def evidence(self) -> dict[str, object]:
        assert not self._thread.is_alive()
        samples = tuple(self._samples)
        return {"clock": "host CLOCK_MONOTONIC", "interval_s": WATCH_INTERVAL,
                "stall_threshold_s": STALL_LIMIT, "samples": samples,
                "stalls_s": scheduling_stalls(samples)}


@contextlib.contextmanager
def host_lane(*, live: bool = False) -> Iterator[None]:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        affinity = os.sched_getaffinity(0)
        try:
            if live:
                reservation = os.environ.get("T5_RESERVED_CPUS", "")
                if not reservation or os.environ.get("T5_HOST_BUILDS_QUIESCENT") != "1":
                    raise EnvironmentFail("ENVIRONMENT timing lane: set T5_RESERVED_CPUS to host-reserved CPU ids "
                                          "and T5_HOST_BUILDS_QUIESCENT=1 after stopping concurrent builds in all reviews")
                try:
                    cpus = {int(value) for value in reservation.split(",")}
                except ValueError as exc:
                    raise EnvironmentFail("ENVIRONMENT timing lane: T5_RESERVED_CPUS must be comma-separated CPU ids") from exc
                if len(cpus) < 2 or not cpus <= affinity:
                    raise EnvironmentFail(f"ENVIRONMENT timing lane: need >=2 reserved CPUs within affinity {sorted(affinity)}; "
                                          f"observed {sorted(cpus)}")
                os.sched_setaffinity(0, cpus)
            yield
        finally:
            os.sched_setaffinity(0, affinity)
            fcntl.flock(lock, fcntl.LOCK_UN)


def save_evidence(path: Path, attempt: int, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps({"attempt": attempt, **evidence}) + "\n")
