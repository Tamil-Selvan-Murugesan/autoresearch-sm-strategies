# CURRENT_STATE.md

A complete snapshot of the system as of 2026-04-17. Read this first if you're making changes.

---

## 1. What the system does

An LLM agent loop that autonomously generates, executes, and refines trading strategies against historical NIFTY/BANKNIFTY/SENSEX OHLCV data. Two concurrent loops run in the same process:

| Loop | Purpose | Driver |
| --- | --- | --- |
| **Main loop** | Explores the strategy space. One planner per timeframe (5min, daily, weekly) asks the LLM for fresh strategies, executes them against that timeframe's parquet, logs stats. Stops when each timeframe hits `max_strategies_per_timeframe`. | `LangGraph StateGraph` in `graph.py` |
| **Refinement loop** | Exploits the best of what the main loop found. Polls the DB; once enough strategies exist, asks the LLM to combine winners into **cross-timeframe** strategies (entry on one timeframe, exit on another) that receive all three DataFrames at once. | Async `while` loop in `refinement.py` |

Every strategy attempt (success, zero-signals, or error) is recorded in an `attempts` table and fed back to the LLM on the next iteration as **memory** so it doesn't regenerate the same idea.

---

## 2. Architecture diagram

```
                              ┌───────────────────────────────┐
                              │          run.py               │
                              │  asyncio.gather(              │
                              │    main_graph.invoke(),       │
                              │    refinement_loop(),         │
                              │  )                            │
                              └──────────────┬────────────────┘
                                             │
         ┌───────────────────────────────────┴───────────────────────────────┐
         │                                                                   │
         ▼                                                                   ▼
┌────────────────────────────────────────────┐           ┌───────────────────────────────────┐
│  Main loop (graph.py, LangGraph)           │           │  Refinement loop (refinement.py)  │
│                                            │           │                                   │
│         START                              │           │  while not stop_event.is_set():   │
│           │                                │           │      total = Σ count_strategies() │
│           ▼                                │           │      if total < next_threshold:   │
│         router (fan-out)                   │           │          await sleep(poll)        │
│         ┌─┼───────────────┐                │           │          continue                 │
│         ▼ ▼               ▼                │           │      winners = query_winners()    │
│   plan_5min  plan_daily  plan_weekly       │           │      while not stop_event:        │
│       │         │         │                │           │          _refine_once(winners)    │
│       ▼         ▼         ▼                │           │                                   │
│   exec_5min  exec_daily  exec_weekly       │           │  _refine_once:                    │
│       │         │         │                │           │   ┌─────────────────────────┐     │
│       └────┬────┴────┬────┘                │           │   │ LLM → strategy_defs     │     │
│            ▼                               │           │   │ execute_multi_strats    │     │
│       stop_check                           │           │   │ log_run(timeframe=mixed)│     │
│            │                               │           │   │ publish(mixed)          │     │
│     ┌──────┴──────┐                        │           │   └─────────────────────────┘     │
│     │ continue    │ end                    │           └───────────────────────────────────┘
│     ▼             ▼                        │                            │
│   router         END ── stop_event.set() ──┼────────────────────────────┘
└────────────────────────────────────────────┘

 Each planner node calls the LLM, receives strategy code as JSON, parses it.
 Each exec node writes strategies/strat_<tf>.py, runs every strategy in an
 isolated exec() namespace, logs results + attempts, then publish()es.
```

Data/control flow between components:

