# Automated Backtesting System — Implementation Spec

You are building an automated backtesting system using **LangGraph**. The system uses LLM agents to generate trading strategies, execute them against historical market data, log results to SQLite, push to GitHub, and iteratively refine strategies based on winners.

Read this entire document before writing any code. Every section matters.

---

## 1. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  MAIN LOOP (LangGraph)                                               │
│                                                                      │
│                      ┌──────────────┐                                │
│                      │    Router    │ (fan-out to parallel planners)  │
│                      └──────┬───────┘                                │
│              ┌──────────────┼──────────────┐                         │
│              ▼              ▼              ▼                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │ Planner Node │  │ Planner Node │  │ Planner Node │               │
│  │   (5-min)    │  │   (daily)    │  │  (weekly)    │               │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │
│         │                 │                 │                        │
│         ▼                 ▼                 ▼                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │  Exec + Log  │  │  Exec + Log  │  │  Exec + Log  │  (parallel)   │
│  │   (5-min)    │  │   (daily)    │  │  (weekly)    │  SQLite +     │
│  │  + Git push  │  │  + Git push  │  │  + Git push  │  GitHub push  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │
│         │                 │                 │                        │
│         └────────────┬────┴─────────────────┘                        │
│                      ▼                                               │
│              ┌──────────────┐                                        │
│              │  Stop Check  │── 50 per timeframe? → END              │
│              └──────────────┘── else → loop back to Router           │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│  REFINEMENT LOOP (independent async task, runs concurrently)         │
│                                                                      │
│         ┌──────────────┐                                             │
│    ┌───►│  Poll DB     │ (every 3 min: count total strategies)       │
│    │    └──────┬───────┘                                             │
│    │           │                                                     │
│    │           ▼                                                     │
│    │    ┌──────────────┐   no                                        │
│    │    │ Hit next 15  │────────────────────┐                        │
│    │    │ threshold?   │                    │ (sleep 3 min, poll)    │
│    │    └──────┬───────┘                    │                        │
│    │      yes  │                            │                        │
│    │           ▼                            │                        │
│    │    ┌──────────────┐                    │                        │
│    │    │ Refresh      │ (query_winners     │                        │
│    │    │ winner pool  │  from ALL logged)  │                        │
│    │    └──────┬───────┘                    │                        │
│    │           │  no winners? ──────────────┤                        │
│    │           │                            │                        │
│    │           ▼                            │                        │
│    │    ┌──────────────┐                    │                        │
│    │    │  Refinement  │ generate 5 cross-  │                        │
│    │    │  Planner     │ timeframe strats   │                        │
│    │    └──────┬───────┘                    │                        │
│    │           │                            │                        │
│    │           ▼                            │                        │
│    │    ┌──────────────┐                    │                        │
│    │    │  Exec + Log  │ backtest_multi.py  │                        │
│    │    │   (mixed)    │ all 3 DataFrames   │                        │
│    │    └──────┬───────┘                    │                        │
│    │           │                            │                        │
│    │           ▼                            │                        │
│    │    ┌──────────────┐   no              │                        │
│    │    │ Next 15      │───► loop back to  │                        │
│    │    │ threshold    │    Refinement     │                        │
│    │    │ reached?     │    Planner        │                        │
│    │    └──────┬───────┘                    │                        │
│    │      yes  │                            │                        │
│    │           └────────────────────────────┘                        │
│    │                    (refresh winners from new threshold)          │
│    │                                                                 │
│    │    Main loop stopped? → EXIT                                    │
│    └─────────────────────────────────────────────────────────────────┘
└──────────────────────────────────────────────────────────────────────┘
```

### Two independent loops

The system runs **two concurrent tasks** that share only the SQLite database:

**Main loop (LangGraph graph):**

1. Router fans out to three planner nodes **in parallel** — one per timeframe (5min, daily, weekly).
2. Each planner generates exactly **5 strategy functions**.
3. Each planner feeds into its own **exec+log node** (parallel) — executes strategies, logs to SQLite, pushes to GitHub.
4. All three exec+log nodes converge into **stop check**.
5. **Stop check**: if a timeframe has reached 50 logged strategies, its planner returns empty on the next iteration (skips). When all three reach 50, the main loop terminates.
6. Otherwise, loops back to the **router** for the next iteration.
7. **No refinement in this loop** — planners are never blocked by refinement.

**Refinement loop (independent async task):**

1. Polls the database every **3 minutes**, counting total logged strategies (across all timeframes, excluding `mixed`).
2. Tracks a **threshold counter** starting at 15, incrementing by 15 each time (15, 30, 45, 60, ...).
3. When total strategies reach the current threshold:
   - **Refresh winner pool**: `query_winners()` from ALL strategies logged so far.
   - If no winners (`|expectancy| >= 0.15%` and `signals >= 30`), stay idle and poll again.
   - If winners exist, enter an **inner loop**: generate 5 cross-timeframe strategies using `backtest_multi.py`, execute+log them, then immediately generate 5 more — keep going until the DB count hits the **next** threshold (meaning the main loop has produced another batch of 15).
   - When the next threshold is hit, **refresh the winner pool** (now includes strategies from the new batch) and continue the inner loop with updated winners.
4. Stops when the main loop signals completion (e.g., via a shared event or by checking `count_strategies()` for all timeframes >= 50).

---

## 2. Project Structure

```
download-data/
├── data/
│   ├── 5min/              # 5-minute OHLCV parquet files
│   │   └── NIFTY_5min.parquet
│   ├── daily/             # daily OHLCV parquet files
│   │   └── NIFTY_daily.parquet
│   └── weekly/            # weekly OHLCV parquet files
│       └── NIFTY_weekly.parquet
├── logs/
│   └── backtest.db        # single SQLite database for all timeframes
├── strategies/
│   ├── strat_5min.py      # LLM-written strategy file for 5min (overwritten each iteration)
│   ├── strat_daily.py     # LLM-written strategy file for daily (overwritten each iteration)
│   ├── strat_weekly.py    # LLM-written strategy file for weekly (overwritten each iteration)
│   └── strat_mixed.py     # LLM-written strategy file for refinement (overwritten each iteration)
├── prompts/
│   ├── planner_5min.md    # system prompt for 5min planner
│   ├── planner_daily.md   # system prompt for daily planner
│   ├── planner_weekly.md  # system prompt for weekly planner
│   └── planner_refinement.md  # system prompt for refinement planner
├── backtest.py            # core engine for single-timeframe strategies (5min/daily/weekly nodes)
├── backtest_multi.py      # core engine for multi-timeframe strategies (refinement loop)
├── graph.py               # LangGraph graph definition and main loop node functions
├── refinement.py          # independent refinement loop (async, runs alongside graph)
├── state.py               # graph state schema
└── run.py                 # entry point — launches main graph + refinement loop concurrently
```

### Data file conventions

All parquet files have the same columns: `date`, `open`, `high`, `low`, `close`, `volume`.

- `data/5min/NIFTY_5min.parquet` — `date` is a datetime with intraday timestamps
- `data/daily/NIFTY_daily.parquet` — `date` is a date-level datetime (copy from existing `data/NIFTY_daily.parquet`)
- `data/weekly/NIFTY_weekly.parquet` — `date` is the week-ending date

The data directories will be populated manually. The system should fail clearly with a `FileNotFoundError` if data is missing — do not silently skip.

---

## 3. Graph State Schema

Define this in `state.py`:

```python
from typing import TypedDict

