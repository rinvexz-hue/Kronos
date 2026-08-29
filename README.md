# Kronos Market Desk

A local, read-only market-observation dashboard. It tracks a fixed set of
instruments (crypto, metals, FX, plus two context indices), forecasts each
one with the [Kronos](https://github.com/shiyu-coder/Kronos) foundation
model as a genuine Monte Carlo distribution (not a point estimate), and
shows the result on one dashboard meant to be readable on a phone in about
30 seconds.

**This is not a trading system.** There is no order execution, no broker
connection, and no API key anywhere in this project ever has trade or
withdraw permissions — only read-only market data. Every forecast is shown
next to the uncertainty band and calibration history it rests on.

## Setup

Prerequisites: Python 3.11+.

```bash
pip install -e ".[dev]"
cp .env.example .env
python -m kmd
```

That's it — three commands. `third_party/kronos/` (the vendored Kronos
model source, see `NOTES/kronos_api.md`) is already part of this repo, no
separate clone or submodule step needed.

## What happens on first run

`python -m kmd` starts the HTTP server **immediately** — `GET /healthz`
answers within milliseconds of process start, regardless of what happens
next. In the background, it then:

1. Backfills at least 1000 bars per instrument/timeframe from the
   configured sources (`config/markets.yaml`) into a local SQLite store.
2. Downloads and loads the configured Kronos model from Hugging Face
   (`KMD_MODEL_NAME` in `.env`, default `NeoQuasar/Kronos-small`, ~25M
   params — a small download, but still a download).
3. Starts the scheduler, which runs the first forecast/analysis/
   calibration cycle and writes the first snapshot.

`GET /healthz` reports which stage this is at:

```json
{"status": "backfilling", "ready": false, "detail": null}
```

`status` moves through `starting` → `backfilling` → `loading_model` →
`ok`, or to `error` with a `detail` string if a step fails — it retries
indefinitely rather than crashing, so a flaky source or a slow model
download degrades to "not ready yet," never to a crash or a fabricated
number. `GET /api/snapshot` returns `503 {"detail": "snapshot not yet
available"}` until the first cycle completes; the dashboard at `/` is
served the whole time, it just has nothing to show yet.

**How long this takes** depends entirely on your network: backfilling a
handful of instruments across three timeframes each, plus a ~25M-parameter
model download, is typically a couple of minutes on a normal connection.
Once `/healthz` reports `{"status": "ok", "ready": true}`, open
`http://127.0.0.1:8000/` (host/port configurable via `KMD_HOST`/`KMD_PORT`
in `.env`).

## Running the tests

```bash
pytest
```

No test in the default run touches the network or downloads real model
weights (fakes/fixtures throughout). One real-model integration test
exists for manual verification only and is excluded by default (marked
`network`); run it explicitly with `pytest -m network` if you have
Hugging Face access and want to sanity-check inference against the real
weights.

## Configuration

Everything instrument-related lives in `config/markets.yaml` (single
source of truth — the code never hardcodes a symbol). Everything
environment-specific (model choice, device, Monte Carlo path count,
sampling parameters, DB path, host/port) lives in `.env`, documented with
defaults in `.env.example`.

## Architecture

```
ingest → quality gate → SQLite store → analysis + Kronos forecast
       → calibration logging → snapshot JSON → dashboard
```

The dashboard never talks to the model directly — it only ever reads the
latest snapshot JSON, rebuilt by the scheduler on every closed candle.
See `config/markets.yaml`, `src/kmd/data/base.py` (data-layer contract),
`src/kmd/dto.py`/`src/kmd/snapshot.py` (the dashboard's DTO contract), and
`NOTES/kronos_api.md` (verified Kronos API facts this project relies on)
for the details. `DECISIONS.md` has the reasoning behind every
non-obvious choice; `REVIEW.md` has the adversarial review history.

## Beperkingen (Limitations)

Being direct about what this system does *not* do, in Dutch since that's
the language the dashboard itself speaks:

- **Geen advies.** Dit systeem voert geen orders uit, heeft geen
  broker-koppeling, en genereert geen koop/verkoopsignalen. `p_up_24h`,
  de q10/q50/q90-band en de setup-kaart (RR ≥ 2.0) zijn statistische
  outputs van een taalmodel voor koersreeksen, geen garanties.
- **Kalibratie kost tijd.** Onder 30 waarnemingen per instrument toont het
  dashboard expliciet "onvoldoende data voor kalibratie" in plaats van een
  cijfer dat nog niets zegt. Vertrouw de Brier-score/MAE/banddekking pas
  zodra dat aantal gehaald is — en zelfs dan is het een indicatie over het
  verleden, geen garantie voor de toekomst.
- **Eén CPU-model, standaard klein.** Standaard draait Kronos-small op
  CPU; dit is bewust een snelheids/nauwkeurigheid-afweging (zie
  `DECISIONS.md`). Overschakelen naar Kronos-base of GPU kan via `.env`,
  maar is niet los getest binnen dit project op het prestatiebudget van
  <90s per volledige refreshcyclus — meet dat zelf na een wijziging.
- **Databronverificatie was structureel, niet live.** De yfinance/ccxt
  tickers in `config/markets.yaml` zijn gecontroleerd tegen de
  gedocumenteerde API's, maar konden niet live getest worden in de
  ontwikkelomgeving van dit project (netwerktoegang naar Binance/
  Coinbase/Yahoo was daar geblokkeerd door sandboxbeleid). Zie
  `NOTES/data_sources.md` voor precies wat wel/niet geverifieerd is —
  controleer dit bij een eerste echte run, met name de `DXY`-ticker
  (`DX-Y.NYB`), die daar als risicovol is gemarkeerd.
- **De `context`-groep (DXY, S&P 500) wordt opgehaald en opgeslagen, maar
  nog niet als cross-asset overlay op het dashboard getoond** — dat is een
  in `config/markets.yaml` aangekondigde maar nog niet gebouwde
  functionaliteit (zie `BACKLOG.md`).
- **Sessieschema van de `context`-groep is een placeholder.** Anders dan
  de FX/metalen-sessies (die een correcte wekelijkse open/close-cyclus
  hebben) kent de `index`-sessie maar één vast tijdvenster, geen echte
  week-kalender — zie `DECISIONS.md` en `REVIEW.md` (Round 1, punt over
  `index`-sessie).
- **Eén proces, lokaal gebruik.** SQLite met één schrijver, geen
  horizontale schaling, geen multi-user auth — dit is bedoeld voor lokaal/
  persoonlijk gebruik, niet als gedeelde productiedienst.
