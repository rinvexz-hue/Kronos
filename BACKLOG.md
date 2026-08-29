# Backlog (alpha-scout proposals)

Format per entry: idea / estimated build cost (hours) / estimated value / risk / status.

Cap: at most 3 active (not-yet-decided) entries at a time.

## 1. Per-tile stale/error visual indicator on the grid

**Idea:** the grid tile (`renderTile` in `web/app.js`) currently shows
nothing at all if `asset.source_status.is_stale` is true — a stale,
error-prone instrument's tile renders pixel-identical to a fresh one.
Confirmed by rendering the real dashboard (Playwright, 390x844) against a
constructed fixture with one deliberately stale instrument: its tile was
visually indistinguishable from the healthy ones on the primary 30-second
view. Staleness is currently only visible in the (now-fixed, see
`DECISIONS.md` #8b) aggregate status bar and the per-instrument detail
view — both a tap or a careful read away, not a glance away. Add a small,
consistent visual cue directly on a stale tile (e.g. an orange left border
or a small "verouderd" chip next to `tile-group`, reusing the existing
`--stale` CSS variable already defined for exactly this purpose) so a
stale instrument is visible at the same glance as its price and regime
badge.

**Cost:** ~1-2 hours. Data already exists on `AssetSnapshot.source_status.
is_stale` — no DTO/backend change, pure `renderTile`/CSS addition.

**Value:** high relative to cost. Directly closes a real gap this round's
own readability check found: the dashboard's central promise ("readable in
30s, never show stale as live") currently fails exactly at the one view
that IS the 30-second glance.

**Risk:** low. Purely additive display logic; no existing behavior changes
for a non-stale tile. Only design judgment needed is exactly how loud the
cue should be (a full-tile treatment could be alarming for a merely-
slow-but-harmless data lag) — left to whoever implements it to pick,
matching the existing badge/status-chip visual language rather than
inventing a new one.

**Status:** proposed, not built.

## 2. Context-group cross-asset overlay (DXY, S&P 500)

**Idea:** `config/markets.yaml`'s `context` group comment promises DXY/S&P
500 will appear "as an analysis overlay (e.g. DXY vs GOUD)" — confirmed
this was never built (`grep` for "context"/"DXY"/"overlay" across
`web/*.{html,js}` returns zero matches; `snapshot.py::build_snapshot`
explicitly filters the `context` group out before ever building a tile).
Both instruments are already ingested, stored, and quality-gated for free
— this is a display/analysis feature on top of data the pipeline already
has, not a new data-source integration. Concretely: show DXY's own
recent change (1h/24h/7d, matching the existing tile convention) alongside
GOUD/EUR-USD/USD-JPY's detail view as a labeled "context" line ("DXY 24u:
+0.4%"), and similarly S&P 500 as general risk-sentiment context — a
correlation NUMBER (e.g. rolling correlation coefficient) is a nice-to-
have beyond that, not required for the overlay to deliver its promised
value.

**Cost:** ~6-10 hours. Needs a small `SnapshotDTO`/manager-owned contract
addition (a `context` list of lightweight price/change summaries,
deliberately NOT the full `AssetSnapshot` shape — no forecast/setup/
calibration is meaningful for a context instrument the system was never
asked to forecast against), frontend rendering, and — importantly — must
deliberately NOT surface `market_session_open`/`DataSourceStatus` for
these two instruments in the overlay, since the `index` session schema is
a known placeholder single-weekly-window (`DECISIONS.md` #4d, `REVIEW.md`
Round 1) that would silently mislabel session state most of the week the
moment it's ever shown to a user. That exclusion is a real, load-bearing
constraint on this proposal's scope, not an afterthought.

**Value:** medium-high. DXY vs. gold/EUR-USD and S&P vs. risk-on/off crypto
sentiment are genuinely useful, commonly-referenced cross-asset context for
exactly the instrument classes this dashboard tracks — and it closes a
gap the config file itself has been advertising since the project's first
pass.

**Risk:** medium. Touches the DTO contract (manager-owned, not builder-
core's to change unilaterally) and has one concrete correctness trap
already identified above (the `index` session placeholder) that a careless
implementation could reintroduce as a user-visible bug rather than the
currently-dormant, harmless gap `REVIEW.md` Round 1 found it to be.

**Status:** proposed, not built. (Assessed per the manager's brief: this
is a real, scoped opportunity worth a formal proposal rather than staying
an acknowledged-but-inert README limitation forever — the underlying data
is already paid for and sitting unused.)

## 3. Regime-conditioned calibration stats

**Idea:** `CalibrationStats` currently aggregates Brier score/MAE/band
coverage across ALL of an instrument's scored forecasts, regardless of
what regime (trending vs. ranging, low vs. high volatility) each forecast
was made in. A model can be well-calibrated in calm, trending conditions
and poorly calibrated in choppy/high-vol ones (or vice versa) — the single
aggregate number the dashboard shows today cannot distinguish these, even
though the regime label for every historical forecast is fully knowable
(it's computed at forecast time already, just not persisted alongside the
forecast log row). Log `regime_label`/`vol_regime` onto `forecast_log` at
write time (same additive-migration pattern already proven for the
`unscorable_*` columns — see `calibration/logger.py`), and let
`aggregate_calibration_stats` optionally produce a per-regime breakdown in
addition to (never instead of) the existing all-up number.

**Cost:** ~5-8 hours. Additive schema migration, one new column write in
`log_forecast`'s caller (`snapshot.py`, which already computes the regime
before calling the forecast/calibration path), an aggregation function
change, and a small, clearly-secondary UI addition (this must NOT compete
with the primary at-a-glance calibration number on a 390px screen — it
belongs deeper in the detail view, if anywhere, as a "kalibratie per
regime" expansion, not a grid-tile change).

**Value:** medium. A genuinely sharper trust signal for a system whose
core positioning is "here's the uncertainty and the track record, not
advice" — but it comes with an honest cost worth stating up front:
splitting an already-scarce sample (the dashboard already gates display at
`min_observations_for_display = 30` for the AGGREGATE count) by regime
will take meaningfully longer to reach "sufficient data" in any single
regime bucket, so this mostly pays off after the system has been running
for a while, not immediately.

**Risk:** low-medium. Mechanically low risk (additive, well-precedented
migration pattern), but real product risk of the opposite failure this
whole review has been guarding against: showing a bucket's number before
it has enough observations to mean anything. Any implementation MUST reuse
the existing `sufficient_data` gating per-bucket, not just once for the
overall total.

**Status:** proposed, not built.