class StrategyDef(TypedDict):
    """A single strategy as produced by a planner node."""
    name: str           # function name, e.g. "gap_down_reversal"
    params: dict        # full param dict for logging
    code: str           # the Python function body as a string

class GraphState(TypedDict):
    """State for the MAIN loop only. Refinement runs independently."""
    # Current iteration's work
    pending_5min: list[StrategyDef]
    pending_daily: list[StrategyDef]
    pending_weekly: list[StrategyDef]

    # Execution results for current iteration (reset each loop)
    results_5min: list[tuple[dict, dict]]       # list of (params, {horizon: summary_stats})
    results_daily: list[tuple[dict, dict]]
    results_weekly: list[tuple[dict, dict]]

    # Cumulative counters (increment across iterations)
    count_5min: int
    count_daily: int
    count_weekly: int

    # Control
    iteration: int
    should_stop: bool
```

---

## 4. Core Engine — `backtest.py`

Adapt the existing POC code. Key changes from POC:

### 4.1. Data loading — timeframe-aware

```python
DATA_DIRS = {
    "5min": "data/5min",
    "daily": "data/daily",
    "weekly": "data/weekly",
}

def load(index: str = "NIFTY", timeframe: str = "daily") -> pd.DataFrame:
    """Load OHLCV data for a given index and timeframe."""
    data_dir = DATA_DIRS[timeframe]
    df = pd.read_parquet(f"{data_dir}/{index}_{timeframe}.parquet")
    return df.sort_values("date").reset_index(drop=True)
```

### 4.2. Database schema — add timeframe column + expectancy

```python
def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at     TEXT NOT NULL,
            index_name TEXT NOT NULL,
            timeframe  TEXT NOT NULL,
            data_from  TEXT,
            data_to    TEXT
        );
        CREATE TABLE IF NOT EXISTS results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER NOT NULL REFERENCES runs(run_id),
            strategy    TEXT NOT NULL,
            params      TEXT NOT NULL,
            signals     INTEGER NOT NULL,
            horizon     TEXT NOT NULL,
            count       INTEGER,
            mean        REAL,
            median      REAL,
            std         REAL,
            min         REAL,
            max         REAL,
            win_rate    REAL,
            avg_win     REAL,
            avg_loss    REAL,
            expectancy  REAL
        );
        CREATE INDEX IF NOT EXISTS idx_results_expectancy ON results(expectancy);
        CREATE INDEX IF NOT EXISTS idx_results_win_rate ON results(win_rate);
        CREATE INDEX IF NOT EXISTS idx_results_strategy ON results(strategy);
        CREATE INDEX IF NOT EXISTS idx_runs_timeframe ON runs(timeframe);
    """)
```

**Winner definition**: A strategy is a **winner** when `|expectancy| >= 0.15` and `signals >= 30`. Expectancy is computed as:

```
expectancy = (win_rate/100 × avg_win) + ((1 - win_rate/100) × avg_loss)
```

Where `avg_win` is the mean of positive returns and `avg_loss` is the mean of negative returns (a negative number).

**Both directions are actionable:**
- `expectancy = +0.45%` → go **long** when signal fires
- `expectancy = -0.33%` → go **short** when signal fires (flip the trade)

Only strategies near zero (~0%) are useless — no edge in either direction. The query must use `ABS(expectancy)` and tag each result with a `direction` column (`long` or `short`).

### 4.3. Logging — accept timeframe

```python
def log_run(
    index: str,
    timeframe: str,
    strategy_names: list[str],
    run_results: list[tuple[dict, pd.DataFrame]],
) -> int:
    """Write results to SQLite. Returns the run_id."""
    run_time = datetime.now().isoformat()

    data_from = data_to = None
    for _, result in run_results:
        if "date" in result.columns and not result.empty:
            data_from = str(result["date"].min().date())
            data_to = str(result["date"].max().date())
            break

    conn = sqlite3.connect(DB_PATH)
    _init_db(conn)

    cur = conn.execute(
        "INSERT INTO runs (run_at, index_name, timeframe, data_from, data_to) VALUES (?, ?, ?, ?, ?)",
        (run_time, index, timeframe, data_from, data_to),
    )
    run_id = cur.lastrowid

    for name, (params, result) in zip(strategy_names, run_results):
        fwd_cols = [c for c in result.columns if c.startswith("fwd_")]
        params_json = json.dumps(params)
        for col in fwd_cols:
            s = summarize(result[col])
            if not s:
                continue
            conn.execute(
                """INSERT INTO results
                   (run_id, strategy, params, signals, horizon, count, mean, median, std, min, max,
                    win_rate, avg_win, avg_loss, expectancy)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, name, params_json, len(result), col,
                 s["count"], s["mean"], s["median"], s["std"], s["min"], s["max"],
                 s["win_rate"], s["avg_win"], s["avg_loss"], s["expectancy"]),
            )

    conn.commit()
    conn.close()
    return run_id
```

### 4.4. Count strategies per timeframe

```python
def count_strategies(timeframe: str) -> int:
    """Return number of distinct strategies logged for a timeframe."""
    conn = sqlite3.connect(DB_PATH)
    _init_db(conn)
    cur = conn.execute(
        """SELECT COUNT(DISTINCT res.strategy)
           FROM results res JOIN runs r ON r.run_id = res.run_id
           WHERE r.timeframe = ?""",
        (timeframe,),
    )
    count = cur.fetchone()[0]
    conn.close()
    return count
