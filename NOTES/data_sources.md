# Data sources — empirical verification (builder-data)

## Network access in this sandbox: blocked by organization egress policy

Before writing `ccxt_source.py`/`yfinance_source.py`, I tried to reach every
host these adapters would actually call, to empirically verify the
`source_symbol`s in `config/markets.yaml`. All three attempts were rejected
**at the outbound proxy**, not by DNS or the remote service:

```
$ curl -sS -m 10 "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1h&range=5d"
curl: (56) CONNECT tunnel failed, response 403

$ curl -sS -m 10 "https://api.binance.com/api/v3/ping"
curl: (56) CONNECT tunnel failed, response 403

$ curl -sS -m 10 "https://api.exchange.coinbase.com/products"
curl: (56) CONNECT tunnel failed, response 403
```

`curl "$HTTPS_PROXY/__agentproxy/status"` (per this environment's own
`/root/.ccr/README.md`) confirms all three as explicit policy denials, not
transient failures:

```json
"recentRelayFailures": [
  {"kind": "connect_rejected", "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)", "host": "query1.finance.yahoo.com:443"},
  {"kind": "connect_rejected", "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)", "host": "api.binance.com:443"},
  {"kind": "connect_rejected", "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)", "host": "api.exchange.coinbase.com:443"}
]
```

Per the proxy README's own instructions ("do not retry organization policy
denials (403/407) - report them instead"), I did not attempt to route
around this. **Nothing in this session ran a live yfinance or ccxt call
against a real upstream.** The tickers in `config/markets.yaml` are
therefore *not* empirically verified in this environment - the assessment
below is instead grounded in yfinance's/ccxt's own documented behavior,
their source, and the vendored Kronos README's own suggested tickers
(which is where `markets.yaml`'s starting points came from in the first
place).

If a future session has real network access, re-running this check is a
five-minute job:

```python
import yfinance as yf
for ticker in ["GC=F", "SI=F", "EURUSD=X", "JPY=X", "DX-Y.NYB", "^GSPC",
               "XAUUSD=X", "XAGUSD=X", "USDJPY=X"]:
    df = yf.Ticker(ticker).history(interval="1h", period="5d")
    print(ticker, "->", len(df), "rows", df.index.min() if len(df) else "EMPTY")
```

## What the adapters were built against instead

`yfinance_source.py`'s interval mapping is based on yfinance's/Yahoo's
**documented** intraday-history limits (consistent across yfinance's own
README and widely-reported behavior of the underlying Yahoo chart API):

| Timeframe       | yfinance `interval` | backfill `period` | why |
|-----------------|----------------------|--------------------|-----|
| `Timeframe.H1`  | `"1h"`               | `"730d"`           | Yahoo restricts 60m/1h intraday history to roughly the trailing 730 days - the longest intraday window it exposes at all. 1000 bars is ~42 days, comfortably inside this even for a 24/7 instrument; for an exchange-hours-limited one (see `^GSPC` caveat below) the full 730d window is requested precisely to maximize how many *trading-hour* bars come back. |
| `Timeframe.D1`  | `"1d"`               | `"max"`            | Daily bars aren't subject to the intraday cap; `"max"` asks for whatever history Yahoo actually has for the ticker rather than guessing a cutoff. |
| `Timeframe.H4`  | *(none - see below)* | *(n/a)*            | Yahoo has no native 4-hour interval at all. |

**`Timeframe.H4` is synthesized**, not fetched directly: the adapter pulls
`"1h"` bars and resamples them into UTC-00/04/08/12/16/20-aligned 4h
buckets (`_resample_h1_to_h4` in `yfinance_source.py`), matching how
Binance's own native `"4h"` candles are aligned. A bucket missing any of
its 4 constituent hourly bars is dropped rather than built from partial
data - except the trailing (most recent) bucket, which is always emitted
from whatever hourly bars exist so far but is unconditionally treated as
not-yet-closed regardless of `compute_is_closed`'s own result. This is
covered by `tests/unit/data/test_yfinance_source.py`.

## Per-ticker assessment (documented behavior, not live-verified)