```
┌─────────────┐     LLM code (JSON)       ┌──────────────────────┐
│  Planner    │──────────────────────────▶│  Execute (isolated)  │
│  (graph.py) │                           │  _exec_single()      │
└─────────────┘◀──── _get_tested_        └──────┬───────────────┘
         ▲            strategies()               │
         │                                       │ per-strategy exec(),
         │                                       │ catch all exceptions
         │                                       ▼
         │                                ┌──────────────┐
         │                                │  log_attempt │──┐
         │                                │  log_run     │  │
         │                                └──────┬───────┘  │
         │                                       │          ▼
         │    memory comes from  ┌───────────────┼────────┐ ┌──────────────┐
         │    attempts ─────────▶│  SQLite DB    │        │ │  publish()   │
         │                       │  (logs/*.db)  │        │ │  git plumbing│
         │                       │   runs        │        │ │  → remote    │
         │                       │   results     │        │ │  experiments/│
         │                       │   attempts    │        │ │  <tf> branch │
         │                       └───────────────┘        │ └──────────────┘
         │                                                 │
         └──── get_attempts() ◀────────────────────────────┘
```

---

## 3. Control flow, step by step

### Main loop (one iteration)

1. `START` → `router` (no-op fan-out).
2. For each timeframe *in parallel*:
   1. `plan_<tf>`: checks `count_strategies(tf) >= max_strategies_per_timeframe` — if yes, returns empty pending list and is effectively skipped. Otherwise reads `prompts/planner_<tf>.md`, calls `_get_tested_strategies(tf)` to build the memory block, sends both to the LLM, parses the JSON response into a `list[StrategyDef]`.
   2. `exec_<tf>`: calls `execute_strategies(index, tf, pending)` — which writes `strategies/strat_<tf>.py`, loads the parquet, and calls `_exec_single()` for each strategy in isolation. Calls `log_run()` to persist stats and `log_attempt()` per strategy. Then `publish()` archives the DB + strategy file to `<branch_prefix>/<tf>`.
3. All three `exec_*` converge on `stop_check`, which computes each timeframe's count and sets `should_stop = all(count >= max)`.
4. If not stopping, loop back to `router`. If stopping, END → the event loop flips `stop_event`, signalling the refinement loop to exit.

### Refinement loop (per threshold crossing)

1. Poll every `refinement_poll_interval` seconds. Compute `total = Σ count_strategies(tf)` across main timeframes.
2. If `total < next_threshold`, sleep and retry.
3. Once the threshold is crossed, call `query_winners()` once to get the current set of winners.
4. Inner loop — keep calling `_refine_once(llm, winners)` back-to-back until `total` crosses the *next* threshold; then advance `next_threshold` and refresh winners.
5. `_refine_once()`:
   - Build the prompt with: current winners (JSON) + prior mixed attempts memory (`get_attempts("mixed")`) + "generate N strategies".
   - Parse LLM JSON → `strategy_defs`.
   - `execute_multi_strategies()` — each strategy function receives `(df_5min, df_daily, df_weekly)` and runs in an isolated namespace.
   - `log_run()` under `timeframe="mixed"`, `log_attempt()` per strategy, `publish()` to `<prefix>/mixed`.

---

## 4. Strategy contract (what the LLM must produce)

The LLM returns JSON like:

```json
[
  {
    "name": "some_identifier",
    "params": {"signal": "human-readable description", "...": "..."},
    "code": "def some_identifier(df):\n    params = {...}\n    result = ...\n    return params, result\n"
  },
  ...
]
```

- `name` is the function name; it must match the `def` in `code`.
- `params` is logged verbatim to the DB and also shown back as memory.
- `code` is executed via `exec(code, {"pd": pd, "np": np})`. Only pandas/numpy are available — no imports.

Signatures:

| Where | Signature |
| --- | --- |
| Main loop planners | `def fn(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]` |
| Refinement planner | `def fn(df_5min, df_daily, df_weekly) -> tuple[dict, pd.DataFrame]` |

Result DataFrame must have `date`, `close`, and **one or more** columns prefixed `fwd_` (e.g. `fwd_1d`, `fwd_5d`, `fwd_10bars`). `summarize()` in `backtest.py` computes stats from every `fwd_*` column.

**Winner definition** (what `query_winners()` selects):

```
long  winner : win_rate >= min_win_rate         AND mean >=  min_abs_mean
short winner : win_rate <= (100 - min_win_rate) AND mean <= -min_abs_mean
AND in both cases: signals >= min_signals
```