```

### 4.5. Query winners — expectancy-based

A winner is defined by **expectancy** (expected return per trade), not just win rate.

```python
def query_winners(
    min_abs_expectancy: float = 0.15,
    min_signals: int = 30,
    timeframe: str | None = None,
    limit: int = 50,
) -> pd.DataFrame:
    """Find strategies with strong edge in either direction (long or short)."""
    conn = sqlite3.connect(DB_PATH)
    _init_db(conn)
    base_query = """
        SELECT r.run_at, r.index_name, r.timeframe, res.strategy, res.params,
               res.horizon, res.signals, res.mean, res.median, res.std,
               res.win_rate, res.avg_win, res.avg_loss, res.expectancy,
               CASE WHEN res.expectancy >= 0 THEN 'long' ELSE 'short' END AS direction
        FROM results res JOIN runs r ON r.run_id = res.run_id
        WHERE ABS(res.expectancy) >= ? AND res.signals >= ?
    """
    if timeframe:
        base_query += " AND r.timeframe = ?"
        base_query += " ORDER BY ABS(res.expectancy) DESC LIMIT ?"
        df = pd.read_sql_query(base_query, conn,
                               params=(min_abs_expectancy, min_signals, timeframe, limit))
    else:
        base_query += " ORDER BY ABS(res.expectancy) DESC LIMIT ?"
        df = pd.read_sql_query(base_query, conn,
                               params=(min_abs_expectancy, min_signals, limit))
    conn.close()
    return df
```

### 4.6. `summarize` — with expectancy

```python
def summarize(returns: pd.Series) -> dict:
    s = returns.dropna()
    if s.empty:
        return {}
    wins = s[s > 0]
    losses = s[s <= 0]
    win_rate = float(len(wins) / len(s) * 100)
    avg_win = float(wins.mean()) if len(wins) > 0 else 0.0
    avg_loss = float(losses.mean()) if len(losses) > 0 else 0.0

    # Expectancy = expected return per trade
    # (win_rate/100 * avg_win) + (loss_rate/100 * avg_loss)
    # avg_loss is already negative, so this naturally penalizes losses
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    return {
        "count": int(len(s)),
        "mean": round(float(s.mean()), 3),
        "median": round(float(s.median()), 3),
        "std": round(float(s.std()), 3),
        "min": round(float(s.min()), 3),
        "max": round(float(s.max()), 3),
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "expectancy": round(expectancy, 3),
    }
```

### 4.7. Publish — adapted for automated runs

```python
def publish(
    branch: str,
    timeframe: str,
    strategy_names: list[str],
    run_results: list[tuple[dict, pd.DataFrame]],
) -> None:
    """Stage, commit with searchable message, and push."""
    lines = [f"backtest({timeframe}): {len(strategy_names)} strategies"]
    lines.append("")
    for name, (params, result) in zip(strategy_names, run_results):
        fwd_cols = [c for c in result.columns if c.startswith("fwd_")]
        best_exp = -999.0
        best_stats = {}
        for col in fwd_cols:
            s = summarize(result[col])
            if s and s["expectancy"] > best_exp:
                best_exp = s["expectancy"]
                best_stats = s
        lines.append(
            f"- {name}: {len(result)} signals, "
            f"expectancy={best_exp}%, win_rate={best_stats.get('win_rate', 0)}%, "
            f"avg_win={best_stats.get('avg_win', 0)}%, avg_loss={best_stats.get('avg_loss', 0)}%"
        )
        for k, v in params.items():
            lines.append(f"    {k}: {v}")
        lines.append("")

    msg = "\n".join(lines)
    _run_git("add", "logs/backtest.db", "strategies/")
    _run_git("commit", "-m", msg)

    current = _run_git("branch", "--show-current").stdout.strip()
    if current != branch:
        _run_git("push", "-u", "origin", f"HEAD:{branch}")
    else:
        _run_git("push", "-u", "origin", branch)
```

### 4.8. Execute LLM-generated strategies (single-timeframe — `backtest.py`)

Used by the 3 main nodes (5min, daily, weekly). Simple: one timeframe, one DataFrame, all strategies share it.

```python
import importlib
import importlib.util
import sys

def execute_strategies(
    index: str,
    timeframe: str,
    strategy_defs: list[StrategyDef],
) -> list[tuple[dict, pd.DataFrame]]:
    """Write LLM-generated strategies to a file, import, and execute them."""
    # 1. Build the strategy file
    strat_file = Path(f"strategies/strat_{timeframe}.py")
    strat_file.parent.mkdir(exist_ok=True)

    file_lines = [
        "# AUTO-GENERATED by planner node. Overwritten each iteration.",
        "import pandas as pd",
        "import numpy as np",
        "",
    ]
    for sdef in strategy_defs:
        file_lines.append(sdef["code"])
        file_lines.append("")

    strat_file.write_text("\n".join(file_lines))

    # 2. Import the module
    module_name = f"strat_{timeframe}"
    spec = importlib.util.spec_from_file_location(module_name, strat_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)

    # 3. Load data once
    df = load(index=index, timeframe=timeframe)

    # 4. Execute each strategy
    results = []
    for sdef in strategy_defs:
        try:
            fn = getattr(mod, sdef["name"])
            params, result_df = fn(df)
            results.append((params, result_df))
        except Exception as e:
            print(f"[WARN] Strategy {sdef['name']} failed: {e}")
            results.append((sdef["params"], pd.DataFrame()))  # empty result

    return results
```

### 4.9. `backtest_multi.py` — Multi-timeframe engine (refinement loop)

This is a **separate file** from `backtest.py`. The refinement loop uses this instead.

The key difference: strategies receive **all 3 DataFrames** (`df_5min`, `df_daily`, `df_weekly`) and can implement cross-timeframe logic like "enter on daily signal, exit on weekly signal."

```python
"""Multi-timeframe backtest engine for refinement strategies."""

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

from backtest import load, summarize, log_run, publish, DB_PATH, _run_git


def load_all(index: str = "NIFTY") -> dict[str, pd.DataFrame]:
    """Load all 3 timeframes once. Returns dict keyed by timeframe."""
    return {
        "5min": load(index, "5min"),
        "daily": load(index, "daily"),
        "weekly": load(index, "weekly"),
    }


