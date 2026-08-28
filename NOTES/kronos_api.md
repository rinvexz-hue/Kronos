# Kronos API — verified against source (Phase 0)

Verified directly against `third_party/kronos/model/kronos.py` and
`third_party/kronos/model/__init__.py` at commit `67b630e`
(`rinvexz-hue/Kronos`, an unmodified fork of `shiyu-coder/Kronos`, same
history as upstream `master`). Nothing here is taken from training data
without a source-line check.

## Repo situation (deviation from the brief)

The brief assumed an empty repository into which Kronos would be cloned
under `third_party/kronos/`. The actual repo *was* a full, unmodified
checkout of `shiyu-coder/Kronos` at the root. We moved that source tree
into `third_party/kronos/` with `git mv` (tracked, reversible, no file
contents changed) so the new application can live at the repo root per
the intended architecture. See `DECISIONS.md` for the full rationale.

## Imports

```python
from model import Kronos, KronosTokenizer, KronosPredictor
```

`model/__init__.py` re-exports exactly these three names plus a
`model_dict` / `get_model_class` registry used by the finetuning code —
irrelevant to inference.

## `KronosTokenizer.from_pretrained(...)` / `Kronos.from_pretrained(...)`

Both classes subclass `nn.Module` **and** `huggingface_hub.PyTorchModelHubMixin`,
so `from_pretrained("NeoQuasar/<repo>")` is the mixin's standard HF
download/instantiate path — it is not custom code in this repo. It pulls
the constructor kwargs from the HF repo's `config.json` and weights from
`model.safetensors`. No local override of `from_pretrained` exists, so
whatever kwargs each hub repo publishes drive `__init__`.

## `KronosPredictor.__init__`

```python
def __init__(self, model, tokenizer, device=None, max_context=512, clip=5):
```

- `device`: if `None`, auto-detects `cuda:0` → `mps` → `cpu`, in that order.
- `max_context`: hard cap on how many *tokens* (bars) of history the
  autoregressive loop keeps in its sliding buffer. It is **not** read
  from the model config — the caller must pass the correct value for the
  model in use (512 for `-small`/`-base`, 2048 for `-mini`). Passing a
  value larger than what the model was trained on is silently accepted
  and will degrade quality, not error.
- `clip`: z-score clipping bound applied to normalized inputs (default 5σ).

## `KronosPredictor.predict(...)`

```python
def predict(self, df, x_timestamp, y_timestamp, pred_len,
            T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=True):
```

Required columns in `df`: `['open', 'high', 'low', 'close']`. `volume`
and `amount` are optional — if `volume` is missing both are filled with
`0.0`; if `volume` is present but `amount` is missing, `amount` is
synthesized as `volume * mean(OHLC)`. **A NaN anywhere in the required +
optional-if-present columns raises `ValueError`** — the predictor does
not silently impute NaNs.

`x_timestamp` / `y_timestamp` are decomposed into `[minute, hour,
weekday, day, month]` calendar features (`calc_time_stamps`) — no
timezone handling happens here at all. Whatever tz (or tz-naive) the
caller passes is what generates these features. **This means Kronos
itself is agnostic to timezone; the calling application is fully
responsible for feeding UTC-consistent timestamps**, exactly as red-team
must check.

Per-call z-score normalization: mean/std computed over the *input
window only* (`x`, not `y`), inputs clipped to `±clip` std after
normalizing. Output is denormalized with the same input-window mean/std
before being returned.

Returns: a `pd.DataFrame` with columns
`['open', 'high', 'low', 'close', 'volume', 'amount']`, indexed by
`y_timestamp`, length `pred_len`.

### `sample_count` — CONFIRMED: internally averaged, not N distinct paths

This is the load-bearing finding for §5 of the brief. In
`auto_regressive_inference` (`model/kronos.py`, the function `predict()`
delegates to via `generate()`):

```python
z = z.reshape(-1, sample_count, z.size(1), z.size(2))
preds = z.cpu().numpy()
preds = np.mean(preds, axis=1)          # <-- averaged over sample_count HERE
return preds
```

`sample_count` batches `sample_count` independent autoregressive rollouts
(distinct multinomial sampling per path — real stochasticity, not just
repeated identical calls) but **the function returns only the mean path**
across those samples, never the individual paths. There is no parameter
or return-value variant that exposes the per-path draws.