Defaults (see `config.yaml`): `min_win_rate=60`, `min_abs_mean=0.30` (percent), `min_signals=30`. `direction` is derived from the sign of `mean` — positive = `long`, negative = `short`.

**Expectancy** is still computed and stored on every `results` row as context for the refinement prompt:

```
expectancy = (win_rate/100) × avg_win + (1 − win_rate/100) × avg_loss
```

but it is **not** part of the winner filter.

---

## 5. Database schema

Single SQLite file at `db_path` (config). Migrations are additive and handled by `_ensure_db()` on every connect — it runs `CREATE TABLE IF NOT EXISTS`, then uses `PRAGMA table_info` + `ALTER TABLE` to add columns to legacy DBs, then creates indexes.

```
runs                          one row per log_run() call (one batch of strategies)
├── run_id   (PK)
├── run_at   TEXT (ISO)
├── iteration INT             main-loop iteration number; NULL for refinement runs
├── index_name TEXT           "NIFTY"
├── timeframe  TEXT           "5min" | "daily" | "weekly" | "mixed"
├── data_from  TEXT           full range of the source parquet (not signal dates)
└── data_to    TEXT

results                       one row per (strategy, horizon) pair
├── id        (PK)
├── run_id    (FK → runs)
├── timeframe TEXT             DENORMALIZED — same value as runs.timeframe, for fast filtering
├── strategy  TEXT             matches attempts.strategy
├── params    TEXT             JSON of the strategy's params dict
├── signals   INT              rows in result DataFrame
├── horizon   TEXT             e.g. "fwd_1d"
├── count, mean, median, std, min, max,
│   win_rate, avg_win, avg_loss, expectancy    (computed by summarize())

attempts                      PLANNER MEMORY — one row per strategy the LLM produced
├── id         (PK)
├── created_at TEXT (ISO)
├── timeframe  TEXT
├── strategy   TEXT
├── params     TEXT (JSON)
├── status     TEXT            'ok' | 'zero_signals' | 'error'
├── signals    INT  (nullable)
└── error      TEXT (nullable) — full "TypeName: message" string
```

**Why `attempts` exists:** a failing strategy (syntax/runtime error) or a zero-signal strategy leaves no rows in `results`, so without `attempts` the LLM has no way to know it was ever tried, and it would regenerate the same idea over and over. The `attempts` table closes that loop.

**Key queries:**

- `count_strategies(tf)` → counts distinct strategy names in `attempts` (guarantees loop termination even when the LLM produces buggy code).
- `query_winners(min_win_rate, min_abs_mean, min_signals, tf)` → selects from `results` with `(win_rate, mean)` thresholds for both long and short, and adds a `direction` column from the sign of `mean`.
- `get_attempts(tf)` → reads the last 500 rows ordered by `created_at` — used directly as planner memory.

---

## 6. Configuration

All tunables live in `config.yaml`. `config.py` is a 3-line loader exposing `CFG`. Module-level constants in `backtest.py`, `graph.py`, `refinement.py` bind at import time — **changes require a process restart**.

| Key | Effect |
| --- | --- |
| `index` | Which parquet file to load (`data/<tf>/<index>_<tf>.parquet`). |
| `timeframes` | Main-loop timeframes. Adding one creates a new planner + exec node automatically via `build_graph()`. |
| `strategies_per_iteration` | Strategies the LLM is asked for per planner call. |
| `max_strategies_per_timeframe` | Main-loop stop threshold per timeframe. |
| `strategies_per_refinement` | Strategies per refinement LLM call. |
| `refinement_poll_interval` | Seconds between DB polls when below threshold. |
| `refinement_threshold_step` | Refinement fires every N *cumulative* main-loop strategies. |
| `min_win_rate`, `min_abs_mean`, `min_signals`, `max_winners` | Winner criteria for refinement's input (see §4 for the exact rule). |
| `db_path` | SQLite file path. |
| `branch_prefix` | Git branch prefix for `publish()`. Final branch names are `<prefix>/<timeframe>` plus `<prefix>/mixed`. |
| `llm.provider` | `azure_openai` | `openai` | `anthropic` — selected by `run.py::_make_llm()`. |
| `llm.model` | Model or Azure deployment name. |
| `llm.api_version` | Azure-only. |