def execute_multi_strategies(
    index: str,
    strategy_defs: list,
) -> list[tuple[dict, pd.DataFrame]]:
    """Execute refinement strategies that can access all timeframes.

    Each strategy function receives 3 DataFrames:
        def strategy(df_5min, df_daily, df_weekly) -> (params, result_df)
    """
    # 1. Write strategy file
    strat_file = Path("strategies/strat_mixed.py")
    strat_file.parent.mkdir(exist_ok=True)

    file_lines = [
        "# AUTO-GENERATED by refinement planner. Overwritten each iteration.",
        "import pandas as pd",
        "import numpy as np",
        "",
    ]
    for sdef in strategy_defs:
        file_lines.append(sdef["code"])
        file_lines.append("")

    strat_file.write_text("\n".join(file_lines))

    # 2. Import
    module_name = "strat_mixed"
    spec = importlib.util.spec_from_file_location(module_name, strat_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)

    # 3. Load all timeframes once
    data = load_all(index)

    # 4. Execute — each strategy gets all 3 DataFrames
    results = []
    for sdef in strategy_defs:
        try:
            fn = getattr(mod, sdef["name"])
            params, result_df = fn(data["5min"], data["daily"], data["weekly"])
            results.append((params, result_df))
        except Exception as e:
            print(f"[WARN] Strategy {sdef['name']} failed: {e}")
            results.append((sdef["params"], pd.DataFrame()))

    return results
