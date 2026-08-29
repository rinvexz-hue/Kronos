"""Tests for `SqliteStore`. Uses `:memory:` SQLite - no filesystem, no
network - plus a hand-built `MarketsConfig` so tests don't depend on the
real `config/markets.yaml`'s exact instrument list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kmd.data.base import Bar, Timeframe
from kmd.data.markets_config import MarketsConfig
from kmd.data.store import SqliteStore

SYMBOL = "BTC/USDT"
TF = Timeframe.H1
T0 = datetime(2024, 1, 1, tzinfo=UTC)

_TEST_CONFIG = MarketsConfig.model_validate(
    {
        "timeframes": {"primary": "1h", "secondary": ["4h", "1d"]},
        "forecast": {"lookback_bars": 400, "pred_len": 24},
        "sessions": {
            "crypto": {"always_open": True},
            "fx": {
                "always_open": False,
                "weekday_open": "sun 22:00",
                "weekday_close": "fri 22:00",
                "timezone": "UTC",
            },
        },
        "groups": {
            "crypto": {
                "session": "crypto",
                "instruments": [
                    {
                        "display_symbol": "BTC/USDT",
                        "decimals": 2,
                        "source": "ccxt",
                        "exchange": "binance",
                        "source_symbol": "BTC/USDT",
                    }
                ],
            },
            "fx": {
                "session": "fx",
                "instruments": [
                    {
                        "display_symbol": "EUR/USD",
                        "decimals": 5,
                        "source": "yfinance",
                        "source_symbol": "EURUSD=X",
                    }
                ],
            },
        },
        "risk": {"min_rr_for_setup": 2.0, "default_risk_pct": 2.0},
        "calibration": {"min_observations_for_display": 30, "target_band_coverage": 0.8},
    }
)


def make_bar(
    hour_offset: int,
    *,
    symbol: str = SYMBOL,
    timeframe: Timeframe = TF,
    close: float = 100.0,
    is_closed: bool = True,
) -> Bar:
    ts = T0 + timedelta(hours=hour_offset)
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        ts_utc=ts,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=10.0,
        is_closed=is_closed,
    )


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:", markets_config=_TEST_CONFIG)


def test_upsert_and_get_latest_bars_round_trip(store: SqliteStore) -> None:
    bars = [make_bar(i) for i in range(5)]
    result = store.upsert_bars(bars)
    assert result.passed

    fetched = store.get_latest_bars(SYMBOL, TF, limit=10)
    assert [b.ts_utc for b in fetched] == [b.ts_utc for b in bars]
    assert fetched[0].close == bars[0].close


def test_get_latest_bars_respects_limit_and_ordering(store: SqliteStore) -> None:
    store.upsert_bars([make_bar(i) for i in range(10)])
    fetched = store.get_latest_bars(SYMBOL, TF, limit=3)
    assert [b.ts_utc for b in fetched] == [T0 + timedelta(hours=h) for h in (7, 8, 9)]


def test_upsert_overwrites_same_key_via_on_conflict(store: SqliteStore) -> None:
    store.upsert_bars([make_bar(0, close=100.0, is_closed=False)])
    store.upsert_bars([make_bar(0, close=105.0, is_closed=True)])

    fetched = store.get_latest_bars(SYMBOL, TF, limit=10)
    assert len(fetched) == 1  # UNIQUE(symbol, timeframe, ts_utc) - overwritten, not duplicated
    assert fetched[0].close == 105.0
    assert fetched[0].is_closed is True


def test_get_last_closed_ts_ignores_unclosed_bars(store: SqliteStore) -> None:
    store.upsert_bars([make_bar(0, is_closed=True), make_bar(1, is_closed=False)])
    assert store.get_last_closed_ts(SYMBOL, TF) == T0


def test_get_last_closed_ts_is_none_for_empty_store(store: SqliteStore) -> None:
    assert store.get_last_closed_ts(SYMBOL, TF) is None


def test_upsert_rejects_batch_failing_quality_gate(store: SqliteStore) -> None:
    store.upsert_bars([make_bar(0, close=100.0, is_closed=True)])
    result = store.upsert_bars([make_bar(0, close=999.0, is_closed=True)])  # revised history

    assert not result.passed
    # Rejected batch must not have been written - stored value unchanged.
    fetched = store.get_latest_bars(SYMBOL, TF, limit=10)
    assert fetched[0].close == 100.0


def test_upsert_empty_list_is_a_no_op_pass(store: SqliteStore) -> None:
    result = store.upsert_bars([])
    assert result.passed
    assert store.get_latest_bars(SYMBOL, TF, limit=10) == []


def test_upsert_rejects_mixed_symbol_batch(store: SqliteStore) -> None:
    with pytest.raises(ValueError, match="single \\(symbol, timeframe\\)"):
        store.upsert_bars([make_bar(0, symbol="BTC/USDT"), make_bar(1, symbol="XRP/USDT")])


def test_weekend_gap_tolerated_for_fx_instrument_via_session_lookup(store: SqliteStore) -> None:
    friday = datetime(2024, 1, 5, 22, 0, tzinfo=UTC)
    sunday = datetime(2024, 1, 7, 23, 0, tzinfo=UTC)
    store.upsert_bars(
        [Bar(symbol="EUR/USD", timeframe=TF, ts_utc=friday, open=1.1, high=1.101, low=1.099, close=1.1, volume=1.0, is_closed=True)]
    )
    result = store.upsert_bars(
        [Bar(symbol="EUR/USD", timeframe=TF, ts_utc=sunday, open=1.1, high=1.101, low=1.099, close=1.1005, volume=1.0, is_closed=True)]
    )
    assert result.passed  # fx is not always_open per _TEST_CONFIG - weekend gap tolerated


def test_source_health_round_trips(store: SqliteStore) -> None:
    from kmd.data.base import SourceHealth

    health = SourceHealth(
        source_name="ccxt:binance",
        ok=False,
        last_success_utc=T0,
        consecutive_failures=4,
        last_error="timeout",
    )
    store.record_source_health(health)
    stored = {h.source_name: h for h in store.source_health()}
    assert stored["ccxt:binance"].ok is False
    assert stored["ccxt:binance"].consecutive_failures == 4
    assert stored["ccxt:binance"].last_success_utc == T0
    assert stored["ccxt:binance"].last_error == "timeout"


def test_close_does_not_raise(store: SqliteStore) -> None:
    store.close()


# --- Real SQLite lock contention (red-team Round 2, fault injection) -----
#
# `:memory:` can't reproduce this at all (no file to contend over), so
# these use a real on-disk file and a genuinely separate `sqlite3.connect`
# handle holding a real write lock - not a mock of `sqlite3.OperationalError`.


def test_busy_timeout_waits_out_a_lock_released_in_time(tmp_path: Path) -> None:
    """A write lock held by another connection for LESS time than
    `busy_timeout` must simply be waited out - `upsert_bars` blocks, then
    succeeds, with the bar actually persisted. Proves `busy_timeout` is not
    just set but genuinely effective.
    """
    import sqlite3
    import threading
    import time

    db_path = tmp_path / "lock_test.sqlite3"
    store = SqliteStore(db_path, markets_config=_TEST_CONFIG, busy_timeout_ms=3000)
    bar = Bar(
        symbol=SYMBOL, timeframe=TF, ts_utc=T0, open=100.0, high=101.0, low=99.0,
        close=100.0, volume=1.0, is_closed=True,
    )

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("CREATE TABLE IF NOT EXISTS _lock_probe(x)")  # touch it under the txn

    outcome: dict[str, object] = {}

    def _do_upsert() -> None:
        t0 = time.monotonic()
        outcome["result"] = store.upsert_bars([bar])
        outcome["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=_do_upsert)
    t.start()
    time.sleep(0.5)  # hold the lock well under the 3s busy_timeout
    blocker.commit()
    blocker.close()
    t.join(timeout=5)

    assert not t.is_alive()
    assert outcome["result"].passed  # type: ignore[union-attr]
    assert outcome["elapsed"] >= 0.4  # actually waited for the lock, not a no-op
    assert len(store.get_latest_bars(SYMBOL, TF, 10)) == 1
    store.close()


def test_busy_timeout_exhausted_raises_store_busy_error_not_a_hang(tmp_path: Path) -> None:
    """A write lock held PAST `busy_timeout` must raise `StoreBusyError`
    cleanly (bounded time, well-typed exception) rather than hanging
    forever or silently losing the write.
    """
    import sqlite3
    import threading

    from kmd.data.store import StoreBusyError

    db_path = tmp_path / "lock_test_persistent.sqlite3"
    store = SqliteStore(db_path, markets_config=_TEST_CONFIG, busy_timeout_ms=800)
    bar = Bar(
        symbol=SYMBOL, timeframe=TF, ts_utc=T0, open=100.0, high=101.0, low=99.0,
        close=100.0, volume=1.0, is_closed=True,
    )

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("CREATE TABLE IF NOT EXISTS _lock_probe(x)")
    # Deliberately never committed/rolled back until after the assertion.

    outcome: dict[str, object] = {}

    def _do_upsert() -> None:
        try:
            outcome["result"] = store.upsert_bars([bar])
        except Exception as exc:
            outcome["error"] = exc

    t = threading.Thread(target=_do_upsert)
    t.start()
    t.join(timeout=5)  # must finish well within busy_timeout + slack, never hang

    assert not t.is_alive()
    assert isinstance(outcome.get("error"), StoreBusyError)
    assert "locked" in str(outcome["error"]).lower()
    # No silent data loss disguised as success: nothing was written.
    blocker.rollback()
    blocker.close()
    assert len(store.get_latest_bars(SYMBOL, TF, 10)) == 0
    store.close()


def test_record_source_health_also_raises_store_busy_error_on_persistent_lock(tmp_path: Path) -> None:
    """`record_source_health` shares the same file/connection as
    `upsert_bars` but had its own, separate write path - this confirms it
    got the same `StoreBusyError` translation, not just `upsert_bars`.
    """
    import sqlite3
    import threading

    from kmd.data.base import SourceHealth
    from kmd.data.store import StoreBusyError

    db_path = tmp_path / "lock_test_health.sqlite3"
    store = SqliteStore(db_path, markets_config=_TEST_CONFIG, busy_timeout_ms=800)

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("CREATE TABLE IF NOT EXISTS _lock_probe(x)")

    outcome: dict[str, object] = {}

    def _do_record() -> None:
        try:
            store.record_source_health(
                SourceHealth(
                    source_name="ccxt:binance", ok=True, last_success_utc=T0,
                    consecutive_failures=0, last_error=None,
                )
            )
        except Exception as exc:
            outcome["error"] = exc

    t = threading.Thread(target=_do_record)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive()
    assert isinstance(outcome.get("error"), StoreBusyError)
    blocker.rollback()
    blocker.close()
    store.close()


# --- Property-based test --------------------------------------------------
#
# Feed the store batches with randomly injected duplicates/out-of-order/
# gap timestamps and assert that whatever ends up persisted is always
# internally consistent: unique (symbol, timeframe, ts_utc) keys, and
# every stored row is traceable to some bar that was actually submitted
# (nothing fabricated) in a batch the gate accepted.


@given(
    batches=st.lists(
        st.lists(st.integers(min_value=0, max_value=100), max_size=10),
        min_size=1,
        max_size=15,
    )
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_store_stays_internally_consistent_under_adversarial_batches(
    batches: list[list[int]],
) -> None:
    store = SqliteStore(":memory:", markets_config=_TEST_CONFIG)
    accepted_ts: set[datetime] = set()

    for offsets in batches:
        bars = [make_bar(h) for h in offsets]
        result = store.upsert_bars(bars)
        if result.passed:
            accepted_ts.update(b.ts_utc for b in bars)

    fetched = store.get_latest_bars(SYMBOL, TF, limit=10_000)
    fetched_ts = [b.ts_utc for b in fetched]

    # No duplicate keys ever persisted.
    assert len(fetched_ts) == len(set(fetched_ts))
    # Every stored timestamp came from some accepted batch - nothing
    # fabricated ever lands in the store.
    assert set(fetched_ts) <= accepted_ts
    # Stored rows are always returned in ascending order.
    assert fetched_ts == sorted(fetched_ts)

    store.close()