---

## 7. Module responsibilities

| File | What's in it |
| --- | --- |
| `run.py` | Entry point. `_make_llm()` factory per provider. Builds `llm_config` dict (one LLM per planner key). Launches main graph (in an executor) + refinement loop via `asyncio.gather`. |
| `state.py` | `StrategyDef` TypedDict (`name`, `params`, `code`) and `GraphState` (pending/results/counts per timeframe + iteration + should_stop). |
| `config.py` / `config.yaml` | Loader + values. |
| `backtest.py` | **Core engine.** Data load, DB schema/migrations, `summarize()`, `log_run()`, `count_strategies()`, `query_winners()`, `log_attempt()`, `get_attempts()`, `publish()` (git plumbing), `execute_strategies()`, `_exec_single()` (isolated per-strategy exec). |
| `backtest_multi.py` | Multi-timeframe engine for refinement strategies. Reuses `_exec_single()` and `log_attempt()` from `backtest.py`. |
| `graph.py` | LangGraph definition. Planner node factory, exec+log node factory, stop_check, router. `build_graph()` iterates over `CFG["timeframes"]` so the graph shape is config-driven. |
| `refinement.py` | Async refinement loop. Threshold tracking, winner fetching, `_refine_once()`. |
| `prompts/planner_{5min,daily,weekly,refinement}.md` | System prompts; `{n}` placeholder is replaced with the strategy count. |
| `strategies/strat_<tf>.py` | **Auto-generated each iteration, overwritten.** Not imported — only archived to the experiments branch for inspection. |

---

## 8. Publish mechanism — important, non-obvious

`publish()` uses **git plumbing** (`write-tree` in an isolated temp index → `commit-tree` → direct push to `refs/heads/<branch>`). Crucially it:

- **Never** checks out a branch, resets HEAD, stashes, or touches the working tree.
- Builds a fresh tree containing **only** `logs/backtest.db` and `strategies/strat_<tf>.py` — no code files leak into the experiments branches.
- Parents onto the remote branch's current tip (via `ls-remote`) if it exists, otherwise creates an orphan commit.
- Serializes all git calls via a module-level `threading.Lock` (`_git_lock`) because the three `exec_*` nodes run in parallel.

**Why this matters:** the three `exec_*` nodes run concurrently on whatever branch the user has checked out (typically `main`). A naive `git add / commit / push` would mix auto-commits into `main`. The plumbing approach keeps `main` clean: it contains **only** your real code commits, while `<branch_prefix>/{5min,daily,weekly,mixed}` hold the experimental logs in parallel. Do not "simplify" `publish()` back to `add/commit/push`.

Branches are log-only — they have no relation to `main`'s history (orphaned from first push onward).

---

## 9. Error handling and memory (the recent additions)

### Error isolation

`_exec_single(sdef, dfs)` in `backtest.py`:

- Runs the strategy's code in a **fresh namespace** (`{"pd": pd, "np": np}`) via `exec()`. A syntax error or `NameError` therefore can't poison anything outside that call.
- Wraps everything in `try/except Exception`. Print a one-line `[WARN] Strategy X failed: <type>: <msg>` to stdout, full traceback to stderr (via `traceback.print_exc()`).
- Returns `(params, empty_df, 'error', err_string)` on failure, so the caller records the attempt but doesn't crash.
- Also validates: the function must be callable, and its return must be a `DataFrame`.

Both `execute_strategies()` and `execute_multi_strategies()` route every strategy through `_exec_single()`, then always call `log_attempt()`. No exception path reaches the LangGraph node.

### Planner memory

