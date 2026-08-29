# Review log

Three mandatory rounds per the build brief: correctness, robustness,
quality/usefulness. Each finding is numbered and tracked to a status.

## Round 1 — correctness

Checklist worked item-by-item against the actual code (not builders' own
self-reports); findings numbered, each with severity, a concrete
reproduction, and a suggested fix. Verified-clean items are stated
explicitly, per the red-team brief, rather than omitted.

### Findings

**1. [BLOCKER] `python -m kmd` cannot serve `/healthz` (or anything else)
until backfill AND the Kronos model finish loading — synchronously, with
no timeout, no partial-failure handling, and no "not ready yet" response
in between.**

Confirmed by tracing `src/kmd/__main__.py::main()` line by line:

```python
run_full_backfill(markets_config, registry, store)      # network I/O, all instruments
predictor = load_predictor(settings)                     # downloads from Hugging Face
...
scheduler.start()
app = create_app(snapshot_store.load)
uvicorn.run(app, host=settings.host, port=settings.port)  # ONLY reached after both above
```

`create_app()` (`src/kmd/api.py`) is where `/healthz` is defined — the
`FastAPI` app object itself does not exist, let alone bind a port, until
`run_full_backfill` and `load_predictor` have both returned. There is no
`try`/`except` around either call in `main()`.

Blast radius, traced concretely rather than assumed:
- `run_full_backfill` → `ingest_instrument` → `_fetch_with_fallback` →
  `with_retry(..., policy=BackoffPolicy(max_attempts=5, base_delay_s=0.5,
  max_delay_s=20.0))`. A single fully-unreachable source (this sandbox;
  also a real cold start against a flaky/rate-limited exchange) makes each
  (instrument, timeframe) pair retry for up to roughly 5 × 20s ≈ 100s
  before raising `CcxtFetchError`/`YfFetchError` **uncaught** out of
  `run_full_backfill` (`ingest.py` has no per-instrument
  try/except around the fetch call) — this races through 6 instruments × 3
  timeframes = 18 pairs, so a fully-down network can block for tens of
  minutes before the process crashes outright with no server ever having
  bound a port — worse than "hangs forever", since it eventually dies with
  a traceback and no explanation on `/healthz` (which never existed to
  answer in the first place).
- Even with network reachable: `load_predictor` downloads real HF weights
  synchronously with no timeout; a slow/degraded HF endpoint on a cold
  start blocks the entire API — including `/healthz` — for that whole
  duration.
- Reproduced directly by the manager: `cp .env.example .env && python -m
  kmd` in this sandbox → `/healthz` connection-refused after several
  seconds (network egress to huggingface.co/binance/yahoo is proxy-blocked
  here, confirmed via `$HTTPS_PROXY/__agentproxy/status`, not guessed).

This directly violates the brief's "degrades visibly, never blocks"
requirement and the "full refresh cycle runs end-to-end" / clean-checkout
gates in this file.

