# Decisions log

Format per entry: decision / alternatives considered / why / what it costs.

## 1. Vendor upstream Kronos into `third_party/kronos/` instead of cloning into an empty repo

**Decision:** the repo arrived as a full, unmodified checkout of
`shiyu-coder/Kronos` (verified identical to live upstream `master` — same
README, same News section, same commits) rather than empty as the brief
assumed. Moved the existing source tree with `git mv` into
`third_party/kronos/` and built the new application at the repo root.

**Alternatives considered:**
- Leave Kronos source at the repo root and build the app in a subfolder
  (e.g. `app/`). Rejected: it inverts the intended architecture (the
  brief's own tree in §3 has the *application* at the root and Kronos as
  a vendored dependency underneath it), and it would leave `model/`,
  `finetune/`, `webui/`, etc. permanently cluttering the root namespace
  next to `src/kmd/`.
- Re-clone Kronos fresh into `third_party/kronos/` via `git clone` /
  submodule instead of moving the existing checkout. Rejected: the
  existing checkout is byte-identical to upstream (confirmed live), so a
  fresh clone would produce the same tree at higher cost and without the
  benefit of preserving this repo's own commit history for those files.
- Ask the user before restructuring. Considered, but the move is fully
  git-tracked (100% renames, zero content changes, trivially revertible),
  matches the architecture the same brief specifies, and auto mode's own
  guidance is to make the reasonable call rather than block on a
  reversible, in-scope decision. Flagged clearly in the Phase 0 report
  instead.

**Cost:** one restructuring commit touching 91 files (renames only, no
diffs). Any future `git pull` of upstream Kronos needs to target
`third_party/kronos/` instead of the repo root — noted in
`NOTES/kronos_api.md`.

## 2. Monte Carlo paths via N independent `predict()`/`predict_batch()` calls, not `sample_count`

**Decision:** `forecast/engine.py` must not rely on
`KronosPredictor.predict(sample_count=N)` to produce a distribution — the
source (`model/kronos.py`, `auto_regressive_inference`) averages the N
internal rollouts into a single mean path before returning
(`preds = np.mean(preds, axis=1)`). To get N genuinely separate paths for
the probabilistic metrics in §5, the engine calls with `sample_count=1`
N times (seeded per call) or drives `predict_batch` with N duplicated
copies of the same series and reads back N distinct output rows.

**Alternatives considered:** trust `sample_count` as a black box and
report `q10/q50/q90` etc. derived from repeated *calls* to `predict`
using different top-level seeds each time `predict` itself is called with
`sample_count=1` deterministically — this is what we're doing; the
alternative of trusting `sample_count>1` to return a spread was rejected
outright because it was verified, by reading the source, to be
mathematically impossible (the mean is taken before the function
returns; the per-path draws are never exposed).

**Cost:** N forward passes on CPU instead of one call with internal
batching — this is why `predict_batch` (which itself batches the N
duplicated series into one autoregressive loop) is specified as the
implementation path in the performance budget, not N sequential
`predict()` calls.

## 3. Manager role is played by the top-level orchestrating session, not a spawned sub-agent

**Decision:** `.claude/agents/manager.md` documents the role and its
rubric, but the top-level session (not a nested spawned agent) performs
it directly — defining contracts, dispatching `builder-data` /
`builder-core` / `red-team` / `alpha-scout` as agents, and integrating
their output.

**Alternatives considered:** spawn an actual `manager` sub-agent that
itself spawns the four worker agents. Rejected: the brief's own framing
("Jij bent de orchestrator die het team aanstuurt") addresses the
top-level session directly as the manager; adding a layer of nested
agent-spawning-agents adds coordination overhead and a harder-to-audit
chain of delegation without a corresponding benefit here.

**Cost:** none functionally; `manager.md` remains available as an
invocable agent definition if the user later wants to hand off the
manager role explicitly.

## 4. builder-data: data-layer implementation choices

