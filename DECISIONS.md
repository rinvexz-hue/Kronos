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

## 5. builder-core: model/engine/scheduler/API/frontend implementation choices

**a. `predict_batch` seeded ONCE per batch call, not per duplicated path.**
`torch.manual_seed(seed)` is called immediately before the single
`predict_batch` call that carries all N duplicated copies of one symbol's
lookback window (see decision #2). This still yields N genuinely distinct,
reproducible paths: `auto_regressive_inference` draws independent
`torch.multinomial` samples per batch row from the one seeded RNG stream,
so the whole batch is deterministic given one seed, and
`test_engine.py::test_run_monte_carlo_produces_n_distinct_reproducible_paths`
asserts both properties (distinctness AND same-seed reproducibility)
against `FakePredictor`, which reproduces this exact RNG-consumption
shape. Alternative considered: seed individually before N sequential
`predict()` calls, matching the brief's literal wording ("Seed
`torch.manual_seed` per path"). Rejected as the *default* path because it
reintroduces the N-sequential-forward-passes cost decision #2 exists to
avoid — the brief's own "Engine implication" paragraph in
`NOTES/kronos_api.md` specifies `predict_batch` for exactly this reason.
Cost: none functionally (still fully reproducible, still genuinely
distinct paths); the difference is purely which RNG-seeding granularity
produces the batch.

**b. Forecast cache + calibration log share one dedicated SQLite file,
kept separate from builder-data's `Settings.db_path`.** `forecast/cache.py`
(`forecast_cache` table) and `calibration/logger.py` (`forecast_log`
table) both take an arbitrary `db_path: Path`; `__main__.py` points both
at `kmd_forecast.sqlite3`, a sibling of `Settings.db_path` (`kmd.sqlite3`,
owned by `SqliteStore`'s `bars`/`source_health` schema). Alternatives
considered: (i) write into `Settings.db_path` itself, alongside
builder-data's tables — rejected, since that blurs schema ownership
across the module boundary the brief draws ("you do not reach into
builder-data's SQLite schema"), even though SQLite would technically
tolerate extra tables in the same file; (ii) two separate files, one per
concern — rejected as unnecessary splitting for two small, low-volume,
single-writer, forecast-adjacent stores that are almost always read/written
together during a refresh cycle. Cost: three separate `sqlite3.connect()`
handles onto two files in one process; each sets its own `busy_timeout`
pragma for basic write-contention safety, sufficient for this system's
single-scheduler-process usage pattern.

**c. Forecast cache stores CLOSE-price paths only, not full OHLCV.** Every
metric in `forecast/metrics.py` (`p_up_24h`, `q10/q50/q90`,
`p_vol_expansion`, `band_width_pct`) is defined purely on the close-price
series (see that module's docstring), so caching the full `open/high/low/
volume/amount` columns Kronos returns would be pure storage overhead with
no consumer. Cost/risk: if a future metric needs high/low/volume path
detail, `CachedForecast`/`MonteCarloResult` will need to grow a field —
noted directly in `forecast/cache.py`'s docstring so it isn't a silent gap.

**d. `kmd/dto.py` split out of `kmd/snapshot.py`.** `kmd.snapshot` imports
`kmd.analysis.{regime,levels,setup}` (to implement `build_snapshot`), and
those modules need the `Regime`/`Level`/`ForecastMetrics`/`SetupCard`
shapes to type their pure functions — importing those shapes from
`kmd.snapshot` would be a circular import. All DTO classes were moved
into a new leaf module, `kmd/dto.py` (no imports from `kmd.analysis.*` or
`kmd.snapshot`), and `kmd/snapshot.py` re-exports every name from it
(`__all__` included) so the documented contract import,
`from kmd.snapshot import SnapshotDTO`, is unchanged for `api.py`,
`scheduler.py`, and every test. Alternative considered: have
`kmd.analysis.*` do the import inside each function body (deferred import)
to dodge the cycle. Rejected — it would work but hide a real architectural
fact (the DTOs are shared, low-level shapes) behind a workaround, whereas
`kmd/dto.py` names it directly.

**e. `DataSourceStatus.error_count_last_hour` is approximated from
`SourceHealth.consecutive_failures`.** `MarketSource`/`MarketStore`
(`kmd/data/base.py`, the only interface builder-core may depend on) expose
a consecutive-failure counter, not a true rolling one-hour error count —
there is no windowed telemetry to read. Flagged directly in
`snapshot.py`'s `_build_source_status` and here: if builder-data's health
telemetry ever exposes a real windowed count, swap this proxy out.

**f. Frontend renders charts with hand-rolled inline SVG, not
lightweight-charts/Plotly.** The brief suggested "vanilla JS +
lightweight-charts (or Plotly)"; a dependency-free inline-SVG renderer was
used instead so the dashboard has zero external runtime dependencies (no
CDN reachability requirement for a local, read-only, ideally
offline-capable observation tool) and to keep "no build step" absolute.
Cost: no pan/zoom/crosshair interactivity a real charting library would
give for free — acceptable for a 30s-glance mobile-first dashboard, and
revisitable later without changing `SnapshotDTO`.

**g. Detail view shows a close-price line + linear quantile fan, not
candlesticks.** `AssetSnapshot` (the manager's DTO contract) carries
`sparkline: list[float]` (recent closes) and `ForecastMetrics`'
`q10/q50/q90` as single END-OF-HORIZON scalars — it does not carry full
OHLC history or a per-step forecast path. The brief's "candles + forecast
fan (q10-q90 band, q50 line)" wording was matched as closely as the actual
contract allows: a close-price line for history, fanning linearly from the
last known close to the three horizon quantiles. This is not a
misrepresentation — the legend/labels are explicit that these are the
end-of-horizon quantiles, not a rendered per-step path (which the raw
Monte Carlo paths could support, but the DTO doesn't carry them to the
frontend, by the manager's design: only the summary metrics do). Extending
`AssetSnapshot` with real OHLC history or a per-step quantile series would
be a contract change outside builder-core's authority to make unilaterally.

**h. Scheduler ingest wiring: `scheduler.py` depends only on
`kmd.data.base.MarketStore`; `build_ingest_fn` is the one seam that takes
a concrete `SqliteStore`.** `kmd.data.ingest.run_incremental_update` itself
requires a concrete `SqliteStore` (it calls `store.record_source_health`,
not part of `MarketStore`), so *some* seam must bridge Protocol-typed
scheduling code to it. `build_ingest_fn(store, registry, config)` is that
one function; everything else in `scheduler.py` (`run_refresh_cycle`,
`build_scheduler`) stays Protocol-only and is tested against
`FakeMarketStore`. `__main__.py` is the only caller of `build_ingest_fn`
in application code.

## 6. Performance budget: could not obtain a real measurement in this session

**Situation:** this sandboxed session's outbound network policy blocks
`huggingface.co` (`curl` returns a `403` from the egress proxy itself, a
policy denial, not a transient failure — confirmed, not retried per the
proxy's own guidance). `KronosTokenizer.from_pretrained`/
`Kronos.from_pretrained` require downloading real weights from Hugging
Face, so the actual <90s-refresh-budget number for `NeoQuasar/Kronos-small`
at the default `lookback=400, pred_len=24, n_paths=30` on 8-core CPU could
NOT be measured here.

**What was done instead:** `tests/integration/test_real_kronos_engine.py`
— the one real-model integration test the brief allows — is written,
excluded from the default run (`pytest -m "not network"`, now actually
enforced via `addopts`, since the marker existed in `pyproject.toml` but
nothing previously excluded it), and prints the measured single-symbol
wall-clock time plus a naive 6x extrapolation when run with real HF access
(`pytest -m network tests/integration/test_real_kronos_engine.py -v -s`).
Whoever runs it first should paste the real number back into this entry.

**What NOT to do:** guess a number and write it here as if measured. The
brief is explicit ("document the measured time... not a guess") and no
number is more honest than a fabricated one.

**If N=30 turns out too slow when actually measured:** the two documented,
untried levers, in order of preference, are (1) reduce `KMD_MC_PATHS`
(fewer Monte Carlo paths, same model, linear-ish cost reduction) and (2)
switch to `NeoQuasar/Kronos-mini` (4.1M params vs. Kronos-small's 24.7M,
`model_max_context=2048` per `NOTES/kronos_api.md`'s model zoo table) —
both are pure `.env`/`Settings` changes, no code changes, since
`forecast/engine.py` reads model identity entirely from `Settings`. A
further lever noted but NOT implemented: `forecast/engine.py` currently
calls `predict_batch` once PER SYMBOL (batch size = `n_paths`);
`NOTES/kronos_api.md` notes multiple symbols can share one `predict_batch`
call as long as their lookback length matches, which it always does here
(fixed `lookback_bars=400`). Cross-symbol batching (batch size =
`n_paths * n_symbols` in one autoregressive loop) would amortize the
loop's fixed overhead across all 6 configured instruments instead of
paying it 6 times — left as a documented, unimplemented optimization
since the current per-symbol design is simpler, independently cacheable
per symbol (a single symbol's newly-closed bar doesn't force recomputing
every other symbol), and correctness-equivalent; it should only be reached
for if the real measurement above shows the per-symbol design actually
misses the 90s budget.

## 7. red-team Round 1 fixes: non-blocking startup, and weekend-horizon calibration scoring

Two follow-ups after red-team's Round 1 review (`REVIEW.md`), both in
builder-core's scope.

**a. `__main__.py` no longer runs backfill/model-load synchronously before
`uvicorn.run`.** Finding #1 (blocker): the previous version called
`run_full_backfill(...)` then `load_predictor(...)` directly in `main()`,
with no timeout and no `try`/`except`, before the FastAPI app object even
existed - a fully-down network could block for tens of minutes (18
`(instrument, timeframe)` pairs × up to ~100s of retry/backoff each) and
then crash the process outright when `CcxtFetchError`/`YfFetchError`
propagated out uncaught.

**Decision:** `uvicorn.run(...)` now starts immediately; backfill, model
load, and starting the scheduler happen in a background thread
(`kmd.scheduler.run_startup_sequence`, called from `__main__.main()`)
that retries the WHOLE sequence indefinitely on any failure, updating a
new `kmd.api.ReadinessState` at each stage (`starting` ->
`backfilling` -> `loading_model` -> `ok`, or `error` with the exception
message on a failed attempt) rather than ever raising out of the thread.
`/healthz` reads that state directly (a pure in-memory read, no I/O) so
it responds in milliseconds regardless of what the background thread is
doing.

**Alternatives considered:**
- Add a timeout to backfill/model-load and fail `main()` fast instead of
  looping forever. Rejected as the top-level default: a cold start during
  a transient network blip should self-heal once connectivity returns,
  not require a manual process restart. (A bounded `max_attempts` is
  still supported and used by tests for determinism/speed — production
  just doesn't set it.)
- Run backfill/model-load as the scheduler's own first cron tick instead
  of a dedicated startup thread. Rejected: the scheduler's jobs are
  timeframe-refresh-shaped (ingest a bit, forecast, done) and firing them
  before the scheduler itself has even started would need its own
  separate bootstrapping anyway; a plain background thread that owns
  "backfill once, then hand off to the scheduler" is simpler to reason
  about and test in isolation (`run_startup_sequence` takes injected
  `backfill_fn`/`load_predictor_fn`/`start_scheduler_fn` and is tested
  with fakes that sleep and raise, including one real-thread test that
  asserts `/healthz` stays fast throughout).
- Make `/healthz` return a non-200 status while not ready (e.g. 503, like
  `/api/snapshot` already correctly does when no snapshot exists).
  Considered but not required by the finding — the blocker was
  *reachability*, not status-code semantics, and a liveness probe that
  returns non-200 during a long, expected cold start can trigger
  container-orchestrator restarts that make a slow-but-progressing
  startup worse, not better. `/healthz` always returns `200` with a
  `{"status", "ready", "detail"}` body; a caller that wants "ready to
  serve real data" should check `ready`, not the HTTP status.

**Cost:** `main()` no longer fails fast on a permanently-misconfigured
environment (e.g. a typo'd model name) - it retries forever, logging the
same error every `retry_delay_s` (default 30s). Accepted: `/healthz`'s
`error`/`detail` fields make a stuck deployment immediately diagnosable
from the outside, which is strictly better than the previous behavior
(no diagnosis possible, because the process had already crashed with
nothing listening).

**b. FX/metals forecasts whose horizon lands inside a closed weekly
session now resolve against the first bar once trading resumes, instead
of never resolving at all.** Finding #3 (major): `forecast/engine.py`
computes `horizon_ts` by pure bar-count arithmetic
(`last_closed_ts + timeframe_delta * pred_len`) with no session
awareness; for EUR/USD, USD/JPY, GOUD, ZILVER, roughly the last
`pred_len` hours of trading before the weekly close produce a
`horizon_ts` that lands inside the closed weekend window, where a bar can
structurally never exist. The original `score_matured_forecasts` required
an EXACT `ts_utc == horizon_ts` match, so those forecasts sat in
`forecast_log` unscored forever, rescanned every refresh cycle, and were
silently excluded from `CalibrationStats` — a systematic sampling bias in
exactly the numbers the dashboard presents as its trust signal.

**Decision (option (a) from red-team's suggested fixes, not (b)):**
`calibration/score.py::score_matured_forecasts` now scores against the
FIRST `is_closed=True` bar at-or-after `horizon_ts` (and at-or-before the
caller's `now`, preserving the look-ahead invariant exactly), within a
bounded `MAX_HORIZON_CATCHUP` (3 days, matching `quality.py`'s own
`_WEEKEND_ALLOWANCE_S`). If no such bar arrives before that window fully
elapses, the row is marked `unscorable`
(`CalibrationLogger.mark_unscorable`, new `unscorable_at_utc`/
`unscorable_reason` columns, additive schema migration for pre-existing
`forecast_log` files) and excluded from `get_unscored_matured` going
forward — bounding the previously-unbounded scan cost, without silently
pretending the gap never happened (the row and its reason stay on disk,
just out of the pending-scan set).

**Alternatives considered:**
- (b) Teach `forecast/engine.py`/`snapshot.py` session-awareness: detect
  via `is_market_open` that a computed `horizon_ts` would land inside a
  closed session and extend `y_timestamps` to the next in-session bar
  before ever calling Kronos. Rejected as the primary fix: it couples the
  forecast engine (which today has zero session/calendar knowledge, by
  design — see `NOTES/kronos_api.md`, Kronos itself is timezone/session
  agnostic) to `kmd.data.sessions`, changes what "24h-ahead" or
  "`pred_len`-bars-ahead" actually means for those four instruments
  (the model would be asked to predict a *different* number of real
  elapsed hours than `pred_len` on weeks where the horizon would
  otherwise cross a closure), and would need the SAME session logic
  duplicated or shared between the forecast side and the (still
  necessary) scoring side. Scoring-side resolution needs no such
  coupling: the model still forecasts exactly `pred_len` bars ahead as
  configured; only "what counts as the realized outcome for that nominal
  instant" changes, and only in the one place (`score.py`) that already
  owns that decision.
- Score against the exact next `is_market_open`-computed session-open bar
  specifically (rather than "first closed bar at-or-after horizon,
  bounded"). Rejected as unnecessary extra coupling to `kmd.data.sessions`
  for the same outcome: "first real bar after horizon" and "first bar
  after the session reopens" are the same bar in practice for these
  instruments (no bar exists in between, by construction — the market is
  closed), so the simpler, session-agnostic version was chosen.
- Leave `get_unscored_matured` unbounded and only fix the resolution
  logic. Rejected: red-team specifically flagged the unbounded-scan-cost
  half of finding #3 as well ("`get_unscored_matured` should also gain a
  way to mark a row permanently unscorable"), and a genuine multi-day
  data-source outage (not just a weekly close) would otherwise still
  accumulate forever.

**Cost:** the "realized outcome" for a weekend-adjacent forecast is now
"the first traded price once the market reopened" rather than "the price
at exactly `pred_len` hours later" — a small, disclosed definitional
shift (documented here and in `score.py`'s docstring) that trades perfect
temporal precision for actually being able to measure calibration on 100%
of forecasts instead of ~80%, which is the more important property for a
trust signal. `MAX_HORIZON_CATCHUP=3 days` is a judgment call, not a
value derived from the data; a real multi-week exchange holiday could
still exceed it, in which case the affected forecasts are correctly
marked unscorable rather than incorrectly scored against an unrelated
much-later bar.
