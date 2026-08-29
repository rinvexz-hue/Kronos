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

> **Status: fixed by builder-core**, per the manager's dispatch after this
> round. `__main__.py::main()` now starts `uvicorn.run(...)` immediately;
> backfill, model load, and starting the scheduler run in a background
> thread (`kmd.scheduler.run_startup_sequence`) that retries the whole
> sequence indefinitely on any failure (never raises out of the thread)
> and updates a new `kmd.api.ReadinessState` at each stage. `/healthz`
> reads that state directly — a pure in-memory read — so it responds in
> milliseconds regardless of backfill/model-load progress or failure.
> `tests/unit/test_scheduler.py::test_healthz_responds_immediately_while_
> startup_is_slow_and_failing` runs this in a real background thread
> against a backfill that sleeps and a predictor loader that raises once
> then succeeds, polling `/healthz` throughout and asserting every
> response stays under 200ms. See `DECISIONS.md` #7a for the alternatives
> considered (bounded retries, a non-200 `/healthz` status while not
> ready) and why they were rejected. Not independently re-verified by
> red-team yet — that re-verification is Round 2's job, not a
> self-certification here.

Confirmed by tracing `src/kmd/__main__.py::main()` line by line (as it
stood at review time — see the fix note above for the current shape):

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

> **Status: fixed by builder-core**, per the manager's dispatch after this
> round, using suggested fix (a) below (score against the first available
> bar, not an exact match) rather than (b). `calibration/score.py::
> score_matured_forecasts` now resolves a matured forecast against the
> first `is_closed=True` bar at-or-after `horizon_ts` (still at-or-before
> `now`, preserving the look-ahead invariant), within a new
> `MAX_HORIZON_CATCHUP` (3 days, matching `quality.py`'s own weekend-gap
> allowance). A forecast still unresolved once that window fully elapses
> is marked `unscorable` (new `CalibrationLogger.mark_unscorable`,
> `unscorable_at_utc`/`unscorable_reason` columns, additive migration for
> existing `forecast_log` files) and excluded from `get_unscored_matured`
> going forward, which also closes the unbounded-scan-cost half of this
> finding. Regression test:
> `tests/unit/calibration/test_score.py::test_score_matured_forecasts_
> resolves_weekend_horizon_against_first_reopen_bar` constructs an
> EUR/USD forecast whose horizon lands on a Saturday (inside the `fx`
> session's Friday-22:00-to-Sunday-22:00-UTC closure) and asserts it
> resolves against the Sunday-evening reopen bar rather than staying
> pending forever; a second test
> (`test_score_matured_forecasts_marks_unscorable_after_catchup_window_
> elapses`) covers the genuine-outage case. See `DECISIONS.md` #7b for why
> (a) was chosen over (b) and the cost this trades away (temporal
> precision of "realized outcome" for weekend-adjacent forecasts, in
> exchange for actually being able to score them). Not independently
> re-verified by red-team yet.

Traced end-to-end, not assumed (as of review time — see the fix note
above for the current behavior):
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

### Post-Round-1 builder-core follow-up (not a red-team self-certification)

Findings #1 and #3 above were dispatched to builder-core after this round
and are now marked fixed inline (see the status notes on each finding and
`DECISIONS.md` #7a/#7b). `ruff check .`, `python -m mypy`, and
`python -m pytest tests -q` all pass after both changes (187 passed, 1
deselected). Finding #2 (no README) remains open — out of builder-core's
scope, tracked separately. **These fixes are builder-core's own report,
not a red-team re-verification** — the checkboxes below and the sign-off
statement stay exactly as red-team left them until Round 2 (or a
dedicated re-check) actually confirms the fixes hold up, including under
fault injection (e.g. does the startup thread actually recover if the
network comes back mid-backoff; does a real weekend gap in a fixture
resolve the way the new regression test claims against the real
`SqliteStore`, not just `FakeMarketStore`).

### Manager verification + finding #2 closure (post-fix, pre-Round-2)

Independently re-ran (not trusting builder-core's self-report alone):
`ruff check .`, `python -m mypy` (note: use `python -m mypy`, not the bare
`mypy` binary — that binary is an isolated `uv tool install` with no
visibility into project dependencies and will falsely report every
third-party import as missing), and `python -m pytest tests -q` — all
clean, 187 passed / 1 deselected, matching builder-core's report exactly.

Then actually ran the app end-to-end (`cp .env.example .env && python -m
kmd`) on what was otherwise a clean checkout, which found one more real
bug builder-core's fix didn't touch: **`SqliteStore.__init__` never
created its `db_path`'s parent directory**, so the default
`KMD_DB_PATH=./data/kmd.sqlite3` crashed the (now-background) startup
thread with `sqlite3.OperationalError: unable to open database file` on
any checkout where `./data/` doesn't already exist — which is every fresh
clone, since it's gitignored runtime state. `ForecastCache` and
`CalibrationLogger` already did `db_path.parent.mkdir(parents=True,
exist_ok=True)` for exactly this reason; `SqliteStore` (written earlier,
before that pattern existed) was missing the same one line. Fixed
directly (commit `6630269`) — small, unambiguous, matches an existing
in-codebase pattern exactly, same bar `red-team`'s own brief uses for a
direct fix.

Re-verified live after that fix: `/healthz` reachable within ~5s of
process start (Python/torch/pandas import time, not network-dependent)
reporting `{"status":"backfilling","ready":false}`; `/api/snapshot`
correctly `503`s with `{"detail":"snapshot not yet available"}` rather
than lying; the static frontend at `/` serves immediately. This is the
first time in this project that "run the app for real" was verified to
actually work past process start, on a genuinely fresh (`rm -rf ./data`)
state, not just via `pytest`.

**Finding #2 (no README) — closed.** Wrote `README.md`: prerequisites,
the 3-command setup, an explicit description of what the fixed
non-blocking startup sequence does and how to read `/healthz`'s
`status` progression, how to run the tests, a short architecture pointer
into the contract files, and a "Beperkingen" section stating plainly (in
Dutch, matching the dashboard's own language) what this system does not
do — including the still-open items below rather than glossing over them.

None of this is a red-team re-verification or a Round 2 fault-injection
pass — it's the manager's own integration check, kept separate from
red-team's findings above on purpose. The Gates checklist immediately
above this section is left exactly as red-team wrote it; Round 2 is
still the pass that gets to move those checkboxes.

## Round 2 — robustness (fault injection)

Per the brief: actually inject each failure (real modules, fakes/mocks only
at the network edge) and confirm the system degrades **visibly** — never
crashes uncaught, never shows a stale/fabricated number as live, never
silently corrupts state. Every case below was triggered for real (a
script or a test that fails on the pre-fix code and passes after,
committed alongside this file) — nothing here is asserted from a static
read alone.

### Findings

**1. [MAJOR — fixed directly, this pass] A single persistently-failing
source (rate-limited/circuit-open, no fallback configured) aborted
ingestion for every OTHER instrument queued behind it in the same
refresh cycle, not just its own.**

`resilience.py`'s `CircuitBreaker`/`BackoffPolicy` themselves work exactly
as designed — confirmed by re-running the existing
`test_circuit_breaker_opens_after_repeated_failures_across_calls` /
`test_persistent_failure_raises_ccxt_fetch_error_and_updates_health` and
by direct injection (a fake exchange raising a simulated `429` on every
call): the breaker opens after `failure_threshold` consecutive failures,
`SourceHealth.ok` correctly flips to `False`, and `consecutive_failures`
increments. The gap was one level up: `data/ingest.py::run_full_backfill`/
`run_incremental_update` loop over every `(instrument, timeframe)` pair
with **no per-pair `try`/`except`** — only `_fetch_with_fallback` catches
`_FETCH_ERRORS`, and only to try a configured fallback; with no fallback
(or a failing one), the exception propagates all the way out of the
loop, uncaught.

**Reproduction:** `SourceRegistry` with two crypto instruments, one
("AAA/USDT") wired to a source that always raises, one ("BBB/USDT") to a
healthy source, no fallback for either. Calling
`run_incremental_update(config, registry, store)` raised the first
instrument's exception straight out of the function — confirmed the
**healthy** source (`BBB/USDT`) was never even called
(`healthy.calls == 0`) and zero bars were stored for it, despite its own
source being perfectly fine. `scheduler.py`'s outer `_job()` try/except
prevented a process crash, but the *entire* refresh cycle silently did
nothing for every instrument that cycle — not the "one bad tile degrades,
everything else keeps working" behavior Round 1 verified for
`snapshot.py::build_snapshot` (a different layer, using already-stored
bars — it was never exercised against a live source failure).

**Fix applied:** `run_full_backfill`/`run_incremental_update` now wrap each
`ingest_instrument(...)` call in its own `try`/`except Exception`,
logging and `continue`-ing to the next pair — the same per-instrument
isolation pattern `snapshot.py::build_snapshot` already uses one layer up,
applied at the layer that actually needed it. Regression tests:
`tests/unit/data/test_ingest.py::test_run_full_backfill_isolates_one_persistently_failing_instrument`
and `test_run_incremental_update_isolates_one_persistently_failing_instrument`
— both fail on the pre-fix code (`healthy.calls == 0`) and pass after.

**2. [MAJOR — fixed directly, this pass] A malformed/poisoned row from
either source adapter raised an untyped exception (or, for a NaN value,
silently produced a corrupted `Bar`) instead of a well-typed
`CcxtFetchError`/`YfFetchError`, and was incorrectly recorded as a
successful fetch.**

Both `CcxtSource.fetch_ohlcv` and `YfinanceSource.fetch_ohlcv` only wrapped
the network call itself (`with_retry(_do_fetch, ...)`) in a try/except;
`Bar` construction (`_row_to_bar` / `_frame_to_bars` / `_resample_h1_to_h4`)
ran *after* that block, with `breaker.on_success()` already called.

**Reproduction (direct injection against the real adapters, fakes only at
the exchange/ticker boundary):**
- A fake ccxt exchange returning a truncated row (`[ts, open, high]`,
  missing low/close/volume) raised a raw `IndexError` — not
  `CcxtFetchError` — and `source.health()` afterward reported `ok=True,
  consecutive_failures=0`: a fetch that produced zero usable bars was
  recorded as **healthy**.
- A fake yfinance ticker returning a frame with `Close=NaN` for one row (a
  real yfinance behavior for a genuinely illiquid period, not
  hypothetical) returned `Bar(close=nan, is_closed=True)` with no error,
  no warning, `health.ok=True` — the exact "malformed response accepted
  as valid data" failure mode the brief calls out.
- Because these failures were untyped (or non-existent), they were also
  invisible to `_fetch_with_fallback`'s fallback routing (`_FETCH_ERRORS`
  doesn't recognize a raw `IndexError`) and, per finding #1's original
  bug, could have aborted the whole ingest cycle for other instruments too.

**Fix applied:** widened both adapters' try/except to also cover bar
construction, so any conversion failure (truncated row, bad dtype, or a
NaN/Inf value now rejected by finding #3's new `Bar` validator) is
uniformly wrapped into `CcxtFetchError`/`YfFetchError` and correctly
recorded via `breaker.on_failure(...)` — never `on_success()` for a fetch
that didn't actually produce valid bars. Regression tests:
`test_ccxt_source.py::test_malformed_row_is_raised_as_ccxt_fetch_error_not_silently_returned`,
`test_nan_close_is_raised_as_ccxt_fetch_error_not_silently_returned`,
`test_yfinance_source.py::test_nan_close_is_raised_as_yf_fetch_error_not_silently_returned`
— all fail on the pre-fix code and pass after.

*Left as documented, not fixed:* an **empty** response (`[]`, zero rows —
distinct from a malformed non-empty one) is still treated as a successful
fetch with zero bars, for both adapters
(`test_yfinance_source.py::test_empty_response_is_not_an_error_but_yields_zero_bars`
documents this explicitly). This is almost certainly correct for an
*incremental* update (no new bars since last check is normal) but
ambiguous for a *first backfill* (zero bars could mean "genuinely no
history yet" or "the source silently returned garbage"). Deciding whether
`ingest_instrument` should treat "zero bars on a full backfill" as a
distinct, flaggable condition is a data-layer semantics decision, not a
mechanical fix — left for builder-data. Its downstream consequence today:
such an instrument's tile simply never appears on the dashboard
(`InsufficientDataError` → skipped in `build_snapshot`, before
`DataSourceStatus`/`is_stale` is ever computed for it) — a silent
*omission*, not a wrong/fabricated number, so lower severity than findings
#1/#2, but still worth a human noticing eventually.

**3. [MAJOR — fixed directly, this pass] A NaN (or ±Inf) value anywhere in
a `Bar`'s OHLCV fields, or in a computed `ForecastMetrics`, was accepted
by pydantic with no validation and could silently corrupt the persisted
snapshot.**

Traced the full chain, confirmed at each step:
- `Bar.open/high/low/close/volume: float` had no finiteness check — a NaN
  close from yfinance (finding #2) passed straight through pydantic
  construction into a "valid" `Bar`.
- `ForecastMetrics`'s numeric fields had the same gap — a NaN could arise
  independently of bad input data too (e.g. genuine model-output
  instability), so a `Bar`-level guard alone would not have been
  sufficient.
- `SnapshotDTO.model_dump(mode="json")` (what `api.py`'s
  `/api/snapshot` handler calls) keeps a NaN as a Python `float('nan')`;
  FastAPI's own `jsonable_encoder` happens to convert that to JSON `null`
  before responding, so a live NaN reaching a request in-memory did not
  itself 500 — confirmed directly via `TestClient`.
- **But** `SnapshotFileStore.save()` (what the scheduler actually calls)
  uses `model_dump_json()` instead, which pydantic-core also turns into
  JSON `null` on write — and `ForecastMetrics.p_up_24h` (etc.) is typed
  `float`, **not** `float | None`. Confirmed directly: writing a NaN-
  containing `SnapshotDTO` via `model_dump_json()` then reading it back
  via `json.loads` + `SnapshotDTO.model_validate(...)` (exactly what
  `SnapshotFileStore.load()` does, called fresh on every
  `/api/snapshot`/`/api/asset/...` request) raises an **uncaught**
  `pydantic.ValidationError` — `"Input should be a valid number
  [type=float_type], input_value=None"`. Nothing in `api.py` catches this,
  so every subsequent request to either endpoint would 500 — for **every**
  asset, not just the one with the bad forecast — until the next scheduled
  refresh happens to overwrite the file with clean data (which could be a
  full hour away, or never, if the underlying cause recurs every cycle).

**Fix applied:** added a `field_validator` rejecting NaN/±Inf to `Bar`
(`data/base.py`) and to `ForecastMetrics`'s six numeric fields (`dto.py`).
The `ForecastMetrics` validator is the one that actually closes the loop:
it fires inside `snapshot.py::_get_or_compute_forecast`, itself inside
`build_snapshot`'s existing per-instrument `try`/`except` — so a NaN
forecast now degrades to "this one tile skipped, logged, this cycle"
(the same path `InsufficientDataError` already takes) instead of
corrupting the persisted snapshot for every other asset. Confirmed with a
new `PredictorProtocol` fake that returns NaN paths for exactly one
instrument: `test_snapshot.py::test_build_snapshot_isolates_one_instrument_whose_forecast_comes_back_nan`
asserts the healthy instrument still appears and the resulting
`SnapshotDTO` round-trips through pydantic JSON cleanly. Direct validator
tests: `tests/unit/test_dto.py` (parametrized over all six fields, plus
±Inf). All fail on the pre-fix code and pass after.

Separately confirmed clean (no fix needed): a degenerate all-identical-
price series does **not** NaN out `historical_realized_vol` (log-returns
are all exactly `0.0`, std of zeros is `0.0`, not NaN) or
`horizon_quantiles` (`np.percentile` on a single repeated value returns
that value, no division involved) — traced by hand, matches
`forecast/metrics.py`'s existing behavior with no changes needed.

**4. [MINOR — fixed directly, this pass] `SqliteStore.record_source_health`
did not get the same `StoreBusyError` translation `upsert_bars` has for a
persistent lock — it would raise a raw `sqlite3.OperationalError`
instead.**

Verified `busy_timeout`/`StoreBusyError` genuinely work for the case they
were built for, with **real** SQLite lock contention (a second, separate
`sqlite3.connect()` on the same on-disk file holding `BEGIN IMMEDIATE`,
not a mocked exception):
- Lock held for less time than `busy_timeout`: `upsert_bars` blocked for
  the actual held duration (~1.0s observed against a 3s timeout), then
  succeeded, bar genuinely persisted.
- Lock held past `busy_timeout`: `upsert_bars` raised `StoreBusyError`
  after almost exactly the configured timeout (~1.0s against a 1s
  timeout) — bounded, no hang, and nothing was silently written or lost.

Both are now permanent regression tests
(`test_store.py::test_busy_timeout_waits_out_a_lock_released_in_time`,
`test_busy_timeout_exhausted_raises_store_busy_error_not_a_hang`) using a
real file and a real second connection — this case was previously
completely untested (`grep -rn "StoreBusyError" tests/` returned nothing
before this round). While verifying this, found `record_source_health`
(same class, same file, a different write path) lacked the equivalent
translation — same fix applied, same pattern, confirmed with
`test_record_source_health_also_raises_store_busy_error_on_persistent_lock`.

*Left as documented, not fixed:* `ForecastCache`/`CalibrationLogger`
(`forecast/cache.py`, `calibration/logger.py`) set `busy_timeout` but have
no equivalent `StoreBusyError`-style translation across their several
write call sites (`put`, `log_forecast`, `mark_scored`,
`mark_unscorable`). A persistent lock there would still raise a raw
`sqlite3.OperationalError`, caught only by the outer safety nets
(`build_snapshot`'s per-instrument `try/except`, the scheduler's `_job()`
catch-all) — no hang, no crash, but a less diagnosable error than
`StoreBusyError` gives. Applying the same mechanical pattern across both
files is straightforward but touches more call sites than felt like a
single "small, unambiguous" fix in this pass; left for builder-core.

**5. [Verified clean, confirmed by injection] A forward system-clock jump
does not corrupt the forecast cache key or confuse `is_market_open`.**

- `ForecastCacheKey.digest()` hashes `last_closed_ts` (a bar timestamp),
  never wall-clock time — re-confirmed by running the **same** refresh
  cycle twice against unchanged store data, once at a normal `now` and
  once with `now` jumped forward 6 hours: the predictor was called exactly
  once total (second call was a clean cache hit,
  `len(predictor.calls)` unchanged) and both snapshots' forecast metrics
  were bit-identical. A wall-clock jump with no new closed bar cannot
  trigger a redundant (or inconsistent) Kronos re-run.
- `is_market_open` recomputes entirely from its `now_utc` argument via a
  fresh `zoneinfo` conversion every call, with no memoized/persisted state
  — confirmed by calling it twice with timestamps 50 hours apart and
  getting the independently-correct answer both times. A clock jump can't
  leave it holding a stale answer because it never holds one at all.

**One residual, real gap found and partially mitigated:** APScheduler's
own defaults (confirmed by reading its source, not assumed) are
`misfire_grace_time=1` second and `coalesce=True`. `coalesce=True` is
good news here — after a jump past several scheduled ticks, APScheduler
runs the job **once** (for the most recent missed tick), never once per
missed tick, so no pile-up/double-fire. But the 1-second grace time means
a tick that becomes due more than ~1s late (a multi-second-or-larger clock
jump, or the previous refresh cycle still running when the next one comes
due, given the default `max_instances=1`) is silently **dropped** —
`EVENT_JOB_MISSED`/`EVENT_JOB_MAX_INSTANCES`, with nothing listening for
either. Not a correctness risk (the cache-key finding above means a
skipped tick just delays picking up the next closed bar, never corrupts
anything), but a silently-skipped refresh should still be observable.
**Fix applied:** `build_scheduler` now installs a listener that logs a
warning on `EVENT_JOB_MISSED`/`EVENT_JOB_MAX_INSTANCES`/`EVENT_JOB_ERROR`.
Confirmed with a real `BackgroundScheduler` and a one-off job whose due
time was already 10s in the past:
`test_scheduler.py::test_build_scheduler_logs_a_missed_job_instead_of_silently_dropping_it`.

**6. [MAJOR — fixed by builder-core in Round 1, independently re-verified
this round under real fault injection] Hugging Face model load failure
degrades to `/healthz`'s `error` status, retries, never crashes the
startup thread.**

Re-ran the existing regression suite for this
(`test_run_startup_sequence_never_raises_on_predictor_load_failure`,
`test_run_startup_sequence_gives_up_after_max_attempts_without_raising`,
`test_healthz_responds_immediately_while_startup_is_slow_and_failing`) —
all still pass, and the mechanism (`run_startup_sequence` catches
`Exception` generically around `load_predictor_fn()`, updates
`ReadinessState`, retries after `retry_delay_s`) is sound regardless of
the specific exception type raised, which matters because of what's below.

**Additionally attempted, per the brief's own suggestion, a real
`load_predictor(settings)` call with a made-up model name** (not just an
injected fake) — this surfaced a genuine inconsistency worth recording
honestly rather than glossing over: one attempt hung for **over 40
seconds with no exception at all** before being killed; two subsequent
attempts (different bogus model names) instead raised quickly (0.2–0.5s)
via an unrelated-looking `TypeError` from the vendored tokenizer's
constructor (`KronosTokenizer.__init__() missing 16 required positional
arguments...` — this originates inside `third_party/kronos`, out of this
project's own code and out of scope to fix per DECISIONS.md #1). Could not
reliably reproduce the hang a second time in this sandbox (network/proxy
behavior here appears non-deterministic across attempts), so this is
reported as an observed-once, plausible risk rather than a confirmed,
reproducible bug — but it points at a real, currently-unmitigated gap:
**nothing in `load_predictor`/`Settings` sets an explicit network
timeout**, and `run_startup_sequence`'s retry logic is triggered only by
an *exception* — a genuine hang (DNS black hole, a half-open TCP
connection to a struggling HF endpoint) would leave `/healthz` stuck at
`status="loading_model"` indefinitely, silently, with no error surfaced
and no retry ever attempted. Suggested fix (not applied — needs a real
network to pick a sane value, same limitation DECISIONS.md #6 already
notes for the performance budget): set an explicit download timeout (e.g.
`huggingface_hub`'s `HF_HUB_DOWNLOAD_TIMEOUT` env var) and/or wrap
`load_predictor_fn()` in `run_startup_sequence` with a bounded
watchdog (a thread + join timeout) so a hang degrades to the same
`status="error"` path a raised exception already takes. Left for
builder-core.

### Gates status (this round's honest read)

- [x] `ruff check .` — clean
- [x] `python -m mypy` — clean, 30 source files
- [x] `python -m pytest tests -q` — 207 passed, 1 deselected (`network`)
      (187 pre-existing from Round 1 + 20 new fault-injection regression
      tests added this round; see each finding for the exact test names)
- [x] All blockers/majors *found this round* are closed (#1, #2, #3 fixed
      directly and proven with tests that fail pre-fix/pass post-fix; #6
      re-verified, not newly broken)
- [ ] Two items left open for builder follow-up, neither a blocker: the
      empty-backfill-response semantics gap (finding #2's undecided half)
      and the forecast-cache/calibration-logger `StoreBusyError` gap
      (finding #4's undecided half) — both documented with a suggested
      fix, both already safety-netted by existing outer `try/except`
      layers so neither can crash or hang the process today
- [ ] One residual risk documented, not fixed: HF load hang with no
      timeout (finding #6) — observed once, not reliably reproducible in
      this sandbox, needs a real network to size a timeout value correctly

### Sign-off (this round)

**Geen openstaande blockers.** Three MAJOR findings from this round's
fault injection (#1 ingest-loop isolation, #2 untyped/silent malformed-
response handling, #3 NaN propagation into the persisted snapshot) are
fixed directly, each with a regression test verified to fail against the
pre-fix code and pass against the post-fix code. One MINOR (#4, partial)
fixed directly. The clock-jump case (#5) and the HF-download-failure case
(#6, from Round 1) were independently re-verified under actual fault
injection rather than re-trusted from the static read, and hold up, with
one observability gap in #5 closed and one residual risk in #6 (a
possible network hang with no timeout) documented honestly rather than
either fixed speculatively or silently dropped. Two items remain
genuinely open (the empty-response and forecast-cache-lock gaps) — both
are lower-severity, already safety-netted by existing broader exception
handling, and correctly left to builder-data/builder-core rather than
patched unilaterally, since both require a real design decision (what
should "zero bars" mean on a first backfill; is the mechanical
`StoreBusyError` pattern worth replicating across every calibration/
forecast-cache write site). Round 2 passes cleanly enough to move toward
Round 3 — nothing found this round should block that.

## Round 3 — quality and usefulness

Per the manager's dispatch, this round is `alpha-scout` and `red-team`
working together in one pass; every finding below is labeled with which
role it comes from (some are joint — labeled as such). Scope per the
brief: (1) one more independent calibration-math pass on the specific
code paths that didn't exist during the original hand-verification, (2)
dashboard readability at 390px, (3) number provenance, (4) dead weight,
(5) alpha-scout proposals (capped at 3, in `BACKLOG.md`).

### 1. Calibration math — fresh independent check of the post-Round-2 code paths (red-team)

Round 1 hand-verified `score_single`/`aggregate_calibration_stats`'
formulas; Round 2 exercised the system under fault injection but did not
specifically re-derive the NEW code this round was asked to check:
`score_matured_forecasts`'s "first bar at-or-after `horizon_ts`, bounded
by `MAX_HORIZON_CATCHUP`" resolution logic and `CalibrationLogger.
mark_unscorable`, both added by the Round 1 finding #3 fix.

Read `calibration/score.py`/`calibration/logger.py` end to end, then
independently re-verified the existing test suite's coverage
(`tests/unit/calibration/test_score.py` — a strong suite already: it
covers exact-match scoring, never-scores-before-horizon, never-scores-an-
unclosed-bar, leaves-unscored-when-no-bar-yet, resolves-weekend-horizon-
against-first-reopen-bar, marks-unscorable-after-catchup-elapses, and
never-uses-a-bar-after-now), and then constructed a genuinely fresh,
independent synthetic script (not reusing any existing fixture) covering
four cases the existing suite doesn't exercise directly:

```
CASE A pass: earliest closed candidate correctly chosen over later one and unclosed one
CASE B pass: bar exactly at the catch-up deadline boundary is usable
CASE C pass: now==deadline exactly leaves pending; now>deadline (even by 1us) marks unscorable
CASE D pass: bar 1us past the deadline correctly excluded from candidates; row marked unscorable

ALL FRESH SYNTHETIC CHECKS PASSED
```

Specifically: (A) with three candidate bars in the store out of
chronological order — one unclosed exactly at `horizon_ts`, one closed 3h
later, one closed 5h later with a wildly different (wrong-if-picked)
close price — `score_matured_forecasts` correctly ignored the unclosed bar
and picked the EARLIEST closed one, not the unclosed one and not a later
closed one; (B)/(C)/(D) probe the exact boundary of
`catchup_deadline = horizon_ts + MAX_HORIZON_CATCHUP`: a candidate bar
landing exactly ON the deadline is usable (inclusive upper bound, correct
per the code's `<=`), `now` exactly equal to the deadline with no
candidate correctly stays pending rather than prematurely marking
unscorable (the code's `elif now > catchup_deadline` is a strict `>`, not
`>=`), and a bar arriving one microsecond past the deadline is correctly
excluded as a candidate and the row correctly ends up `unscorable` once
`now` also passes the deadline. All four cases passed against the
as-shipped code with no changes needed — **no bug found**; this is a
positive, independently-obtained result, not an assumption carried over
from Round 1/2. `aggregate_calibration_stats` itself is unchanged since
Round 1's hand-verification and was not re-derived from scratch again
here, but its one Round-2-adjacent dependency — that `unscorable` and
`scored` rows are mutually exclusive, so `get_scored()` (the only feed
into `aggregate_calibration_stats` in `snapshot.py`) never sees a
partially-scored or double-counted row — is enforced at the SQL/dataclass
level (`mark_unscorable` never touches the score columns, `mark_scored`
never touches the unscorable columns) and is exercised by the existing
`test_score_matured_forecasts_marks_unscorable_after_catchup_window_
elapses` assertion `unscorable.scored_at_utc is None`.

### 2. Dashboard readability at 390px — actually rendered, not just read (red-team + alpha-scout, joint)

Playwright's Chromium was available in this sandbox (`npx playwright
install chromium` succeeded; browsers were already staged at
`$PLAYWRIGHT_BROWSERS_PATH`) — used it rather than reasoning from a static
read alone, per the brief's own instruction to say explicitly which one
was actually done. Served `web/` as static files with a stubbed
`/api/snapshot` returning a hand-built, realistic 6-instrument
`SnapshotDTO` fixture (matching every field in `src/kmd/dto.py`,
deliberately including one asset with `sufficient_data: false` and one
with `source_status.is_stale: true`), then screenshotted the real,
unmodified `web/index.html`/`app.js`/`styles.css` at a 390×844 viewport
(iPhone-class).

**Grid view (the actual 30-second-glance view) — genuinely fits above the
fold.** All 6 configured forecast instruments (BTC/USDT, XRP/USDT, GOUD,
ZILVER, EUR/USD, USD/JPY — the `context` group is correctly excluded, see
below) rendered as a 2-column grid, all fully visible in a single 390×844
viewport with **zero scrolling** required. Each tile shows: symbol +
group, a large tabular-figures price, three color-coded (green/red)
period changes, a regime badge, a 7-point sparkline, and `P(stijging)`/
`band` — genuinely conveys direction, recent momentum, and regime at a
glance without reading a sentence of prose anywhere. This is a real,
visually-confirmed result, not an inference from the CSS grid math alone
(though that math — `minmax(170px,1fr)` auto-fill inside a ~366px content
width — also independently predicts exactly the observed 2-column layout).

**Detail view — appropriately NOT a 30-second view, and it doesn't
pretend to be.** Tapping a tile shows price/change/chart immediately, but
`Setup`, `Kalibratie`, and `Databron` sections require scrolling even
within the detail view (confirmed: the initial 844px-tall screenshot cuts
off partway into the `Regime` section). This is fine for a "drill in for
detail" view — the brief's 30-second requirement is about the primary
glance (the grid), not every nested view — but see the finding below,
because WHICH content ends up below that scroll point matters.

**Finding (red-team, MAJOR — fixed directly this round): the status bar
could show a source as fresh when a specific instrument using it was
actually stale.** `renderStatusBar` deduped chips by `source_name`
(several instruments share one, e.g. every yfinance-backed instrument)
by keeping whichever asset's status happened to be first in
`snapshot.assets`. Confirmed by fixture + screenshot: with GOUD (fresh)
ordered before ZILVER (stale, 2 consecutive errors), both `source_name:
"yfinance"`, the status bar rendered a **green, non-stale** "yfinance"
chip — exactly the "stale shown as live" failure mode `red-team`'s own
Round 2 checklist forbids, just one level up (the aggregate bar) from
where it was already checked (individual tiles/detail view). **Fixed**:
the aggregation now takes `is_stale` as OR, `error_count_last_hour` as
max, and the OLDEST `last_update_utc` across every asset sharing a
`source_name` — re-verified visually after the fix (same fixture now
renders the yfinance chip correctly orange/stale). See `DECISIONS.md` #8b.

**Finding (red-team, MINOR — not fixed, proposed in `BACKLOG.md` #1):
even after the above fix, an individual stale tile is still visually
indistinguishable from a healthy one on the grid itself.** The status bar
now correctly flags "something from yfinance is stale," but a viewer
still cannot tell WHICH tile without opening it — on the primary
30-second view, ZILVER's tile in the fixture looked pixel-identical to
GOUD's. This is a real, screenshot-confirmed gap, but deciding exactly how
loud a per-tile cue should be is a design call (a full-tile treatment
could read as alarming for a merely-slow-but-harmless lag), so it's filed
as a proposal, not applied unilaterally.

**Finding (red-team, MINOR — not fixed, documented): the calibration
"insufficient data" warning is buried below the fold, both on the grid
(entirely absent) and initially in the detail view (below `Regime`).** A
tile's `P(stijging)` is shown with equal visual weight regardless of
whether the instrument backing it has 3 scored forecasts or 300 — the
"onvoldoende data voor kalibratie" caveat that exists specifically to
prevent over-trusting an early number is real, correctly computed, and
correctly gates the *number itself* being shown as a stat — but its mere
existence/absence is not surfaced anywhere a 30-second glance would catch
it. Deciding how to surface this (a tile-level indicator? reordering
detail sections so `Kalibratie` comes before `Setup`/chart?) is a design
decision left undone here rather than restyled unilaterally — flagged for
the manager.

### 3. Number provenance (red-team)

Grepped the frontend and DTOs for every displayed number and traced each
to either a raw price/OHLCV point, a labeled model output, or a
`reason`-carrying derived value.

**`Level.reason` — verified always populated, not just present in the
type.** Read `analysis/levels.py` (not just `dto.py`'s `reason: str`
field declaration) end to end: every one of the four level-producing
functions (`swing_highs_lows`, `previous_day_high_low`, `ma_clusters`,
`round_numbers`) constructs a real, specific f-string reason (e.g.
`"swing high 65200 at 2026-08-27T14:00:00+00:00, local max of highs over
+/-3 bars"`) at every single construction site — there is no code path
that could produce an empty or placeholder reason. Confirmed rendered,
not just modeled: `renderLevels` in `app.js` displays `lvl.reason`
directly under each level's price/kind. Same for `Regime.reason`
(`analysis/regime.py`'s `classify_trend`/`classify_volatility` both
always return a real reason string) — rendered via
`asset.regime.reason` in the detail view.

**Finding (red-team, MAJOR — not fixed, documented): `SetupCard` is the
one numeric block in this whole DTO with NO provenance field at all.**
Unlike `Level`/`Regime` (both carry a mandatory, always-populated
`reason: str`), `SetupCard` (`dto.py`) has `direction`/`entry`/
`invalidation`/`target`/`rr`/`risk_pct` and nothing explaining where
`invalidation`/`target` came from. Traced `analysis/setup.py`:
`invalidation` is literally the SAME price as one specific `Level` already
computed elsewhere on the same detail page (the nearest `swing_low`/
`swing_high` — see `_nearest_support_below`/`_nearest_resistance_above`)
— that Level's own `reason` string exists and is shown in the `Niveaus`
section of the same page, but the `Setup` section never cross-references
it, so a viewer sees "Invalidatie: 62.800,00" with no stated basis, even
though the justification is sitting one section away under a different
number's label. `target` is one of the forecast's own quantiles (`q90`
for a long, `q10` for a short) — also never labeled as such in the Setup
card itself (though it does appear, separately, in the chart legend).
Additionally, **`risk_pct` is not a computed property of this specific
setup at all** — it is literally `markets_config.risk.default_risk_pct`
(a single static config constant, currently 2.0%), identical on every
setup card for every instrument, with no connection whatsoever to the
`entry`/`invalidation` distance shown right next to it. This is
defensible as "recommended fixed account-risk-per-trade, independent of
stop distance" (a legitimate position-sizing convention), but the UI
presents it as an undifferentiated "Risico" figure indistinguishable from
a per-setup computed value, with nothing stating it's actually a global
constant. Given this project's own explicit "Geen advies" positioning
(README) and that `SetupCard` — direction + entry + invalidation + target
+ R:R — is the closest thing to a trade recommendation anywhere in the
system, this is the single highest-value place for a `reason` field of
exactly the kind `Level`/`Regime` already have. **Not fixed**: adding a
`SetupCard.reason` field (or cross-referencing the specific `Level` used)
is a DTO/contract change, and deciding its exact wording/shape is a real
design decision — filed here rather than patched unilaterally.

**Finding (red-team, MINOR — fixed directly this round): the
`error_count_last_hour` display label asserted a precision the number
doesn't have.** The backend value (`DECISIONS.md` #5e) is
`SourceHealth.consecutive_failures` — consecutive failures since the last
success — not a true rolling one-hour count, but the UI labeled it
"Fouten (laatste uur)" ("errors in the last hour"), asserting a specific
time window that isn't what's computed (e.g. it resets to 0 on any single
success even if many failures happened earlier within that same clock
hour). Fixed the display text only, to "Opeenvolgende fouten"/
"opeenvolgende fout(en)" ("consecutive errors") — the underlying DTO field
name is a contract-level concern left alone; see `DECISIONS.md` #8c.

**Everything else checked and found clean:** `p_up_24h`/`q10`/`q50`/`q90`/
`p_vol_expansion`/`band_width_pct` are all labeled forecast outputs
(`forecast/metrics.py`'s docstrings define each precisely) shown next to
the model name, path count, and generation timestamp
(`forecast.model_name`/`n_paths`/`generated_at_utc` — all rendered in the
detail view's "Model: ... · N Monte Carlo paden · gegenereerd ..." line);
the tile grid's compact "band 6,5%" abbreviation expands to the full,
labeled legend in the detail view, so it's compact, not unlabeled.
`change_1h/24h/7d_pct` are self-evident raw price-derived percentages.
Raw price/sparkline values need no provenance statement — they are the
literal OHLCV data.

### 4. Dead weight (red-team + alpha-scout, joint)

Wrote a small AST-based script listing every non-underscore-prefixed
top-level function/class across `src/kmd/`, then grepped each name across
`src/` and `tests/` for any reference beyond its own definition line.
Cross-checked every hit by hand (the heuristic has real false positives —
FastAPI route handlers like `get_snapshot`/`get_asset` are "unreferenced
by name" but are genuinely used, dispatched by HTTP path, not a Python
call; pydantic `field_validator`-decorated methods like
`must_be_utc_aware` are invoked by the framework, not by name either).

**Fixed directly (both confirmed genuinely dead, zero callers in `src/`
or `tests/`):**
- `kmd.config.get_settings()` — every real call site (`api.py`,
  `__main__.py`, every test that needs a `Settings` instance) constructs
  `Settings()` directly; the wrapper was never called anywhere.
- `ForecastCacheKey.from_result()` (`forecast/cache.py`) — never called;
  also conceptually backwards for this cache's real flow (the key must be
  computable BEFORE running Monte Carlo, to decide whether to run it at
  all — `snapshot.py`'s actual cache-check path builds the key from
  `instrument`/`timeframe`/`settings`, never from an already-computed
  `MonteCarloResult`).

Both removed; `ruff check .`/`python -m mypy`/`python -m pytest tests -q`
unchanged at 207 passed afterward, confirming no hidden caller existed.
See `DECISIONS.md` #8a.

**Flagged, not removed (alpha-scout): `Settings.enable_llm_summary`/
`llm_api_key` are read by nothing in `src/`.** Confirmed via grep — the
two fields exist on `Settings`, are documented in `.env.example` with a
comment ("Optional LLM day-summary (off by default; never used in
data/metrics path)"), but nothing in the codebase ever reads
`settings.enable_llm_summary` to do anything. Unlike the two removals
above, this reads as a deliberately staged placeholder for a
not-yet-built, explicitly-named future feature rather than accidental
cruft — removing a documented, forward-looking config surface is a
product decision (is the LLM-summary feature still planned?), not a
mechanical dead-code deletion, so it's left in place and flagged here
rather than deleted unilaterally.

**No redundant caching layer found.** `forecast/cache.py` (Monte Carlo
paths, keyed on `last_closed_ts` + model/sampling params) and
`calibration/logger.py` (`forecast_log`, a permanent audit trail scored
later) look superficially similar (both SQLite, both forecast-adjacent,
both documented together in `DECISIONS.md` #5b) but serve genuinely
different purposes — one is a recompute-avoidance cache with no history
requirement (only the latest entry per key matters, per
`test_cache.py`), the other is an append-only, never-overwritten log
whose entire purpose IS the history. Not redundant.

### 5. alpha-scout proposals

Three proposals filed in `BACKLOG.md`, at the mandated cap of 3, each with
build-cost/value/risk:

1. **Per-tile stale/error visual indicator** (~1-2h, high value/cost
   ratio, low risk) — directly closes the readability finding in §2 above.
2. **Context-group cross-asset overlay** (DXY, S&P 500) (~6-10h, medium-
   high value, medium risk) — the `config/markets.yaml`-promised, never-
   built "analysis overlay." **Assessed per the manager's explicit ask**:
   this is NOT left as a purely-acknowledged README gap forever — the
   underlying data is already ingested and paid for, sitting unused, and
   there's a genuine, scoped feature here. It IS filed as a formal
   proposal with a concrete build cost and — critically — a named
   correctness trap to avoid (the `index` session's placeholder single-
   weekly-window schema, `DECISIONS.md` #4d, must NOT be surfaced as this
   overlay's session-open/closed status, or the currently-dormant Round 1
   limitation becomes a real, user-visible bug).
3. **Regime-conditioned calibration stats** (~5-8h, medium value, low-
   medium risk) — split Brier/MAE/coverage by the regime label active at
   forecast time, with an honest cost called out: it will multiply the
   time-to-"sufficient-data" for any single bucket, so this pays off later
   in the system's life, not immediately.

None implemented, per alpha-scout's mandate (propose only).

### Fixes applied this round (summary)

Four small, unambiguous fixes, each verified not to require a design
decision — full reasoning for each in `DECISIONS.md` #8:
1. Removed `kmd.config.get_settings()` (dead code).
2. Removed `ForecastCacheKey.from_result()` (dead code).
3. Fixed `web/app.js`'s status-bar source aggregation (stale-hiding bug).
4. Fixed `web/app.js`'s misleading "Fouten (laatste uur)" label.

`ruff check .`, `python -m mypy`, and `python -m pytest tests -q` all
re-run clean after every fix above (207 passed, 1 deselected — same count
as before this round's fixes, confirming the two removed functions truly
had no test depending on them and the two `app.js` fixes broke nothing
Python-side; no JS test harness exists for this project, per Round 1
finding #4's same documented limitation — the frontend fixes were
verified instead via the constructed fixture + Playwright screenshots
described in §2 above, which is a real, if manual, regression check, not
a claim of automated coverage that doesn't exist).

### Gates status (this round's honest read)

- [x] `ruff check .` — clean, re-run after every fix this round
- [x] `python -m mypy` — clean, 30 source files (one file's line count
      shrank by a few lines from the two dead-code removals; still 30
      files, still zero errors)
- [x] `python -m pytest tests -q` — 207 passed, 1 deselected (`network`),
      unchanged by this round (no new automated tests were added — this
      round found no correctness bug needing a regression test; the two
      frontend display fixes have no JS test harness, verified manually
      instead, see above)
- [x] Calibration math — independently re-verified for the SPECIFIC
      post-Round-2 code paths this round was asked to check (candidate
      selection/ordering, catch-up-deadline boundary inclusivity,
      unscorable-marking timing), via a fresh synthetic script, not reused
      fixtures — see §1. No bug found.
- [x] Dashboard readability at 390px — actually rendered with Playwright,
      not just read statically (see §2 for exactly what was and wasn't
      verified this way). Grid view (the actual 30-second view) fits
      fully above the fold for all 6 instruments; two real gaps found
      (stale-tile invisibility, buried calibration-insufficient-data
      warning) are documented, one contributing bug (status-bar
      aggregation) fixed directly.
- [x] Number provenance — checked systematically; `Level`/`Regime.reason`
      verified always-populated AND rendered, not just present in the
      type; one real gap found and documented (`SetupCard` has no
      provenance field at all — the highest-value place for one, given
      this project's "Geen advies" positioning); one mislabeled number
      fixed directly (`error_count_last_hour`'s display text).
- [x] Dead weight — checked via AST + grep cross-reference, not guessed;
      two confirmed-dead functions removed, one confirmed-unread config
      option flagged (not removed, since it's a deliberate staged
      placeholder, a product decision not a mechanical cleanup); no
      redundant caching layer found.
- [x] alpha-scout proposals — exactly 3 filed in `BACKLOG.md`, each with
      cost/value/risk, none implemented; the `context`-group question the
      brief specifically raised was assessed and included as proposal #2
      rather than left unaddressed.
- [x] All blockers/majors from this file closed or explicitly documented
      — two MAJOR findings this round (`SetupCard` provenance gap,
      status-bar stale-hiding) — the first is documented (a design/DTO
      decision, correctly not patched unilaterally), the second is FIXED
      directly with before/after screenshot verification (no automated
      regression test possible, see above; manual verification is
      explicitly recorded rather than silently omitted).
- [x] red-team sign-off: "geen openstaande blockers" — see below.

### Sign-off (this round)

**Geen openstaande blockers.** No BLOCKER-severity finding was raised this
round. Two MAJOR findings were raised: the status-bar stale-hiding bug
(fixed directly this round, re-verified visually) and the `SetupCard`
provenance gap (a real, documented DTO/design gap, correctly left for a
deliberate fix rather than a unilateral contract change — this is the
kind of finding Round 1/2 also correctly left open when it required a
manager/contract-owner decision, not evidence of it being ignored). Two
MINOR findings were raised and fixed directly (the misleading error-count
label, dead code); two MINOR findings were documented, not fixed (the
per-tile stale-indicator gap and the buried calibration-insufficient-data
warning — both filed, the first as a `BACKLOG.md` proposal, the second as
a documented finding for the manager). The calibration math's specific
post-Round-2 code paths were independently re-derived with a fresh
synthetic example and found correct with no bug. Rounds 1 and 2's own
sign-offs stand un-revisited-and-un-contradicted by anything found this
round. All three mandatory rounds are now complete.

## Gates

Updated after Round 3 — the manager's brief lists these as the project's
overall completion gates; each is checked here only where an actual round
(cited) backs it up:

- [x] `ruff check .` and `python -m mypy` zero errors — Round 2's clean
      state re-confirmed after Round 3's fixes; still clean (30 files)
- [x] `pytest` fully green, no network access — 207 passed, 1 deselected,
      unchanged through Round 3
- [ ] Full refresh cycle end-to-end on a clean checkout within the
      performance budget — **still not verified by anyone**: this
      sandbox's network policy has blocked Hugging Face/Binance/Yahoo
      access in every round of this project so far; the manager's one
      post-Round-1 live run confirmed process startup and `/healthz`/
      `/api/snapshot` behavior up to (never through) the network-dependent
      steps. This gate needs a real network to close and Round 3 could
      not provide one.
- [x] Calibration log holds real forecasts; scoring code proven correct on
      a synthetic example — reaffirmed this round with a FRESH,
      independent synthetic example specifically targeting the
      post-Round-2 code paths (§1) in addition to Round 1's original
      hand-verification of the core formulas; no bug found either time
- [x] All blockers/majors from this file closed; minors explicitly
      accepted or filed — every blocker across all three rounds is
      closed; the two Round 3 majors are handled per this round's own
      sign-off above (one fixed, one correctly deferred as a documented
      design decision, not silently dropped); every minor across all
      three rounds is either fixed or explicitly filed/accepted, never
      omitted
- [x] red-team sign-off: "geen openstaande blockers" — true as of Round 3;
      see the sign-off immediately above
- [ ] README start instructions literally followed on a clean environment
      and worked — the manager did this once, post-Round-1 (see the
      "Manager verification + finding #2 closure" section above), and it
      worked up to the network-dependent steps; still not independently
      re-run end-to-end by red-team through a real network in any round,
      for the same sandbox-network-policy reason as the performance-budget
      gate above — left unchecked on principle rather than re-claiming
      someone else's partial verification as this round's own

**Honest overall read:** every gate that this sandboxed environment is
actually capable of verifying is now met and re-confirmed. The two
remaining open gates (full end-to-end timing; a literal from-scratch
README-follow-through with real network access) are not blocked by any
code defect found in three rounds of review — they are blocked by this
environment's network policy, a limitation stated plainly and
consistently since Round 1 rather than glossed over. There are no known
open correctness, robustness, or quality defects at BLOCKER or unfixed-
MAJOR severity that don't already have a documented, deliberate reason for
being left open (the `SetupCard` provenance gap, requiring a DTO/design
decision).

## Gates

Updated after Round 2, per Round 1's own note that Round 2 is what gets to
move these checkboxes — each one only checked where this round (or an
earlier, still-valid verification cited explicitly) actually backs it up,
never assumed true by default:

- [x] `ruff check .` and `python -m mypy` zero errors — re-run at the end
      of this round, both clean (note: project uses `mypy = strict = true`
      in `pyproject.toml` rather than a `--strict` CLI flag; equivalent)
- [x] `pytest` fully green, no network access — `python -m pytest tests -q`:
      207 passed, 1 deselected (the `network`-marked real-model test,
      correctly excluded by `addopts`)
- [ ] Full refresh cycle end-to-end on a clean checkout within the
      performance budget — **not re-verified this round**: this sandbox's
      network policy still blocks Hugging Face/Binance/Yahoo (same
      limitation Round 1 and the manager's own verification hit); the
      manager's post-Round-1 run confirmed the process starts and
      `/healthz`/`/api/snapshot` behave correctly up to (but not through)
      the network-dependent backfill/model-load steps — that evidence
      still stands, but a full end-to-end timing run remains unperformed
      by anyone on this project
- [x] Calibration log holds real forecasts; scoring code proven correct on
      a synthetic example — scoring math verified by hand in Round 1
      (unchanged this round); the weekend-horizon sampling-bias gap
      (Round 1 finding #3) was fixed by builder-core and is what makes
      "holds real forecasts representatively" true rather than ~80%-true
- [x] All blockers/majors from this file closed; minors explicitly
      accepted or filed — Round 1's blocker #1 and majors #2/#3 closed;
      Round 2's majors #1/#2/#3 (this round, above) fixed directly with
      passing regression tests; two minor-severity items remain
      deliberately open and documented (Round 2 finding #2's empty-
      response semantics question, finding #4's forecast-cache lock-error
      translation) — both filed for builder-data/builder-core, neither a
      blocker or major, neither able to crash/hang the process today
- [x] red-team sign-off: "geen openstaande blockers" — true as of this
      round; see the Round 2 sign-off above
- [ ] README start instructions literally followed on a clean environment
      and worked — the manager did this once, post-Round-1 (see "Manager
      verification + finding #2 closure" above) and it worked; **not
      independently re-run by red-team this round** (same network
      limitation as the full-refresh-cycle item above), so left unchecked
      here on principle rather than re-claiming someone else's
      verification as this round's own
