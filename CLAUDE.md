# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                      # install deps (Python >= 3.11)
uv run python run.py         # run the full system (main loop + refinement loop)
```

No test suite. To quickly sanity-check imports after edits:

```bash
uv run python -c "import state, backtest, backtest_multi, graph, refinement, run"
```

### Required environment variables (Azure OpenAI provider)

`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`. Exports must live **above** the non-interactive early-return guard in `~/.bashrc` or `uv run` won't see them.

### Inspecting results

```bash
sqlite3 logs/backtest-1.db '.schema'                        # see tables (runs, results)
sqlite3 logs/backtest-1.db 'SELECT * FROM results LIMIT 5'
```

DB path is `db_path` in `config.yaml`.

## Architecture

### Two concurrent loops

`run.py` launches these together via `asyncio.gather`:

1. **Main loop** (`graph.py`) — a LangGraph `StateGraph`. Router fans out in parallel to one planner node per timeframe (`plan_5min`, `plan_daily`, `plan_weekly`), each of which calls the LLM to generate strategy code. Each feeds its own `exec_<tf>` node which writes the code to `strategies/strat_<tf>.py`, dynamically imports it, runs each strategy function against the loaded parquet, logs results to SQLite, and calls `publish()`. All three `exec_*` nodes converge on `stop_check`, which loops until every timeframe has `>= max_strategies_per_timeframe` distinct strategies logged.

2. **Refinement loop** (`refinement.py`) — an independent async loop. Polls `count_strategies()` for the main timeframes; when the cumulative total crosses the next threshold (multiples of `refinement_threshold_step`), it fetches winners (see **Winner semantics** below) and asks the LLM for cross-timeframe strategies. Those are executed via `backtest_multi.execute_multi_strategies()`, which passes **all three DataFrames** to each strategy function. Results go to the same DB with `timeframe='mixed'`.

### LLM-generated strategy contract

Strategies are Python function source produced by the LLM and written to `strategies/strat_<tf>.py`. Each main-loop strategy signature: `def fn(df) -> tuple[dict, pd.DataFrame]`. Refinement strategy signature: `def fn(df_5min, df_daily, df_weekly) -> tuple[dict, pd.DataFrame]`. Result DataFrame must contain `date`, `close`, and one or more `fwd_<N>d` / `fwd_<N>bars` columns of forward returns in percent. `summarize()` computes stats from every column whose name starts with `fwd_`.

### Winner semantics

A strategy is a winner when it shows **both** directional consistency **and** significant average movement, plus enough signals to matter:

- **Long winner** : `win_rate >= min_win_rate` AND `mean >=  min_abs_mean`
- **Short winner**: `win_rate <= (100 - min_win_rate)` AND `mean <= -min_abs_mean`
- AND `signals >= min_signals` in both cases.

Defaults: `min_win_rate=60`, `min_abs_mean=0.30`, `min_signals=30` (see `config.yaml`). `query_winners()` adds a `direction` column derived from the sign of `mean`. Expectancy — `(win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)` — is still computed and stored on every row, but it is no longer part of the filter; it's kept only for context in the refinement prompt.

### Publish flow — important, non-obvious

`backtest.publish()` uses **git plumbing** (`write-tree` in an isolated temp index, `commit-tree`, direct push to `refs/heads/<branch>`). It intentionally **does not** touch HEAD, the working tree, or the real index. Do not "simplify" to `git add / commit / push` — that regresses to polluting whatever branch is checked out (which is `main`) with auto-commits. Each run produces commits on `experiments-1/{5min,daily,weekly,mixed}` branches containing only the DB snapshot and that timeframe's generated strategy file, parented onto the previous remote tip (or orphaned on first push).

A module-level `threading.Lock` (`_git_lock`) serializes all git operations because the three `exec_*` nodes run in parallel.

### DB schema + migrations

`_ensure_db()` runs on every connect. It creates tables if absent, uses `PRAGMA table_info` + `ALTER TABLE` to add new columns on existing DBs, and backfills the denormalized `timeframe` column on `results` from the `runs` table. `count_strategies()` and `query_winners()` rely on `results.timeframe` (no join needed for filtering). The `strategies_per_refinement` runs write `timeframe='mixed'` without an `iteration`.

`runs.strategies` holds a JSON array `[{name, params, code}, ...]` for every strategy in the batch. This is the only persisted source of the generated code — `strategies/strat_<tf>.py` is overwritten every iteration — so to inspect how a specific `results` row was computed: `SELECT strategies FROM runs WHERE run_id = <x>` and parse the JSON.

### Config

All tunables live in `config.yaml` (loaded once by `config.py` as `CFG`). Module-level constants in `backtest.py`, `graph.py`, `refinement.py` are derived from `CFG` at import time — changing config requires a process restart. `BRANCH_MAP` in `backtest.py` is built from `branch_prefix` + `timeframes` + a hardcoded `"mixed"` entry.

### LLM provider abstraction

`run.py::_make_llm()` dispatches on `CFG["llm"]["provider"]` — `azure_openai` | `openai` | `anthropic`. All planner nodes and the refiner share one LLM instance by default.

## Known failure modes

- **LLM regenerates failing strategies**: if generated code raises at import time or produces zero signals, nothing is written to `results`, so `count_strategies()` and `_get_tested_strategies()` don't see it, and the planner generates the same name/idea again. The 5min loop is especially prone to this (VWAP-cross variants). Import-time failures are not currently caught per-strategy — one syntax error kills the whole batch.
- **Per-strategy runtime errors** are caught in `execute_strategies()` / `execute_multi_strategies()` and logged as `[WARN] Strategy X failed: …`; the run continues with an empty DataFrame for that strategy (which means no row in `results`).
