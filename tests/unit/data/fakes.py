"""Shared test doubles for `src/kmd/data/` — no real ccxt/yfinance/network
calls anywhere in this file or in anything that imports it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


class FrozenClock:
    """A `Callable[[], datetime]` that starts at `start` and only advances
    when `advance()` is called or `sleep()` (see `NoopSleep` below) ticks
    it forward — so retry/backoff/circuit-breaker tests never depend on
    real wall-clock time.
    """

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class RecordingSleep:
    """A `Callable[[float], None]` that records every requested delay
    (instead of actually sleeping) and, if given a clock, advances it by
    that amount — so a retry loop's backoff is observable without the test
    taking real wall-clock time.
    """

    def __init__(self, clock: FrozenClock | None = None) -> None:
        self.delays: list[float] = []
        self._clock = clock

    def __call__(self, delay_s: float) -> None:
        self.delays.append(delay_s)
        if self._clock is not None:
            self._clock.advance(timedelta(seconds=delay_s))


class FakeCcxtExchange:
    """Minimal stand-in for a `ccxt.Exchange`, satisfying
    `ccxt_source.CcxtExchangeLike`. `rows` mimics exactly what
    `ccxt.Exchange.fetch_ohlcv` returns: `[timestamp_ms, o, h, l, c, v]`.
    """

    def __init__(
        self,
        rows: list[list[float]],
        *,
        id: str = "fakeexchange",  # noqa: A002 - mirrors ccxt.Exchange's own attribute name
        fail_next: int = 0,
        failure: Callable[[], Exception] = lambda: RuntimeError("simulated fetch failure"),
    ) -> None:
        self.id = id
        self._rows = rows
        self._fail_next = fail_next
        self._failure = failure
        self.calls: list[tuple[str, str, int | None, int | None]] = []

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: int | None = None,
        limit: int | None = None,
    ) -> list[list[float]]:
        self.calls.append((symbol, timeframe, since, limit))
        if self._fail_next > 0:
            self._fail_next -= 1
            raise self._failure()
        rows = self._rows
        if since is not None:
            rows = [r for r in rows if r[0] >= since]
        if limit is not None:
            rows = rows[:limit]
        return rows


class FakeYfTicker:
    """Minimal stand-in for a `yfinance.Ticker`, satisfying
    `yfinance_source.YfHistoryClient`. `frame` mimics exactly what
    `Ticker.history()` returns: a tz-aware `DatetimeIndex`-ed DataFrame
    with Open/High/Low/Close/Volume columns.
    """

    def __init__(
        self,
        frame: pd.DataFrame,
        *,
        fail_next: int = 0,
        failure: Callable[[], Exception] = lambda: RuntimeError("simulated fetch failure"),
    ) -> None:
        self._frame = frame
        self._fail_next = fail_next
        self._failure = failure
        self.calls: list[dict[str, object]] = []

    def history(
        self,
        *,
        interval: str,
        period: str | None = None,
        start: datetime | None = None,
        auto_adjust: bool = False,
    ) -> pd.DataFrame:
        self.calls.append({"interval": interval, "period": period, "start": start})
        if self._fail_next > 0:
            self._fail_next -= 1
            raise self._failure()
        frame = self._frame
        if start is not None:
            frame = frame[frame.index >= start]
        return frame


def load_yf_fixture(name: str) -> pd.DataFrame:
    """Loads a CSV fixture shaped like `yfinance.Ticker.history()`'s real
    output into a tz-aware-indexed DataFrame.
    """
    path = FIXTURES_DIR / name
    frame = pd.read_csv(path, index_col="Datetime", parse_dates=["Datetime"])
    frame.index = pd.to_datetime(frame.index, utc=False)
    return frame