```

**Strategy function signature for refinement** — note the 3 arguments:

```python
def cross_tf_momentum(df_5min: pd.DataFrame, df_daily: pd.DataFrame, df_weekly: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    params = {
        "signal": "Enter when daily 3-day return < -2%, exit when weekly close > weekly 10-period MA",
        "entry_timeframe": "daily",
        "exit_timeframe": "weekly",
        "entry_condition": "3-day close-to-close return <= -2%",
        "exit_condition": "weekly close > 10-week MA",
        "derived_from": ["drop_2_3pct_in_3d", "weekly_ma_cross"],
        "refinement_type": "cross_timeframe",
    }

    # Use daily data for entry signals
    daily_ret_3d = df_daily["close"].pct_change(3) * 100
    entry_mask = daily_ret_3d <= -2

    # For each entry, find the exit on weekly data
    weekly_ma10 = df_weekly["close"].rolling(10).mean()
    # ... compute entry-to-exit returns ...

    result = df_daily.loc[entry_mask, ["date", "close"]].copy()
    # ... attach forward return columns ...

    return params, result
```

**Note**: Mixed/refinement execution is handled by the independent refinement loop (section 7.4), not by a graph node. The `_refine_once()` function in the refinement loop calls `execute_multi_strategies()` from `backtest_multi.py` directly.

The 3 main exec+log nodes in the LangGraph graph use `backtest.py`:

```python
def make_execute_and_log_node(timeframe: str):
    def node(state: GraphState) -> dict:
        pending = state.get(f"pending_{timeframe}", [])
        if not pending:
            return {f"results_{timeframe}": []}

        results = execute_strategies("NIFTY", timeframe, pending)
        strategy_names = [s["name"] for s in pending]

        run_id = log_run("NIFTY", timeframe, strategy_names, results)

        summaries = []
        for name, (params, result_df) in zip(strategy_names, results):
            fwd_cols = [c for c in result_df.columns if c.startswith("fwd_")]
            stats = {col: summarize(result_df[col]) for col in fwd_cols}
            summaries.append({"name": name, "params": params, "stats": stats})

        publish(branch="experiments", timeframe=timeframe,
                strategy_names=strategy_names, run_results=results)

        return {f"results_{timeframe}": summaries}

    return node
```

---

## 5. LLM Configuration — Provider Agnostic

Use `langchain_core`'s `BaseChatModel` as the type. Each node can have its own model.

```python
from langchain_core.language_models import BaseChatModel

# Example: different providers per node
# from langchain_anthropic import ChatAnthropic
# from langchain_openai import ChatOpenAI

# planner_5min_llm = ChatAnthropic(model="claude-sonnet-4-20250514")
# planner_daily_llm = ChatOpenAI(model="gpt-4o")
# etc.

# Store in a config dict:
LLM_CONFIG = {
    "planner_5min": None,      # set at runtime
    "planner_daily": None,
    "planner_weekly": None,
    "planner_refinement": None,
}
```

In `run.py`, the user configures which LLM each node uses before launching the graph.

---

## 6. Planner Node Prompts

Store these in `prompts/`. Each planner node loads its prompt from the corresponding file.

### 6.1. `prompts/planner_5min.md`

```markdown
You are a quantitative strategy designer working with 5-minute OHLCV bar data for Indian indices (NIFTY, BANKNIFTY, SENSEX).

## Your task

Generate exactly 5 unique strategy functions. Each strategy detects a pattern in intraday 5-minute bars and measures forward returns.

## Data you will receive

A pandas DataFrame `df` with columns: `date` (datetime with intraday timestamps), `open`, `high`, `low`, `close`, `volume`.
The data is 5-minute bars. A trading day has roughly 75 bars (9:15 AM to 3:30 PM IST).

## What makes a good intraday strategy signal

Think about:
- Opening range breakouts (first 15-30 min high/low breaks)
- Volume spikes relative to intraday rolling average
- Gap open behaviors (open vs previous bar close)
- VWAP crosses
- Intraday momentum (N consecutive bars in same direction)
- Range compression then expansion
- Pre-close vs post-open patterns

Do NOT repeat strategies already tested. You will be given a list of previously tested strategy names and params — generate novel ones.

## Output format

Return exactly 5 strategy definitions. For each, provide:

1. `name`: a snake_case function name (unique, descriptive)
2. `params`: a dict that fully describes the signal (someone reading just this dict must be able to recreate the strategy)
3. `code`: the complete Python function as a string

## Function signature contract

Every function MUST follow this exact pattern:

```python
def strategy_name(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    params = {
        "signal": "human-readable description of the exact signal logic",
        "field": "which OHLCV fields are used",
        # ... all thresholds, lookbacks, windows as key-value pairs
        "forward_bars": [1, 3, 5],  # which forward periods to measure
    }

    # ... compute signal mask using vectorized pandas/numpy ...

    mask = <boolean series>
    result = df.loc[mask, ["date", "close"]].copy()

    # ... attach forward return columns ...
    # MUST be named fwd_Xd where X matches forward_bars
    result["fwd_1d"] = ...
    result["fwd_3d"] = ...

    return params, result
```

## Rules

- Use ONLY pandas and numpy. No other libraries.
- ALL computation must be vectorized. No iterrows, no apply with lambdas over rows.
- Forward returns: `df["close"].shift(-N) / df["close"] * 100 - 100` — this is the convention.
- The `params` dict is the ONLY record of what this strategy does. Be precise and complete.
- Name forward columns `fwd_Xd` where X is the number of bars forward (not calendar days).
- Ensure at least 2 forward horizons per strategy.
- Do NOT hardcode date ranges or filter to specific periods.

## Response format

Return your response as a JSON array inside a ```json code fence. Each element must have keys: "name", "params", "code".
```

### 6.2. `prompts/planner_daily.md`

```markdown
You are a quantitative strategy designer working with daily OHLCV data for Indian indices (NIFTY, BANKNIFTY, SENSEX).

## Your task

Generate exactly 5 unique strategy functions. Each strategy detects a pattern in daily bars and measures forward returns.

## Data you will receive

A pandas DataFrame `df` with columns: `date` (datetime, one row per trading day), `open`, `high`, `low`, `close`, `volume`.

## What makes a good daily strategy signal

Think about:
- Multi-day drawdowns (e.g., N-day return crosses threshold)
- Consecutive up/down days
- Volatility compression (N-day range narrows) then breakout
- Volume-price divergences
- Gap opens relative to prior close
- Moving average crossovers or distance from moving average
- Support/resistance levels (N-day high/low breaks)
- Monthly/weekly seasonality (day-of-week, month-of-year effects)
- Mean reversion after extreme moves

Do NOT repeat strategies already tested. You will be given a list of previously tested strategy names and params — generate novel ones.

## Output format

Return exactly 5 strategy definitions. For each, provide:

1. `name`: a snake_case function name (unique, descriptive)
2. `params`: a dict that fully describes the signal
3. `code`: the complete Python function as a string

## Function signature contract

Every function MUST follow this exact pattern:

```python
def strategy_name(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    params = {
        "signal": "human-readable description of the exact signal logic",
        "field": "which OHLCV fields are used",
        # ... all thresholds, lookbacks, windows ...
        "forward_days": [1, 2, 5],
    }

    # ... compute signal mask using vectorized pandas/numpy ...

    mask = <boolean series>
    result = df.loc[mask, ["date", "close"]].copy()

    # Forward returns — use this exact convention:
    # fwd_Xd = df["close"].shift(-X) / df["close"] * 100 - 100
    result["fwd_1d"] = ...
    result["fwd_2d"] = ...

    return params, result
```

## Rules

- Use ONLY pandas and numpy. No other libraries.
- ALL computation must be vectorized. No iterrows, no apply with lambdas over rows.
- Forward returns: `df["close"].shift(-N) / df["close"] * 100 - 100`.
- The `params` dict must fully describe the strategy. Be precise and complete.
- Name forward columns `fwd_Xd` where X is trading days forward.
- Ensure at least 2 forward horizons per strategy.
- Do NOT hardcode date ranges or filter to specific periods.

## Response format

Return your response as a JSON array inside a ```json code fence. Each element must have keys: "name", "params", "code".
```

### 6.3. `prompts/planner_weekly.md`

```markdown
You are a quantitative strategy designer working with weekly OHLCV data for Indian indices (NIFTY, BANKNIFTY, SENSEX).

## Your task

Generate exactly 5 unique strategy functions. Each strategy detects a pattern in weekly bars and measures forward returns.

## Data you will receive

A pandas DataFrame `df` with columns: `date` (datetime, one row per week), `open`, `high`, `low`, `close`, `volume`.

## What makes a good weekly strategy signal

Think about:
- Multi-week trend reversals
- Weekly candle patterns (engulfing, inside bar, outside bar)
- Distance from N-week high/low
- Weekly volume anomalies
- Reversion after N consecutive down/up weeks
- Quarterly/yearly seasonality
- Volatility regime shifts (weekly range expansion/contraction)
- Relative performance (close vs 10/20/50-week moving average)

Do NOT repeat strategies already tested. You will be given a list of previously tested strategy names and params — generate novel ones.

## Output format

Return exactly 5 strategy definitions. For each, provide:

1. `name`: a snake_case function name (unique, descriptive)
2. `params`: a dict that fully describes the signal
3. `code`: the complete Python function as a string

## Function signature contract

Every function MUST follow this exact pattern:

```python
def strategy_name(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    params = {
        "signal": "human-readable description of the exact signal logic",
        "field": "which OHLCV fields are used",
        # ... all thresholds, lookbacks, windows ...
        "forward_weeks": [1, 2, 4],
    }

    # ... compute signal mask using vectorized pandas/numpy ...

    mask = <boolean series>
    result = df.loc[mask, ["date", "close"]].copy()

    # Forward returns — convention:
    # fwd_Xd = df["close"].shift(-X) / df["close"] * 100 - 100
    # X here is weeks, but column name stays fwd_Xd for consistency
    result["fwd_1d"] = ...
    result["fwd_2d"] = ...

    return params, result
```

## Rules

- Use ONLY pandas and numpy. No other libraries.
- ALL computation must be vectorized. No iterrows, no apply with lambdas over rows.
- Forward returns: `df["close"].shift(-N) / df["close"] * 100 - 100`.
- The `params` dict must fully describe the strategy. Be precise and complete.
- Name forward columns `fwd_Xd` where X is weeks forward.
- Ensure at least 2 forward horizons per strategy.
- Do NOT hardcode date ranges or filter to specific periods.

## Response format

Return your response as a JSON array inside a ```json code fence. Each element must have keys: "name", "params", "code".
```

### 6.4. `prompts/planner_refinement.md`

```markdown
You are a quantitative strategy refinement agent. You receive a list of WINNING strategies across multiple timeframes. Your job is to interpolate, combine, and refine them into more nuanced strategies — including **cross-timeframe strategies** that use one timeframe for entry and another for exit.

## Your task

Analyze the winning strategies and generate exactly 5 new strategies that explore the space between and around the winners.

## What you receive

A list of winning strategies with their params and stats:
- strategy name, timeframe, direction (long/short)
- params dict (signal type, thresholds, lookback, field, forward horizons)
- performance stats: **expectancy** (expected return per trade), win_rate, avg_win, avg_loss, signal count

Expectancy = (win_rate/100 × avg_win) + (loss_rate/100 × avg_loss). A winner has |expectancy| >= 0.15%. Positive expectancy = long, negative = short.

## How to refine

1. **Interpolate thresholds**: If strategy A uses lookback=3 with threshold=-2% and strategy B uses lookback=5 with threshold=-3%, try lookback=4 with threshold=-2.5%.
2. **Combine signals**: If a momentum strategy and a volatility strategy are both winners, combine them — require BOTH conditions.
3. **Tighten/loosen**: If a strategy with threshold=-2% has expectancy=0.3%, try -1.5% and -2.5% to find the sweet spot.
4. **Add filters**: Take a winning signal and add a volume filter, or a volatility filter.
5. **Cross-timeframe entry/exit**: This is the most powerful refinement. Examples:
   - Enter when daily 3-day return drops below -2%, exit when weekly close crosses above the 10-week moving average
   - Enter on a 5-min volume spike, hold until the daily close confirms the move
   - Use weekly trend direction as a filter for daily entry signals
   - Enter on daily breakout, use 5-min data to find precise exit timing

## Data you receive

Your function receives **3 DataFrames** — all timeframes loaded simultaneously:

```python
def strategy_name(df_5min: pd.DataFrame, df_daily: pd.DataFrame, df_weekly: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
```

Each DataFrame has columns: `date`, `open`, `high`, `low`, `close`, `volume`.
- `df_5min`: 5-minute bars (~75 bars per trading day)
- `df_daily`: one row per trading day
- `df_weekly`: one row per week

You can use any combination. A single-timeframe strategy can simply ignore the other two DataFrames.

## Output format

5 strategies, each with `name`, `params`, `code`.

## Function signature contract

**IMPORTANT**: Unlike the main planner nodes, refinement strategies take 3 DataFrames:

```python
def strategy_name(df_5min: pd.DataFrame, df_daily: pd.DataFrame, df_weekly: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    params = {
        "signal": "human-readable description of the full entry/exit logic",
        "entry_timeframe": "daily",       # which timeframe triggers entry
        "exit_timeframe": "weekly",       # which timeframe triggers exit (can be same)
        "entry_condition": "3-day close-to-close return <= -2%",
        "exit_condition": "weekly close > 10-week MA",
        "derived_from": ["drop_2_3pct_in_3d", "weekly_ma_cross"],
        "refinement_type": "cross_timeframe",  # or: interpolation, combination, tightening, filtered
        "forward_days": [1, 2, 5],  # forward horizons measured on the entry timeframe
    }

    # Example: use daily for entry, weekly for context
    daily_ret = df_daily["close"].pct_change(3) * 100
    weekly_ma = df_weekly["close"].rolling(10).mean()

    # ... compute entry mask, forward returns ...

    result = df_daily.loc[mask, ["date", "close"]].copy()
    result["fwd_1d"] = ...
    result["fwd_2d"] = ...

    return params, result
```

## Cross-timeframe implementation tips

When combining data from different timeframes, you often need to align them. Common patterns:

```python
# Map weekly signals to daily dates (forward-fill weekly values to daily)
weekly_signal = df_weekly.set_index("date")["some_column"]
daily_dates = df_daily.set_index("date").index
aligned = weekly_signal.reindex(daily_dates, method="ffill")

# Map daily signals to 5-min bars
daily_signal = df_daily.set_index("date")["some_column"]
fivemin_dates = df_5min["date"].dt.normalize()  # strip time component
df_5min["daily_signal"] = fivemin_dates.map(daily_signal).values
```

## Rules

- Use ONLY pandas and numpy. No other libraries.
- ALL computation must be vectorized. No iterrows, no apply with lambdas over rows.
- Forward returns: `df["close"].shift(-N) / df["close"] * 100 - 100` — measured on the entry timeframe's DataFrame.
- The `params` dict must fully describe the strategy. Include `entry_timeframe`, `exit_timeframe`, `entry_condition`, `exit_condition`.
- The `derived_from` field in params MUST list which winner strategies inspired this one.
- The `refinement_type` field MUST be one of: `interpolation`, `combination`, `tightening`, `cross_timeframe`, `filtered`.
- Name forward columns `fwd_Xd` with at least 2 horizons.

## Response format

Return your response as a JSON array inside a ```json code fence. Each element must have keys: "name", "params", "code".
```

---

## 7. Node Implementations — `graph.py`

### 7.1. Planner nodes

All three timeframe planner nodes follow the same pattern. Parameterize, don't duplicate.

```python
import json
from pathlib import Path
from langchain_core.messages import SystemMessage, HumanMessage

def _load_prompt(timeframe: str) -> str:
    return Path(f"prompts/planner_{timeframe}.md").read_text()

def _get_tested_strategies(timeframe: str) -> str:
    """Build a summary of already-tested strategies for this timeframe."""
    tested = query_winners(min_abs_expectancy=0, min_signals=0, timeframe=timeframe, limit=200)
    if tested.empty:
        return "No strategies tested yet for this timeframe."
    lines = []
    for _, row in tested.iterrows():
        lines.append(f"- {row['strategy']}: params={row['params']}, "
                     f"expectancy={row['expectancy']}%, direction={row['direction']}")
    return "\n".join(lines)

def make_planner_node(timeframe: str, llm: BaseChatModel):
    """Factory: returns a planner node function for the given timeframe."""

    def planner_node(state: GraphState) -> dict:
        # Skip if this timeframe has reached 50
        if count_strategies(timeframe) >= 50:
            return {f"pending_{timeframe}": []}

        system_prompt = _load_prompt(timeframe)
        tested = _get_tested_strategies(timeframe)

        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"Previously tested strategies:\n{tested}\n\nGenerate 5 new strategies."),
        ])

        # Parse the LLM response into StrategyDef list
        strategy_defs = _parse_strategies(response.content)
        return {f"pending_{timeframe}": strategy_defs}

    return planner_node
```

**Critical: the `_parse_strategies` function.** The LLM output must be parsed into `StrategyDef` objects. Use structured output if supported by the LLM provider, or instruct the LLM to return JSON and parse it. Define this clearly:

```python
def _parse_strategies(response_text: str) -> list[StrategyDef]:
    """Parse LLM response into list of StrategyDef.

    Expect the LLM to return a JSON array:
    [
        {
            "name": "strategy_name",
            "params": { ... },
            "code": "def strategy_name(df: pd.DataFrame) -> ..."
        },
        ...
    ]
    """
    # Extract JSON from response (may be wrapped in markdown code fences)
    text = response_text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]

    strategies = json.loads(text)
    return [StrategyDef(**s) for s in strategies]