Every strategy the LLM generates is persisted to `attempts` with status `ok`, `zero_signals`, or `error` + error message. On the next iteration, `_get_tested_strategies(tf)` (main) and the tested-mixed block (refinement) dump the last 500 attempts into the prompt — including failures with their error messages so the LLM can learn. Example line in the prompt:

```
- intraday_vwap_cross_and_hold [FAILED: TypeError: bad operand type for unary ~: 'float'] — Signal is True when the close crosses above ...
```

This fixed the pathological case where the 5min planner kept regenerating the same broken VWAP-cross variants because they left no trace in `results`.

---

## 10. How to make common changes

| I want to… | Do this |
| --- | --- |
| **Add a new timeframe.** | Add it to `timeframes` in `config.yaml`. Add `prompts/planner_<tf>.md`. Add a `<tf>` key to `data_dirs`. Place `data/<tf>/<index>_<tf>.parquet`. `build_graph()` will wire it up automatically. |
| **Change strategy batch size.** | `strategies_per_iteration` / `strategies_per_refinement` in config. |
| **Change winner definition.** | `min_win_rate`, `min_abs_mean`, `min_signals` in config. Applied to refinement's input pool (see §4). |
| **Swap LLM provider.** | Set `llm.provider` to `openai`/`anthropic` and set `llm.model`. Export the appropriate API keys. `run.py::_make_llm()` handles the rest. |
| **Use a different LLM per planner.** | In `run.py`, replace the `llm_config = {f"planner_{tf}": llm for tf in _timeframes}` dict with individual instances. Keys are `planner_5min`, `planner_daily`, `planner_weekly`, `planner_refinement`. |
| **Tweak a planner's prompt.** | Edit `prompts/planner_<tf>.md`. `{n}` is replaced with the strategy count at load time. |
| **Change what gets archived to the experiments branches.** | Edit the `files_to_archive` list in `publish()` in `backtest.py`. |
| **Query the DB from outside the app.** | `sqlite3 logs/backtest-1.db 'SELECT * FROM attempts WHERE status = "error"'`. Tables: `runs`, `results`, `attempts`. |
| **Inspect why the 5min planner is stuck.** | `SELECT strategy, status, error FROM attempts WHERE timeframe = '5min' ORDER BY created_at DESC`. Errors show what the LLM keeps getting wrong. |
| **Reset an experiment from scratch.** | Delete the DB file at `db_path` and the `experiments-*/*` branches on the remote. `_ensure_db()` recreates the schema on first connect. |

---

## 11. Running it

```bash
# First-time
uv sync

# Required env vars (Azure OpenAI provider)
export AZURE_OPENAI_API_KEY=...
export AZURE_OPENAI_ENDPOINT=...
export AZURE_OPENAI_API_VERSION=2024-12-01-preview

# Run
uv run python run.py

# Sanity check imports after edits
uv run python -c "import state, backtest, backtest_multi, graph, refinement, run"
```

If exporting vars in `~/.bashrc`, put them **above** the non-interactive early-return guard, otherwise `uv run` won't see them.

---

## 12. Known limitations and open areas

- **Prompt size**: the memory block can grow large with hundreds of attempts. Currently capped at 500 rows per lookup and each line's `signal` description at 180 chars. If the prompt starts hitting token limits, either reduce the cap or compress older entries (e.g. drop params, keep just name + status + error).
- **Git auth**: `publish()` assumes `origin` is reachable with current credentials. A push failure is logged as `[WARN]` and the app continues — no retry.
- **DB path is shared** across all timeframes. With parallel `exec_*` nodes, writes are serialized via SQLite's own locking, not by the `_git_lock`.
- **No test suite.** Smoke-test via the sanity-check import command above; structural correctness of changes to the engine should be verified by running a short real session.
- **Strategies branches don't track code changes** to `main`. If `publish()` itself changes, prior experiments branches remain valid but won't contain the new behavior.
- **No per-strategy timeout.** A runaway strategy function could block its batch indefinitely. Currently not guarded.