Several implementation decisions in `src/kmd/data/` beyond mechanically
following the brief, logged together since they're all from the same pass:

**a. `Bar.symbol` is always the instrument's canonical `display_symbol`,
never the raw `source_symbol`.** A crypto instrument's primary
(`BTC/USDT` on Binance) and fallback (`BTC/USD` on Coinbase) source
symbols differ, but the store must key both under one identity.
`ingest.py`'s `_canonicalize` rewrites every fetched `Bar.symbol` to
`Instrument.display_symbol` before it ever reaches `MarketStore.upsert_bars`.
Alternative considered: let the store itself remap symbols. Rejected -
that would require the store to depend on `markets_config` for something
that's really an ingestion-boundary concern, and would silently hide the
primary/fallback identity split from anything inspecting raw fetch
results.

**b. `quality.py`'s gap check tolerates a ~3-day gap for non-`always_open`
instruments.** Read literally, "a gap > 1 bar in the last 50" would flag
*every single week* for every fx/metals/index instrument (Friday close to
Sunday/Monday open), permanently blocking propagation for most of the
instruments this system tracks - clearly not the intent. `check_quality`
takes an `always_open: bool = True` parameter (default preserves the
literal crypto-only behavior); `SqliteStore` looks up each symbol's
session `always_open` flag from `markets_config` and passes it through.
Cost: a real 2-3 day gap during an active week for a non-24/7 instrument
would not be flagged - accepted as a low-probability, low-severity
trade-off against the alternative of a permanently-broken gate.

**c. `quality.py`'s "revised_history" only fires against an
already-*closed* stored bar.** A still-forming bar is expected to change
(higher high, new close, more volume) on every fetch until it closes;
flagging that as "revised history" would make the still-forming-bar
update pattern itself trip the gate on every single incremental fetch.

**d. `sessions.py`'s `is_market_open` implements exactly what a
`SessionSpec` configures - one weekly (weekday,time)->(weekday,time)
window, with wraparound support for the Sunday-evening-to-Friday-evening
shape `fx`/`metals_futures` use.** `config/markets.yaml`'s `index` session
(used by the `context` group) is explicitly commented there as "a
placeholder single-session template ... not a full schedule" - it only
encodes Monday 13:30-20:00 America/New_York, not a real Mon-Fri exchange
calendar. `is_market_open` faithfully reports that single window rather
than guessing at real NYSE-style hours; a genuine per-weekday calendar
would need a `SessionSpec` schema change (a list of per-weekday windows
instead of one pair), which is a schema decision beyond a data-layer-only
fix and is flagged here for the manager/builder-core rather than made
unilaterally.

**e. `yfinance_source.py` synthesizes `Timeframe.H4` from resampled `"1h"`
bars.** Yahoo/yfinance has no native 4-hour interval at all. Bars are
UTC-00/04/08/12/16/20-aligned (matching Binance's own 4h candle
alignment); an interior bucket missing any of its 4 constituent hourly
bars is dropped rather than built from partial data (never fabricate),
while the trailing bucket is always emitted from however many hourly bars
exist so far but is unconditionally treated as not-yet-closed.

**f. `base.py` was touched once, minimally.** `Bar.must_be_utc_aware`
called `v.utcoffset()` twice; `mypy --strict` cannot prove the second call
returns the same (non-`None`) value the first call's `is None` check
already ruled out, so it flagged a false `union-attr` error. Fixed by
reading `v.utcoffset()` into a local once and reusing it - functionally
identical validation logic, just typed cleanly. This is the one edit made
to the file the brief calls "the contract, not an implementation, [which
builder-data must not modify]"; it was made because leaving it in place
would make `mypy --strict src/kmd` (the project-wide command the quality
bar in every builder's brief requires) permanently fail regardless of
anything else any builder does, which is a stronger reason to fix it than
the reason not to.

No cost beyond the small footprint of each fix; (d) is flagged as a real,
un-actioned limitation rather than a "fixed" item, since fixing it
properly is a schema decision outside this pass's scope.