```

### 7.2. Combined execution + logging nodes

DataFrames are large — do NOT put full DataFrames in `GraphState`. Each timeframe gets a combined execute+log node that executes strategies, logs to SQLite, publishes to GitHub, and returns only summary stats to state.

The execution node does NOT call an LLM. It writes the code to a file, imports it, and runs it. The LLM's work is already done in the planner node. Add this header comment when writing the strategy file:

```python
# AUTO-GENERATED by planner node. This file is overwritten each iteration.
# Do not edit manually — changes will be lost.
```

```python
def make_execute_and_log_node(timeframe: str):
    """Combined execution + logging node per timeframe."""

    def node(state: GraphState) -> dict:
        pending = state.get(f"pending_{timeframe}", [])
        if not pending:
            return {f"results_{timeframe}": []}

        # Execute
        results = execute_strategies("NIFTY", timeframe, pending)
        strategy_names = [s["name"] for s in pending]

        # Log to DB
        run_id = log_run("NIFTY", timeframe, strategy_names, results)

        # Summarize for state (no DataFrames)
        summaries = []
        for params, result_df in results:
            fwd_cols = [c for c in result_df.columns if c.startswith("fwd_")]
            stats = {col: summarize(result_df[col]) for col in fwd_cols}
            summaries.append({"name": strategy_names[len(summaries)], "params": params, "stats": stats})

        # Publish to GitHub
        publish(
            branch="experiments",
            timeframe=timeframe,
            strategy_names=strategy_names,
            run_results=results,
        )

        return {f"results_{timeframe}": summaries}

    return node
