"""Pure functions implementing the data-quality gate.

No I/O, no store dependency, no `datetime.now()` — everything needed is
passed in, so this is fully testable with in-memory `Bar` lists. `store.py`
is the only caller in production; it loads `existing` via
`get_latest_bars` and feeds the result of `check_quality` straight back out
through `MarketStore.upsert_bars`'s return value.
"""

from __future__ import annotations

from itertools import pairwise

from kmd.data.base import Bar, QualityGateResult, QualityIssue, Timeframe
from kmd.data.timeutil import TIMEFRAME_DURATIONS

_GAP_WINDOW_BARS = 50

# A market that is not always-open (fx/metals_futures/index) has a weekly
# closure of up to ~2.5 days (Friday close to Sunday/Monday open) baked
# into every single timeframe's bar sequence. Without this allowance, the
# plain "expected_step" gap check below would flag *every week* as a gap
# for every non-crypto instrument, permanently blocking propagation for
# most of the instruments this system tracks - clearly not the intent of
# "detect an anomalous gap". See `check_quality`'s `always_open` parameter.
_WEEKEND_ALLOWANCE_S = 3 * 24 * 3600.0


def _timeframe_seconds(timeframe: Timeframe) -> float:
    return TIMEFRAME_DURATIONS[timeframe].total_seconds()


def _bars_differ(a: Bar, b: Bar) -> bool:
    return (a.open, a.high, a.low, a.close, a.volume) != (b.open, b.high, b.low, b.close, b.volume)


def _assert_single_series(bars: list[Bar], symbol: str, timeframe: Timeframe, label: str) -> None:
    for bar in bars:
        if bar.symbol != symbol or bar.timeframe != timeframe:
            raise ValueError(
                f"check_quality requires a single (symbol, timeframe) pair; `{label}` contains "
                f"{bar.symbol}/{bar.timeframe} mixed with {symbol}/{timeframe}"
            )


def check_quality(
    incoming: list[Bar],
    existing: list[Bar],
    *,
    always_open: bool = True,
) -> QualityGateResult:
    """`existing` is the already-stored history for one (symbol, timeframe)
    pair (oldest-first, as `MarketStore.get_latest_bars` returns it).
    `incoming` is the new batch about to be written; it need not be
    pre-sorted (`out_of_order` specifically checks whether it *is*).

    Detects, per `base.py`'s `QualityIssueKind`:

    - `out_of_order`: an incoming bar whose `ts_utc` does not strictly
      follow the previous incoming bar's, in the order `incoming` was
      passed in (i.e. as the source actually returned it).
    - `duplicate`: two bars *within `incoming` itself* sharing a `ts_utc`.
      An incoming bar re-covering a `ts_utc` already in `existing` is not a
      duplicate - that is the normal incremental-update / still-forming-bar
      pattern, handled below as an ordinary update or as `revised_history`.
    - `revised_history`: an incoming bar whose `ts_utc` matches an
      *already-closed* (`is_closed=True`) stored bar but whose OHLCV
      differs. A still-forming stored bar (`is_closed=False`) is *expected*
      to change on every fetch until it closes, so that case is never
      flagged.
    - `gap`: a jump of more than one bar's duration between consecutive
      bars in the merged (existing + incoming) timeline, restricted to its
      most recent `_GAP_WINDOW_BARS` bars. For a `always_open=False`
      instrument, a gap up to `_WEEKEND_ALLOWANCE_S` is tolerated without
      being flagged (see the module-level comment on why).
    """
    if not incoming:
        return QualityGateResult(passed=True, issues=[])

    symbol, timeframe = incoming[0].symbol, incoming[0].timeframe
    _assert_single_series(incoming, symbol, timeframe, "incoming")
    _assert_single_series(existing, symbol, timeframe, "existing")

    issues: list[QualityIssue] = []

    for prev, cur in pairwise(incoming):
        if cur.ts_utc <= prev.ts_utc:
            issues.append(
                QualityIssue(
                    kind="out_of_order",
                    symbol=symbol,
                    timeframe=timeframe,
                    detail=(
                        f"bar at {cur.ts_utc.isoformat()} does not strictly follow "
                        f"{prev.ts_utc.isoformat()} in the incoming batch order"
                    ),
                    ts_utc=cur.ts_utc,
                )
            )

    sorted_incoming = sorted(incoming, key=lambda b: b.ts_utc)
    existing_by_ts = {bar.ts_utc: bar for bar in existing}
    seen_incoming_ts: set[object] = set()
    for bar in sorted_incoming:
        if bar.ts_utc in seen_incoming_ts:
            issues.append(
                QualityIssue(
                    kind="duplicate",
                    symbol=symbol,
                    timeframe=timeframe,
                    detail=f"timestamp {bar.ts_utc.isoformat()} appears more than once in the incoming batch",
                    ts_utc=bar.ts_utc,
                )
            )
        seen_incoming_ts.add(bar.ts_utc)

        prior = existing_by_ts.get(bar.ts_utc)
        if prior is not None and prior.is_closed and _bars_differ(prior, bar):
            issues.append(
                QualityIssue(
                    kind="revised_history",
                    symbol=symbol,
                    timeframe=timeframe,
                    detail=(
                        f"closed bar at {bar.ts_utc.isoformat()} changed: "
                        f"OHLCV ({prior.open}, {prior.high}, {prior.low}, {prior.close}, {prior.volume}) "
                        f"-> ({bar.open}, {bar.high}, {bar.low}, {bar.close}, {bar.volume})"
                    ),
                    ts_utc=bar.ts_utc,
                )
            )

    merged_by_ts: dict[object, Bar] = {bar.ts_utc: bar for bar in existing}
    merged_by_ts.update({bar.ts_utc: bar for bar in sorted_incoming})
    merged_sorted = sorted(merged_by_ts.values(), key=lambda b: b.ts_utc)
    window = merged_sorted[-_GAP_WINDOW_BARS:]

    expected_step = _timeframe_seconds(timeframe)
    max_normal_gap_s = expected_step * 2 if always_open else max(
        expected_step * 2, _WEEKEND_ALLOWANCE_S
    )
    for prev, cur in pairwise(window):
        actual_step = (cur.ts_utc - prev.ts_utc).total_seconds()
        if actual_step > max_normal_gap_s:
            issues.append(
                QualityIssue(
                    kind="gap",
                    symbol=symbol,
                    timeframe=timeframe,
                    detail=(
                        f"gap between {prev.ts_utc.isoformat()} and {cur.ts_utc.isoformat()} "
                        f"({actual_step:.0f}s, expected ~{expected_step:.0f}s)"
                    ),
                    ts_utc=cur.ts_utc,
                )
            )

    return QualityGateResult(passed=not issues, issues=issues)