**Consequence for `forecast/engine.py`:** to build the probabilistic
fan (`q10/q50/q90`, `p_up_24h`, `p_vol_expansion`) described in §5, the
engine must call `predictor.predict(..., sample_count=1)` **N times**
(N = configured Monte Carlo path count, default 30) in a loop, collecting
each call's returned path itself as one Monte Carlo draw. Do not rely on
`sample_count > 1` for this — it would collapse the whole distribution
into a single mean path before the application ever sees it, which is
exactly the "silently deterministic averaged forecast masquerading as
probabilistic" bug red-team's checklist is watching for.

Also note: there is no explicit `seed` parameter anywhere in this API.
Determinism (needed so calibration scoring is meaningful and refresh
cycles are reproducible) must be achieved by the caller doing
`torch.manual_seed(seed)` (and, if ever run on GPU, also
`torch.cuda.manual_seed_all(seed)`) immediately before each `predict()`
call, since sampling uses `torch.multinomial` seeded off the global RNG
state.

## `KronosPredictor.predict_batch(...)`

```python
def predict_batch(self, df_list, x_timestamp_list, y_timestamp_list,
                   pred_len, T=1.0, top_k=0, top_p=0.9, sample_count=1,
                   verbose=True):
```

Requires every series in `df_list` to share the **same** historical
length and the same `pred_len` (raises `ValueError` otherwise — checked
via `set(seq_lens)` / `set(y_lens)`). Internally batches all series (and
all Monte-Carlo samples per series) into one tensor and runs a single
autoregressive loop — this is what makes it useful for a refresh cycle
across many symbols on CPU. Same internal-averaging caveat for
`sample_count` applies per series. Returns `List[pd.DataFrame]`, one per
input series, same shape/columns as `predict()`.

**Engine implication:** for the performance budget in §5 (<90s for a
full 1h refresh across all instruments on 8-core CPU), `forecast/engine.py`
should batch the N Monte-Carlo rollouts for a *single* symbol via
`predict_batch` (N copies of the same history), not via N sequential
`predict()` calls — same result, one autoregressive loop instead of N.
Multiple symbols can additionally share a batch call as long as their
lookback windows are the same length, which they are by construction
(fixed lookback=400 for all symbols/timeframes per config).

## Model zoo (from README, cross-checked against HF repo IDs referenced in source/examples)

| Model | Tokenizer repo | `max_context` to pass | Params |
|---|---|---|---|
| Kronos-mini | `NeoQuasar/Kronos-Tokenizer-2k` | 2048 | 4.1M |
| Kronos-small | `NeoQuasar/Kronos-Tokenizer-base` | 512 | 24.7M |
| Kronos-base | `NeoQuasar/Kronos-Tokenizer-base` | 512 | 102.3M |
| Kronos-large | (not open-sourced) | — | 499.2M |

Default for this project: **Kronos-small on CPU**, `max_context=512`,
lookback=400, `pred_len=24` — configurable in `config/markets.yaml` /
`.env` toward `Kronos-base` or CUDA without code changes.

## Licensing

- `third_party/kronos/LICENSE`: MIT, Copyright (c) 2025 ShiYu. Permits
  commercial use, modification, and redistribution with attribution;
  no warranty.
- HF model cards (`NeoQuasar/Kronos-*`) were **not** independently
  re-verified for a separate license grant in this pass — the repo's own
  README asserts all listed models are "Open-source ✅" and links only to
  the HF hub pages, but the HF model card's own license field must still
  be checked before any redistribution of model weights (not just usage)
  is done. This is a flagged gap, not a confirmed clearance, since the
  live HF pages were not fetched in this session.

## Live upstream cross-check

Fetched `https://raw.githubusercontent.com/shiyu-coder/Kronos/master/README.md`
directly (2026-08-28) and confirmed the fork is current: same three most
recent News entries (AAAI 2026 acceptance 2025-11-10, finetuning scripts
2025-08-17, arXiv paper 2025-08-02), same Model Zoo table, same
`predict()` example. No drift between this fork and upstream `master`.

## Things intentionally NOT re-verified here

- The exact `config.json` contents on each HF hub repo (i.e. the literal
  `d_model`, `n_layers`, etc. for `Kronos-small`) — irrelevant to the
  application, since `from_pretrained` handles this transparently.
- GPU/CUDA code paths — this project targets CPU by default per the brief.