**Suggested fix** (left to builder-core — this is a real restructuring of
`__main__.py`/`scheduler.py`, out of red-team's fix scope per the
manager's brief): bind and serve the FastAPI app **first** —
`uvicorn.run(...)` (or start it in a background thread/task) before
backfill or model loading ever run. Give `/healthz` a real state machine
(`starting` → `backfilling` → `loading_model` → `ready`, or simply
`{"status": "starting"}` vs `{"status": "ok"}`) backed by a module-level
flag, and move `run_full_backfill`/`load_predictor` into the scheduler's
first tick (or a background thread kicked off after `uvicorn` binds).
`GET /api/snapshot` already correctly returns `503` when no snapshot
exists yet (`api.py`) — the same "not ready, not broken" pattern should
apply to the whole startup sequence, not just the snapshot endpoint.
**Not fixed by red-team** — architecturally out of this pass's scope per
the manager's own instructions.

**2. [MAJOR] No `README.md` exists anywhere in this repository** (`find
... -iname "*.md"` at the repo root returns only `BACKLOG.md`,
`DECISIONS.md`, `REVIEW.md` — no README at all, not even a stub).

This is why finding #1 above went undetected until the manager guessed at
run instructions manually (`cp .env.example .env && python -m kmd`) —
there is no documented "how to run this" anywhere, so the literal
"clean-checkout, README-followed" verification this file's own Gates
section requires cannot even be attempted. `.env.example` documents
config keys but is not a substitute for setup/run instructions (model
download size/time expectations, first-run backfill duration, how to tell
the dashboard is ready, etc.).

**Reproduction:** `find /home/user/Kronos -maxdepth 1 -iname "README*"`
→ no results.

**Suggested fix:** a `README.md` covering: prerequisites, `cp
.env.example .env`, `python -m kmd`, what a cold start actually does
(backfill + HF model download — currently blocking, see #1) and how long
to expect it to take, how to confirm readiness, and where the dashboard
is served. Left to builder-core/whoever owns the next pass — writing
project documentation is outside red-team's mandate, but the gap itself
must be on record since it blocks one of this file's own Gates.

**3. [MAJOR] Every week, roughly 20% of FX/metals-futures forecasts get a
`horizon_ts` that lands inside that instrument's closed weekend session —
a bar can never exist at that exact timestamp, so the forecast can never
be scored. It accumulates in `forecast_log` forever and is re-scanned on
every refresh cycle.**

Traced end-to-end, not assumed:
- `config/markets.yaml`'s `fx` session: open Sun 22:00 UTC, closed Fri
  22:00 → Sun 22:00 UTC (a 48h weekly closure). `metals_futures` is
  similar (Sun 23:00 America/Chicago → Fri 22:00 America/Chicago).
- `forecast/engine.py::_future_timestamps` computes
  `horizon_ts = last_closed_ts + timeframe_delta * pred_len` with **no
  session awareness at all** — it has no way to know the horizon it just
  computed falls on a weekend.
- Default config: `pred_len=24`, `timeframe=1h`. Any forecast whose
  `last_closed_ts` falls in the **last 24 hours of trading before Friday's
  close** (i.e. `last_closed_ts` in `[Thu 22:00 UTC, Fri 22:00 UTC)` for
  `fx`) produces a `horizon_ts` that lands inside `[Fri 22:00 UTC, Sat
  22:00 UTC)` — squarely inside the closed weekend window. No bar will
  *ever* be written at that `ts_utc` for that symbol (the market is
  closed; sources return nothing there; nothing is ever fabricated, per
  `base.py`'s own rule) — the forecast is permanently unscorable.
- That is 24 of ~120 trading hours per FX week, i.e. **every EUR/USD and
  USD/JPY forecast generated in the last day before the weekend, forever,
  every single week** (same mechanism for GOUD/ZILVER against the
  `metals_futures` session).
- `calibration/logger.py::get_unscored_matured` has no upper bound and no
  way to mark a row "unscorable" — `calibration/score.py::
  score_matured_forecasts` re-fetches `store.get_latest_bars(...,
  bars_lookup_limit=1000)` for **every one of these rows, every single
  refresh cycle, forever** — an unbounded, permanently-growing scan cost
  (~24 rows/instrument/week × 4 non-crypto instruments × 52 weeks/year ≈
  5000 dead rows/year, each rechecked hourly).
- **Consequence for calibration correctness** (this is the checklist's
  own concern, not just a performance note): `CalibrationStats` for
  EUR/USD, USD/JPY, GOUD, ZILVER is computed only from the ~80% of
  forecasts whose horizon happened to avoid the weekend — a systematic,
  silent sampling bias in exactly the kind of number (`brier_score`,
  `mae_q50`, `band_coverage`) the dashboard presents as this system's
  trust signal.
- Verified this is a genuinely untested gap: no test in
  `tests/unit/calibration/test_score.py` or elsewhere constructs a
  forecast whose horizon crosses a non-24/7 session's weekend closure.

**Suggested fix** (a scoring-semantics decision, left to builder-core):
score against the first available `is_closed=True` bar at-or-after
`horizon_ts` (within some bounded window, e.g. the next session's open +
a few bars) instead of requiring an exact `ts_utc` match, OR have
`build_snapshot`/`_get_or_compute_forecast` skip caching+logging a
forecast whose computed `horizon_ts` is known (via `is_market_open`) to
land inside a closed session for that instrument, and instead extend the
horizon to the next in-session bar before computing `y_timestamps`. Either
way, `get_unscored_matured` should also gain a way to mark a row
permanently unscorable (e.g. after some grace period well past any
plausible catch-up) so the query doesn't scan a forever-growing dead set.
**Not fixed by red-team** — this changes forecast/scoring semantics,
outside this pass's fix scope.

**4. [MINOR — fixed directly, this pass] `fmtDateTime` in `web/app.js`
formatted every timestamp via `Intl.DateTimeFormat("nl-NL", {...})` with
no `timeZone` option, so every displayed time (last update, forecast
generated-at, last-closed-candle) silently used the *viewing browser's*
local timezone rather than the brief's required fixed Europe/Amsterdam
presentation timezone.** Every other layer in the system is correctly
UTC-internal-only, Amsterdam-at-the-edge in intent (`sessions.py` uses
real `zoneinfo` conversions, with an actual DST-boundary test in
`tests/unit/data/test_sessions.py::test_metals_dst_shift_winter_vs_
summer_chicago`) — this was the one presentation-layer spot that assumed
rather than declared the target timezone. A viewer whose OS/browser is
not set to Europe/Amsterdam (a remote session, a different regional
setting, a misconfigured clock) would see every timestamp mislabeled
without any indication.

**Reproduction:** `grep -n "Amsterdam" -r .` (outside `.claude/agents/`)
returned nothing before this fix — the string never appeared anywhere in
the actual application code.

**Fix applied:** added `timeZone: "Europe/Amsterdam"` to the one
`Intl.DateTimeFormat` call in `fmtDateTime` (`web/app.js`). No test
harness exists for the vanilla-JS frontend (no `.test.js` files, no
`package.json` at the repo root) so there is no automated regression test
possible for this without introducing a JS test runner, which is out of
scope for a one-line display fix — flagging this explicitly rather than
claiming a regression test exists.

### Checklist items verified clean (explicitly, per the brief's own
instruction not to omit these)

**Look-ahead bias — verified clean.** Traced `is_closed` end-to-end:
`Bar.is_closed` is set once, conservatively, in `timeutil.py::
compute_is_closed` (any ambiguity resolves to `False`) at the two source
adapters (`ccxt_source.py`, `yfinance_source.py`, including the
`H4` resample path in `yfinance_source.py::_resample_h1_to_h4`, which
additionally forces the trailing bucket unclosed regardless of the
per-member check). `forecast/engine.py::select_closed_lookback` filters
to `is_closed=True` bars only, with a redundant explicit re-check
(`UnclosedBarError`) immediately after, and `run_monte_carlo` derives
`last_closed_ts` from `window[-1].ts_utc` (the filtered window's own last
element), never from `datetime.now()`. `forecast/cache.py::
ForecastCacheKey.digest()` hashes `last_closed_ts` (plus model/sampling
params) — no wall-clock component anywhere in the key. Confirmed there is
no second code path into `run_monte_carlo`/the cache that could supply an
unfiltered bar list — `snapshot.py::_get_or_compute_forecast` is the only
caller, and it passes `bars` straight from `store.get_latest_bars(...)`
into `run_monte_carlo`, which itself re-filters via
`select_closed_lookback`.

**Timezones/DST (data layer) — verified clean.** `Bar.ts_utc` is
validated tz-aware-UTC-only at the model boundary (`base.py::
must_be_utc_aware`, offset must be exactly 0). `sessions.py::
is_market_open` converts `now_utc` into the session's real IANA timezone
*per call* via `zoneinfo` (never a precomputed fixed offset) before
comparing against the configured weekly window — confirmed by hand-tracing
`test_metals_dst_shift_winter_vs_summer_chicago`, which asserts the same
local "Sunday 23:00" open lands at two different UTC instants (05:00 UTC
in January CST vs. 04:00 UTC in July CDT) and that `is_market_open` gets
both right. `BackgroundScheduler(timezone=ZoneInfo("UTC"))` in
`scheduler.py` means cron trigger firing times are themselves DST-immune.
The one presentation-layer gap found is finding #4 above (now fixed).

**Calibration correctness — independently re-verified by hand, not taken
on faith.** Constructed a 3-observation synthetic example script (run in
this sandbox, output captured):

```
case: p_up=0.9, q10=95, q50=100, q90=110, last_close=90,  realized=105 (up,   in-band)
  -> brier=0.01, mae=5,  in_band=True
case: p_up=0.2, q10=95, q50=100, q90=110, last_close=100, realized=90  (down, below q10)
  -> brier=0.04, mae=10, in_band=False
case: p_up=0.5, q10=95, q50=100, q90=110, last_close=100, realized=100 (flat->"down", in-band)
  -> brier=0.25, mae=0,  in_band=True

mean brier    = 0.0999999... ≈ 0.10  (hand calc: (0.01+0.04+0.25)/3 = 0.10)   ✓ matches
mean mae      = 5.0                   (hand calc: (5+10+0)/3 = 5.0)          ✓ matches
band coverage = 0.6667                (hand calc: 2/3)                        ✓ matches
```

`score.py::score_single`'s formulas (`(p_up - actual_up)**2`,
`abs(q50 - realized_close)`, `q10 <= realized_close <= q90`) and
`aggregate_calibration_stats`'s plain means/fraction all matched hand
computation exactly (float noise only, e.g. `0.009999999999999995`).
`horizon_quantiles` uses `np.percentile`, whose monotonicity guarantees
`q10 <= q50 <= q90` by construction, so `band_width_pct`/band-coverage
can never see an inverted band.

**Second look-ahead check (scoring side) — verified clean, with one real
caveat filed as finding #3.** `score_matured_forecasts` re-verifies a
`b.ts_utc == record.horizon_ts and b.is_closed` match against the live
store before ever scoring — a forecast whose horizon has "matured" by
`now` but has no real closed bar yet is correctly left unscored for a
later cycle, never scored against a still-forming or fabricated bar. The
gap is not that this check is wrong — it's that for non-24/7 instruments,
the required bar can structurally never arrive for ~20% of forecasts
(finding #3), which this correct-but-unconditional check doesn't detect
or route around.

**Gaps/non-24/7 markets — mixed: the data-layer half is solid, the
forecast/calibration half has the bug in finding #3.**
`quality.py::check_quality`'s weekend allowance
(`_WEEKEND_ALLOWANCE_S = 3 days`, gated on `always_open`, looked up per
symbol from `markets_config` in `store.py`) is correctly scoped and
tested (`test_weekend_gap_tolerated_for_non_always_open_instrument`,
`test_same_weekend_gap_flagged_for_always_open_instrument`) — a real
weekly FX/metals gap does not spuriously trip the quality gate, but the
same-sized gap on a crypto (`always_open=True`) instrument correctly
still does. What is NOT handled anywhere is the forecast-horizon side of
the same gap — see finding #3.

**`config/markets.yaml`'s `index`-session single-weekly-window limitation
(DECISIONS.md #4d) — assessed: currently dormant/inert, not actually
misleading on the dashboard today, but only because of a separate,
undocumented scope gap.** Traced where `context`-group instruments (DXY,
S&P 500, the only two instruments using the `index` session) actually
surface: `snapshot.py::build_snapshot` explicitly filters
`i.group != _CONTEXT_GROUP` before ever computing a `DataSourceStatus`
(which is the only place `market_session_open` is populated) — so DXY/S&P
never appear in `SnapshotDTO.assets` at all, and grepping `web/*.{html,js}`
for "context"/"DXY"/"overlay" returns **zero matches**. The "analysis
overlay" `config/markets.yaml` itself promises for the `context` group
(comment: "shown as an analysis overlay ... never as a first-class
forecast tile") was never actually built — so today, the `index` session's
placeholder-single-window limitation cannot mislead anyone because there
is no UI surface it feeds. This is fine to leave as builder-data flagged
it (a schema decision, correctly not made unilaterally) **but the manager
should be aware the overlay feature itself is simply missing**, not just
imprecise — the moment someone wires DXY/S&P into any UI element that
reads `market_session_open`, the Monday-only-window bug becomes real and
user-visible. Filed here as a heads-up, not a numbered finding, since
nothing incorrect is currently displayed.

**Data integrity (SQLite UPSERT + quality gate) — verified clean via
adversarial fixtures that already exist and pass.** Read
`tests/unit/data/test_quality.py` and `test_store.py` directly (not just
their names): both contain genuinely adversarial cases —
`test_revised_closed_history_is_flagged` /
`test_unclosed_bar_updating_in_place_is_not_flagged_as_revised` (a
still-forming bar changing every fetch must NOT trip `revised_history`;
an already-closed bar changing MUST), `test_duplicate_timestamp_within_
incoming_batch_is_flagged`, `test_out_of_order_incoming_batch_is_flagged`,
`test_mismatched_symbol_in_incoming_raises`, and property-style fuzz tests
(`test_check_quality_never_crashes_on_arbitrary_batches`,
`test_store_stays_internally_consistent_under_adversarial_batches`). The
`ON CONFLICT(symbol, timeframe, ts_utc) DO UPDATE` upsert is exactly
right for the "still-forming bar gets overwritten in place" pattern, and
`_bar_ts_key`'s `timespec="microseconds"` forcing avoids the
zero-microsecond ISO-8601 lexical-sort trap (verified this reasoning
myself: Python's `isoformat()` omits the fractional part when it is
exactly zero, which — without the forced timespec — would make two
same-instant-adjacent timestamps of different microsecond-presence sort
incorrectly under `ORDER BY ts_utc` as plain SQLite TEXT).

**Float/decimal precision — verified clean.** `config/markets.yaml`'s
per-instrument `decimals` (XRP 4, JPY 3, gold/BTC/XRP-adjacent 2, silver
3, EUR/USD 5) is threaded through correctly: `dto.py`'s `AssetSnapshot.
decimals` carries it, and every price/level display call in `web/app.js`
(`fmtNumber(asset.price, asset.decimals)`, `renderLevels(...,
asset.decimals)`, `renderSetup(..., asset.decimals)`, the fan-chart
quantile legend) routes through that field — grepped the whole frontend
for `toFixed`/hardcoded decimal counts and found only SVG pixel-coordinate
rounding (`x.toFixed(2)` for chart geometry, not price data), never a
naive price rounding. `analysis/levels.py::round_numbers` is the one place
Python itself rounds a price for display and it correctly takes
`decimals` as a parameter (`round(below, decimals)`) rather than a
hardcoded value. `SnapshotDTO`/`ForecastMetrics`/`Level`/`SetupCard`
intentionally carry full-precision floats end-to-end (rounding only ever
happens at the final display step), which is the right design — it means
`calibration/score.py`'s math (finding above) is never contaminated by a
premature rounding.

**Failure modes — mostly solid, with the one architectural gap already
covered as finding #1.** `resilience.py`'s `BackoffPolicy` +
`CircuitBreaker` + `MinIntervalLimiter` are dependency-free, fully
clock/sleep/rng-injected, and used consistently by both source adapters;
a source that is down or rate-limited correctly opens its breaker,
short-circuits further calls, and reports `SourceHealth` — which
`snapshot.py::_build_source_status` correctly surfaces per-tile as
`is_stale`, never masked as a live number. `build_snapshot` wraps each
instrument's whole per-asset pipeline in `try/except Exception: ... skip`
so one bad symbol never blanks the dashboard, and the scheduler's `_job`
wrapper in `scheduler.py` does the same at the refresh-cycle level once
the process is actually running. The one place this discipline does NOT
apply is `main()`'s startup sequence itself — finding #1.

**Security — verified clean.** `.env` is git-ignored (confirmed via
`.gitignore` and `git status --ignored`, which shows the working-tree-only
`.env` from the manager's own manual smoke test as `!!` / ignored, never
staged); `git log --all --diff-filter=A --name-only | grep -i '\.env'`
across the whole history returns only `.env.example`, never a real `.env`
— nothing was ever committed. Read the actual (real, filled-in-by-the-
manager) `.env` on disk directly: every credential field is blank
(`KMD_CCXT_API_KEY=`, `KMD_CCXT_API_SECRET=`, `KMD_LLM_API_KEY=`), so even
the untracked working-tree copy holds no live secret. Grepped
`src/`+`web/`+`config/` for hardcoded credential-shaped strings and found
none. `config.py`/`.env.example`'s own comments correctly assert
read-only, market-data-only scopes; `ingest.py::build_default_source_
registry`'s docstring explicitly notes the one configured ccxt key pair is
never assumed to work against a *fallback* exchange either — no
credential-scope confusion across primary/fallback routing.

## Gates status (this round's honest read)

- [x] `ruff check .` — clean (re-run after this round's one fix)
- [x] `python -m mypy` — clean, 30 files (re-run after this round's fix;
      the fix touched only `web/app.js`, outside mypy's scope, so this was
      never at risk)
- [x] `python -m pytest tests -q` — 178 passed, 1 deselected (`network`),
      unchanged by this round's fix
- [ ] Full refresh cycle end-to-end on a clean checkout within budget —
      **cannot pass**: finding #1 means the process may never reach a
      running state at all on a slow/degraded network, and the
      performance budget itself was never measured (DECISIONS.md #6,
      network-blocked in this sandbox, unrelated to red-team)
- [ ] Calibration log holds real forecasts; scoring code proven correct on
      a synthetic example — **scoring code correctness: yes**, verified by
      hand above; **"holds real forecasts" representatively: no**, finding
      #3 means a real, structural ~20%/week slice of FX/metals forecasts
      can never be scored, biasing the calibration numbers the dashboard
      shows as its trust signal
- [ ] All blockers/majors from this file closed — **no**: #1, #2, #3 open
- [ ] red-team sign-off: "geen openstaande blockers" — **not given this
      round**, see below
- [ ] README start instructions literally followed on a clean environment
      — **cannot even be attempted**: no README exists (finding #2)

## Sign-off

**Ik kan hier nog geen "geen openstaande blockers" schrijven.** One
blocker (#1: synchronous, unbounded startup sequencing that can leave the
API completely unreachable, or crash the process outright, before ever
serving `/healthz`) is open and unfixed by design (it is a real
restructuring, correctly left for a builder-core follow-up per this pass's
scope rules, not something red-team should patch unilaterally). Two majors
(#2: no README at all; #3: a structural, recurring calibration-scoring gap
for every non-24/7 instrument) are also open. One minor (#4) was fixed
directly this round. Round 1 is otherwise a clean pass on look-ahead bias,
timezone/DST handling, calibration math, data-integrity adversarial
testing, float precision, and security — each explicitly checked and
recorded above, not asserted by omission.

## Round 2 — robustness (fault injection)

_Not started._

## Round 3 — quality and usefulness

_Not started._

## Gates

- [ ] `ruff check` and `mypy --strict` zero errors
- [ ] `pytest` fully green, no network access
- [ ] Full refresh cycle end-to-end on a clean checkout within the performance budget
- [ ] Calibration log holds real forecasts; scoring code proven correct on a synthetic example
- [ ] All blockers/majors from this file closed; minors explicitly accepted or filed
- [ ] red-team sign-off: "geen openstaande blockers" (only once true)
- [ ] README start instructions literally followed on a clean environment and worked