```

### 7.3. Refinement loop — `refinement.py` (independent from the main graph)

The refinement loop is **not a LangGraph node**. It runs as a separate async task alongside the main graph. It polls the database, generates cross-timeframe strategies using `backtest_multi.py`, and logs them — all without touching the main graph's state.

```python
import asyncio
import json
from pathlib import Path
from langchain_core.messages import SystemMessage, HumanMessage

from backtest import query_winners, count_strategies, summarize, log_run, publish
from backtest_multi import execute_multi_strategies


def _count_main_strategies() -> int:
    """Count total strategies from the 3 main timeframes (excludes mixed)."""
    return (
        count_strategies("5min")
        + count_strategies("daily")
        + count_strategies("weekly")
    )


def _get_winner_descriptions() -> list[dict]:
    """Fetch winners and format them for the LLM."""
    winners = query_winners(min_abs_expectancy=0.15, min_signals=30)
    if winners.empty:
        return []

    descriptions = []
    for _, row in winners.iterrows():
        descriptions.append({
            "strategy": row["strategy"],
            "timeframe": row["timeframe"],
            "params": json.loads(row["params"]),
            "win_rate": row["win_rate"],
            "avg_win": row["avg_win"],
            "avg_loss": row["avg_loss"],
            "expectancy": row["expectancy"],
            "direction": row["direction"],
            "signals": row["signals"],
        })
    return descriptions


def _refine_once(llm, winner_descriptions: list[dict]) -> None:
    """Generate 5 cross-timeframe strategies, execute, log, and publish."""
    prompt = Path("prompts/planner_refinement.md").read_text()

    # Also fetch previously tested mixed strategies to avoid repeats
    tested = query_winners(min_abs_expectancy=0, min_signals=0, timeframe="mixed", limit=200)
    tested_summary = "No mixed strategies tested yet."
    if not tested.empty:
        lines = []
        for _, row in tested.iterrows():
            lines.append(f"- {row['strategy']}: params={row['params']}, "
                         f"expectancy={row['expectancy']}%")
        tested_summary = "\n".join(lines)

    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=(
            f"Winning strategies:\n{json.dumps(winner_descriptions, indent=2)}\n\n"
            f"Previously tested mixed strategies:\n{tested_summary}\n\n"
            f"Generate 5 new refined strategies."
        )),
    ])

    strategy_defs = _parse_strategies(response.content)

    # Execute with backtest_multi.py — all 3 timeframes
    results = execute_multi_strategies("NIFTY", strategy_defs)
    strategy_names = [s["name"] for s in strategy_defs]

    # Log to DB
    log_run("NIFTY", "mixed", strategy_names, results)

    # Publish to GitHub
    publish(
        branch="experiments",
        timeframe="mixed",
        strategy_names=strategy_names,
        run_results=results,
    )

    # Print summary
    for name, (params, result_df) in zip(strategy_names, results):
        fwd_cols = [c for c in result_df.columns if c.startswith("fwd_")]
        stats = {col: summarize(result_df[col]) for col in fwd_cols}
        print(f"  [refinement] {name}: signals={len(result_df)}, stats={stats}")


async def refinement_loop(llm, stop_event: asyncio.Event, poll_interval: int = 180):
    """Independent refinement loop. Runs concurrently with the main graph.

    Args:
        llm: BaseChatModel for the refinement planner.
        stop_event: Set by the main loop when it finishes.
        poll_interval: Seconds between DB polls (default 3 minutes).
    """
    next_threshold = 15

    while not stop_event.is_set():
        total = _count_main_strategies()

        if total < next_threshold:
            # Haven't hit the threshold yet — sleep and poll again
            print(f"[refinement] {total} strategies logged, waiting for {next_threshold}. "
                  f"Polling again in {poll_interval}s.")
            await asyncio.sleep(poll_interval)
            continue

        # Threshold reached — refresh winner pool
        print(f"[refinement] Threshold {next_threshold} reached ({total} strategies). "
              f"Refreshing winner pool.")
        winner_descriptions = _get_winner_descriptions()

        if not winner_descriptions:
            print(f"[refinement] No winners yet. Staying idle.")
            await asyncio.sleep(poll_interval)
            continue

        # Inner loop: keep generating+executing batches of 5 until next threshold
        while not stop_event.is_set():
            print(f"[refinement] Generating 5 cross-timeframe strategies "
                  f"from {len(winner_descriptions)} winners...")
            _refine_once(llm, winner_descriptions)

            # Check if main loop has produced enough for the next threshold
            current_total = _count_main_strategies()
            if current_total >= next_threshold + 15:
                # Next threshold hit — break inner loop to refresh winners
                next_threshold += 15
                print(f"[refinement] Main loop hit {current_total}. "
                      f"New threshold: {next_threshold}. Refreshing winners.")
                break

            # Small pause to avoid hammering the LLM
            await asyncio.sleep(5)

    print("[refinement] Main loop finished. Refinement loop exiting.")
```

### 7.4. Stop check node

```python
def stop_check(state: GraphState) -> dict:
    c5 = count_strategies("5min")
    cd = count_strategies("daily")
    cw = count_strategies("weekly")

    should_stop = (c5 >= 50) and (cd >= 50) and (cw >= 50)

    return {
        "count_5min": c5,
        "count_daily": cd,
        "count_weekly": cw,
        "should_stop": should_stop,
        "iteration": state["iteration"] + 1,
    }
```

---

## 8. Graph Definition — `graph.py`

```python
from langgraph.graph import StateGraph, START, END

def router(state: GraphState) -> dict:
    """No-op node that fans out to all 3 planners in parallel."""
    return {}

