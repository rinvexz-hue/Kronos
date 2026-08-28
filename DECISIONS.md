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