| `display_symbol` | `source_symbol` | Assessment |
|---|---|---|
| `GOUD` | `GC=F` (COMEX gold futures) | Standard, heavily-used yfinance ticker for gold; COMEX futures are quoted in USD/troy oz, which is what `decimals: 2` implies. Should return normal hourly/daily OHLCV under yfinance's continuous-contract handling. No change recommended. |
| `GOUD` fallback | `XAUUSD=X` | Yahoo's synthetic USD/XAU cross - a spot-gold proxy, not a second futures contract. Reasonable fallback *shape* (still USD-denominated gold), but it is a different underlying instrument (spot vs. futures) with a different basis; if it's ever actually used, the resulting series has a level/behavior discontinuity right at the fallback switchover. Worth a code comment (added in `ingest.py`/`ccxt_source.py`'s docstrings) rather than a `markets.yaml` change - I'm not confident enough without live data to declare it "thin" and remove it. |
| `ZILVER` | `SI=F` | Same situation as `GC=F`, for COMEX silver. No change recommended. |
| `ZILVER` fallback | `XAGUSD=X` | Same caveat as `XAUUSD=X` above (spot proxy, not futures). |
| `EUR/USD` | `EURUSD=X` | Yahoo's standard FX-cross ticker naming (`EURUSD=X`); this is the most reliable, widely-used FX ticker pattern on yfinance. No change recommended. `fallback_source_symbol: null` is reasonable - there is no meaningfully different alternate ticker for this pair on Yahoo. |
| `USD/JPY` | `JPY=X` | This is Yahoo's *shorthand* for the USD/JPY cross (`JPY=X` means "USD base implied"), functionally equivalent to `USDJPY=X`. Both forms are documented to resolve on Yahoo; keeping `JPY=X` as primary and `USDJPY=X` as fallback is a reasonable, low-risk redundancy (they are two tickers for the same pair, so a "fallback" here is really just a second name for the same underlying data, which is actually a *good* fallback property - no shared-failure-mode risk from the futures/spot mismatch that `GC=F`/`XAUUSD=X` has). No change recommended. |
| `DXY` | `DX-Y.NYB` | This is the ICE-listed US Dollar Index ticker as carried on Yahoo (NYBOT/ICE exchange suffix `.NYB`). This specific ticker has a *documented history of intermittent flakiness/staleness on Yahoo* in community reports (it is a lower-liquidity index-of-an-index product relative to `^GSPC`), which is exactly the kind of thing this task asked to verify empirically - I could not confirm or refute this in this sandbox. If a future session with network access finds `DX-Y.NYB` returns thin/stale intraday data, `"DX=F"` (ICE Dollar Index futures) is the standard alternate ticker to try instead. Flagging this as the single most likely candidate for a real `markets.yaml` correction once network access is available; not changed here since I have no live evidence either way. |
| `S&P 500` | `^GSPC` | The standard, extremely liquid Yahoo ticker for the S&P 500 index. Very low risk. No change recommended. Note: `^GSPC` only trades ~6.5h/day (NYSE cash session), so its *intraday* (1h) bar count over any given calendar window is much lower than a 24-hour instrument's - `period="730d"` for `Timeframe.H1` backfill is deliberately generous for exactly this reason (to still clear 1000+ bars for exchange-hours-limited tickers like this one). |

## ccxt (Binance/Coinbase): not live-verified, but low-risk

`BTC/USDT` and `XRP/USDT` on Binance, with `BTC/USD`/`XRP/USD` on Coinbase
as fallback, are both extremely standard, top-liquidity ccxt symbol
strings for their respective exchanges - both exchanges have supported
these exact pairs for years and ccxt's own symbol normalization is
well-exercised for them. This is the lowest-risk pairing in the whole
config; I have no documented or historical reason to expect either symbol
string to be wrong. `ccxt_source.py`'s test suite instead validates the
*adapter* (timestamp conversion, `is_closed`, retry/circuit-breaker
behavior) against a recorded fixture shaped exactly like
`ccxt.Exchange.fetch_ohlcv`'s real return value
(`tests/fixtures/ccxt_btc_usdt_1h.json` - clearly a synthetic random-walk
fixture, not real market data, per the "no mock data that could be
mistaken for real data" rule).

## Bottom line

No `config/markets.yaml` ticker was changed. Every choice already there is
defensible on documented grounds; the one entry I'd want to double check
first with real network access is `DXY`'s `DX-Y.NYB` (community-reported
Yahoo flakiness for this specific ticker, not something I could confirm or
deny from here). Everything else - the `Timeframe.H4` synthesis strategy,
the `period`/`interval` choices, and the ccxt symbol pairs - is built on
solid documented ground even without a live call in this sandbox.