def build_graph(llm_config: dict[str, BaseChatModel]) -> StateGraph:
    graph = StateGraph(GraphState)

    # Router (fans out to parallel planners, also used for looping back)
    graph.add_node("router", router)

    # Planner nodes
    graph.add_node("plan_5min", make_planner_node("5min", llm_config["planner_5min"]))
    graph.add_node("plan_daily", make_planner_node("daily", llm_config["planner_daily"]))
    graph.add_node("plan_weekly", make_planner_node("weekly", llm_config["planner_weekly"]))

    # Execution + logging nodes
    graph.add_node("exec_5min", make_execute_and_log_node("5min"))
    graph.add_node("exec_daily", make_execute_and_log_node("daily"))
    graph.add_node("exec_weekly", make_execute_and_log_node("weekly"))

    # Stop check (no refinement in the main loop — it runs independently)
    graph.add_node("stop_check", stop_check)

    # ── Edges ──

    # START → router → all 3 planners in parallel
    graph.add_edge(START, "router")
    graph.add_edge("router", "plan_5min")
    graph.add_edge("router", "plan_daily")
    graph.add_edge("router", "plan_weekly")

    # Each planner → its execution node
    graph.add_edge("plan_5min", "exec_5min")
    graph.add_edge("plan_daily", "exec_daily")
    graph.add_edge("plan_weekly", "exec_weekly")

    # All execution nodes → stop check (waits for all 3)
    graph.add_edge("exec_5min", "stop_check")
    graph.add_edge("exec_daily", "stop_check")
    graph.add_edge("exec_weekly", "stop_check")

    # Conditional: loop back to router or stop
    graph.add_conditional_edges(
        "stop_check",
        lambda state: "end" if state["should_stop"] else "continue",
        {"end": END, "continue": "router"},
    )

    return graph.compile()
```

---

## 9. Entry Point — `run.py`

```python
import asyncio
from graph import build_graph
from refinement import refinement_loop
from state import GraphState

# Configure LLMs — swap providers as needed
# from langchain_anthropic import ChatAnthropic
# from langchain_openai import ChatOpenAI

llm_config = {
    "planner_5min": ...,       # any BaseChatModel
    "planner_daily": ...,
    "planner_weekly": ...,
    "planner_refinement": ...,
}

graph = build_graph(llm_config)

initial_state: GraphState = {
    "pending_5min": [],
    "pending_daily": [],
    "pending_weekly": [],
    "results_5min": [],
    "results_daily": [],
    "results_weekly": [],
    "count_5min": 0,
    "count_daily": 0,
    "count_weekly": 0,
    "iteration": 0,
    "should_stop": False,
}


async def main():
    stop_event = asyncio.Event()

    async def run_main_graph():
        # LangGraph's invoke is synchronous — run in executor to not block
        loop = asyncio.get_event_loop()
        final_state = await loop.run_in_executor(None, graph.invoke, initial_state)
        print(f"Main loop completed in {final_state['iteration']} iterations")
        print(f"Strategies: 5min={final_state['count_5min']}, "
              f"daily={final_state['count_daily']}, "
              f"weekly={final_state['count_weekly']}")
        # Signal refinement loop to stop
        stop_event.set()

    # Run both concurrently
    await asyncio.gather(
        run_main_graph(),
        refinement_loop(
            llm=llm_config["planner_refinement"],
            stop_event=stop_event,
            poll_interval=180,  # 3 minutes
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 10. Error Handling

Strategies generated by the LLM will sometimes fail — syntax errors, runtime errors, empty results. The execution node MUST handle this gracefully:

```python
def execute_strategies(index, timeframe, strategy_defs):
    # ... write file, import module ...

    results = []
    for sdef in strategy_defs:
        try:
            fn = getattr(mod, sdef["name"])
            params, result_df = fn(df)
            results.append((params, result_df))
        except Exception as e:
            # Log the failure but don't crash the pipeline
            print(f"[WARN] Strategy {sdef['name']} failed: {e}")
            results.append((sdef["params"], pd.DataFrame()))  # empty result

    return results
```

The logging function already handles empty DataFrames — `summarize` returns `{}` for empty series, and those are skipped.

---

## 11. Dependencies

Add to `pyproject.toml`:

```toml
dependencies = [
    # ... existing deps ...
    "langgraph>=0.2.0",
    "langchain-core>=0.3.0",
    # Add provider-specific packages as needed:
    # "langchain-anthropic>=0.3.0",
    # "langchain-openai>=0.3.0",
]
```

---

## 12. Checklist

Before considering the implementation complete, verify:

**Core engine:**
- [ ] `backtest.py` has timeframe-aware `load()`, `log_run()`, `query_winners()`, `count_strategies()`, `publish()`, `execute_strategies()`
- [ ] `backtest_multi.py` has `load_all()`, `execute_multi_strategies()` — loads all 3 timeframes, passes all 3 DataFrames to each strategy
- [ ] Database schema has `timeframe` column on `runs` table and `expectancy`, `avg_win`, `avg_loss` columns on `results` table
- [ ] `summarize()` computes `avg_win`, `avg_loss`, and `expectancy`
- [ ] `query_winners()` filters on `ABS(expectancy)` and returns a `direction` column (`long`/`short`)
- [ ] Error handling wraps each strategy execution in try/except

**Main loop (LangGraph):**
- [ ] `state.py` defines `GraphState` (no refinement fields) and `StrategyDef`
- [ ] `graph.py` builds the main graph: START → router → 3 parallel planners → 3 parallel exec+log → stop check → loop back to router or END
- [ ] Refinement is NOT in the main graph — no `refinement_node` or `exec_mixed` in the graph definition
- [ ] Execution nodes write to `strategies/strat_{timeframe}.py`, overwriting each iteration
- [ ] GitHub push happens inside each `make_execute_and_log_node` after logging completes
- [ ] Stop condition checks `count_strategies()` from DB, not from state counters alone

**Refinement loop (independent async):**
- [ ] `refinement_loop()` is an async function that polls DB every 3 minutes
- [ ] Tracks threshold counter (15, 30, 45, ...) — refreshes winner pool at each threshold
- [ ] Inner loop: generates+executes batches of 5 cross-timeframe strategies using `backtest_multi.py` until the next threshold is hit
- [ ] Stays idle if no winners exist at a threshold
- [ ] Uses `_parse_strategies()` and `execute_multi_strategies()` — same parsing/execution as main loop
- [ ] Passes previously tested mixed strategies to the LLM to avoid repeats
- [ ] Stops when the main loop sets the `stop_event`

**Integration:**
- [ ] `refinement.py` contains the refinement loop code (separate from `graph.py`)
- [ ] `run.py` launches both main graph and refinement loop via `asyncio.gather`
- [ ] Both share SQLite DB but have no shared in-memory state
- [ ] All 4 prompt files exist in `prompts/` with the JSON response format instruction appended
- [ ] Data directories `data/5min/`, `data/daily/`, `data/weekly/` exist (can be empty, but code must raise clear error if parquet missing)
